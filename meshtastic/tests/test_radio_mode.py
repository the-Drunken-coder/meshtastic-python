"""Operator radio-mode workflow through protobuf admin messages, without hardware."""

import argparse
import base64
from collections import deque

import pytest

from meshtastic import mt_config
from meshtastic.__main__ import initParser, onConnected
from meshtastic.mesh_interface import MeshInterface
from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, apponly_pb2, channel_pb2, config_pb2, mesh_pb2, portnums_pb2


pytestmark = pytest.mark.unit


class AdminWire(MeshInterface):
    """Replace serial I/O while exercising the actual protobuf decoder and callbacks."""

    def __init__(self, replies):
        super().__init__()
        self.replies = deque(replies)
        self.sent = []
        self.nodes = {}
        self.nodesByNum = {}
        self.myInfo = mesh_pb2.MyNodeInfo(my_node_num=123)
        self.localNode.nodeNum = 123
        self._getOrCreateByNum(123)["adminSessionPassKey"] = b"test-session"
        self.localNode.localConfig.lora.CopyFrom(config_pb2.Config.LoRaConfig(
            region=config_pb2.Config.LoRaConfig.US,
            use_preset=False, bandwidth=250, spread_factor=9, coding_rate=5,
            channel_num=12, tx_enabled=True,
        ))

    def _sendToRadioImpl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Encode the request and deliver a scripted protobuf or routing response."""
        if not toRadio.HasField("packet"):
            return
        packet = toRadio.packet
        assert packet.decoded.portnum == portnums_pb2.ADMIN_APP
        sent = admin_pb2.AdminMessage.FromString(packet.decoded.payload)
        self.sent.append(sent)
        if sent.HasField("get_radio_mode_status_request") or (
            sent.HasField("set_config") and sent.set_config.lora.HasField("radio_mode")
        ):
            reply = self.replies.popleft()
            if reply is None:
                return
            response = mesh_pb2.MeshPacket(to=123)
            setattr(response, "from", 123)
            response.decoded.request_id = packet.id
            if isinstance(reply, str):
                response.decoded.portnum = portnums_pb2.ROUTING_APP
                response.decoded.payload = mesh_pb2.Routing(
                    error_reason=mesh_pb2.Routing.Error.Value(reply)
                ).SerializeToString()
            else:
                response.decoded.portnum = portnums_pb2.ADMIN_APP
                response.decoded.payload = reply.SerializeToString()
            self._handleFromRadio(mesh_pb2.FromRadio(packet=response).SerializeToString())


def status_reply(**changes):
    """Provide an explicit firmware response according to the versioned contract."""
    status = admin_pb2.RadioModeStatus(
        capability_version=1, flrc_supported=True,
        configured_mode=config_pb2.Config.LoRaConfig.LORA,
        active_mode=config_pb2.Config.LoRaConfig.LORA,
        active_initialized=True, configuration_valid=True, transmit_allowed=True,
    )
    for name, value in changes.items():
        setattr(status, name, value)
    return admin_pb2.AdminMessage(get_radio_mode_status_response=status)


def test_select_flrc_reads_capability_and_saved_mode_without_changing_lora_tuning():
    """Select flrc reads capability and saved mode without changing lora tuning."""
    wire = AdminWire([
        status_reply(),
        status_reply(configured_mode=1, restart_pending=True),
    ])
    before = config_pb2.Config.LoRaConfig()
    before.CopyFrom(wire.localNode.localConfig.lora)

    result = wire.localNode.setRadioMode("flrc")

    assert result.configured_mode == 1
    assert result.active_mode == 0
    assert result.restart_pending is True
    assert [p.WhichOneof("payload_variant") for p in wire.sent] == [
        "get_radio_mode_status_request", "set_config"
    ]
    saved = wire.sent[1].set_config.lora
    assert saved.HasField("radio_mode")
    assert saved.radio_mode == 1
    saved.ClearField("radio_mode")
    assert saved == before


def test_flrc_signal_does_not_display_a_retained_lora_snr():
    """Flrc signal does not display a retained lora snr."""
    interface = MeshInterface(noProto=True)
    interface.nodesByNum = {123: {"num": 123, "snr": 7, "snrUnavailable": True}}
    table = interface.showNodes(showFields=["user.id", "snr"])
    assert "N/A" in table
    assert "7 dB" not in table


@pytest.mark.parametrize("reply, message", [
    ("BAD_REQUEST", "does not provide radio-mode status"),
    (status_reply(capability_version=0), "does not support this radio-mode"),
    (status_reply(flrc_supported=False), "does not support FLRC on this hardware"),
])
def test_unsupported_firmware_cannot_appear_to_accept_flrc(reply, message):
    """Unsupported firmware cannot appear to accept flrc."""
    wire = AdminWire([reply])
    previous = wire.localNode.localConfig.SerializeToString()
    with pytest.raises((RuntimeError, ValueError), match=message):
        wire.localNode.setRadioMode("flrc")
    assert all(not p.HasField("set_config") for p in wire.sent)
    assert wire.localNode.localConfig.SerializeToString() == previous


def test_explicit_lora_is_present_on_the_wire_and_legacy_tuning_write_omits_mode():
    """Explicit lora is present on the wire and legacy tuning write omits mode."""
    wire = AdminWire([
        status_reply(configured_mode=1, active_mode=1, carrier_mhz=915.0),
        status_reply(configured_mode=0, active_mode=1, restart_pending=True, carrier_mhz=915.0),
    ])
    wire.localNode.localConfig.lora.radio_mode = 1
    result = wire.localNode.setRadioMode("lora")
    assert result.configured_mode == 0
    assert result.active_mode == 1
    assert wire.sent[1].set_config.lora.HasField("radio_mode")
    assert wire.sent[1].set_config.lora.radio_mode == 0

    legacy = AdminWire([])
    legacy.localNode.localConfig.lora.tx_power = 10
    legacy.localNode.writeConfig("lora")
    assert len(legacy.sent) == 1
    assert not legacy.sent[0].set_config.lora.HasField("radio_mode")


def test_incompatible_region_is_rejected_before_write_and_saved_mode_is_restored():
    """Incompatible region is rejected before write and saved mode is restored."""
    wire = AdminWire([status_reply()])
    wire.localNode.localConfig.lora.region = config_pb2.Config.LoRaConfig.EU_868
    previous = wire.localNode.localConfig.SerializeToString()
    with pytest.raises(ValueError, match="requires region US"):
        wire.localNode.setRadioMode("flrc")
    assert wire.localNode.localConfig.SerializeToString() == previous
    assert len(wire.sent) == 1


def test_firmware_rejection_cannot_be_reported_as_saved_selection():
    """Firmware rejection cannot be reported as saved selection."""
    wire = AdminWire([status_reply(), status_reply()])
    previous = wire.localNode.localConfig.SerializeToString()
    with pytest.raises(RuntimeError, match="rejected the radio-mode update"):
        wire.localNode.setRadioMode("flrc")
    assert wire.localNode.localConfig.SerializeToString() == previous


def test_invalid_saved_mode_can_be_corrected_locally():
    """Invalid saved mode can be corrected locally."""
    wire = AdminWire([
        status_reply(configured_mode=99, active_mode=99, active_initialized=False,
                     configuration_valid=False, transmit_allowed=False,
                     blocked_reason=admin_pb2.RadioModeStatus.INVALID_CONFIGURATION),
        status_reply(configured_mode=0, active_mode=99, active_initialized=False,
                     restart_pending=True, configuration_valid=False, transmit_allowed=False,
                     blocked_reason=admin_pb2.RadioModeStatus.INVALID_CONFIGURATION),
    ])
    wire.localNode.localConfig.lora.radio_mode = 99
    result = wire.localNode.setRadioMode("lora")
    assert result.configured_mode == 0
    assert result.restart_pending
    assert not result.transmit_allowed


def test_unknown_mode_and_remote_selection_do_not_send_config():
    """Unknown mode and remote selection do not send config."""
    wire = AdminWire([])
    with pytest.raises(ValueError, match="Unknown radio mode"):
        wire.localNode.setRadioMode("fsk")
    remote = Node(wire, 456)
    with pytest.raises(ValueError, match="only on the local node"):
        remote.setRadioMode("flrc")
    assert not wire.sent


def run_cli(monkeypatch, wire, *args):
    """Run the real argument parser and connection workflow over the admin wire."""
    monkeypatch.setattr("sys.argv", ["meshtastic", *args])
    mt_config.reset()
    mt_config.parser = argparse.ArgumentParser(add_help=False)
    initParser()
    mt_config.args.dest = "^all"
    onConnected(wire)


def test_cli_saves_before_requested_reboot_and_explains_verification(monkeypatch, capsys):
    """Cli saves before requested reboot and explains verification."""
    wire = AdminWire([
        status_reply(), status_reply(configured_mode=1, restart_pending=True),
    ])
    run_cli(monkeypatch, wire, "--radio-mode", "flrc", "--reboot")
    assert wire.sent[1].set_config.lora.radio_mode == 1
    assert wire.sent[-1].WhichOneof("payload_variant") == "reboot_seconds"
    output = capsys.readouterr().out
    assert "configured=FLRC, active=LORA" in output
    assert "Restart pending: yes" in output
    assert "Reconnect with --radio-status" in output


def test_cli_reconnected_status_distinguishes_rf_gate_and_experimental_build(monkeypatch, capsys):
    """Cli reconnected status distinguishes rf gate and experimental build."""
    wire = AdminWire([status_reply(
        configured_mode=1, active_mode=1, carrier_mhz=915.0,
        transmit_allowed=False, blocked_reason=admin_pb2.RadioModeStatus.RF_APPROVAL_REQUIRED,
    )])
    run_cli(monkeypatch, wire, "--radio-status")
    output = capsys.readouterr().out
    assert "configured=FLRC, active=FLRC" in output
    assert "Restart pending: no" in output
    assert "915.000 MHz" in output
    assert "Transmission: disabled (RF approval required)" in output
    assert len(wire.sent) == 1

    experimental = AdminWire([status_reply(
        configured_mode=1, active_mode=1, carrier_mhz=915.0,
        transmit_allowed=True, experimental_tx_enabled=True,
    )])
    run_cli(monkeypatch, experimental, "--radio-status")
    output = capsys.readouterr().out
    assert "Transmission: enabled" in output
    assert "Experimental FLRC TX firmware: enabled; RF acceptance remains unverified" in output


def test_cli_generic_set_cannot_bypass_firmware_capability(monkeypatch, capsys):
    """Cli generic set cannot bypass firmware capability."""
    wire = AdminWire(["BAD_REQUEST"])
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, wire, "--set", "lora.radio_mode", "FLRC")
    assert error.value.code == 1
    assert "does not provide radio-mode status" in capsys.readouterr().out
    assert all(not p.HasField("set_config") for p in wire.sent)


def test_cli_import_checks_capability_before_starting_transaction(monkeypatch, capsys, tmp_path):
    """Cli import checks capability before starting transaction."""
    config = tmp_path / "radio.yaml"
    config.write_text("config:\n  lora:\n    region: US\n    radio_mode: FLRC\n")
    wire = AdminWire(["BAD_REQUEST"])
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, wire, "--configure", str(config))
    assert "does not provide radio-mode status" in capsys.readouterr().out
    assert len(wire.sent) == 1


@pytest.mark.parametrize("reply", [None, "NONE"])
def test_missing_status_or_transport_ack_does_not_establish_support(reply):
    """An ACK from older firmware cannot replace the versioned capability response."""
    wire = AdminWire([reply])
    with pytest.raises(RuntimeError, match="could not be verified"):
        wire.localNode.getRadioModeStatus(timeout=0.01)
    assert not wire.responseHandlers
    assert len(wire.sent) == 1


def test_unrelated_admin_response_does_not_establish_support():
    """Only an explicit radio-mode status can authorize a configuration write."""
    wire = AdminWire([admin_pb2.AdminMessage()])
    with pytest.raises(RuntimeError, match="did not return radio-mode status"):
        wire.localNode.setRadioMode("flrc")
    assert len(wire.sent) == 1


def test_generic_selection_is_verified_before_reboot(monkeypatch):
    """Generic set follows the same capability and readback contract as mode selection."""
    wire = AdminWire([
        status_reply(), status_reply(), status_reply(configured_mode=1, restart_pending=True),
    ])
    run_cli(monkeypatch, wire, "--set", "lora.radio_mode", "FLRC", "--reboot")
    assert [p.WhichOneof("payload_variant") for p in wire.sent] == [
        "get_radio_mode_status_request", "get_radio_mode_status_request",
        "set_config", "reboot_seconds",
    ]


def test_multisection_set_rejects_unsupported_mode_before_writing_other_settings(monkeypatch):
    """A rejected mode cannot leave half of a generic settings transaction applied."""
    wire = AdminWire(["BAD_REQUEST"])
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, wire, "--set", "device.role", "CLIENT", "--set", "lora.radio_mode", "FLRC")
    assert len(wire.sent) == 1


def test_rejected_update_to_existing_flrc_mode_is_not_reported_as_saved(monkeypatch, capsys):
    """A routing NAK takes precedence even when a status read would match the mode."""
    wire = AdminWire([
        status_reply(configured_mode=1, active_mode=1),
        "BAD_REQUEST",
    ])
    wire.localNode.localConfig.lora.radio_mode = 1
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, wire, "--radio-mode", "flrc", "--reboot")
    assert "rejected the radio configuration: BAD_REQUEST" in capsys.readouterr().out
    assert [p.WhichOneof("payload_variant") for p in wire.sent] == [
        "get_radio_mode_status_request", "set_config",
    ]


def test_save_ack_without_correlated_status_does_not_reboot(monkeypatch, capsys):
    """Receipt of a write packet cannot prove that firmware saved its settings."""
    wire = AdminWire([status_reply(), "NONE"])
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, wire, "--radio-mode", "flrc", "--reboot")
    assert "save could not be verified" in capsys.readouterr().out
    assert not wire.responseHandlers
    assert [p.WhichOneof("payload_variant") for p in wire.sent] == [
        "get_radio_mode_status_request", "set_config",
    ]


def test_generic_mode_write_is_durable_before_other_settings_transaction(monkeypatch):
    """Mode saves cannot be reported successful while staged in an open edit transaction."""
    wire = AdminWire([
        status_reply(), status_reply(), status_reply(configured_mode=1, restart_pending=True),
    ])
    run_cli(monkeypatch, wire, "--set", "lora.radio_mode", "FLRC", "--set", "device.role", "CLIENT",
            "--set", "position.position_broadcast_secs", "60", "--reboot")
    payloads = [p.WhichOneof("payload_variant") for p in wire.sent]
    mode_write = next(i for i, p in enumerate(wire.sent) if p.set_config.lora.HasField("radio_mode"))
    assert mode_write < payloads.index("begin_edit_settings")
    assert payloads.index("commit_edit_settings") < payloads.index("reboot_seconds")


def test_import_saves_mode_after_committing_other_sections(monkeypatch, tmp_path):
    """An imported explicit mode gets its own verified write outside staged edits."""
    profile = tmp_path / "radio.yaml"
    profile.write_text("config:\n  device:\n    role: CLIENT\n  lora:\n    region: US\n    radio_mode: FLRC\n")
    wire = AdminWire([
        status_reply(), status_reply(), status_reply(configured_mode=1, restart_pending=True),
    ])
    run_cli(monkeypatch, wire, "--configure", str(profile), "--reboot")
    payloads = [p.WhichOneof("payload_variant") for p in wire.sent]
    mode_write = next(i for i, p in enumerate(wire.sent) if p.set_config.lora.HasField("radio_mode"))
    assert payloads.index("commit_edit_settings") < mode_write < payloads.index("reboot_seconds")


def test_channel_url_selection_cannot_bypass_verified_mode_save():
    """A URL's optional mode field follows the same capability and durable-save contract."""
    profile = apponly_pb2.ChannelSet()
    profile.settings.add(name="test")
    profile.lora_config.region = config_pb2.Config.LoRaConfig.US
    profile.lora_config.radio_mode = config_pb2.Config.LoRaConfig.FLRC
    url = "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(profile.SerializeToString()).decode()
    wire = AdminWire([status_reply(), "BAD_REQUEST"])
    wire.localNode.channels = [channel_pb2.Channel(index=0)]
    with pytest.raises(RuntimeError, match="rejected the radio configuration"):
        wire.localNode.setURL(url)
    assert not wire.localNode.localConfig.lora.HasField("radio_mode")


def test_node_signal_metadata_clears_unavailable_flag_when_lora_returns():
    """A retained zero or stale SNR cannot appear as an FLRC measurement."""
    from meshtastic import _receiveInfoUpdate  # pylint: disable=import-outside-toplevel

    wire = AdminWire([])
    _receiveInfoUpdate(wire, {"from": 456, "rxSnr": 7, "rxSnrUnavailable": True})
    assert wire.nodesByNum[456]["snrUnavailable"]
    _receiveInfoUpdate(wire, {"from": 456, "rxSnr": 3})
    assert not wire.nodesByNum[456]["snrUnavailable"]

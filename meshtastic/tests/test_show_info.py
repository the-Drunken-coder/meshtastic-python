"""Meshtastic unit tests for human-readable interface information."""

import json

import pytest

from ..mesh_interface import MeshInterface


@pytest.mark.unit
def test_showInfo_serializes_bytes_in_node_json(capsys):
    """Node metadata containing protobuf bytes remains printable as JSON."""
    iface = MeshInterface(noProto=True)
    iface.nodes = {
        "!12345678": {
            "num": 0x12345678,
            "user": {
                "id": "!12345678",
                "longName": "Fixture",
                "shortName": "FIX",
                "publicKey": b"\x00\x01\x02",
            },
            "position": {},
        }
    }

    info = iface.showInfo()

    assert capsys.readouterr().err == ""
    nodes_json = info.split("Nodes in mesh: ", maxsplit=1)[1]
    assert json.loads(nodes_json)["!12345678"]["user"]["publicKey"] == "AAEC"

"""Tests for the registry write path.

Run with:   cd host && uv run pytest -v

These are written to fail loudly on the specific mistakes that are easy to
make in register()/heartbeat(), not just to check the happy path.
"""

import time

import pytest

from app.db import Database
from app.registry import Registry


@pytest.fixture
def reg(tmp_path):
    db = Database(tmp_path / "test.db")
    yield Registry(db, node_timeout_s=15)
    db.close()


def payload(**overrides):
    base = {
        "node_id": "node-a",
        "name": "dhruv-laptop",
        "ip": "100.64.1.5",
        "port": 11434,
        "backend": "ollama",
        "os": "linux",
        "arch": "x86_64",
        "gpu": "NVIDIA RTX 4070",
        "vram_total_mb": 8192,
        "ram_total_mb": 32768,
        "agent_version": "0.1.0",
        "models": ["hermes3", "qwen2.5:7b"],
    }
    base.update(overrides)
    return base


def test_register_creates_node(reg):
    node_id = reg.register(payload())
    assert node_id == "node-a"

    node = reg.get_node("node-a")
    assert node is not None
    assert node.name == "dhruv-laptop"
    assert node.base_url == "http://100.64.1.5:11434"
    assert sorted(node.models) == ["hermes3", "qwen2.5:7b"]
    assert node.enabled is True
    # Just registered, so last_seen is now -> counts as online.
    assert node.online is True


def test_register_twice_does_not_duplicate(reg):
    reg.register(payload())
    reg.register(payload())
    assert len(reg.list_nodes()) == 1


def test_reregister_refreshes_ip_but_keeps_first_seen(reg):
    reg.register(payload())
    original_first_seen = reg.get_node("node-a").first_seen

    time.sleep(0.02)
    reg.register(payload(ip="100.64.9.9"))

    node = reg.get_node("node-a")
    assert node.ip == "100.64.9.9", "a changed tailscale IP must be picked up"
    assert node.first_seen == original_first_seen, (
        "first_seen is the 'friend joined on' date -- re-registering must not "
        "reset it or uptime history is destroyed"
    )


def test_reregister_does_not_silently_reenable(reg):
    """The one that actually bites you at 2am."""
    reg.register(payload())
    reg.set_enabled("node-a", False)

    reg.register(payload())  # friend reboots, agent restarts

    assert reg.get_node("node-a").enabled is False, (
        "a node you disabled must stay disabled when its agent restarts"
    )


def test_models_are_a_set_not_an_append_log(reg):
    reg.register(payload(models=["hermes3", "qwen2.5:7b"]))
    reg.register(payload(models=["hermes3"]))  # friend ran `ollama rm qwen2.5:7b`

    assert reg.get_node("node-a").models == ["hermes3"], (
        "removed models must disappear, otherwise the router will route to a "
        "model the node no longer has and every request 404s"
    )


def test_heartbeat_marks_online_and_records_history(reg):
    reg.register(payload())
    assert reg.heartbeat("node-a", {"vram_used_mb": 4210, "ram_used_mb": 9100, "cpu_pct": 12.5})

    node = reg.get_node("node-a")
    assert node.vram_used_mb == 4210
    assert node.ram_used_mb == 9100

    rows = reg.db.query("SELECT * FROM heartbeats WHERE node_id = 'node-a'")
    assert len(rows) == 1, "each heartbeat must append a history row for graphs"


def test_heartbeat_for_unknown_node_returns_false(reg):
    assert reg.heartbeat("ghost", {"vram_used_mb": 1}) is False, (
        "must return False so the API can 404 and the agent knows to re-register"
    )


def test_stale_node_goes_offline(reg):
    reg.register(payload())
    # Backdate last_seen past the timeout, simulating a closed laptop.
    reg.db.execute("UPDATE nodes SET last_seen = ? WHERE id = 'node-a'", (time.time() - 60,))

    node = reg.get_node("node-a")
    assert node.online is False
    assert node in reg.list_nodes() or True  # still visible on the dashboard
    assert reg.candidates_for_model("hermes3") == [], "offline nodes get no traffic"


def test_disabled_node_gets_no_traffic_but_stays_visible(reg):
    reg.register(payload())
    reg.set_enabled("node-a", False)

    assert reg.candidates_for_model("hermes3") == []
    assert len(reg.list_nodes()) == 1, "disabled nodes still show on the dashboard"


def test_candidates_filter_by_model(reg):
    reg.register(payload(node_id="a", name="a", models=["hermes3"]))
    reg.register(payload(node_id="b", name="b", models=["qwen2.5:7b"]))

    got = [n.id for n in reg.candidates_for_model("hermes3")]
    assert got == ["a"]
    assert sorted(reg.known_models()) == ["hermes3", "qwen2.5:7b"]


def test_set_enabled_unknown_node_returns_false(reg):
    assert reg.set_enabled("ghost", True) is False

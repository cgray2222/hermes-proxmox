"""Offline tests: no Proxmox, no network, no Hermes runtime.

Every test pins the behaviour that matters in review: read tools shape data, write tools
refuse without an explicit confirm, and credentials resolve from either supported shape.
"""

import json

import pytest

from hermes_proxmox import pve_client, tools


# ── fixtures: a fake cluster ─────────────────────────────────────────────────

NODES = [{"node": "pve", "status": "online", "uptime": 7 * 86400 + 700,
          "cpu": 0.28, "mem": 10 * 2**30, "maxmem": 16 * 2**30,
          "disk": 20 * 2**30, "maxdisk": 64 * 2**30}]

GUESTS = [
    {"vmid": 102, "name": "haos", "type": "qemu", "node": "pve", "status": "running",
     "uptime": 3600, "mem": 3 * 2**30, "maxmem": 6 * 2**30, "cpu": 0.15,
     "disk": 0, "maxdisk": 64 * 2**30},
    {"vmid": 200, "name": "hermes-lxc", "type": "lxc", "node": "pve", "status": "stopped",
     "uptime": 0, "mem": 0, "maxmem": 12 * 2**30, "cpu": 0, "tags": "core"},
]


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setattr(pve_client, "nodes", lambda: NODES)
    monkeypatch.setattr(pve_client, "cluster_guests", lambda: GUESTS)
    monkeypatch.setattr(pve_client, "node_status", lambda node: {
        "pveversion": "pve-manager/9.2.25/88a31f5fd94aab6d",
        "current-kernel": {"release": "6.17.13-11-pve"},
    })
    monkeypatch.setattr(pve_client, "node_storage", lambda node: [
        {"storage": "data-thin", "type": "lvmthin", "active": 1,
         "used": 346 * 2**30, "total": 1800 * 2**30},
        {"storage": "offline", "type": "nfs", "active": 0, "used": 0, "total": 0},
    ])
    return monkeypatch


# ── read tools ───────────────────────────────────────────────────────────────

def test_list_guests_counts_and_summarises(cluster):
    payload = json.loads(tools.pve_list_guests({}))
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["running"] == 1
    assert [g["vmid"] for g in payload["guests"]] == [102, 200]
    assert payload["guests"][0]["type"] == "qemu"
    assert payload["guests"][1]["type"] == "lxc"


def test_list_guests_status_filter(cluster):
    payload = json.loads(tools.pve_list_guests({"status": "stopped"}))
    assert [g["vmid"] for g in payload["guests"]] == [200]


def test_node_status_hides_inactive_storage_and_formats_version(cluster):
    payload = json.loads(tools.pve_node_status({}))
    node = payload["nodes"][0]
    assert node["pve_version"] == "9.2.25"
    assert [s["storage"] for s in node["storage"]] == ["data-thin"]
    assert node["storage"][0]["pct"] == "19%"


def test_guest_status_includes_config_and_snapshots(cluster):
    cluster.setattr(pve_client, "guest_status", lambda vmid, node=None: {
        "status": "running", "uptime": 3600, "cpu": 0.14, "cpus": 2,
        "mem": 3 * 2**30, "maxmem": 6 * 2**30, "qmpstatus": "running",
    })
    cluster.setattr(pve_client, "guest_config", lambda vmid, node=None: {
        "name": "haos", "cores": 2, "memory": "6144", "agent": "enabled=1",
    })
    cluster.setattr(pve_client, "snapshots", lambda vmid, node=None: [
        {"name": "current"},
        {"name": "pre-upgrade", "snaptime": 1762000000, "description": "before"},
    ])
    payload = json.loads(tools.pve_guest_status({"vmid": 102}))
    assert payload["status"]["agent_enabled"] is True
    assert [s["name"] for s in payload["snapshots"]] == ["pre-upgrade"]


def test_backup_status_counts_failures(cluster):
    cluster.setattr(pve_client, "node_tasks", lambda node, limit=200, typefilter=None: [
        {"type": "vzdump", "id": "102", "starttime": 4_000_000_000,
         "endtime": 4_000_000_120, "status": "OK"},
        {"type": "vzdump", "id": "200", "starttime": 4_000_000_000,
         "endtime": 4_000_000_060, "status": "ERROR: no space"},
        {"type": "qmstart", "id": "102", "starttime": 4_000_000_000, "status": "OK"},
    ])
    monkeypatch_time = 4_000_000_100
    cluster.setattr(tools.time, "time", lambda: monkeypatch_time)
    payload = json.loads(tools.pve_backup_status({"days": 7}))
    assert payload["count"] == 2          # qmstart filtered out
    assert payload["failures"] == 1


# ── write tools refuse without confirm ──────────────────────────────────────

@pytest.mark.parametrize("action", ["create", "delete"])
def test_snapshot_write_requires_confirm(cluster, action):
    called = []
    cluster.setattr(pve_client, "create_snapshot", lambda *a, **k: called.append("create"))
    cluster.setattr(pve_client, "delete_snapshot", lambda *a, **k: called.append("delete"))
    payload = json.loads(tools.pve_snapshot({"vmid": 102, "action": action, "name": "pre-upgrade"}))
    assert payload["ok"] is False
    assert "confirm=true" in payload["error"]
    assert called == []


def test_exec_requires_confirm(cluster):
    called = []
    cluster.setattr(pve_client, "run_agent_command", lambda *a, **k: called.append(a) or {})
    payload = json.loads(tools.pve_guest_exec({"vmid": 102, "command": "uptime"}))
    assert payload["ok"] is False
    assert "confirm=true" in payload["error"]
    assert called == []


def test_exec_rejects_empty_command(cluster):
    payload = json.loads(tools.pve_guest_exec({"vmid": 102, "command": [], "confirm": True}))
    assert payload["ok"] is False


def test_exec_splits_string_commands(cluster):
    seen = {}

    def fake_run(vmid, command, node=None, timeout=30.0):
        seen["command"] = command
        return {"exited": True, "exitcode": 0, "out-data": "ok\n"}

    cluster.setattr(pve_client, "run_agent_command", fake_run)
    payload = json.loads(
        tools.pve_guest_exec({"vmid": 102, "command": 'df -h "/"', "confirm": True})
    )
    assert payload["ok"] is True
    assert seen["command"] == ["df", "-h", "/"]


# ── credentials ─────────────────────────────────────────────────────────────

def test_combined_token_shape(monkeypatch):
    monkeypatch.setattr(pve_client, "get_secret",
                        lambda name, default=None: {"PVE_API_TOKEN": "hermes@pve!t=abc"}.get(name, default))
    assert pve_client.build_token() == "hermes@pve!t=abc"


def test_split_token_shape(monkeypatch):
    env = {"PVE_USER": "hermes@pve", "PVE_TOKEN_NAME": "t", "PVE_TOKEN_VALUE": "abc"}
    monkeypatch.setattr(pve_client, "get_secret",
                        lambda name, default=None: env.get(name, default))
    assert pve_client.build_token() == "hermes@pve!t=abc"


def test_missing_credentials_explains_itself(monkeypatch):
    monkeypatch.setattr(pve_client, "get_secret", lambda name, default=None: default)
    with pytest.raises(pve_client.PveAuthError) as exc:
        pve_client.build_token()
    assert "PVE_API_TOKEN" in str(exc.value)


# ── formatting helpers ──────────────────────────────────────────────────────

def test_fmt_bytes():
    assert tools._fmt_bytes(0) == "0B"
    assert tools._fmt_bytes(1024) == "1.0KB"
    assert tools._fmt_bytes(3 * 2**30) == "3.0GB"


def test_fmt_uptime():
    assert tools._fmt_uptime(90) == "1m"
    assert tools._fmt_uptime(3700) == "1h 1m"
    assert tools._fmt_uptime(90000) == "1d 1h"

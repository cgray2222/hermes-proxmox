"""The six ``pve_*`` tools.

Every handler returns a JSON string and never raises: Proxmox failures come back as
``{"ok": false, "error": ...}`` so the model can report them instead of the tool call
blowing up. Write actions (snapshot create/delete, guest exec) require an explicit
``confirm: true`` in the same call — a deliberate two-step so a demo can show the refusal.
"""

from __future__ import annotations

import json
import shlex
import time
from typing import Any, Dict, List, Optional

from . import pve_client as pve

TOOLSET = "proxmox"


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_bytes(value: Any) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}TB"


def _fmt_uptime(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _pct(used: Any, total: Any) -> str:
    try:
        used_f, total_f = float(used), float(total)
    except (TypeError, ValueError):
        return "?"
    if total_f <= 0:
        return "?"
    return f"{100.0 * used_f / total_f:.0f}%"


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, default=str)


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def _pve_version(raw: Any) -> str:
    """``pve-manager/9.2.3/88a31f5fd94aab6d`` -> ``9.2.3``."""
    text = str(raw or "")
    parts = text.split("/")
    return parts[1] if len(parts) > 1 else text


def _guest_row(resource: Dict[str, Any]) -> Dict[str, Any]:
    maxdisk = resource.get("maxdisk") or 0
    row = {
        "vmid": resource.get("vmid"),
        "name": resource.get("name") or "",
        "type": "lxc" if resource.get("type") == "lxc" else "qemu",
        "node": resource.get("node"),
        "status": resource.get("status"),
        "uptime": _fmt_uptime(resource.get("uptime")),
        "mem": f"{_fmt_bytes(resource.get('mem'))}/{_fmt_bytes(resource.get('maxmem'))}",
        "cpu_pct": round(100.0 * float(resource.get("cpu") or 0), 1),
        "tags": resource.get("tags") or "",
    }
    if maxdisk:
        used = resource.get("disk") or 0
        row["disk"] = f"{_fmt_bytes(used)}/{_fmt_bytes(maxdisk)}" if used else f"0/{_fmt_bytes(maxdisk)}"
    return row


def _node_row(node: Dict[str, Any]) -> Dict[str, Any]:
    mem_used = node.get("mem", 0)
    mem_total = node.get("maxmem", 0)
    disk_used = node.get("disk", 0)
    disk_total = node.get("maxdisk", 0)
    return {
        "node": node.get("node"),
        "status": node.get("status"),
        "uptime": _fmt_uptime(node.get("uptime")),
        "cpu_pct": round(100.0 * float(node.get("cpu") or 0), 1),
        "mem": f"{_fmt_bytes(mem_used)}/{_fmt_bytes(mem_total)} ({_pct(mem_used, mem_total)})",
        "rootfs": f"{_fmt_bytes(disk_used)}/{_fmt_bytes(disk_total)} ({_pct(disk_used, disk_total)})",
    }


# ── tools ────────────────────────────────────────────────────────────────────

def pve_list_guests(args: Dict[str, Any], **_: Any) -> str:
    """Inventory of every VM and container in the cluster."""
    try:
        wanted_node = (args.get("node") or "").strip() or None
        wanted_status = (args.get("status") or "all").strip()
        nodes = [_node_row(n) for n in pve.nodes()]
        guests = [_guest_row(g) for g in pve.cluster_guests()]
        if wanted_node:
            guests = [g for g in guests if g["node"] == wanted_node]
        if wanted_status != "all":
            guests = [g for g in guests if g["status"] == wanted_status]
        guests.sort(key=lambda g: (str(g["node"]), int(g["vmid"] or 0)))
        running = sum(1 for g in guests if g["status"] == "running")
        return _ok(
            nodes=nodes,
            guests=guests[:200],
            count=len(guests),
            running=running,
            summary=f"{len(guests)} guests ({running} running) across {len(nodes)} node(s)",
        )
    except Exception as exc:
        return _err(exc)


def pve_guest_status(args: Dict[str, Any], **_: Any) -> str:
    """Full status for one guest: live state, config highlights, snapshots."""
    try:
        vmid = int(args["vmid"])
        node = (args.get("node") or "").strip() or None
        status = pve.guest_status(vmid, node)
        config = pve.guest_config(vmid, node)
        snaps = [
            {
                "name": s.get("name"),
                "description": s.get("description") or "",
                "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("snaptime", 0)))
                if s.get("snaptime")
                else "",
                "parent": s.get("parent") or "",
            }
            for s in pve.snapshots(vmid, node)
            if s.get("name") != "current"
        ]
        config_highlights = {
            key: config.get(key)
            for key in ("name", "cores", "sockets", "memory", "balloon", "ostype", "boot",
                        "agent", "net0", "scsi0", "virtio0", "rootfs", "onboot", "tags")
            if config.get(key) not in (None, "")
        }
        return _ok(
            vmid=vmid,
            node=pve.resolve_node(vmid, node),
            type=pve.guest_kind(vmid) or "qemu",
            status={
                "state": status.get("status"),
                "uptime": _fmt_uptime(status.get("uptime")),
                "cpu_pct": round(100.0 * float(status.get("cpu") or 0), 1),
                "cpus": status.get("cpus"),
                "mem": f"{_fmt_bytes(status.get('mem'))}/{_fmt_bytes(status.get('maxmem'))}",
                "disk_read": _fmt_bytes(status.get("diskread")),
                "disk_write": _fmt_bytes(status.get("diskwrite")),
                "netin": _fmt_bytes(status.get("netin")),
                "netout": _fmt_bytes(status.get("netout")),
                "qmpstatus": status.get("qmpstatus"),
                "agent_enabled": bool(config.get("agent")) if config.get("agent") else False,
            },
            config=config_highlights,
            snapshots=snaps,
        )
    except Exception as exc:
        return _err(exc)


def pve_node_status(args: Dict[str, Any], **_: Any) -> str:
    """Node health: CPU, memory, root filesystem, storage pools."""
    try:
        wanted = (args.get("node") or "").strip() or None
        rows: List[Dict[str, Any]] = []
        for node in pve.nodes():
            name = str(node.get("node") or "")
            if wanted and name != wanted:
                continue
            status = pve.node_status(name)
            stores = []
            for store in pve.node_storage(name):
                if not store.get("active"):
                    continue
                stores.append(
                    {
                        "storage": store.get("storage"),
                        "type": store.get("type"),
                        "used": _fmt_bytes(store.get("used")),
                        "total": _fmt_bytes(store.get("total")),
                        "pct": _pct(store.get("used"), store.get("total")),
                    }
                )
            rows.append(
                {
                    "node": name,
                    "status": node.get("status"),
                    "uptime": _fmt_uptime(node.get("uptime")),
                    "kernel": (status.get("current-kernel") or {}).get("release"),
                    "pve_version": _pve_version(status.get("pveversion")),
                    "loadavg": (status.get("loadavg") or [])[:3],
                    "cpu_pct": round(100.0 * float(node.get("cpu") or 0), 1),
                    "mem": _node_row(node)["mem"],
                    "rootfs": _node_row(node)["rootfs"],
                    "storage": stores,
                }
            )
        if not rows:
            return _err(pve.PveError(f"node {wanted!r} not found"))
        return _ok(nodes=rows, count=len(rows))
    except Exception as exc:
        return _err(exc)


def pve_snapshot(args: Dict[str, Any], **_: Any) -> str:
    """List, create or delete a guest snapshot. create/delete need ``confirm: true``."""
    try:
        action = str(args.get("action") or "list").strip().lower()
        vmid = int(args["vmid"])
        node = (args.get("node") or "").strip() or None

        if action == "list":
            snaps = [
                {
                    "name": s.get("name"),
                    "description": s.get("description") or "",
                    "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("snaptime", 0)))
                    if s.get("snaptime")
                    else "",
                }
                for s in pve.snapshots(vmid, node)
                if s.get("name") != "current"
            ]
            return _ok(vmid=vmid, action="list", snapshots=snaps, count=len(snaps))

        if action not in {"create", "delete"}:
            return _err(pve.PveError("action must be one of: list, create, delete"))

        name = str(args.get("name") or "").strip()
        if not name:
            return _err(pve.PveError("'name' is required for create and delete"))

        if not args.get("confirm"):
            return _err(
                pve.PveError(
                    f"refused: {action} snapshot {name!r} on guest {vmid} needs confirm=true "
                    "(the same call, after the operator agrees)"
                )
            )

        if action == "create":
            upid = pve.create_snapshot(vmid, name, str(args.get("description") or ""), node)
            finished = pve.wait_for_task(upid, timeout=float(args.get("timeout") or 60))
            return _ok(
                vmid=vmid,
                action="create",
                snapshot=name,
                upid=upid,
                task_status=finished.get("status"),
                exitstatus=finished.get("exitstatus"),
            )

        upid = pve.delete_snapshot(vmid, name, node)
        finished = pve.wait_for_task(upid, timeout=float(args.get("timeout") or 60))
        return _ok(
            vmid=vmid,
            action="delete",
            snapshot=name,
            upid=upid,
            task_status=finished.get("status"),
            exitstatus=finished.get("exitstatus"),
        )
    except Exception as exc:
        return _err(exc)


def pve_backup_status(args: Dict[str, Any], **_: Any) -> str:
    """Recent backup jobs (vzdump / Proxmox Backup Server) with per-job result."""
    try:
        days = float(args.get("days") or 7)
        cutoff = time.time() - days * 86400
        wanted = (args.get("node") or "").strip() or None
        jobs: List[Dict[str, Any]] = []
        for node in pve.nodes():
            name = str(node.get("node") or "")
            if wanted and name != wanted:
                continue
            for task in pve.node_tasks(name, limit=200):
                kind = str(task.get("type") or "")
                if "backup" not in kind and kind != "vzdump":
                    continue
                if float(task.get("starttime") or 0) < cutoff:
                    continue
                jobs.append(
                    {
                        "node": name,
                        "type": kind,
                        "id": task.get("id"),
                        "user": task.get("user"),
                        "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(task.get("starttime", 0))),
                        "duration": _fmt_uptime((task.get("endtime") or task.get("starttime") or 0)
                                                - (task.get("starttime") or 0)),
                        "status": task.get("status"),
                    }
                )
        jobs.sort(key=lambda j: j["started"], reverse=True)
        failed = [j for j in jobs if str(j["status"]).lower() not in {"ok", "running", ""}]
        return _ok(
            window_days=days,
            jobs=jobs[:100],
            count=len(jobs),
            failures=len(failed),
            summary=f"{len(jobs)} backup job(s) in {days:g}d, {len(failed)} not OK",
        )
    except Exception as exc:
        return _err(exc)


def pve_guest_exec(args: Dict[str, Any], **_: Any) -> str:
    """Run a command inside a VM through the QEMU guest agent. Needs ``confirm: true``."""
    try:
        vmid = int(args["vmid"])
        node = (args.get("node") or "").strip() or None
        raw = args.get("command")
        if isinstance(raw, str):
            command = shlex.split(raw)
        elif isinstance(raw, list) and raw:
            command = [str(part) for part in raw]
        else:
            return _err(pve.PveError("'command' must be a string or a non-empty array of strings"))
        if not command:
            return _err(pve.PveError("'command' is empty"))
        if not args.get("confirm"):
            return _err(
                pve.PveError(
                    f"refused: running {' '.join(command)!r} in guest {vmid} needs confirm=true "
                    "(the same call, after the operator agrees)"
                )
            )
        timeout = float(args.get("timeout") or 30)
        result = pve.run_agent_command(vmid, command, node, timeout=timeout)
        out = result.get("out-data") or result.get("out") or ""
        err = result.get("err-data") or result.get("err") or ""
        return _ok(
            vmid=vmid,
            node=pve.resolve_node(vmid, node),
            command=command,
            exitcode=result.get("exitcode"),
            exited=bool(result.get("exited")),
            timed_out=bool(result.get("timed_out")),
            stdout=str(out)[-4000:],
            stderr=str(err)[-2000:],
        )
    except Exception as exc:
        return _err(exc)


# ── registration ─────────────────────────────────────────────────────────────

def check_available() -> bool:
    return pve.is_configured()


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "pve_list_guests",
        "description": (
            "List every Proxmox VM and LXC container in the cluster with node, status, uptime, "
            "memory, CPU and disk. Use for inventory questions ('what is running', 'which node "
            "hosts the NAS', 'what is stopped'). Also returns node-level summary rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Only guests on this node (optional)"},
                "status": {
                    "type": "string",
                    "enum": ["running", "stopped", "all"],
                    "description": "Filter by power state (default all)",
                },
            },
            "additionalProperties": False,
        },
        "handler": pve_list_guests,
        "emoji": "📋",
    },
    {
        "name": "pve_guest_status",
        "description": (
            "Deep status for one VM or container: live state, CPU/memory/disk/network counters, "
            "uptime, key config (cores, memory, disks, network, guest agent), and its snapshots. "
            "Use before answering any question about why a specific guest is slow or full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "Guest ID, e.g. 102"},
                "node": {"type": "string", "description": "Node name; resolved automatically when omitted"},
            },
            "required": ["vmid"],
            "additionalProperties": False,
        },
        "handler": pve_guest_status,
        "emoji": "🔎",
    },
    {
        "name": "pve_node_status",
        "description": (
            "Health of every (or one) Proxmox node: PVE version, kernel, load average, CPU, "
            "memory, root filesystem and each storage pool's used/total with percentage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node name (default: all nodes)"},
            },
            "additionalProperties": False,
        },
        "handler": pve_node_status,
        "emoji": "🩺",
    },
    {
        "name": "pve_snapshot",
        "description": (
            "List, create or delete snapshots on a VM or container. 'list' is read-only. "
            "'create' and 'delete' require confirm=true and return the task result; never "
            "invent confirm=true without the operator agreeing to the change."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "Guest ID"},
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "delete"],
                    "description": "Operation to perform (default list)",
                },
                "name": {"type": "string", "description": "Snapshot name (required for create/delete)"},
                "description": {"type": "string", "description": "Snapshot description (create only)"},
                "node": {"type": "string", "description": "Node name (optional)"},
                "confirm": {"type": "boolean", "description": "Must be true to create or delete"},
                "timeout": {"type": "number", "description": "Seconds to wait for the task (default 60)"},
            },
            "required": ["vmid"],
            "additionalProperties": False,
        },
        "handler": pve_snapshot,
        "emoji": "📸",
    },
    {
        "name": "pve_backup_status",
        "description": (
            "Recent backup jobs across the cluster (vzdump and Proxmox Backup Server): start time, "
            "duration, guest, user and result. Use to answer 'did last night's backups run' and to "
            "find failures."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "number", "description": "How far back to look (default 7 days)"},
                "node": {"type": "string", "description": "Node name (default: all nodes)"},
            },
            "additionalProperties": False,
        },
        "handler": pve_backup_status,
        "emoji": "💾",
    },
    {
        "name": "pve_guest_exec",
        "description": (
            "Run a command inside a running VM through the QEMU guest agent and return stdout, "
            "stderr and the exit code. VMs only (not containers), agent must be enabled. Requires "
            "confirm=true. Use for read-only diagnostics inside a guest (df -h, uptime, systemctl "
            "status); never for destructive commands without explicit operator agreement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vmid": {"type": "integer", "description": "VM ID (qemu only)"},
                "command": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": "Command string or argv array, e.g. \"df -h\" or [\"df\",\"-h\"]",
                },
                "node": {"type": "string", "description": "Node name (optional)"},
                "timeout": {"type": "number", "description": "Seconds to wait for output (default 30)"},
                "confirm": {"type": "boolean", "description": "Must be true to run the command"},
            },
            "required": ["vmid", "command"],
            "additionalProperties": False,
        },
        "handler": pve_guest_exec,
        "emoji": "⌨️",
    },
]


def register_tools(ctx) -> None:
    for tool in TOOLS:
        ctx.register_tool(
            name=tool["name"],
            toolset=TOOLSET,
            schema={
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
            handler=tool["handler"],
            check_fn=check_available,
            requires_env=["PVE_HOST"],
            description=tool["description"],
            emoji=tool.get("emoji", ""),
        )

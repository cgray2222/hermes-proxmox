"""``@pve:<target>`` context reference.

Pulls live Proxmox state into the prompt the same way ``@file:`` pulls a file:

    @pve:all    cluster inventory (nodes + guests + powers state)
    @pve:102    one guest — status, config, snapshots
    @pve:pve    one node — health and storage

Everything is fetched on demand; nothing is injected into a session until the
operator or the model actually references it.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from agent.context_references import ContextCompletionItem, ContextReferenceProvider

from . import pve_client as pve


def _ver(raw) -> str:
    """``pve-manager/9.2.3/88a31f5fd94aab6d`` -> ``9.2.3``."""
    parts = str(raw or "").split("/")
    return parts[1] if len(parts) > 1 else (parts[0] or "?")


def _fleet_text() -> str:
    nodes = pve.nodes()
    guests = pve.cluster_guests()
    lines = ["# Proxmox cluster snapshot"]
    lines.append(f"nodes: {len(nodes)}  guests: {len(guests)}")
    for node in nodes:
        name = node.get("node")
        lines.append(
            f"- node {name}: {node.get('status')} cpu {100.0 * float(node.get('cpu') or 0):.0f}% "
            f"mem {float(node.get('mem') or 0) / 2**30:.1f}/{float(node.get('maxmem') or 0) / 2**30:.1f} GiB "
            f"up {int((node.get('uptime') or 0) // 3600)}h"
        )
    running = [g for g in guests if g.get("status") == "running"]
    lines.append(f"running: {len(running)}/{len(guests)}")
    for guest in sorted(guests, key=lambda g: (str(g.get("node")), int(g.get("vmid") or 0)))[:60]:
        lines.append(
            f"- {guest.get('vmid')} {guest.get('name') or ''} "
            f"({guest.get('type')}, {guest.get('node')}): {guest.get('status')}"
        )
    return "\n".join(lines)


def _guest_text(vmid: int) -> str:
    node = pve.resolve_node(vmid)
    status = pve.guest_status(vmid, node)
    config = pve.guest_config(vmid, node)
    snaps = [s.get("name") for s in pve.snapshots(vmid, node)]
    lines = [f"# Proxmox guest {vmid} ({config.get('name') or 'unnamed'}) on {node}"]
    lines.append(f"type: {'lxc' if pve.guest_kind(vmid) == 'lxc' else 'qemu'}")
    lines.append(f"state: {status.get('status')}  uptime: {int((status.get('uptime') or 0) // 3600)}h")
    lines.append(
        f"cpu: {100.0 * float(status.get('cpu') or 0):.0f}% of {status.get('cpus')} "
        f"mem: {float(status.get('mem') or 0) / 2**20:.0f}/{float(status.get('maxmem') or 0) / 2**20:.0f} MiB"
    )
    for key in ("cores", "sockets", "memory", "balloon", "ostype", "onboot", "agent", "net0", "scsi0", "virtio0", "rootfs", "tags"):
        if config.get(key) not in (None, ""):
            lines.append(f"{key}: {config.get(key)}")
    lines.append(f"snapshots: {', '.join(snaps) if snaps else 'none'}")
    return "\n".join(lines)


def _node_text(node: str) -> str:
    status = pve.node_status(node)
    row = next((n for n in pve.nodes() if n.get("node") == node), {})
    lines = [f"# Proxmox node {node}"]
    lines.append(f"pve: {_ver(status.get('pveversion'))}")
    lines.append(f"kernel: {(status.get('current-kernel') or {}).get('release')}")
    lines.append(
        f"cpu: {100.0 * float(row.get('cpu') or 0):.0f}%  "
        f"mem: {float(row.get('mem') or 0) / 2**30:.1f}/{float(row.get('maxmem') or 0) / 2**30:.1f} GiB  "
        f"rootfs: {float(row.get('disk') or 0) / 2**30:.0f}/{float(row.get('maxdisk') or 0) / 2**30:.0f} GiB"
    )
    for store in pve.node_storage(node):
        if not store.get("active"):
            continue
        total = float(store.get("total") or 0)
        used = float(store.get("used") or 0)
        pct = (100.0 * used / total) if total else 0.0
        lines.append(
            f"storage {store.get('storage')} ({store.get('type')}): "
            f"{used / 2**30:.0f}/{total / 2**30:.0f} GiB ({pct:.0f}%)"
        )
    return "\n".join(lines)


def _expand_sync(target: str) -> Optional[str]:
    """Blocking worker; raises PveError on bad input."""
    wanted = (target or "").strip().strip('"').strip("'")
    if not wanted or wanted.lower() in {"all", "cluster", "fleet"}:
        return _fleet_text()
    if wanted.isdigit():
        return _guest_text(int(wanted))
    return _node_text(wanted)


class PveContextReference(ContextReferenceProvider):
    prefix = "pve"
    description = "Live Proxmox VE state (vmid, node name, or 'all')"

    async def autocomplete(self, query: str, *, limit: int = 10) -> List[ContextCompletionItem]:
        try:
            guests = await asyncio.to_thread(pve.cluster_guests)
        except Exception:
            return [ContextCompletionItem(text="all", display="all", meta="cluster inventory")]
        needle = (query or "").strip().lower()
        items: List[ContextCompletionItem] = [
            ContextCompletionItem(text="all", display="all", meta="cluster inventory")
        ]
        for guest in guests:
            vmid = str(guest.get("vmid"))
            name = str(guest.get("name") or "")
            if needle and needle not in vmid and needle not in name.lower():
                continue
            items.append(
                ContextCompletionItem(
                    text=vmid,
                    display=f"{vmid} {name}".strip(),
                    meta=f"{guest.get('type')} on {guest.get('node')} · {guest.get('status')}",
                )
            )
            if len(items) >= limit:
                break
        return items

    async def expand(self, target: str) -> Optional[str]:
        try:
            return await asyncio.to_thread(_expand_sync, target)
        except Exception as exc:
            raise RuntimeError(f"@pve:{target} — {exc}") from None


__all__ = ["PveContextReference"]

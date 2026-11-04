"""Minimal Proxmox VE API client — standard library only.

Auth model: a PVE API token sent as ``Authorization: PVEAPIToken=<user>@<realm>!<tokenid>=<secret>``.
Two shapes are accepted so the plugin works with an existing install:

* ``PVE_API_TOKEN`` already holds ``user@realm!tokenid=secret`` (one secret, preferred), or
* ``PVE_USER`` + ``PVE_TOKEN_NAME`` + ``PVE_TOKEN_VALUE`` are set separately.

``PROXMOX_*`` names are accepted as a fallback for users who already export them for
another Proxmox integration. Secrets are read through ``agent.secret_scope.get_secret``
so the active Hermes profile wins over a bare ``os.environ`` read.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

try:  # inside Hermes
    from agent.secret_scope import get_secret
except Exception:  # bare interpreter (catalog probe / standalone test)
    def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:  # type: ignore[misc]
        return os.environ.get(name, default)

DEFAULT_PORT = 8006
DEFAULT_TIMEOUT = 20.0


class PveError(RuntimeError):
    """Any Proxmox failure that should be reported to the model as data, not raised."""


class PveAuthError(PveError):
    """Missing or rejected credentials."""


@dataclass(frozen=True)
class PveConfig:
    host: str
    port: int
    token: str
    verify_tls: bool
    timeout: float


def _secret(*names: str, default: str = "") -> str:
    for name in names:
        value = get_secret(name, None)
        if value:
            return str(value).strip()
    return default


def _truthy(name: str, default: str = "1") -> bool:
    return _secret(name, default=default).strip().lower() not in {"0", "false", "no", "off"}


def build_token() -> str:
    """Resolve the ``user@realm!tokenid=secret`` header value."""
    combined = _secret("PVE_API_TOKEN", "PROXMOX_API_TOKEN")
    if combined and "!" in combined:
        return combined

    user = _secret("PVE_USER", "PROXMOX_USER")
    token_name = _secret("PVE_TOKEN_NAME", "PROXMOX_TOKEN_NAME")
    token_value = combined or _secret("PVE_TOKEN_VALUE", "PROXMOX_TOKEN_VALUE")

    if not user or not token_name or not token_value:
        raise PveAuthError(
            "Proxmox credentials missing. Set PVE_API_TOKEN to "
            "'user@realm!tokenid=secret', or set PVE_USER, PVE_TOKEN_NAME and "
            "PVE_TOKEN_VALUE. The token needs at least PVEAuditor on / for the read "
            "tools, plus PVEVMAdmin on the guests you want to snapshot or exec into."
        )
    if "@" not in user:
        raise PveAuthError(f"PVE_USER must include the realm (e.g. 'root@pam'), got {user!r}")
    return f"{user}!{token_name}={token_value}"


def load_config() -> PveConfig:
    host = _secret("PVE_HOST", "PROXMOX_HOST")
    if not host:
        raise PveAuthError("PVE_HOST is not set (e.g. 192.168.6.12 or pve.example.com)")
    host = host.removeprefix("https://").removeprefix("http://").rstrip("/")
    if ":" in host:  # host:port in one value
        host, _, port_part = host.partition(":")
        port_part = port_part.split("/")[0] or _secret(
            "PVE_PORT", "PROXMOX_PORT", default=str(DEFAULT_PORT)
        )
    else:
        port_part = _secret("PVE_PORT", "PROXMOX_PORT", default=str(DEFAULT_PORT))
    try:
        port = int(port_part)
    except ValueError:
        raise PveAuthError(f"invalid Proxmox port {port_part!r}") from None
    try:
        timeout = float(_secret("PVE_TIMEOUT", default=str(DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return PveConfig(
        host=host,
        port=port,
        token=build_token(),
        verify_tls=_truthy("PVE_VERIFY_TLS", "1"),
        timeout=max(3.0, min(timeout, 120.0)),
    )


def is_configured() -> bool:
    """Cheap, offline check used as the tools' ``check_fn``."""
    try:
        load_config()
    except PveError:
        return False
    return True


def _ssl_context(cfg: PveConfig) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not cfg.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
    cfg: Optional[PveConfig] = None,
) -> Any:
    """Call ``/api2/json<path>`` and return the ``data`` member."""
    cfg = cfg or load_config()
    url = f"https://{cfg.host}:{cfg.port}/api2/json{path}"
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    if clean_params:
        url += "?" + urllib.parse.urlencode(clean_params, doseq=True)

    payload = None
    headers = {"Authorization": f"PVEAPIToken={cfg.token}", "Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(
            req, timeout=timeout or cfg.timeout, context=_ssl_context(cfg)
        ) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        if exc.code in (401, 403):
            raise PveAuthError(
                f"Proxmox rejected the API token ({exc.code}). Check the token value, "
                f"that the token is not expired, and its role/permissions. {detail}".strip()
            ) from None
        raise PveError(f"Proxmox {method.upper()} {path} failed: HTTP {exc.code} {detail}".strip()) from None
    except urllib.error.URLError as exc:
        raise PveError(f"cannot reach Proxmox at {cfg.host}:{cfg.port} — {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise PveError(f"Proxmox request timed out or failed: {exc}") from None

    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise PveError(f"Proxmox returned non-JSON for {path}") from None
    return parsed.get("data")


# ── cluster / node reads ──────────────────────────────────────────────────────

def nodes() -> List[Dict[str, Any]]:
    return request("GET", "/nodes") or []


def cluster_guests() -> List[Dict[str, Any]]:
    """Every VM and container in the cluster, each carrying its ``node``."""
    return request("GET", "/cluster/resources", params={"type": "vm"}) or []


def node_status(node: str) -> Dict[str, Any]:
    return request("GET", f"/nodes/{urllib.parse.quote(node)}/status") or {}


def node_storage(node: str) -> List[Dict[str, Any]]:
    return request("GET", f"/nodes/{urllib.parse.quote(node)}/storage") or []


def guest_kind(vmid: int) -> Optional[str]:
    for guest in cluster_guests():
        if int(guest.get("vmid", -1)) == int(vmid):
            return "lxc" if guest.get("type") == "lxc" else "qemu"
    return None


def resolve_node(vmid: int, node: Optional[str] = None) -> str:
    if node:
        return node
    for guest in cluster_guests():
        if int(guest.get("vmid", -1)) == int(vmid):
            found = guest.get("node")
            if found:
                return str(found)
    raise PveError(f"guest {vmid} not found in the cluster")


def guest_status(vmid: int, node: Optional[str] = None) -> Dict[str, Any]:
    node = resolve_node(vmid, node)
    kind = guest_kind(vmid) or "qemu"
    return request("GET", f"/nodes/{node}/{kind}/{vmid}/status/current") or {}


def guest_config(vmid: int, node: Optional[str] = None) -> Dict[str, Any]:
    node = resolve_node(vmid, node)
    kind = guest_kind(vmid) or "qemu"
    return request("GET", f"/nodes/{node}/{kind}/{vmid}/config") or {}


def snapshots(vmid: int, node: Optional[str] = None) -> List[Dict[str, Any]]:
    node = resolve_node(vmid, node)
    kind = guest_kind(vmid) or "qemu"
    items = request("GET", f"/nodes/{node}/{kind}/{vmid}/snapshot") or []
    return [s for s in items if s.get("name") != "current"]


def create_snapshot(vmid: int, name: str, description: str = "", node: Optional[str] = None) -> str:
    node = resolve_node(vmid, node)
    kind = guest_kind(vmid) or "qemu"
    upid = request(
        "POST",
        f"/nodes/{node}/{kind}/{vmid}/snapshot",
        body={"snapname": name, "description": description},
    )
    return str(upid or "")


def delete_snapshot(vmid: int, name: str, node: Optional[str] = None) -> str:
    node = resolve_node(vmid, node)
    kind = guest_kind(vmid) or "qemu"
    upid = request(
        "DELETE",
        f"/nodes/{node}/{kind}/{vmid}/snapshot/{urllib.parse.quote(name)}",
    )
    return str(upid or "")


# ── tasks ────────────────────────────────────────────────────────────────────

def node_tasks(node: str, limit: int = 50, typefilter: Optional[str] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit, "start": 0}
    if typefilter:
        params["typefilter"] = typefilter
    return request("GET", f"/nodes/{urllib.parse.quote(node)}/tasks", params=params) or []


def task_status(upid: str) -> Dict[str, Any]:
    """``UPID:<node>:...`` — the node is the second colon-separated field."""
    parts = upid.split(":")
    if len(parts) < 3 or not parts[1]:
        raise PveError(f"malformed task id {upid!r}")
    node = parts[1]
    return request("GET", f"/nodes/{urllib.parse.quote(node)}/tasks/{urllib.parse.quote(upid, safe='')}/status") or {}


def wait_for_task(upid: str, timeout: float = 30.0, interval: float = 1.0) -> Dict[str, Any]:
    """Poll a task until it stops or ``timeout`` elapses. Returns the last status payload."""
    import time

    deadline = time.monotonic() + max(1.0, timeout)
    status: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = task_status(upid)
        if str(status.get("status", "")).strip() == "stopped":
            return status
        time.sleep(interval)
    status.setdefault("status", "running")
    status["timed_out"] = True
    status["upid"] = upid
    return status


# ── qemu guest agent ─────────────────────────────────────────────────────────

def agent_exec(vmid: int, command: Sequence[str], node: Optional[str] = None) -> Dict[str, Any]:
    node = resolve_node(vmid, node)
    if guest_kind(vmid) == "lxc":
        raise PveError(
            "the QEMU guest agent is not available in containers — use the container's "
            "own shell (the Proxmox UI, or ssh) instead"
        )
    data = request(
        "POST",
        f"/nodes/{node}/qemu/{vmid}/agent/exec",
        body={"command": list(command)},
    )
    pid = data.get("pid") if isinstance(data, dict) else data
    if pid is None:
        raise PveError(f"guest agent accepted the exec but returned no pid: {data!r}")
    return {"pid": int(pid)}


def agent_exec_status(vmid: int, pid: int, node: Optional[str] = None) -> Dict[str, Any]:
    node = resolve_node(vmid, node)
    return request(
        "GET",
        f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
        params={"pid": pid},
    ) or {}


def run_agent_command(
    vmid: int,
    command: Sequence[str],
    node: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    import time

    started = agent_exec(vmid, command, node)
    pid = int(started["pid"])
    deadline = time.monotonic() + max(1.0, timeout)
    result: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = agent_exec_status(vmid, pid, node)
        if result.get("exited"):
            break
        time.sleep(1.0)
    else:
        result.setdefault("exited", False)
        result["timed_out"] = True
    result["pid"] = pid
    return result

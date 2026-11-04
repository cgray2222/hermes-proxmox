"""Proxmox VE plugin for Hermes Agent.

Public surface only: ``ctx.register_tool`` for the six ``pve_*`` tools plus one
``@pve:`` context reference. Nothing here patches or introspects Hermes core.

Layout:
    plugin.yaml      manifest (the catalog PR pins this tree)
    pve_client.py    stdlib HTTP client + credential resolution
    tools.py         tool schemas and handlers
    references.py    ``@pve:<target>`` context reference provider
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    from .tools import register_tools

    register_tools(ctx)

    try:
        from .references import PveContextReference

        ctx.register_context_reference(PveContextReference())
    except Exception as exc:  # a broken reference must never block the tools
        logger.warning("proxmox: @pve: context reference not registered: %s", exc)


__all__ = ["register"]

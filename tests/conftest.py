"""Load the plugin directory (its name has a hyphen) under a valid package name."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "hermes_proxmox", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules["hermes_proxmox"] = module
spec.loader.exec_module(module)

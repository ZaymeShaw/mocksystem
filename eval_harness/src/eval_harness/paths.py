"""Repository locations shared by runners and adapters.

MOCK_SYSTEM_ROOT can point an installed harness at a separate workspace checkout.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("MOCK_SYSTEM_ROOT", Path(__file__).resolve().parents[3])).expanduser().resolve()
HARNESS_ROOT = PROJECT_ROOT / "eval_harness"
CONFIG_ROOT = HARNESS_ROOT / "configs"
WORKSPACES_ROOT = PROJECT_ROOT / "workspaces"

#!/usr/bin/env python3
"""Sync workspaces/claude/.claude/settings.local.json from llm_gateway/.env."""
from __future__ import annotations
import json, os
from pathlib import Path

GW = Path(__file__).resolve().parent
SETTINGS = GW.parent / "workspaces" / "claude" / ".claude" / "settings.local.json"

def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def g(env: dict, k: str, default: str = "") -> str:
    return os.environ.get(k) or env.get(k) or default

def main() -> None:
    env = load_dotenv(GW / ".env")
    host = g(env, "LITELLM_HOST", "127.0.0.1")
    port = g(env, "LITELLM_PORT", "4001")
    master = g(env, "LITELLM_MASTER_KEY")
    if not master:
        raise SystemExit("LITELLM_MASTER_KEY is not set")
    model = g(env, "UPSTREAM_MODEL", "deepseek-v4-flash-0731")
    base = f"http://{host}:{port}"
    if not SETTINGS.exists():
        raise SystemExit(f"missing settings: {SETTINGS}")
    data = json.loads(SETTINGS.read_text())
    data.setdefault("env", {})
    data["env"]["ANTHROPIC_BASE_URL"] = base
    data["env"]["ANTHROPIC_AUTH_TOKEN"] = master
    data["env"]["ANTHROPIC_MODEL"] = model
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"synced {SETTINGS}")
    print(f"  ANTHROPIC_BASE_URL={base}")
    print(f"  ANTHROPIC_MODEL={model}")
    print(f"  ANTHROPIC_AUTH_TOKEN=***len={len(master)}")

if __name__ == "__main__":
    main()

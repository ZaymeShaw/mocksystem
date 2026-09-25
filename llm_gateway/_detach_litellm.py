#!/usr/bin/env python3
"""Start litellm in a new session so parent Shell teardown cannot kill it."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DIR = Path(__file__).resolve().parent
cfg = os.environ.get("LITELLM_CONFIG") or str(DIR / "config.litellm.with_master.yaml")
if not Path(cfg).is_file():
    cfg = str(DIR / "config.yaml")
host = os.environ.get("LITELLM_HOST", "127.0.0.1")
port = os.environ.get("LITELLM_PORT", "4001")
master = os.environ.get("LITELLM_MASTER_KEY", "")
log = Path(os.environ.get("LLM_GATEWAY_LOG_DIR", DIR / "logs")) / "litellm.stdout.log"
pid_path = DIR / "run" / "litellm.pid"
log.parent.mkdir(parents=True, exist_ok=True)
pid_path.parent.mkdir(parents=True, exist_ok=True)

cmd = [sys.executable, str(DIR / "_litellm_log_supervisor.py")]
pid_path.unlink(missing_ok=True)
with (log.parent / "litellm.supervisor.log").open("wb") as out:
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=out,
        stdin=subprocess.DEVNULL,
        env=os.environ.copy(),
        cwd=str(DIR),
        start_new_session=True,
    )
print(f"started LiteLLM log supervisor pid={p.pid} http://{host}:{port}")

url = f"http://{host}:{port}/v1/models"
deadline = time.time() + 25
last_err: Exception | None = None
while time.time() < deadline:
    if p.poll() is not None:
        print(f"litellm supervisor exited early code={p.returncode}; see {log}", file=sys.stderr)
        sys.exit(1)
    if pid_path.is_file():
        try:
            child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            child_pid = 0
    else:
        child_pid = 0
    if child_pid <= 0:
        time.sleep(0.1)
        continue
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {master}"})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status == 200:
                print(f"health ok; litellm pid={child_pid}")
                sys.exit(0)
    except Exception as e:  # noqa: BLE001
        last_err = e
    time.sleep(0.5)
print(f"litellm started but health check failed: {last_err}", file=sys.stderr)
sys.exit(1)

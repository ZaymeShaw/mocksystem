#!/usr/bin/env python3
"""Drain LiteLLM diagnostic stdout into a bounded rotating file.

The structured llm_calls.jsonl callback is independent of this diagnostic
stream and is deliberately never rotated here.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main() -> int:
    directory = Path(__file__).resolve().parent
    cfg = os.environ.get("LITELLM_CONFIG") or str(directory / "config.litellm.with_master.yaml")
    host = os.environ.get("LITELLM_HOST", "127.0.0.1")
    port = os.environ.get("LITELLM_PORT", "4001")
    log_dir = Path(os.environ.get("LLM_GATEWAY_LOG_DIR", str(directory / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    limit_mb = max(1, min(int(os.environ.get("LITELLM_STDOUT_MAX_MB", "32")), 1024))
    backups = max(1, min(int(os.environ.get("LITELLM_STDOUT_BACKUPS", "3")), 20))
    handler = RotatingFileHandler(log_dir / "litellm.stdout.log", maxBytes=limit_mb * 1024 * 1024,
                                  backupCount=backups, encoding="utf-8")
    pid_file = directory / "run" / "litellm.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    command = ["litellm", "--config", cfg, "--host", host, "--port", port, "--telemetry", "False"]
    child_env = os.environ.copy()
    child_env["LLM_ATTRIBUTION_GATEWAY_PROCESS"] = "1"
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, cwd=directory, env=child_env,
                              start_new_session=True, bufsize=0) as child:
            pid_file.write_text(f"{child.pid}\n", encoding="utf-8")
            assert child.stdout is not None
            for line in child.stdout:
                handler.emit(logging.makeLogRecord({"msg": line.decode("utf-8", errors="replace").rstrip("\n"),
                                                    "levelno": logging.INFO, "levelname": "INFO"}))
            return child.wait()
    finally:
        handler.close()


if __name__ == "__main__":
    sys.exit(main())

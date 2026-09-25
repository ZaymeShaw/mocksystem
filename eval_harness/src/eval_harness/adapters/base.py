"""Common adapter result types and artifact helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SECRET_KEY_FRAGMENTS = (
    "api_key", "apikey", "authorization", "secret", "token", "password", "dashscope", "bearer",
)


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            out[k] = "***REDACTED***" if any(f in lk for f in SECRET_KEY_FRAGMENTS) else redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    return obj


@dataclass
class TurnResult:
    index: int
    prompt: str
    exit_code: int
    stream_path: Path
    session_id: Optional[str] = None
    final_text: str = ""
    wall_ms: int = 0
    first_frame_ms: Optional[int] = None
    first_frame_kind: Optional[str] = None
    raw_events: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    api_ms: Optional[int] = None


@dataclass
class CaseRunResult:
    case_id: str
    session_id: Optional[str]
    turns: list[TurnResult]
    success: bool
    exit_code: int
    error: Optional[str]
    wall_ms: int
    transcript_path: Optional[Path] = None


def concat_streams(case_dir: Path, n_turns: int, out_name: str = "stream.jsonl") -> Path:
    out = case_dir / out_name
    with out.open("w", encoding="utf-8") as w:
        for i in range(1, n_turns + 1):
            sp = case_dir / f"stream_turn{i}.jsonl"
            if not sp.is_file():
                continue
            w.write(f"### TURN {i}\n")
            body = sp.read_text(encoding="utf-8", errors="replace")
            w.write(body)
            if body and not body.endswith("\n"):
                w.write("\n")
    return out

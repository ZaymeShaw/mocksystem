"""Dataset Bundle v1: load/save/validate JSONL cases; import from prompt-only markdown."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from eval_harness.parse_dataset import Case, parse_dataset

TZ = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = "1.0"


@dataclass
class BundleCase:
    case_id: str
    turns: List[str]
    class_: str = ""
    tags: List[str] = field(default_factory=list)
    source: Optional[dict] = None
    extensions: Optional[dict] = None
    schema_version: str = SCHEMA_VERSION

    @property
    def class_letter(self) -> str:
        return self.class_ or (self.case_id[:1] if self.case_id else "")

    # aliases used by older runner field names
    @property
    def n_turns(self) -> int:
        return len(self.turns)


def case_to_obj(c: BundleCase) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "schema_version": c.schema_version,
        "case_id": c.case_id,
        "turns": list(c.turns),
    }
    if c.class_ or c.case_id:
        obj["class"] = c.class_ or c.case_id[:1]
    if c.tags:
        obj["tags"] = list(c.tags)
    if c.source:
        obj["source"] = c.source
    if c.extensions:
        obj["extensions"] = c.extensions
    return obj


def obj_to_case(obj: dict[str, Any]) -> BundleCase:
    if obj.get("schema_version") not in (None, SCHEMA_VERSION, "1.0"):
        raise ValueError(f"Unsupported bundle schema_version: {obj.get('schema_version')}")
    case_id = obj["case_id"]
    turns = obj["turns"]
    if not isinstance(turns, list) or not turns or any(not isinstance(t, str) or not t.strip() for t in turns):
        raise ValueError(f"Invalid turns for case {case_id}")
    tags = list(obj.get("tags") or [])
    if len(turns) > 1 and "multi_turn" not in tags:
        tags.append("multi_turn")
    elif len(turns) == 1 and "single_turn" not in tags:
        tags.append("single_turn")
    return BundleCase(
        case_id=case_id,
        turns=turns,
        class_=obj.get("class") or case_id[:1],
        tags=tags,
        source=obj.get("source"),
        extensions=obj.get("extensions"),
        schema_version=str(obj.get("schema_version") or SCHEMA_VERSION),
    )


def validate_case_obj(obj: dict[str, Any]) -> None:
    for key in ("schema_version", "case_id", "turns"):
        if key not in obj:
            raise ValueError(f"Bundle case missing {key}")
    if obj["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(obj["case_id"], str) or not obj["case_id"]:
        raise ValueError("case_id must be non-empty string")
    turns = obj["turns"]
    if not isinstance(turns, list) or not turns:
        raise ValueError("turns must be non-empty array")
    for t in turns:
        if not isinstance(t, str) or not t.strip():
            raise ValueError(f"empty turn in {obj['case_id']}")


def load_bundle(path: str | Path) -> list[BundleCase]:
    path = Path(path)
    cases: list[BundleCase] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {e}") from e
            validate_case_obj(obj)
            c = obj_to_case(obj)
            if c.case_id in seen:
                raise ValueError(f"Duplicate case_id in bundle: {c.case_id}")
            seen.add(c.case_id)
            cases.append(c)
    return cases


def write_bundle(path: str | Path, cases: Sequence[BundleCase], *, meta_path: str | Path | None = None,
                 bundle_id: str = "", title: str = "", source_files: Optional[list[str]] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in cases:
            obj = case_to_obj(c)
            validate_case_obj(obj)
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    if meta_path is None:
        meta_path = path.with_suffix(".meta.json")
    meta = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id or path.stem,
        "title": title or path.stem,
        "description": "Prompt-only eval cases (no gold/scoring).",
        "case_count": len(cases),
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source_files": source_files or [],
    }
    Path(meta_path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def import_from_markdown(md_path: str | Path, *, source_label: str | None = None) -> list[BundleCase]:
    md_path = Path(md_path)
    parsed = parse_dataset(md_path)
    out: list[BundleCase] = []
    for c in parsed:
        tags = ["multi_turn"] if len(c.turns) > 1 else ["single_turn"]
        out.append(
            BundleCase(
                case_id=c.case_id,
                turns=list(c.turns),
                class_=c.class_letter,
                tags=tags,
                source={"file": source_label or str(md_path), "locator": c.case_id},
            )
        )
    return out


def filter_bundle_cases(cases: Iterable[BundleCase], ids: Optional[Sequence[str]]) -> list[BundleCase]:
    if not ids:
        return list(cases)
    want = set(ids)
    return [c for c in cases if c.case_id in want]


def to_legacy_case(c: BundleCase) -> Case:
    """Bridge to existing Case dataclass used by runner."""
    return Case(case_id=c.case_id, class_letter=c.class_letter, turns=list(c.turns), raw_block="")

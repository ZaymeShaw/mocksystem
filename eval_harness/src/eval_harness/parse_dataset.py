"""Parse prompt-only dataset markdown into cases with ordered turns."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

CASE_HEADER_RE = re.compile(r"^\*\*([A-F]\d{2})\*\*\s*$", re.MULTILINE)
TURN_RE = re.compile(r"^第\s*(\d+)\s*轮[：:]\s*(.*)$")


@dataclass
class Case:
    case_id: str
    class_letter: str
    turns: List[str] = field(default_factory=list)
    raw_block: str = ""

    @property
    def n_turns(self) -> int:
        return len(self.turns)


def _split_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(CASE_HEADER_RE.finditer(text))
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        case_id = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip("\n")
        blocks.append((case_id, body))
    return blocks


def _parse_turns(body: str) -> list[str]:
    lines = body.splitlines()
    turn_map: dict[int, list[str]] = {}
    found = False
    for line in lines:
        m = TURN_RE.match(line.strip())
        if m:
            found = True
            idx = int(m.group(1))
            rest = m.group(2).strip()
            turn_map.setdefault(idx, [])
            if rest:
                turn_map[idx].append(rest)
            continue
        if found and turn_map and line.strip():
            turn_map[max(turn_map)].append(line.strip())
    if turn_map:
        return ["\n".join(turn_map[i]).strip() for i in sorted(turn_map) if "\n".join(turn_map[i]).strip()]
    paras = [ln.strip() for ln in lines if ln.strip()]
    return ["\n".join(paras)] if paras else []


def parse_dataset(path: str | Path) -> list[Case]:
    text = Path(path).read_text(encoding="utf-8")
    cases: list[Case] = []
    for case_id, body in _split_blocks(text):
        cases.append(Case(case_id=case_id, class_letter=case_id[0], turns=_parse_turns(body), raw_block=body))
    return cases


def parse_case_spec(spec: str, all_ids: Sequence[str]) -> list[str]:
    id_set = set(all_ids)
    order = {cid: i for i, cid in enumerate(all_ids)}
    selected: set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and not part.startswith("-"):
            left, right = [x.strip() for x in part.split("-", 1)]
            if left in id_set and right in id_set and left[0] == right[0]:
                letter = left[0]
                a, b = int(left[1:]), int(right[1:])
                lo, hi = min(a, b), max(a, b)
                for n in range(lo, hi + 1):
                    cid = f"{letter}{n:02d}"
                    if cid in id_set:
                        selected.add(cid)
                continue
        if part in id_set:
            selected.add(part)
        else:
            raise ValueError(f"Unknown case id: {part}")
    return sorted(selected, key=lambda c: order[c])


def filter_cases(cases: Iterable[Case], ids: Optional[Sequence[str]]) -> list[Case]:
    if not ids:
        return list(cases)
    want = set(ids)
    return [c for c in cases if c.case_id in want]

"""CLI: import prompt-only markdown into Dataset Bundle JSONL."""
from __future__ import annotations

import argparse
from pathlib import Path

from eval_harness.bundle_io import import_from_markdown, write_bundle


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Import markdown dataset into Bundle JSONL v1")
    p.add_argument("--md", required=True, help="Path to prompt-only markdown")
    p.add_argument("--out", required=True, help="Output .jsonl path")
    p.add_argument("--bundle-id", default="", help="Bundle id for meta")
    p.add_argument("--title", default="", help="Bundle title")
    args = p.parse_args(argv)
    md = Path(args.md)
    cases = import_from_markdown(md)
    write_bundle(
        args.out,
        cases,
        bundle_id=args.bundle_id or Path(args.out).stem,
        title=args.title or Path(args.out).stem,
        source_files=[str(md)],
    )
    print(f"wrote {args.out} cases={len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI entry: batch-run Dataset Bundle cases through a harness adapter; write Trace + Excel."""
from __future__ import annotations

import argparse
import shutil
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from eval_harness.bundle_io import filter_bundle_cases, load_bundle, to_legacy_case
from eval_harness.parse_dataset import parse_case_spec
from eval_harness.adapters import DEFAULT_HARNESS, dispatch_run_case, resolve_harness
from eval_harness.normalize_trace import build_trace, extract_tool_rows, write_trace
from eval_harness.excel_export import case_row_from_trace, turn_rows_from_trace, write_results_xlsx
from eval_harness.llm_trace_html import build_llm_trace_html, pack_run_zip
from eval_harness.llm_gateway_ingest import (
    attach_llm_calls_to_trace,
    filter_pairs_for_case,
    load_gateway_pairs,
    pairs_to_excel_rows,
    pairs_to_trace_calls,
    write_case_llm_jsonl,
)
from eval_harness.simple_yaml import load_simple_yaml

TZ = ZoneInfo("Asia/Shanghai")
CURRENT_CASE_ID_FILE = Path(
    "/Users/xiaozijian/WorkSpace/package/mock_system/llm_gateway/run/current_case_id"
)


def _set_current_case_id(case_id: str | None) -> None:
    CURRENT_CASE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if case_id:
        CURRENT_CASE_ID_FILE.write_text(case_id, encoding="utf-8")
    elif CURRENT_CASE_ID_FILE.exists():
        CURRENT_CASE_ID_FILE.unlink()



def _now_tag() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        if yaml is not None:
            return yaml.safe_load(text) or {}
        return load_simple_yaml(text)
    return json.loads(text)


def _resolve(base: Path, p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _claude_version(claude_bin: str) -> str:
    try:
        out = subprocess.check_output(
            [claude_bin, "--version"], text=True, stderr=subprocess.STDOUT, timeout=30
        )
        return out.strip().splitlines()[0][:200]
    except Exception as e:
        return f"unknown ({e})"


def _merge_profile(cfg: dict[str, Any], harness_root: Path) -> dict[str, Any]:
    """Load Harness Profile if configured; profile fields override flat config."""
    merged = dict(cfg)
    profile_ref = cfg.get("profile_path") or cfg.get("profile")
    if not profile_ref:
        return merged
    profile_path = _resolve(harness_root, str(profile_ref))
    if not profile_path.is_file():
        raise SystemExit(f"Profile not found: {profile_path}")
    profile = _load_mapping(profile_path)
    # Map profile schema -> runner keys
    mapped = {
        "harness": profile.get("harness"),
        "project_cwd": profile.get("project_cwd"),
        "claude_bin": profile.get("bin") or profile.get("claude_bin"),
        "agent_md": profile.get("agent_md"),
        "mcp_config": profile.get("mcp_config"),
        "permission_mode": profile.get("permission_mode"),
        "dangerously_skip_permissions": profile.get("dangerously_skip_permissions"),
        "timeout_sec_per_turn": profile.get("timeout_sec_per_turn"),
        "concurrency": profile.get("concurrency"),
        "workspace_isolation": profile.get("workspace_isolation"),
        "session_policy": profile.get("session_policy"),
        "notes": profile.get("notes"),
        "profile_id": profile.get("profile_id"),
        "schema_version_profile": profile.get("schema_version"),
    }
    adapter = profile.get("adapter") or {}
    mapped["output_format"] = adapter.get("output_format", merged.get("output_format", "stream-json"))
    mapped["verbose"] = adapter.get("verbose", merged.get("verbose", True))
    mapped["include_partial_messages"] = adapter.get(
        "include_partial_messages", merged.get("include_partial_messages", True)
    )
    mapped["append_system_prompt"] = adapter.get(
        "append_system_prompt", merged.get("append_system_prompt", True)
    )
    mapped["extra_args"] = adapter.get("extra_args", merged.get("extra_args") or [])
    # Pi (and future) adapter knobs — ignored by Claude adapter via **kwargs omission at call site
    for _k in ("provider", "model", "thinking", "no_builtin_tools", "approve"):
        if _k in adapter and adapter.get(_k) is not None:
            mapped[_k] = adapter.get(_k)
    out = profile.get("output") or {}
    if out.get("eval_runs_dir"):
        mapped["eval_runs_dir"] = out["eval_runs_dir"]
    for k, v in mapped.items():
        if v is not None:
            merged[k] = v
    merged["_profile_path"] = str(profile_path)
    return merged


def _resolve_bundle_path(cfg: dict[str, Any], harness_root: Path) -> Path:
    raw = cfg.get("bundle_path")
    if not raw:
        raise SystemExit(
            "config missing bundle_path (Dataset Bundle JSONL). "
            "Import markdown first: python3 -m eval_harness.import_bundle --md ... --out ..."
        )
    path = Path(raw)
    if not path.is_absolute():
        candidates = [
            _resolve(harness_root, raw),
            harness_root / "bundles" / Path(raw).name,
        ]
        path = next((c for c in candidates if c.is_file()), candidates[0])
    if not path.is_file():
        raise SystemExit(f"Bundle not found: {raw}")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Eval harness runner (Bundle + Profile -> Trace)")
    p.add_argument("--config", required=True, help="Path to config.yaml")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="Run all cases in bundle")
    g.add_argument("--cases", type=str, help="Case selector, e.g. A01,E01 or A01-A05")
    p.add_argument("--run-id", type=str, default=None, help="Optional run id (default: timestamp)")
    p.add_argument("--dry-parse", action="store_true", help="Only load bundle and print case list")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Reuse run-id dir: skip success=true; wipe failed/incomplete case dirs then re-run as never-run",
    )
    p.add_argument(
        "--rebuild-excel",
        action="store_true",
        help="Do not run cases; rebuild results.xlsx from existing per-case process artifacts under run-id",
    )
    p.add_argument(
        "--pack-zip",
        action="store_true",
        help="After excel/html build, write <run-id>_share.zip (xlsx+html+cases) for sharing",
    )
    p.add_argument(
        "--eval-runs-dir",
        type=str,
        default=None,
        help="Override profile/config output.eval_runs_dir (absolute preferred)",
    )
    return p



def _classify_error(err: Optional[str]) -> str:
    if not err:
        return "none"
    e = err.lower()
    if "arrearage" in e or "overdue" in e or "欠费" in err:
        return "arrearage"
    if "idle timeout" in e:
        return "idle_timeout"
    if "incomplete" in e:
        return "incomplete"
    if "timeout" in e:
        return "timeout"
    return "other"


def _append_progress(path: Path, row: dict[str, Any]) -> None:
    """Append-only run progress (does not change Trace/Excel schemas)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_trace(case_dir: Path) -> Optional[dict[str, Any]]:
    p = case_dir / "trace.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _case_is_success(trace: Optional[dict[str, Any]]) -> bool:
    return bool(trace) and bool(trace.get("success"))


def _wipe_case_dir(case_dir: Path) -> None:
    """Resume policy: failed/incomplete cases are treated as never-run — delete process dir."""
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)


def _cases_for_excel_rebuild(
    selected: list,
    bundle_cases: list,
    cases_root: Path,
) -> list:
    """Prefer every case dir under the run that has a trace and is in the bundle.

    So `--cases D20,F11 --resume --run-id <full-run>` still rebuilds the full
    results.xlsx / HTML coverage for that run, not just the re-run subset.
    Falls back to `selected` when the run dir has no broader set of traces.
    """
    by_id = {c.case_id: to_legacy_case(c) for c in bundle_cases}
    ids: list[str] = []
    if cases_root.is_dir():
        for d in sorted(p for p in cases_root.iterdir() if p.is_dir()):
            if d.name in by_id and (d / "trace.json").is_file():
                ids.append(d.name)
    if len(ids) <= len(selected):
        # no broader coverage (fresh run or subset-only dir)
        return selected
    return [by_id[i] for i in ids]




def _llm_rows_from_case_file(case_dir: Path, case_id: str) -> list[dict[str, Any]]:
    """Rebuild llm Excel rows from per-case llm_calls.jsonl (process artifact)."""
    p = case_dir / "llm_calls.jsonl"
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        req = d.get("request")
        resp = d.get("response")
        if isinstance(req, (dict, list)):
            req = json.dumps(req, ensure_ascii=False, indent=2)
        if isinstance(resp, (dict, list)):
            resp = json.dumps(resp, ensure_ascii=False, indent=2)
        rows.append(
            {
                "case_id": d.get("case_id") or case_id,
                "seq": d.get("seq"),
                "call_id": d.get("call_id"),
                "ts": d.get("ts"),
                "model": d.get("model"),
                "stream": d.get("stream"),
                "path": d.get("path"),
                "status_code": d.get("status_code"),
                "latency_ms": d.get("latency_ms"),
                "request": req or "",
                "response": resp or "",
                "error": d.get("error") or "",
            }
        )
    return rows


def _rows_from_existing_case(
    case,
    case_dir: Path,
    run_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    trace = _load_trace(case_dir)
    if not trace:
        raise FileNotFoundError(f"missing trace.json under {case_dir}")
    rel = str((case_dir / "trace.json").relative_to(run_dir))
    case_row = case_row_from_trace(
        trace,
        class_letter=case.class_letter,
        n_turns=case.n_turns,
        trace_relpath=rel,
    )
    turn_rows = turn_rows_from_trace(trace)
    tool_rows = extract_tool_rows(trace.get("events") or [], case.case_id)
    llm_rows = _llm_rows_from_case_file(case_dir, case.case_id)
    return case_row, turn_rows, tool_rows, llm_rows


def _rebuild_excel_from_cases(
    *,
    selected: list,
    cases_root: Path,
    run_dir: Path,
    xlsx_path: Path,
    run_meta: dict[str, Any],
) -> tuple[int, int]:
    case_rows: list[dict[str, Any]] = []
    turn_rows: list[dict[str, Any]] = []
    tool_rows: list[dict[str, Any]] = []
    llm_rows: list[dict[str, Any]] = []
    missing = []
    for case in selected:
        case_dir = cases_root / case.case_id
        try:
            cr, tr, tool, llm = _rows_from_existing_case(case, case_dir, run_dir)
        except FileNotFoundError:
            missing.append(case.case_id)
            continue
        case_rows.append(cr)
        turn_rows.extend(tr)
        tool_rows.extend(tool)
        llm_rows.extend(llm)
    write_results_xlsx(
        xlsx_path,
        case_rows=case_rows,
        turn_rows=turn_rows,
        tool_rows=tool_rows,
        run_meta=run_meta,
        llm_rows=llm_rows,
    )
    n_ok = sum(1 for r in case_rows if r.get("success"))
    if missing:
        print(f"[rebuild] missing_trace={','.join(missing)}")
    
    try:
        html_path = build_llm_trace_html(run_dir)
        print(f"[rebuild] llm_trace_html={html_path}")
    except Exception as e:
        print(f"[rebuild] llm_trace_html failed: {e!r}")
    return n_ok, len(case_rows)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    cfg = _merge_profile(_load_mapping(config_path), config_path.parent)
    harness_root = config_path.parent
    if getattr(args, "eval_runs_dir", None):
        # CLI wins over profile (dual_run nesting under eval_runs/dual_*).
        cfg["eval_runs_dir"] = args.eval_runs_dir

    project_cwd = Path(cfg["project_cwd"]).expanduser()
    if project_cwd.exists():
        project_cwd = project_cwd.resolve()

    bundle_path = _resolve_bundle_path(cfg, harness_root)
    bundle_cases = load_bundle(bundle_path)
    all_ids = [c.case_id for c in bundle_cases]

    if args.all:
        selected_bundle = bundle_cases
    else:
        ids = parse_case_spec(args.cases, all_ids)
        selected_bundle = filter_bundle_cases(bundle_cases, ids)

    selected = [to_legacy_case(c) for c in selected_bundle]

    if args.dry_parse:
        for c in selected:
            preview = c.turns[0][:60] if c.turns else ""
            print(f"{c.case_id}\tturns={c.n_turns}\tpreview={preview}")
        print(f"total={len(selected)}")
        print(f"bundle={bundle_path}")
        print(f"profile={cfg.get('_profile_path') or '(inline config)'}")
        return 0

    if not selected:
        raise SystemExit("No cases selected")

    run_id = args.run_id or _now_tag()
    eval_runs_dir = Path(cfg.get("eval_runs_dir", "eval_runs"))
    if not eval_runs_dir.is_absolute():
        eval_runs_dir = (project_cwd / eval_runs_dir).resolve()
    run_dir = eval_runs_dir / run_id
    cases_root = run_dir / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)

    claude_bin = cfg.get("claude_bin") or "claude"
    version = _claude_version(claude_bin)
    started_at = datetime.now(TZ).isoformat(timespec="seconds")

    print(f"[run] id={run_id} cases={','.join(c.case_id for c in selected)} cwd={project_cwd}")
    print(f"[run] bin={claude_bin} version={version} harness={cfg.get('harness', DEFAULT_HARNESS)} bundle={bundle_path}")
    print(f"[run] profile={cfg.get('profile_id') or cfg.get('_profile_path')}")
    if args.resume:
        print("[run] resume=1 (skip success=true traces; re-run others)")
    if args.rebuild_excel:
        print("[run] rebuild-excel=1 (no case execution)")

    progress_path = run_dir / "progress.jsonl"
    xlsx_path = run_dir / "results.xlsx"

    if args.rebuild_excel:
        if not args.run_id:
            raise SystemExit("--rebuild-excel requires --run-id pointing at an existing run dir")
        if not cases_root.is_dir():
            raise SystemExit(f"cases dir not found: {cases_root}")
        run_meta = {
            "schema_version": "1.0",
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "claude_version": version,
            "bundle_path": str(bundle_path),
            "profile_id": cfg.get("profile_id"),
            "profile_path": cfg.get("_profile_path"),
            "project_cwd": str(project_cwd),
            "harness": cfg.get("harness", DEFAULT_HARNESS),
            "workspace_isolation": cfg.get("workspace_isolation", "none"),
            "notes": (cfg.get("notes") or "") + " | rebuilt excel from process artifacts",
            "n_cases": len(selected),
            "timezone": "Asia/Shanghai",
            "mode": "rebuild_excel",
        }
        excel_cases = _cases_for_excel_rebuild(selected, bundle_cases, cases_root)
        run_meta["n_cases"] = len(excel_cases)
        n_ok, n_total = _rebuild_excel_from_cases(
            selected=excel_cases,
            cases_root=cases_root,
            run_dir=run_dir,
            xlsx_path=xlsx_path,
            run_meta=run_meta,
        )
        run_meta["n_cases"] = n_total
        run_meta["n_ok"] = n_ok
        (run_dir / "run_meta.json").write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _append_progress(
            progress_path,
            {
                "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                "run_id": run_id,
                "action": "rebuild_excel",
                "success_count": n_ok,
                "total_with_trace": n_total,
                "selected_ids": [c.case_id for c in selected],
                "excel_ids": [c.case_id for c in excel_cases],
            },
        )
        print(f"[done] results={xlsx_path}")
        print(f"[done] run_dir={run_dir}")
        print(f"[done] success={n_ok}/{n_total} (from process artifacts)")
        if args.pack_zip:
            try:
                z = pack_run_zip(run_dir)
                print(f"[done] share_zip={z}")
            except Exception as e:
                print(f"[warn] pack_zip failed: {e!r}")
        return 0 if n_ok == n_total and n_total == len(excel_cases) else 2

    if args.resume and not args.run_id:
        raise SystemExit("--resume requires --run-id of the interrupted run")

    case_rows = []
    turn_rows = []
    tool_rows = []
    llm_rows = []
    case_windows: list[tuple[str, datetime, datetime]] = []
    n_skipped = 0
    n_rerun = 0

    for case in selected:
        case_dir = cases_root / case.case_id
        existing = _load_trace(case_dir) if args.resume else None
        if args.resume and _case_is_success(existing):
            cr, tr, tool, llm = _rows_from_existing_case(case, case_dir, run_dir)
            case_rows.append(cr)
            turn_rows.extend(tr)
            tool_rows.extend(tool)
            llm_rows.extend(llm)
            n_skipped += 1
            _append_progress(
                progress_path,
                {
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "action": "skip_success",
                    "success": True,
                    "error": None,
                    "error_class": "none",
                },
            )
            print(f"[case] {case.case_id} SKIP success (resume)", flush=True)
            continue

        if args.resume and existing is not None:
            prev_err = existing.get("error")
            err_class = _classify_error(prev_err)
            print(
                f"[case] {case.case_id} DISCARD+RE-RUN prior_fail class={err_class} err={prev_err!r}",
                flush=True,
            )
            _append_progress(
                progress_path,
                {
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "action": "discard_failed",
                    "success": False,
                    "error": prev_err,
                    "error_class": err_class,
                },
            )
            _wipe_case_dir(case_dir)
            n_rerun += 1
        elif args.resume and existing is None and case_dir.exists():
            # Incomplete dir (no usable success trace) — same as never-run
            print(f"[case] {case.case_id} DISCARD incomplete dir then run", flush=True)
            _append_progress(
                progress_path,
                {
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "action": "discard_incomplete",
                    "success": False,
                    "error": "incomplete_or_corrupt_trace",
                    "error_class": "incomplete",
                },
            )
            _wipe_case_dir(case_dir)
            n_rerun += 1
            print(f"[case] {case.case_id} turns={case.n_turns} ...", flush=True)
        else:
            print(f"[case] {case.case_id} turns={case.n_turns} ...", flush=True)

        case_t0 = datetime.now(TZ)
        _set_current_case_id(case.case_id)
        try:
            harness_name = resolve_harness(cfg.get("harness"))
            result = dispatch_run_case(
                harness_name,
                case_id=case.case_id,
                turns=case.turns,
                case_dir=case_dir,
                project_cwd=project_cwd,
                claude_bin=claude_bin,
                agent_md=cfg.get("agent_md", "agent.md"),
                permission_mode=cfg.get("permission_mode", "bypassPermissions"),
                dangerously_skip_permissions=bool(cfg.get("dangerously_skip_permissions", True)),
                output_format=cfg.get("output_format", "stream-json"),
                verbose=bool(cfg.get("verbose", True)),
                include_partial_messages=bool(cfg.get("include_partial_messages", True)),
                mcp_config=cfg.get("mcp_config"),
                timeout_sec=int(cfg.get("timeout_sec_per_turn", 300)),
                extra_args=list(cfg.get("extra_args") or []),
                append_system_prompt=bool(cfg.get("append_system_prompt", True)),
                provider=cfg.get("provider"),
                model=cfg.get("model"),
                thinking=cfg.get("thinking"),
                no_builtin_tools=cfg.get("no_builtin_tools", False),
                approve=cfg.get("approve", True),
            )
            trace = build_trace(result, harness=harness_name)
            # Stamp schema_version for Trace v1 without breaking older consumers
            if isinstance(trace, dict):
                trace.setdefault("schema_version", "1.0")
                raw_stream = case_dir / "stream.jsonl"
                if raw_stream.is_file():
                    trace.setdefault("artifacts", {})["raw_stream"] = "stream.jsonl"
            trace_path = case_dir / "trace.json"
            write_trace(trace, trace_path)
            with (case_dir / "events.jsonl").open("w", encoding="utf-8") as f:
                for ev in trace.get("events") or []:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            
            rel = str(trace_path.relative_to(run_dir))
            case_rows.append(
                case_row_from_trace(
                    trace,
                    class_letter=case.class_letter,
                    n_turns=case.n_turns,
                    trace_relpath=rel,
                )
            )
            turn_rows.extend(turn_rows_from_trace(trace))
            tool_rows.extend(extract_tool_rows(trace.get("events") or [], case.case_id))
            case_t1 = datetime.now(TZ)
            case_windows.append((case.case_id, case_t0, case_t1))
            gw_log = Path(str(cfg.get("llm_gateway_log") or "")).expanduser() if cfg.get("llm_gateway_log") else None
            if gw_log is None or not str(gw_log):
                gw_log = Path("/Users/xiaozijian/WorkSpace/package/mock_system/llm_gateway/logs/llm_calls.jsonl")
            capture_complete = False
            try:
                import time as _time

                deadline = _time.monotonic() + 30.0
                while True:
                    capture_end = datetime.now(TZ)
                    pairs = filter_pairs_for_case(
                        load_gateway_pairs(gw_log),
                        case_id=case.case_id,
                        start=case_t0,
                        end=capture_end,
                    )
                    capture_complete = bool(pairs) and all(
                        pair.get("request") is not None
                        and (
                            pair.get("status_code") is not None
                            or pair.get("error") is not None
                        )
                        for pair in pairs
                    )
                    if capture_complete or _time.monotonic() >= deadline:
                        break
                    _time.sleep(0.25)
                calls = pairs_to_trace_calls(pairs, case_id=case.case_id)
                if calls:
                    write_case_llm_jsonl(case_dir / "llm_calls.jsonl", calls)
                    trace = attach_llm_calls_to_trace(trace, calls, embed=False)
                    write_trace(trace, trace_path)
                    llm_rows.extend(pairs_to_excel_rows(pairs, case_id=case.case_id))
                    print(f"[case] {case.case_id} llm_calls={len(calls)} from {gw_log.name}", flush=True)
                else:
                    print(f"[case] {case.case_id} llm_calls=0 (gateway log empty for window)", flush=True)
            except Exception as e:
                print(f"[case] {case.case_id} llm_calls ingest failed: {e!r}", flush=True)
            if result.success and not capture_complete:
                trace["success"] = False
                trace["error"] = "llm_trace_missing_or_incomplete"
                write_trace(trace, trace_path)
            effective_success = bool(result.success and capture_complete)
            status = "OK" if effective_success else f"FAIL exit={result.exit_code}"
            print(f"[case] {case.case_id} {status} wall_ms={result.wall_ms} err={result.error!r}", flush=True)
            _append_progress(
                progress_path,
                {
                    "ts": datetime.now(TZ).isoformat(timespec="seconds"),
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "action": "rerun" if args.resume else "run",
                    "success": effective_success,
                    "exit_code": result.exit_code,
                    "error": result.error,
                    "error_class": _classify_error(result.error),
                    "wall_ms": result.wall_ms,
                },
            )
        finally:
            _set_current_case_id(None)

    # Final Excel is always rebuilt from per-case process artifacts so resume
    # and mid-run interruption never leave a partial in-memory-only sheet.
    run_meta = {
        "schema_version": "1.0",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "claude_version": version,
        "bundle_path": str(bundle_path),
        "profile_id": cfg.get("profile_id"),
        "profile_path": cfg.get("_profile_path"),
        "project_cwd": str(project_cwd),
        "harness": cfg.get("harness", DEFAULT_HARNESS),
        "workspace_isolation": cfg.get("workspace_isolation", "none"),
        "notes": cfg.get("notes", ""),
        "n_cases": len(selected),
        "timezone": "Asia/Shanghai",
    }
    run_meta["mode"] = "resume" if args.resume else "run"
    run_meta["n_skipped_success"] = n_skipped
    run_meta["n_rerun"] = n_rerun
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    excel_cases = _cases_for_excel_rebuild(selected, bundle_cases, cases_root)
    run_meta["n_cases"] = len(excel_cases)
    n_ok, n_total = _rebuild_excel_from_cases(
        selected=excel_cases,
        cases_root=cases_root,
        run_dir=run_dir,
        xlsx_path=xlsx_path,
        run_meta=run_meta,
    )
    run_meta["n_cases"] = n_total
    run_meta["n_ok"] = n_ok
    (run_dir / "run_meta.json").write_text(
        json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[done] results={xlsx_path}")
    print(f"[done] run_dir={run_dir}")
    print(f"[done] success={n_ok}/{len(excel_cases)} (excel_rows={n_total} skipped={n_skipped} rerun={n_rerun} ran={len(selected)})")

    if args.pack_zip:
        try:
            z = pack_run_zip(run_dir)
            print(f"[done] share_zip={z}")
        except Exception as e:
            print(f"[warn] pack_zip failed: {e!r}")

    return 0 if n_ok == n_total and n_total == len(excel_cases) else 2


if __name__ == "__main__":
    raise SystemExit(main())

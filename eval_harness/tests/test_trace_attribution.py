from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "llm_gateway"))
sys.path.insert(0, str(ROOT / "eval_harness" / "src"))

from callbacks import trace_callback
from callbacks.attribution import load_config, resolve
from eval_harness.attribution_lane import LaneLease, lane_config, recover_blocked_lane
from eval_harness.llm_gateway_ingest import (
    attach_llm_calls_to_trace, filter_pairs_by_execution_id, load_gateway_pairs,
    read_trace_llm_calls, write_case_llm_jsonl,
)
from eval_harness.llm_trace_html import build_payload
from eval_harness.adapters import insurance as insurance_qa_adapter


@pytest.fixture
def lane(tmp_path: Path, monkeypatch):
    config = tmp_path / "attribution_lanes.json"
    config.write_text(json.dumps({"registry_dir": "run/attribution", "lanes": {
        "insurance_1": {"chat_url": "http://localhost:18062/v1/chat", "gateway_key_env": "TEST_LANE_KEY"}
    }}), encoding="utf-8")
    monkeypatch.setenv("TEST_LANE_KEY", "sk-test-lane")
    monkeypatch.setenv("LLM_ATTRIBUTION_CONFIG", str(config))
    directory, _ = lane_config(config, "insurance_1", "http://localhost:18062/v1/chat")
    return directory


def _key_kwargs(value: str) -> dict:
    return {"litellm_call_id": "call-one", "metadata": {"user_api_key_hash": value},
            "litellm_params": {}, "optional_params": {}}


def test_master_and_virtual_key_normalization(lane: Path):
    with LaneLease(lane, "insurance_1") as lease:
        lease.start(run_id="run-1", case_id="INS_A01", execution_id="execution-1")
        expected = {"case_id": "INS_A01", "execution_id": "execution-1",
                    "attribution_status": "attributed"}
        for value in ("sk-test-lane", hashlib.sha256(b"sk-test-lane").hexdigest()):
            actual = resolve(_key_kwargs(value), load_config())
            assert all(actual[k] == v for k, v in expected.items())
        assert resolve(_key_kwargs("other-key"), load_config())["attribution_status"] == "unmapped"
        lease.finish()
    assert resolve(_key_kwargs("sk-test-lane"), load_config())["attribution_status"] == "idle"


def test_snapshot_survives_registry_change_and_explicit_tag(lane: Path, tmp_path: Path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(trace_callback, "LOG_FILE", log)
    callback = trace_callback.EvalTraceLogger()
    with LaneLease(lane, "insurance_1") as lease:
        lease.start(run_id="run-1", case_id="INS_A01", execution_id="execution-1")
        kwargs = _key_kwargs("sk-test-lane")
        callback.log_pre_api_call("model", [{"role": "user", "content": "hello"}], kwargs)
        lease.finish()
        lease.start(run_id="run-1", case_id="INS_A02", execution_id="execution-2")
        asyncio.run(callback.async_log_success_event(kwargs, {"choices": []}, None, None))

        explicit = _key_kwargs("sk-test-lane")
        explicit["litellm_call_id"] = "call-explicit"
        explicit["optional_params"] = {"eval_case_id": "CLAUDE_X"}
        callback.log_pre_api_call("model", [], explicit)
        asyncio.run(callback.async_log_success_event(explicit, {"choices": []}, None, None))
        lease.finish()

    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [r["execution_id"] for r in records[:2]] == ["execution-1", "execution-1"]
    assert records[2]["case_id"] == "CLAUDE_X"
    assert "execution_id" not in records[2]
    pairs = load_gateway_pairs(log)
    matched = filter_pairs_by_execution_id(pairs, execution_id="execution-1", case_id="INS_A01")
    assert len(matched) == 1 and matched[0]["response"]["response"] == {"choices": []}
    assert filter_pairs_by_execution_id(pairs, execution_id="execution-2", case_id="INS_A02") == []
    offset = log.read_bytes().index(b'\n') + 1
    assert load_gateway_pairs(log, start_offset=offset)


def test_zero_llm_case_shows_only_runner_artifacts(tmp_path: Path):
    case_dir = tmp_path / "cases" / "INS_A01"
    case_dir.mkdir(parents=True)
    (case_dir / "meta.json").write_text(json.dumps({
        "case_id": "INS_A01", "prompt": "输入", "answer": "输出", "success": True,
        "wall_ms": 10, "attribution_status": "unknown_no_calls"
    }), encoding="utf-8")
    payload = build_payload(tmp_path)
    assert len(payload["cases"]) == 1
    case = payload["cases"][0]
    assert case["n_calls"] == 0 and case["calls"] == []
    assert case["overview"]["runner_only"] is True
    assert case["overview"]["turns"] == [{"index": 1, "prompt": "输入", "final_text": "输出"}]
    assert "未观测到 LLM 调用" in case["overview"]["note"]


def test_insurance_llm_overview_uses_business_answer(tmp_path: Path):
    case_dir = tmp_path / "cases" / "INS_A01"
    case_dir.mkdir(parents=True)
    (case_dir / "meta.json").write_text(json.dumps({
        "case_id": "INS_A01", "prompt": "业务问题", "answer": "业务接口最终答案",
        "success": True, "wall_ms": 42, "attribution_status": "attributed",
    }), encoding="utf-8")
    (case_dir / "llm_calls.jsonl").write_text(json.dumps({
        "seq": 1, "call_id": "call-one", "case_id": "INS_A01",
        "request": {"messages": [{"role": "user", "content": "业务问题"}]},
        "response": {"response": {"choices": [{"message": {
            "role": "assistant", "content": "模型草稿，非业务最终答案",
        }}]}},
    }) + "\n", encoding="utf-8")

    case = build_payload(tmp_path)["cases"][0]
    assert case["n_calls"] == 1
    assert case["overview"]["turns"] == [{
        "index": 1, "prompt": "业务问题", "final_text": "业务接口最终答案",
    }]
    assert case["overview"]["success"] is True
    assert case["overview"]["metrics"]["wall_ms"] == 42
    assert case["overview"]["artifacts"]["business_response"] == "response.json"
    assert case["calls"][0]["assistant"] == "模型草稿，非业务最终答案"


def test_lane_rejects_concurrent_writer_and_requires_recovery(lane: Path):
    with LaneLease(lane, "insurance_1") as lease:
        lease.start(run_id="run-1", case_id="INS_A01", execution_id="execution-1")
        with pytest.raises(BlockingIOError):
            with LaneLease(lane, "insurance_1"):
                pass
        lease.finish(blocked=True, reason="request_failed")
    with pytest.raises(RuntimeError, match="requires recovery"):
        with LaneLease(lane, "insurance_1"):
            pass
    with pytest.raises(RuntimeError, match="execution_id"):
        recover_blocked_lane(lane, "insurance_1", "other-execution")
    recover_blocked_lane(lane, "insurance_1", "execution-1")
    with LaneLease(lane, "insurance_1"):
        pass


def test_live_batch_exact_ingest_and_zero_call_overview(lane: Path, tmp_path: Path, monkeypatch):
    config = tmp_path / "attribution_lanes.json"
    marker = lane / "gateway_ready.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": os.getpid(), "config_path": str(config),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()}), encoding="utf-8")
    gateway_log = tmp_path / "gateway.jsonl"
    gateway_log.touch()
    monkeypatch.setattr(trace_callback, "LOG_FILE", gateway_log)
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("\n".join(json.dumps({"schema_version": "1.0", "case_id": cid,
        "turns": ([f"question {cid}"] if cid == "A01" else
                  ["question A02", "follow up A02"])})
        for cid in ("A01", "A02")) + "\n", encoding="utf-8")

    posted: list[dict] = []

    def fake_post(*, prompt: str, session_id: str, messages=None, **_kwargs):
        posted.append({"prompt": prompt, "session_id": session_id, "messages": messages})
        if prompt.endswith("A01"):
            callback = trace_callback.EvalTraceLogger()
            kwargs = _key_kwargs("sk-test-lane")
            callback.log_pre_api_call("model", [{"role": "user", "content": prompt}], kwargs)
            asyncio.run(callback.async_log_success_event(kwargs, {"choices": [{"message": {"content": "model reply"}}]}, None, None))
            foreign = _key_kwargs("sk-test-lane")
            foreign["litellm_call_id"] = "foreign"
            foreign["optional_params"] = {"eval_case_id": "CLAUDE_X"}
            callback.log_pre_api_call("model", [], foreign)
            asyncio.run(callback.async_log_success_event(foreign, {"choices": []}, None, None))
        return 200, {"answer": "business answer " + prompt}, None

    monkeypatch.setattr(insurance_qa_adapter, "_post_live_chat", fake_post)
    result = insurance_qa_adapter.run_live_batch(bundle_case_ids=["A01", "A02"], run_id="test-run",
        chat_url="http://localhost:18062/v1/chat", bundle_path=bundle,
        eval_runs_dir=tmp_path / "eval_runs", gateway_log=gateway_log, attribution_config=config)
    assert result["n_ok"] == 2 and Path(result["html_path"]).is_file()
    run_dir = Path(result["run_dir"])
    a = json.loads((run_dir / "cases" / "INS_A01" / "meta.json").read_text())
    b = json.loads((run_dir / "cases" / "INS_A02" / "meta.json").read_text())
    assert a["execution_id"] != b["execution_id"]
    assert a["attribution_status"] == "captured" and b["attribution_status"] == "unknown_no_calls"
    assert b["num_turns"] == 2 and len(b["turns"]) == 2
    b_posts = [row for row in posted if "A02" in row["prompt"]]
    assert len({row["session_id"] for row in b_posts}) == 1
    assert b_posts[1]["messages"] == [
        {"role": "user", "content": "question A02"},
        {"role": "assistant", "content": "business answer question A02"},
        {"role": "user", "content": "follow up A02"},
    ]
    payload = build_payload(run_dir)
    assert [case["n_calls"] for case in payload["cases"]] == [1, 0]
    assert payload["cases"][0]["calls"][0]["execution_id"] == a["execution_id"]
    assert [turn["prompt"] for turn in payload["cases"][1]["overview"]["turns"]] == [
        "question A02", "follow up A02",
    ]

    # A later subset resume must preserve the cumulative two-case summary.
    (run_dir / "llm_trace.html").unlink()
    (run_dir / "summary.json").unlink()
    resumed = insurance_qa_adapter.run_live_batch(
        bundle_case_ids=["A01"], run_id="test-run",
        chat_url="http://localhost:18062/v1/chat", bundle_path=bundle,
        eval_runs_dir=tmp_path / "eval_runs", gateway_log=gateway_log,
        attribution_config=config, resume=True,
    )
    assert resumed["n_cases"] == 2 and resumed["n_ok"] == 2
    assert len(json.loads((run_dir / "summary.json").read_text())) == 2


def test_trace_reference_preserves_frontend_payload(tmp_path: Path):
    call = {"seq": 1, "call_id": "one", "case_id": "A01", "protocol": "chat_completions",
            "request": {"messages": [{"role": "user", "content": "hello"}]},
            "response": {"response": {"choices": [{"message": {"content": "world"}}]}}}
    base = {"case_id": "A01", "success": True, "metrics": {"wall_ms": 3},
            "turns": [{"index": 1, "prompt": "hello", "final_text": "world"}]}
    for name, embed in (("legacy", True), ("reference", False)):
        case_dir = tmp_path / name / "cases" / "A01"
        case_dir.mkdir(parents=True)
        write_case_llm_jsonl(case_dir / "llm_calls.jsonl", [call])
        trace = attach_llm_calls_to_trace(base, [call], embed=embed)
        (case_dir / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
        assert read_trace_llm_calls(trace, case_dir) == [call]
    assert build_payload(tmp_path / "legacy")["cases"] == build_payload(tmp_path / "reference")["cases"]
    ref_dir = tmp_path / "reference" / "cases" / "A01"
    (ref_dir / "llm_calls.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        read_trace_llm_calls(json.loads((ref_dir / "trace.json").read_text()), ref_dir)


def test_new_ingest_rejects_corrupted_tail_legacy_tolerates_it(tmp_path: Path):
    log = tmp_path / "calls.jsonl"
    log.write_text('{"event":"pre_api_call","call_id":"ok"}\n{broken\n', encoding="utf-8")
    assert len(load_gateway_pairs(log)) == 1
    with pytest.raises(ValueError, match="malformed gateway log"):
        load_gateway_pairs(log, strict=True)

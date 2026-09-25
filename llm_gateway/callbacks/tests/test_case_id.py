from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from callbacks.case_id import detect_protocol, extract_case_id, strip_eval_case_id_from_body


def test_priority_header_over_body():
    cid, src, warns = extract_case_id(
        headers={"X-Eval-Case-Id": "HDR"},
        body={"eval_case_id": "BODY", "messages": []},
    )
    assert cid == "HDR" and src == "header"
    assert warns and "conflict" in warns[0]


def test_body_field_chat():
    cid, src, warns = extract_case_id(body={"eval_case_id": "PROBE_CC", "messages": [{"role": "user", "content": "x"}]})
    assert cid == "PROBE_CC" and src == "body" and not warns


def test_optional_params():
    cid, src, _ = extract_case_id(optional_params={"eval_case_id": "PROBE_CC"})
    assert cid == "PROBE_CC" and src == "optional_params"


def test_responses_metadata():
    cid, src, _ = extract_case_id(body={"input": "ping", "metadata": {"eval_case_id": "PROBE_RESP"}})
    assert cid == "PROBE_RESP" and src == "body"


def test_text_marker_fallback():
    cid, src, _ = extract_case_id(messages=[{"role": "system", "content": "<!--eval_case_id:A01--> hi"}])
    assert cid == "A01" and src == "text"


def test_no_file_fallback_returns_none():
    cid, src, _ = extract_case_id(body={"messages": [{"role": "user", "content": "ping"}]})
    assert cid is None and src is None


def test_strip_body():
    out = strip_eval_case_id_from_body({"model": "m", "eval_case_id": "X", "metadata": {"eval_case_id": "X", "k": 1}})
    assert "eval_case_id" not in out
    assert out["metadata"] == {"k": 1}


def test_detect_protocol():
    assert detect_protocol(path="/v1/chat/completions") == "chat_completions"
    assert detect_protocol(path="/v1/responses") == "responses"
    assert detect_protocol(path="/v1/messages") == "anthropic_messages"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL_PASS")

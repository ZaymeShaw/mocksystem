"""Helpers to stamp eval_case_id onto traffic headed at the local relay.

Analysis does not require a harness Trace adapter. Any client that speaks
Chat Completions / Responses / Anthropic Messages through the relay and
carries eval_case_id (header preferred) can be sliced into wire-only HTML.

Priority at the gateway (see llm_gateway/callbacks/case_id.py):
  1. Header X-Eval-Case-Id
  2. Body / optional_params eval_case_id (+ Responses metadata)
  3. Text <!--eval_case_id:…-->
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping, MutableMapping

CASE_ID_HEADER = "X-Eval-Case-Id"
CASE_ID_BODY_FIELD = "eval_case_id"
CASE_ID_TEXT_MARKER = "<!--eval_case_id:{case_id}-->"


def text_marker(case_id: str) -> str:
    return CASE_ID_TEXT_MARKER.format(case_id=case_id)


def openai_compatible_kwargs(case_id: str) -> dict[str, Any]:
    """Kwargs for OpenAI / Agno OpenAIChat (Chat Completions path).

    Example (insurance_qa_agent model_router):
        OpenAIChat(..., **openai_compatible_kwargs("bs-001"))
        # or mutate: model.extra_headers = ...; model.extra_body = ...
    """
    return {
        "extra_headers": {CASE_ID_HEADER: case_id},
        "extra_body": {CASE_ID_BODY_FIELD: case_id},
    }


def apply_openai_compatible_case_id(model: Any, case_id: str) -> Any:
    """Mutate an Agno/OpenAIChat-like instance in place for the current case."""
    headers = dict(getattr(model, "extra_headers", None) or {})
    headers[CASE_ID_HEADER] = case_id
    model.extra_headers = headers

    body = dict(getattr(model, "extra_body", None) or {})
    body[CASE_ID_BODY_FIELD] = case_id
    model.extra_body = body

    # default_headers is also honored by the OpenAI SDK client constructor
    defaults = dict(getattr(model, "default_headers", None) or {})
    defaults[CASE_ID_HEADER] = case_id
    model.default_headers = defaults
    return model


def anthropic_cli_env(
    case_id: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Env for Claude Code CLI (and similar Anthropic clients using these vars)."""
    env: dict[str, str] = dict(base_env if base_env is not None else os.environ)

    header_line = f"{CASE_ID_HEADER}: {case_id}"
    existing = env.get("ANTHROPIC_CUSTOM_HEADERS", "")
    lines = [ln for ln in existing.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith(CASE_ID_HEADER.lower() + ":")]
    lines.append(header_line)
    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(lines)

    extra: dict[str, Any] = {}
    raw_extra = env.get("CLAUDE_CODE_EXTRA_BODY", "").strip()
    if raw_extra:
        try:
            parsed = json.loads(raw_extra)
            if isinstance(parsed, dict):
                extra = dict(parsed)
        except json.JSONDecodeError:
            extra = {}
    extra[CASE_ID_BODY_FIELD] = case_id
    env["CLAUDE_CODE_EXTRA_BODY"] = json.dumps(extra, ensure_ascii=False)
    return env


def merge_case_id_into_body(body: MutableMapping[str, Any], case_id: str) -> MutableMapping[str, Any]:
    """Stamp structured eval_case_id onto a request body dict (any of the 3 protocols)."""
    body[CASE_ID_BODY_FIELD] = case_id
    meta = body.get("metadata")
    if isinstance(meta, dict):
        meta = dict(meta)
        meta[CASE_ID_BODY_FIELD] = case_id
        body["metadata"] = meta
    return body

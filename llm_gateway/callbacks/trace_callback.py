"""LiteLLM custom logger: persist full request/response for eval traces."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from callbacks.case_id import (
    detect_protocol,
    extract_case_id,
    proxy_request_bits,
)
from callbacks.attribution import load_config as load_lane_config, mark_gateway_ready, resolve as resolve_lane_attribution

try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover
    class CustomLogger:  # type: ignore
        pass


LOG_DIR = Path(os.environ.get("LLM_GATEWAY_LOG_DIR", str(Path(__file__).resolve().parents[1] / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "llm_calls.jsonl"
_LANE_CONFIG = (
    load_lane_config()
    if os.environ.get("LLM_ATTRIBUTION_CONFIG") and os.environ.get("LLM_ATTRIBUTION_GATEWAY_PROCESS") == "1"
    else None
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(obj: Any, *, _depth: int = 0) -> Any:
    """Convert LiteLLM/pydantic objects to JSON-friendly structures (no Python repr)."""
    if _depth > 40:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return repr(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, _depth=_depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v, _depth=_depth + 1) for v in obj]
    # pydantic v2 / v1
    for meth in ("model_dump", "dict"):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                dumped = fn(mode="json") if meth == "model_dump" else fn()
                return _jsonable(dumped, _depth=_depth + 1)
            except TypeError:
                try:
                    return _jsonable(fn(), _depth=_depth + 1)
                except Exception:
                    pass
            except Exception:
                pass
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        try:
            data = {
                k: v
                for k, v in vars(obj).items()
                if not k.startswith("_") and not callable(v)
            }
            if data:
                return _jsonable(data, _depth=_depth + 1)
        except Exception:
            pass
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except Exception:
        return str(obj)


def _safe(obj: Any) -> Any:
    return _jsonable(obj)


def _append(record: dict) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=lambda o: _jsonable(o)) + "\n")


def _resolve_case_and_protocol(messages: Any, kwargs: dict) -> tuple[str | None, str | None, str, list[str]]:
    litellm_params = kwargs.get("litellm_params") or {}
    headers, body, path = proxy_request_bits(litellm_params)
    optional_params = kwargs.get("optional_params")
    case_id, source, warnings = extract_case_id(
        headers=headers,
        body=body,
        optional_params=optional_params,
        messages=messages,
    )
    protocol = detect_protocol(path=path, body=body if isinstance(body, dict) else None, messages=messages)
    return case_id, source, protocol, warnings


def _attribution_snapshot(messages: Any, kwargs: dict) -> tuple[dict[str, Any], str, list[str]]:
    case_id, source, protocol, warnings = _resolve_case_and_protocol(messages, kwargs)
    if case_id:
        return {"case_id": case_id, "case_id_source": source,
                "attribution_status": "explicit"}, protocol, warnings
    if not os.environ.get("LLM_ATTRIBUTION_CONFIG"):
        return {"case_id": None, "attribution_status": "unmapped"}, protocol, warnings
    try:
        lane = resolve_lane_attribution(kwargs, config=_LANE_CONFIG)
    except (OSError, ValueError, TypeError, KeyError):
        lane = {"attribution_status": "config_unavailable"}
    return lane, protocol, warnings


class EvalTraceLogger(CustomLogger):
    def log_pre_api_call(self, model, messages, kwargs):  # sync hook
        call_id = kwargs.get("litellm_call_id") or str(uuid.uuid4())
        kwargs["_eval_trace_call_id"] = call_id
        kwargs["_eval_trace_t0"] = time.time()
        optional_params = kwargs.get("optional_params")
        attribution, protocol, warnings = _attribution_snapshot(messages, kwargs)
        # The first callback freezes attribution, including an orphan decision.
        kwargs["_eval_attribution"] = attribution
        if attribution.get("case_id"):
            kwargs["_eval_case_id"] = attribution["case_id"]
        kwargs["_eval_protocol"] = protocol
        rec = {
            "event": "pre_api_call",
            "ts": _now(),
            "call_id": call_id,
            **attribution,
            "protocol": protocol,
            "model": model,
            "messages": _safe(messages),
            "optional_params": _safe(optional_params),
            "tools": _safe(
                kwargs.get("tools")
                or (optional_params or {}).get("tools")
                if isinstance(optional_params, dict)
                else kwargs.get("tools")
            ),
            "litellm_params_keys": sorted((kwargs.get("litellm_params") or {}).keys()),
        }
        if warnings:
            rec["case_id_warnings"] = warnings
        _append(rec)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        call_id = kwargs.get("_eval_trace_call_id") or kwargs.get("litellm_call_id") or str(uuid.uuid4())
        t0 = kwargs.get("_eval_trace_t0")
        latency_ms = int((time.time() - t0) * 1000) if t0 else None
        messages = kwargs.get("messages")
        attribution = kwargs.get("_eval_attribution")
        if not isinstance(attribution, dict):
            # A missing pre-call snapshot cannot be reconstructed from a later
            # lane file: it may already belong to the next case.
            attribution = {"case_id": kwargs.get("_eval_case_id"),
                           "attribution_status": "missing_request_snapshot"}
        protocol = kwargs.get("_eval_protocol")
        warnings: list[str] = []
        if not protocol:
            _, _, protocol, warnings = _resolve_case_and_protocol(messages, kwargs)
        rec = {
            "event": "success",
            "ts": _now(),
            "call_id": call_id,
            **attribution,
            "protocol": protocol,
            "model": kwargs.get("model"),
            "messages": _safe(messages),
            "tools": _safe(kwargs.get("tools")),
            "response": _safe(response_obj),
            "latency_ms": latency_ms,
            "usage": _safe(
                getattr(response_obj, "usage", None)
                or (response_obj.get("usage") if isinstance(response_obj, dict) else None)
            ),
        }
        if warnings:
            rec["case_id_warnings"] = warnings
        _append(rec)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        call_id = kwargs.get("_eval_trace_call_id") or kwargs.get("litellm_call_id") or str(uuid.uuid4())
        messages = kwargs.get("messages")
        attribution = kwargs.get("_eval_attribution")
        if not isinstance(attribution, dict):
            attribution = {"case_id": kwargs.get("_eval_case_id"),
                           "attribution_status": "missing_request_snapshot"}
        protocol = kwargs.get("_eval_protocol")
        warnings: list[str] = []
        if not protocol:
            _, _, protocol, warnings = _resolve_case_and_protocol(messages, kwargs)
        rec = {
            "event": "failure",
            "ts": _now(),
            "call_id": call_id,
            **attribution,
            "protocol": protocol,
            "model": kwargs.get("model"),
            "messages": _safe(messages),
            "error": _safe(response_obj),
        }
        if warnings:
            rec["case_id_warnings"] = warnings
        _append(rec)


if os.environ.get("LLM_ATTRIBUTION_CONFIG") and os.environ.get("LLM_ATTRIBUTION_GATEWAY_PROCESS") == "1":
    mark_gateway_ready()

proxy_handler_instance = EvalTraceLogger()

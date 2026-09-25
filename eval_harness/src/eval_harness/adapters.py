"""Thin harness → adapter registry. Default remains Claude."""
from __future__ import annotations

from typing import Any, Callable

from eval_harness.claude_adapter import CaseRunResult, run_case as claude_run_case

DEFAULT_HARNESS = "claude_code"

_CLAUDE_KEYS = {
    "case_id",
    "turns",
    "case_dir",
    "project_cwd",
    "claude_bin",
    "agent_md",
    "permission_mode",
    "dangerously_skip_permissions",
    "output_format",
    "verbose",
    "include_partial_messages",
    "mcp_config",
    "timeout_sec",
    "extra_args",
}


def _claude_run_case(**kwargs: Any) -> CaseRunResult:
    filtered = {k: kwargs[k] for k in _CLAUDE_KEYS if k in kwargs}
    return claude_run_case(**filtered)


def _pi_run_case(**kwargs: Any) -> CaseRunResult:
    from eval_harness.pi_adapter import run_case as pi_run_case

    return pi_run_case(
        case_id=kwargs["case_id"],
        turns=kwargs["turns"],
        case_dir=kwargs["case_dir"],
        project_cwd=kwargs["project_cwd"],
        claude_bin=kwargs.get("claude_bin"),
        agent_md=kwargs.get("agent_md", "agent.md"),
        timeout_sec=int(kwargs.get("timeout_sec") or 300),
        extra_args=list(kwargs.get("extra_args") or []),
        append_system_prompt=bool(kwargs.get("append_system_prompt", True)),
        provider=str(kwargs.get("provider") or "local-relay"),
        model=str(kwargs.get("model") or "deepseek-v4-flash"),
        thinking=str(kwargs.get("thinking") or "off"),
        no_builtin_tools=bool(kwargs.get("no_builtin_tools", False)),
        approve=bool(kwargs.get("approve", True)),
    )


def _insurance_run_case(**_kwargs: Any) -> CaseRunResult:
    raise SystemExit(
        "harness=insurance_qa_agno is not run via eval_harness.run batch; "
        "use eval_harness.dual_run or python -m eval_harness.insurance_qa_adapter --live-batch"
    )


REGISTRY: dict[str, Callable[..., CaseRunResult]] = {
    "claude_code": _claude_run_case,
    "pi_coding": _pi_run_case,
    "insurance_qa_agno": _insurance_run_case,
}


def resolve_harness(name: str | None) -> str:
    h = (name or DEFAULT_HARNESS).strip() or DEFAULT_HARNESS
    if h not in REGISTRY:
        raise SystemExit(f"Unknown harness={h!r}; known={sorted(REGISTRY)}")
    return h


def dispatch_run_case(harness: str | None, **kwargs: Any) -> CaseRunResult:
    h = resolve_harness(harness)
    return REGISTRY[h](**kwargs)

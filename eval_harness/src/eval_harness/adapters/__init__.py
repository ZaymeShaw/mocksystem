"""Harness adapters and their shared dispatch interface."""
from .base import CaseRunResult, TurnResult
from .registry import (
    DEFAULT_HARNESS, REGISTRY, Adapter, dispatch_run_batch, dispatch_run_case,
    get_adapter, register_adapter, resolve_harness,
)

__all__ = [
    "Adapter", "CaseRunResult", "TurnResult", "DEFAULT_HARNESS", "REGISTRY",
    "dispatch_run_batch", "dispatch_run_case", "get_adapter", "register_adapter",
    "resolve_harness",
]

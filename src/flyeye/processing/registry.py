"""Stage type registry.

Stages register themselves by decorator at import time. `stages/__init__.py`
imports every module in the package, so dropping a new file into
`processing/stages/` is all that is required to make a stage available to the
config file and the UI.
"""

from __future__ import annotations

from typing import TypeVar

from .base import Stage

STAGES: dict[str, type[Stage]] = {}

S = TypeVar("S", bound=type[Stage])


def register(type_name: str):
    def deco(cls: S) -> S:
        if type_name in STAGES:
            raise ValueError(f"stage type {type_name!r} already registered by {STAGES[type_name]}")
        cls.type_name = type_name  # type: ignore[misc]
        STAGES[type_name] = cls
        return cls

    return deco


def build_stage(type_name: str, name: str | None = None, **params) -> Stage:
    try:
        cls = STAGES[type_name]
    except KeyError:
        raise ValueError(
            f"unknown stage type {type_name!r}; known: {sorted(STAGES)}"
        ) from None
    return cls(name=name, **params)


def catalogue() -> dict[str, dict]:
    """Every registered stage type and its parameter schema.

    The UI uses this to offer an 'add stage' menu without knowing anything
    about the stages themselves.
    """
    return {
        type_name: {
            "type": type_name,
            "accepts": list(cls.accepts),
            "schema": cls.Params.model_json_schema(),
        }
        for type_name, cls in sorted(STAGES.items())
    }

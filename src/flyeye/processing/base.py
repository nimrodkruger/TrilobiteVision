"""Processing stage contract.

The single design decision that determines whether this project needs
rebuilding every time it grows: **a stage declares its parameters, it does not
hardcode them.**

Each stage carries a pydantic model describing its knobs -- name, type, range,
default, units. From that model the system gets, for free and forever:

  * validation of anything the web UI or a config file sends
  * an auto-generated control panel in the browser, no HTML to write per stage
  * a serialisable record of the exact settings that produced a saved image
  * a place to hang provenance for the calibration work later

Adding a stage is: subclass Stage, declare a Params model, register the class.
No route, no template, no UI change. That is the whole point.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from ..types import Frame

log = logging.getLogger(__name__)


class StageParams(BaseModel):
    """Base for every stage's parameter model.

    `enabled` is universal: every stage can be bypassed at runtime without
    being removed from the pipeline, which is how you A/B a processing step
    while looking at a live image.
    """

    enabled: bool = True

    model_config = {"validate_assignment": True, "extra": "forbid"}


class Stage(ABC):
    # Registry key, set by the @register decorator.
    type_name: ClassVar[str] = "stage"

    # Which frame spaces this stage can accept. Empty means "anything".
    accepts: ClassVar[tuple[str, ...]] = ()

    Params: ClassVar[type[StageParams]] = StageParams

    def __init__(self, name: str | None = None, **params: Any) -> None:
        self.name = name or self.type_name
        self.params = self.Params(**params)

    # -- the actual work ------------------------------------------------

    def __call__(self, frame: Frame) -> Frame:
        if not self.params.enabled:
            return frame
        if self.accepts and frame.space not in self.accepts:
            log.warning(
                "%s: skipping, accepts %s but got space=%s",
                self.name,
                self.accepts,
                frame.space,
            )
            return frame
        return self.apply(frame)

    @abstractmethod
    def apply(self, frame: Frame) -> Frame:
        """Transform the frame. Must not mutate frame.data in place."""

    def reset(self) -> None:  # noqa: B027 - optional hook, not every stage has state
        """Clear any internal state. Called when a source restarts."""

    # -- parameter plumbing ---------------------------------------------

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply a partial parameter update.

        Returns the full parameter set after the update. Raises ValidationError
        on bad input, which the web layer turns into a 422 rather than a
        crashed capture thread.
        """
        merged = self.params.model_dump()
        merged.update(values)
        try:
            self.params = self.Params(**merged)
        except ValidationError:
            log.warning("%s: rejected parameter update %s", self.name, values)
            raise
        return self.params.model_dump()

    def describe(self) -> dict[str, Any]:
        """Everything the UI needs to render controls for this stage."""
        return {
            "name": self.name,
            "type": self.type_name,
            "schema": self.Params.model_json_schema(),
            "values": self.params.model_dump(),
        }

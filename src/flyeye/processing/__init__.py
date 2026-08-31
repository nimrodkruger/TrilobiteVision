"""Processing layer. Importing this package registers every built-in stage."""

from . import stages  # noqa: F401  (import for side effect: stage registration)
from .base import Stage, StageParams  # noqa: F401
from .pipeline import Pipeline  # noqa: F401
from .registry import STAGES, build_stage, catalogue, register  # noqa: F401

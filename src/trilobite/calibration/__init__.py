"""Calibration: settings, readiness, and (later) detection and fitting.

See docs/calibration-spec.md for the model and docs/calibration-ui-spec.md for
the acquisition workflow this implements.
"""

from .settings import CalibrationSettings, DerivedOptics, readiness_report  # noqa: F401

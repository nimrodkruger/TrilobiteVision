"""Calibration: settings, readiness, live detection, and (later) fitting.

See docs/calibration-spec.md for the model and docs/calibration-ui-spec.md for
the acquisition workflow this implements.

`detect` is imported lazily by the app, not re-exported here: it needs OpenCV,
and the settings and readiness code must stay importable on a machine without
it so that `readiness_report` can be the thing that *tells you* cv2 is missing.
"""

from .settings import CalibrationSettings, DerivedOptics, readiness_report  # noqa: F401

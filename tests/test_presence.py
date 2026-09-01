"""The cheap detector: counting checkerboard corners without finding them.

The property that matters most is not accuracy, it is **discrimination and
cost**. A board must read far above an empty scene, and one pass must be small
enough that two cameras at 1 Hz are a rounding error rather than a core. The
detector it replaces was correct and needed 210% of a core; being right is not
sufficient.

The second property, easy to miss and the actual cause of the first crash: the
cost must not depend on the number of tiles. A grid at the wrong pitch yields
thousands of micro-images, and the old per-tile loop turned that into an
unbounded amount of work. This one does the same three convolutions either way.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from trilobite.cameras.offline import SyntheticSource
from trilobite.config import CameraConfig, StageConfig
from trilobite.optics.mla import MLAGeometry
from trilobite.processing.pipeline import Pipeline

pytest.importorskip("cv2")
from trilobite.calibration.presence import PresenceDetector  # noqa: E402

FULL = (1456, 1088)
PREVIEW = (728, 544)
PITCH = 100.0
BOARD = (4, 3)


def source(pattern="plenoptic_board", rotation=0.0):
    s = SyntheticSource(CameraConfig(
        cam_id="left", backend="synthetic",
        full_resolution=FULL, preview_resolution=PREVIEW,
        synthetic_pattern=pattern, synthetic_pitch_px=PITCH,
        synthetic_rotation_deg=rotation, synthetic_board=BOARD,
        # No drift: these tests compare against a geometry pinned to the frame
        # centre, so the rendered array has to sit exactly there.
        synthetic_drift_px=0.0,
    ))
    s.open()
    return s


def preview_geometry(rotation=0.0):
    return MLAGeometry(width=PREVIEW[0], height=PREVIEW[1],
                       pitch=PITCH * PREVIEW[0] / FULL[0], rotation_deg=rotation)


def counts_on(img, rotation=0.0):
    det = PresenceDetector()
    return det.run(img, preview_geometry(rotation))


# -- discrimination ---------------------------------------------------------


def test_a_board_reads_far_above_an_empty_scene():
    """The negative case here is deliberately unfair. The `gratings` pattern
    carries a hard square grid, so it genuinely contains saddle corners -- a
    real empty scene (a wall, a bench) reads far lower. If the threshold
    separates these two it will separate anything."""
    board = counts_on(source().read_preview().data)
    empty = counts_on(source(pattern="gratings").read_preview().data)
    on_board = board.counts[board.whole]
    on_empty = empty.counts[empty.whole]
    assert np.median(on_board) > 25
    assert np.median(on_board) > 3 * max(np.median(on_empty), 1)


def test_the_default_threshold_separates_them():
    """A 4x3 board fills a micro-image with about 30 grid vertices. The default
    threshold of 20 is two thirds of that: high enough to reject a busy scene,
    low enough that a partly-covered micro-image still counts."""
    board = counts_on(source().read_preview().data)
    empty = counts_on(source(pattern="gratings").read_preview().data)
    assert len(board.seeing(20)) > 0.9 * board.n_whole
    assert len(empty.seeing(20)) < 0.1 * empty.n_whole


def test_a_lens_cap_reads_zero():
    flat = np.full((PREVIEW[1], PREVIEW[0]), 128, dtype=np.uint8)
    r = counts_on(flat)
    assert r.counts.max() == 0
    assert r.seeing(8) == []


def test_rotation_does_not_hurt_it():
    r = counts_on(source(rotation=3.0).read_preview().data, rotation=3.0)
    assert len(r.seeing(20)) > 0.85 * r.n_whole


def test_noise_does_not_hurt_it():
    rng = np.random.default_rng(0)
    img = source().read_preview().data.astype(np.float32)
    noisy = np.clip(img + rng.normal(0, 12, img.shape), 0, 255).astype(np.uint8)
    assert len(counts_on(noisy).seeing(20)) > 100


def test_defocus_raises_the_count_but_collapses_the_strength():
    """The trap worth having a test for. Blur spreads the saddle response and
    creates MORE local maxima, so the count alone cannot judge focus -- it
    reads a defocused board as a better one. The peak strength is what
    distinguishes them, and it comes from the same convolutions."""
    import cv2

    sharp = source().read_preview().data
    blurred = cv2.GaussianBlur(sharp, (9, 9), 0)
    a, b = counts_on(sharp), counts_on(blurred)
    assert np.median(b.counts[b.whole]) >= np.median(a.counts[a.whole])
    assert b.strength < 0.5 * a.strength


# -- the cost property ------------------------------------------------------


def test_cost_does_not_grow_with_the_number_of_tiles():
    """The first crash in one assertion. A pitch eight times too small yields
    dozens of times the micro-images; the old per-tile loop scaled with that,
    this does not."""
    import time

    img = source().read_preview().data
    det = PresenceDetector()

    def timed(pitch):
        g = MLAGeometry(width=PREVIEW[0], height=PREVIEW[1], pitch=pitch)
        det.run(img, g)                                   # build and cache labels
        t0 = time.perf_counter()
        for _ in range(5):
            det.run(img, g)
        return (time.perf_counter() - t0) / 5, det._whole.sum()

    coarse_ms, coarse_n = timed(50.0)
    fine_ms, fine_n = timed(6.0)
    assert fine_n > 20 * coarse_n, "the fine grid must really have far more tiles"
    assert fine_ms < 3 * coarse_ms, "cost must not scale with tile count"


# -- edges ------------------------------------------------------------------


def test_partial_micro_images_are_excluded_from_seeing():
    """A cell half off the sensor collects fewer peaks because it is smaller,
    not because it is short of pattern. Counting it would make the edge of the
    array look permanently bad."""
    r = counts_on(source().read_preview().data)
    assert r.whole.size > r.n_whole > 0
    assert all(r.whole[j - r.origin[1], i - r.origin[0]] for i, j in r.seeing(8))


def test_the_signature_ignores_partial_tiles():
    r = counts_on(source().read_preview().data)
    sig = r.signature()
    assert sig.shape == (r.counts.size,)
    assert not sig.reshape(r.counts.shape)[~r.whole].any()


# -- as a pipeline stage ----------------------------------------------------


def build_pipeline(pitch=50.0, min_corners=8):
    pipe = Pipeline.from_config([
        StageConfig(type="mla_grid_overlay", name="mla",
                    params={"enabled": True, "pitch_px": pitch}),
        StageConfig(type="checkerboard_presence", name="presence",
                    params={"enabled": True, "min_corners": min_corners}),
    ])
    pipe.stage("presence").bind_geometry(pipe.stage("mla"))
    return pipe


def test_the_stage_reports_into_frame_metadata():
    pipe = build_pipeline()
    src = source()
    for _ in range(3):
        frame = pipe(src.read_preview())
    assert frame.meta["presence_tiles_seeing"] > 100
    assert frame.meta["presence_strength"] > 1000
    assert frame.meta["presence_ms"] < 60


def test_the_stage_needs_no_camera_access_of_its_own():
    """It is a pipeline stage on frames that already flow. That is not a detail
    -- a second consumer of the camera is what took the rig down."""
    import trilobite.calibration.presence as mod

    text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "capture_request" not in text
    assert "threading" not in text


def test_the_stage_tints_the_micro_images_that_see_the_board():
    """Aiming a board while reading a numeric display does not work with both
    hands full, so the feedback goes on the image itself."""
    plain = build_pipeline(min_corners=8)
    plain.stage("presence").update({"tint": False})
    tinted = build_pipeline(min_corners=8)
    src = source()
    a = plain(src.read_preview())
    b = tinted(src.read_preview())
    assert b.data.mean() > a.data.mean()


def test_the_stage_is_inert_without_an_mla_stage():
    pipe = Pipeline.from_config([
        StageConfig(type="checkerboard_presence", name="presence", params={"enabled": True}),
    ])
    frame = pipe(source().read_preview())
    assert "presence_tiles_seeing" not in frame.meta
    assert pipe.stage("presence").result is None

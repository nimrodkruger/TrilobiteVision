"""The hands-free capture session.

Every test here is really a question about the bench, not about the code: the
operator has both hands on the board, cannot press a key, and is not looking at
the screen. So the loop has to be right about *when* to take a shot, has to
refuse to take fifty of the same one, has to say out loud what it did, and must
never lose a pose that was recorded.

The session is driven synchronously in most of these -- `_tick()` called by
hand -- rather than started as a thread. A state machine tested through its own
timing is a state machine tested slowly and flakily.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from trilobite.app import Application
from trilobite.calibration.settings import CalibrationSettings
from trilobite.config import AppConfig, CameraConfig, StageConfig, StorageConfig

pytest.importorskip("cv2")
from trilobite.calibration.session import CaptureSession, Phase, _square_px  # noqa: E402

BOARD = (4, 3)


def make_app(tmp_path, cameras=("left", "right"), pattern="plenoptic_board"):
    cfg = AppConfig(
        cameras=[
            CameraConfig(
                cam_id=cid, backend="synthetic", fps=1000,
                full_resolution=(1456, 1088), preview_resolution=(728, 544),
                synthetic_pattern=pattern, synthetic_pitch_px=100.0,
                synthetic_board=BOARD,
                # 50 preview px is 100 px on the sensor. The preview has to
                # resolve the board's squares or the presence map sees nothing:
                # at 1/8 scale a micro-image is 12 px across and its squares
                # are two, which is below anything a saddle detector can find.
                pipeline=[
                    StageConfig(type="mla_grid_overlay", name="mla",
                                params={"enabled": True, "pitch_px": 50.0}),
                    StageConfig(type="checkerboard_presence", name="presence",
                                params={"enabled": True, "min_corners": 8, "tint": False}),
                ],
            )
            for cid in cameras
        ],
        storage=StorageConfig(root=str(tmp_path / "data")),
    )
    return Application(cfg)


def settings(**capture):
    s = CalibrationSettings()
    s.board.cols, s.board.rows, s.board.square_mm = BOARD[0], BOARD[1], 7.0
    s.acceptance.min_cross_tiles = 5
    for k, v in capture.items():
        setattr(s.capture, k, v)
    return s


def make_session(tmp_path, app, **capture):
    return CaptureSession(app.cameras, settings(**capture), root=tmp_path / "out")


def warm(app, n=3):
    """Let each camera produce presence maps."""
    for cam in app.cameras.values():
        for _ in range(n):
            cam.pipeline(cam.source.read_preview())


# -- the state machine ------------------------------------------------------


def test_it_asks_for_the_board_when_it_cannot_see_one(tmp_path):
    app = make_app(tmp_path, pattern="gratings")
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._tick()
        assert s.phase is Phase.SEARCHING
        assert s.state()["title"] == "SHOW THE BOARD"
    finally:
        app.stop()


def test_it_waits_for_the_board_to_settle_before_shooting(tmp_path):
    """The board is visible from the first tick, but a shot must not be taken
    until the picture has stopped changing -- otherwise every capture is a
    motion-blurred one taken mid-sweep."""
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=4)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._tick()
        # The board is visible immediately, so it goes to HOLD at once -- but
        # with nothing to compare against yet, so the settle counter is zero.
        assert s.phase is Phase.HOLD
        assert "0/4" in s.state()["hint"]
        s._tick()
        assert s.phase is Phase.HOLD
        assert "1/4" in s.state()["hint"]
        assert s.kept == 0, "four still frames are required, not two"
    finally:
        app.stop()


def test_a_settled_board_is_captured_and_then_locked_out(tmp_path):
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=2, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        for _ in range(6):
            s._tick()
            if s.poses:
                break
        assert s.kept == 1, "a still board should produce exactly one pose"
        s._tick()
        # Review is instant here, so the next tick must find the movement gate.
        assert s.phase in (Phase.REVIEW, Phase.MOVE)
        for _ in range(4):
            s._tick()
        assert s.kept == 1, "holding still must not bank duplicates"
        assert s.phase is Phase.MOVE
    finally:
        app.stop()


def test_the_keyboard_override_shoots_through_every_gate(tmp_path):
    """Some of the most valuable poses -- extreme angles, dim corners -- never
    arm on their own. The override exists for those."""
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=99, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s._tick()
        assert s.kept == 0, "settle_frames=99 must never arm by itself"
        s.force()
        s._tick()
        assert s.kept == 1
        assert any(c["forced"] for c in [
            json.loads((s.dir / "pose_0001" / "left.json").read_text())])
    finally:
        app.stop()


def test_a_rejected_shot_says_which_camera_and_why(tmp_path):
    """A wrong board size finds nothing. The operator must be told which camera
    failed and how close it got, not just that something did not work."""
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0,
                         reject_cooldown_s=0.0)
        s.settings.board.cols, s.settings.board.rows = 9, 6
        s.detector.board = s.settings.board
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        for _ in range(4):
            s._tick()
        assert s.kept == 0
        rejected = [e for e in s.events if e.kind == "rejected"]
        assert rejected and "cross tiles" in rejected[-1].text
    finally:
        app.stop()


# -- what gets written ------------------------------------------------------


def test_a_pose_records_both_frames_their_corners_and_the_frozen_geometry(tmp_path):
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s.force()
        s._tick()
        assert s.kept == 1

        pose = s.dir / "pose_0001"
        for cid in ("left", "right"):
            assert np.load(pose / f"{cid}.npy").shape == (1088, 1456)
            side = json.loads((pose / f"{cid}.json").read_text())
            assert side["cam_id"] == cid
            assert len(side["cross"]) >= 5
            assert side["square_px"] > 0
            # Corners in FULL-RESOLUTION sensor coordinates, not tile-local.
            xy = np.array(next(iter(side["corners"].values())))
            assert xy.max() > 200

        manifest = json.loads((s.dir / "session.json").read_text())
        geom = manifest["mla_geometry_sensor_px"]["left"]
        assert geom["pitch_px"] == pytest.approx(100.0)
        assert geom["sensor"] == [1456, 1088]
        assert manifest["settings"]["board"]["cols"] == BOARD[0]
    finally:
        app.stop()


def test_the_index_lists_every_pose_kept_or_not(tmp_path):
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s.force()
        s._tick()
        assert s.discard_last() is True

        rows = [json.loads(x) for x in
                (s.dir / "poses.jsonl").read_text().splitlines() if x.strip()]
        assert len(rows) == 1
        assert rows[0]["discarded"] is True
        assert s.kept == 0
    finally:
        app.stop()


def test_discarding_leaves_the_files_alone(tmp_path):
    """A pose rejected by reflex and wanted back is worth more than the 3 MB it
    occupies, and an offline tool can always honour the flag."""
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s.force()
        s._tick()
        s.discard_last()
        assert (s.dir / "pose_0001" / "left.npy").exists()
    finally:
        app.stop()


def test_discarding_gives_the_coverage_back(tmp_path):
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s.force()
        s._tick()
        before = sum(s.coverage["left"].values())
        assert before >= 5
        s.discard_last()
        assert sum(s.coverage["left"].values()) == 0
    finally:
        app.stop()


def test_it_stops_rather_than_filling_the_disk(tmp_path):
    """The one failure that loses work already done."""
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = CaptureSession(app.cameras, settings(min_free_gb=1.0),
                           root=tmp_path / "out", storage_free_bytes=lambda: 5_000_000)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s.force()
        s._tick()
        assert s.kept == 0
        assert s.phase is Phase.STOPPED
        assert any("GB free" in e.text for e in s.events)
    finally:
        app.stop()


# -- what the operator hears ------------------------------------------------


def test_every_event_has_a_sequence_number_so_a_tone_plays_once(tmp_path):
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s.force()
        s._tick()
        seqs = [e["seq"] for e in s.state()["events"]]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        assert any(e["kind"] == "captured" for e in s.state()["events"])
    finally:
        app.stop()


def test_the_banner_says_something_useful_in_every_phase():
    from trilobite.calibration.session import BANNER

    for phase in Phase:
        title, _hint = BANNER[phase]
        assert title.isupper() and 0 < len(title) <= 20


# -- the depth proxy --------------------------------------------------------


def test_square_size_falls_as_the_board_gets_further_away():
    """kappa and D enter the model only through kappa/(Z - D) and are one
    parameter at a single depth, so a session at one distance fits perfectly
    and is wrong. This is the number that makes that visible on the bench."""
    near = np.array([[0.0, 0.0], [20.0, 0.0], [0.0, 20.0], [20.0, 20.0]])
    far = near * 0.5
    assert _square_px(near) == pytest.approx(20.0)
    assert _square_px(far) == pytest.approx(10.0)
    assert _square_px(np.empty((0, 2))) == 0.0


# -- lifecycle through the application --------------------------------------


def test_a_session_survives_and_reports_through_the_application(tmp_path):
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        app.calibration = settings(settle_frames=2, review_s=0.0, tick_hz=20)
        state = app.session_start()
        assert state["running"] is True
        assert app.session_running
        import time
        time.sleep(1.5)
        app.session_stop()
        assert not app.session_running
        assert Path(state["directory"]).exists()
    finally:
        app.stop()


def test_starting_is_refused_while_a_precondition_blocks(tmp_path):
    app = make_app(tmp_path)
    app.camera("left").pipeline.update_params("mla", {"enabled": False})
    app.start()
    try:
        warm(app)
        with pytest.raises(RuntimeError, match="MLA"):
            app.session_start()
    finally:
        app.stop()


# -- the noise dead band ----------------------------------------------------


def test_counting_noise_does_not_read_as_movement():
    """The bug this exists for: a saddle count fluctuates by one or two per
    micro-image from frame to frame, and across a hundred and thirty tiles that
    sums to about the size of a real stillness threshold. Without the dead band
    a motionless board reads as moving and the loop never arms -- observed on
    the synthetic rig as '0/4 still', indefinitely, with nothing moving."""
    from trilobite.calibration.session import CaptureSession as CS

    rng = np.random.default_rng(0)
    still = np.full(130, 29.0)
    jittered = still + rng.integers(-2, 3, size=130)
    assert CS._relative_change(still, jittered) < 0.02

    # A board that has actually moved off a third of the micro-images.
    moved = still.copy()
    moved[:43] = 0
    assert CS._relative_change(still, moved) > 0.25


def test_a_mismatched_map_reads_as_completely_changed():
    from trilobite.calibration.session import CaptureSession as CS

    assert CS._relative_change(None, np.zeros(4)) == 1.0
    assert CS._relative_change(np.zeros(4), np.zeros(9)) == 1.0


def test_the_depth_spread_counts_poses_not_cameras(tmp_path):
    """Both heads see the same board at the same distance. Counting both would
    double the apparent sample size of the one distribution this polices."""
    app = make_app(tmp_path)
    app.start()
    try:
        warm(app)
        s = make_session(tmp_path, app, settle_frames=1, review_s=0.0)
        s.dir = tmp_path / "out"
        s.dir.mkdir(parents=True, exist_ok=True)
        s._write_manifest()
        s.force()
        s._tick()
        assert s.kept == 1
        assert len(s.state()["depth_px"]) == 1
    finally:
        app.stop()

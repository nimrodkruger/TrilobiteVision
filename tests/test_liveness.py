"""Status that stops being true when the thing it describes stops.

Review finding F5. The failure class is not that something breaks — it is that
something breaks and everything keeps reporting success. Three specific ways
that was possible:

  * `RateMeter` computed from its last N samples with no reference to the
    present, so a camera that stopped an hour ago went on reporting the rate it
    had when it stopped.
  * `status.sensor_fps` returned the *configured* fps. It could not fall to
    zero because it was never a measurement.
  * `Pipeline.__call__` caught a stage exception, logged it, and passed the
    frame through. Nothing above that line could see it: not the frame, not
    `CameraRuntime.errors`, not the API.

Each test below drives the failure and asserts that the report changes. An
assertion that a healthy rig reports healthy is not enough — that is what
already worked.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from trilobite.app import CameraRuntime, RateMeter
from trilobite.config import CameraConfig, StageConfig
from trilobite.processing.base import Stage, StageParams
from trilobite.processing.pipeline import Pipeline
from trilobite.processing.registry import register
from trilobite.types import Frame

# -- the rate meter ---------------------------------------------------------


def test_a_rate_meter_reports_the_rate_while_it_is_being_ticked():
    m = RateMeter(stale_after=1.0)
    for _ in range(5):
        m.tick()
        time.sleep(0.02)
    assert 20 < m.fps < 200


def test_a_rate_meter_falls_to_zero_when_the_ticks_stop():
    """The bug, in one assertion. A stopped camera used to keep reporting the
    rate it had when it stopped, for the life of the process."""
    m = RateMeter(stale_after=0.15)
    for _ in range(5):
        m.tick()
        time.sleep(0.01)
    assert m.fps > 0
    time.sleep(0.25)
    assert m.fps == 0.0, "a meter that has not been ticked is not measuring anything"


def test_a_rate_meter_reports_age_and_distinguishes_never_from_zero():
    m = RateMeter()
    assert m.age is None, "None means never ticked; 0.0 would mean just now"
    m.tick()
    assert m.age is not None and m.age < 0.5


# -- stage failures ---------------------------------------------------------


class _Boom(StageParams):
    pass


@register("test_explode")
class Explode(Stage):
    """A stage that always throws. Registered because the pipeline builds by
    type name, and the point is to exercise the real catch."""

    Params = _Boom

    def apply(self, frame: Frame) -> Frame:
        raise RuntimeError("deliberate")


def _frame():
    return Frame.now(np.zeros((4, 4), dtype=np.uint8), "left", 1)


def test_a_throwing_stage_still_passes_the_frame_through():
    """Unchanged behaviour, asserted so the fix does not accidentally turn a
    degraded preview into a dead rig mid-experiment."""
    p = Pipeline.from_config([StageConfig(type="test_explode", name="boom")])
    out = p(_frame())
    assert out is not None and out.data.shape == (4, 4)


def test_a_throwing_stage_is_counted_and_named():
    p = Pipeline.from_config([StageConfig(type="test_explode", name="boom")])
    for _ in range(3):
        p(_frame())
    assert p.failure_count == 3
    assert p.failures["boom"]["count"] == 3
    assert "deliberate" in p.failures["boom"]["last_error"]


def test_the_frame_itself_records_that_a_stage_failed():
    """So a consumer holding only the frame -- a sink, a sidecar writer -- can
    tell it is not the frame that was asked for. Before this the frame came out
    the far end indistinguishable from a clean one."""
    p = Pipeline.from_config([StageConfig(type="test_explode", name="boom")])
    assert p(_frame()).meta["pipeline_failed_stages"] == ["boom"]


def test_a_healthy_pipeline_reports_no_failures_and_marks_nothing():
    p = Pipeline.from_config([StageConfig(type="stats", name="stats")])
    out = p(_frame())
    assert p.failures == {}
    assert p.failure_count == 0
    assert "pipeline_failed_stages" not in out.meta


def test_stage_failures_reach_the_camera_status():
    """The path that mattered: `CameraRuntime.errors` counts capture-loop
    exceptions and never saw these, because the pipeline swallowed them."""
    cam = CameraRuntime(CameraConfig(
        cam_id="left", backend="synthetic", full_resolution=(32, 24),
        preview_resolution=(32, 24), synthetic_drift_px=0.0,
        pipeline=[StageConfig(type="test_explode", name="boom")]), writer=None)
    cam.source.open()
    cam.preview.publish(cam.pipeline(cam.source.read_preview()))
    st = cam.status()
    assert st["stage_failures"]["boom"]["count"] == 1
    assert st["errors"] == 0, "a caught stage failure is not a capture-loop error"
    cam.source.close()


# -- measured versus configured rates ---------------------------------------


def _run_briefly(cam, seconds=1.0):
    cam.source.open()
    cam._stop.clear()
    t = threading.Thread(target=cam._run, daemon=True)
    t.start()
    time.sleep(seconds)
    return t


def test_the_sensor_rate_is_measured_not_the_configured_number():
    """`sensor_fps` used to return `cfg.fps` verbatim. Here the configured rate
    is a deliberate lie -- the synthetic source paces itself at 20 -- and the
    reported number must follow the source, not the config."""
    cam = CameraRuntime(CameraConfig(
        cam_id="left", backend="synthetic", fps=20.0,
        full_resolution=(32, 24), preview_resolution=(32, 24),
        synthetic_drift_px=0.0, process_fps=5.0), writer=None)
    cam.cfg.__dict__["fps"] = 20.0
    t = _run_briefly(cam, 1.2)
    st = cam.status()
    cam._stop.set()
    t.join(timeout=2)
    cam.source.close()

    # Acquisition runs at the source's rate; the pipeline runs at the cap.
    assert 12 <= st["sensor_fps"] <= 26, st["sensor_fps"]
    assert 3 <= st["fps"] <= 8, st["fps"]
    assert st["process_fps"] == 5.0
    assert st["configured_fps"] == 20.0
    assert st["skipped"] > 0


def test_a_stopped_camera_reports_zero_and_an_age():
    """The end-to-end version of the meter test: after the capture loop stops,
    both measured rates must be zero and the ages must be growing."""
    cam = CameraRuntime(CameraConfig(
        cam_id="left", backend="synthetic", fps=40.0,
        full_resolution=(32, 24), preview_resolution=(32, 24),
        synthetic_drift_px=0.0), writer=None)
    t = _run_briefly(cam, 0.6)
    assert cam.status()["fps"] > 0
    cam._stop.set()
    t.join(timeout=2)

    time.sleep(3.2)                      # past RateMeter's default stale_after
    st = cam.status()
    cam.source.close()
    assert st["fps"] == 0.0, "the pipeline stopped and the report still claimed a rate"
    assert st["sensor_fps"] == 0.0
    assert st["published_age_s"] > 3.0
    assert st["acquired_age_s"] > 3.0


def test_ages_are_none_before_anything_has_happened():
    """None means "never", which is not the same claim as "0 seconds ago"."""
    cam = CameraRuntime(CameraConfig(
        cam_id="left", backend="synthetic",
        full_resolution=(32, 24), preview_resolution=(32, 24)), writer=None)
    st = cam.status()
    assert st["published_age_s"] is None
    assert st["acquired_age_s"] is None
    assert st["fps"] == 0.0


# -- the writer's last successful write -------------------------------------


def test_the_writer_reports_when_something_last_actually_landed(tmp_path):
    """A mounted, writable, roomy device says nothing about whether the last
    capture reached it -- and the moment that matters is exactly the moment all
    three still look fine."""
    from trilobite.config import StorageConfig
    from trilobite.storage.writer import SessionWriter

    w = SessionWriter(StorageConfig(root=str(tmp_path / "d")), tmp_path / "d")
    assert w.state()["last_write"] is None, "nothing written yet is not 'written long ago'"

    w.save_still(Frame.now(np.zeros((8, 8), dtype=np.uint8), "left", 1))
    lw = w.state()["last_write"]
    assert lw["bytes"] > 0
    assert lw["age_s"] < 5
    assert lw["durability"] in ("strict", "file-only")
    assert lw["file"].endswith(".npy")


def test_last_write_is_recorded_only_after_both_members_are_verified(tmp_path, monkeypatch):
    """"Last write" must not come to mean "last write attempted". The sidecar
    is written after the image; a failure there must leave the field alone."""
    import trilobite.storage.writer as W
    from trilobite.config import StorageConfig
    from trilobite.storage.writer import SessionWriter

    w = SessionWriter(StorageConfig(root=str(tmp_path / "d")), tmp_path / "d")
    real = W.write_durably

    def fail_on_json(path, payload):
        if str(path).endswith(".json"):
            raise OSError("no room for the sidecar")
        return real(path, payload)

    monkeypatch.setattr(W, "write_durably", fail_on_json)
    with pytest.raises(OSError):
        w.save_still(Frame.now(np.zeros((8, 8), dtype=np.uint8), "left", 1))
    assert w.state()["last_write"] is None


# -- the stream stops asserting liveness it does not have -------------------


def _stream_body_bytes(base: str, path: str, *windows: float) -> list[int]:
    """Read one MJPEG connection across consecutive time `windows`.

    Returns the body bytes received in each. Consecutive windows on ONE
    connection is the shape the stale test needs: a client that connects to a
    stalled camera should get the last frame once -- otherwise the viewport is
    blank -- and then nothing. Two separate connections could not tell that
    apart from "sends a frame every time it is asked".

    A bare socket, and both halves of that are the result of getting it wrong.

    `TestClient.stream()` blocks indefinitely on a *sync* streaming generator,
    and the endpoint under test is deliberately sync — JPEG encoding is
    CPU-bound, so an `async def` generator would stall the event loop. So this
    needs a real server and a real client.

    And it cannot be `urllib`. Its buffered reader raises
    `OSError: cannot read from timed out object` after the first socket
    timeout and stays broken, so a helper built on it exits at the first quiet
    moment and reports zero — for a live stream as readily as a dead one. That
    version of this helper passed while the behaviour it checked was mutated
    away, which is precisely the failure mutation testing exists to expose.

    Headers are excluded from the count: they arrive whether or not a single
    frame is ever sent, so counting them would make "the server answered" look
    like "the server is streaming".
    """
    import socket

    host, port = base.removeprefix("http://").split(":")
    counts = [0] * len(windows)
    header_done = False
    buf = b""

    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        s.settimeout(0.25)
        for i, seconds in enumerate(windows):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    continue             # quiet right now; a plain socket recovers
                if not chunk:
                    return counts
                if header_done:
                    counts[i] += len(chunk)
                    continue
                buf += chunk
                if b"\r\n\r\n" in buf:
                    header_done = True
                    counts[i] += len(buf.split(b"\r\n\r\n", 1)[1])
    return counts


@pytest.fixture
def served(tmp_path):
    """The real application behind a real uvicorn server, on a free port."""
    import socket

    import uvicorn

    from trilobite.app import Application
    from trilobite.config import AppConfig, StorageConfig
    from trilobite.web.server import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])

    app = Application(
        AppConfig(storage=StorageConfig(root=str(tmp_path / "d")),
                  cameras=[CameraConfig(
                      cam_id="left", backend="synthetic", fps=30.0,
                      full_resolution=(32, 32), preview_resolution=(32, 32),
                      synthetic_drift_px=0.0)]),
        state_path=None, restore=False)
    server = uvicorn.Server(uvicorn.Config(
        create_app(app), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(timeout=5)


def test_a_stalled_camera_stops_producing_stream_bytes(served):
    """It used to re-encode and re-send the frame it had already sent whenever
    the bus wait timed out.

    Two costs, and the second is why this is a liveness test. It burned a JPEG
    encode per timeout on a camera that had stopped — the worst moment to spend
    CPU on a Pi. And it made the byte stream indistinguishable from a live one,
    so a frozen sensor arrived at the browser as a healthy stream of identical
    images.
    """
    base, app = served
    cam = app.cameras["left"]

    # Running: parts keep arriving in both windows.
    cam.start()
    a, b = _stream_body_bytes(base, "/stream/left.mjpg", 1.5, 1.5)
    cam.stop()
    assert a > 0 and b > 0, f"a running camera produced {a}, then {b}"

    # Stopped, with the last frame still sitting in the bus. One frame on
    # connect is correct -- a new viewer should not get a blank pane -- and
    # then the stream must go quiet. The old code re-sent that same frame every
    # time the 2 s bus wait timed out, for as long as the tab stayed open.
    first, then = _stream_body_bytes(base, "/stream/left.mjpg", 2.5, 5.0)
    assert first > 0, "a client connecting to a stalled camera got no frame at all"
    assert then == 0, (
        f"a stopped camera kept producing preview bytes ({then} in 5 s) — the "
        f"stream is asserting liveness it does not have"
    )

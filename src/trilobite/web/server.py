"""FastAPI layer: MJPEG preview, sub-aperture views, parameter control, capture.

MJPEG is the starting transport because it works in every browser with no
client library and no negotiation. It is bandwidth-hungry and carries no
timestamps, so it will eventually be replaced -- probably by WebRTC. That
replacement touches this module and the static page and nothing else, which is
why the encoder and the transport sit behind the sink boundary rather than
inside the capture loop.

Streaming generators are plain `def`, not `async def`. Starlette runs sync
generators on a threadpool, which is what we want: JPEG encoding is CPU-bound
and would block the event loop otherwise.

**Connection budget.** A browser allows only about six concurrent HTTP/1.1
connections per origin, and an MJPEG stream holds one open forever. Two
cameras with a main preview and three sub-aperture tiles each would be eight
permanent connections -- over the limit, so every other request, including
every button press, queues behind them and never completes. The symptom is a
UI whose controls silently do nothing.

So: exactly one persistent stream per camera (the main preview), and the
sub-aperture tiles are served as single-shot JPEGs that the page polls. Short
requests complete and release their connection. This is a hard constraint on
the design, not a tuning parameter -- adding a third persistent stream per
camera would reintroduce the deadlock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from ..acquisition import CommandExpired, CommandRejected
from ..app import Application, CameraRuntime
from ..calibration import CalibrationSettings
from ..cameras.rawformat import RawFormatError
from ..config import StageConfig
from ..optics.mla import UI_SUBAPERTURES
from ..optics.orientation import Orientation
from ..processing.registry import catalogue
from ..recording.buffer import PrefaultFailed
from ..sinks.jpeg import encode_jpeg
from ..storage.writer import StorageRefused

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# Defined in the optics module, so the overlay's highlight boxes and the tiles
# served here are guaranteed to name the same lenslets.
SUBAPERTURE_VIEWS = UI_SUBAPERTURES


class ParamUpdate(BaseModel):
    values: dict[str, Any]


class ControlUpdate(BaseModel):
    controls: dict[str, Any]


class StageAdd(BaseModel):
    type: str
    name: str | None = None
    params: dict[str, Any] = {}
    index: int | None = None


class StorageDeviceRef(BaseModel):
    device: str            # /dev/sda1
    mount: str | None = None


class StorageTarget(BaseModel):
    path: str
    # Mount points get the data subdirectory appended; an explicit directory
    # chosen by hand does not. Defaulting to True is right because the common
    # case is picking a device off the list, and writing session folders into
    # the root of someone's USB stick is rude.
    append_subdir: bool = True


class MeasureRequest(BaseModel):
    """How much to write while measuring sustained throughput.

    The defaults are the plan's: 8 GB or 30 s, whichever comes first. Both
    matter -- the byte budget is what gets past an SLC cache, and the time
    budget is what stops a slow device holding the UI for ten minutes.
    """

    budget_mb: int = 8192
    budget_seconds: float = 30.0


class RecordingKind(BaseModel):
    kind: Literal["burst", "continuous"] = "continuous"


class RecordingPlanRequest(BaseModel):
    format: str = "raw16"
    fps: float = 30.0
    duration_s: float | None = None


class RecordingArmRequest(RecordingPlanRequest):
    kind: Literal["burst", "continuous"] = "continuous"
    # Per request, never remembered. See /api/recording/save.
    internal_ok: bool = False
    max_drop_fraction: float | None = None
    # Overrides for the memory-derived sizes. Present so a bench run can force
    # a small buffer or a small queue to exercise the limits deliberately,
    # which is step 3 of the plan's bench test.
    frames: int | None = None
    slots: int | None = None


class RecordingSaveRequest(BaseModel):
    internal_ok: bool = False
    label: str | None = None


def _subaperture_tile(cam: CameraRuntime, view: str) -> np.ndarray | None:
    """Crop the named sub-aperture out of the current preview frame.

    Returns None when there is no MLA stage, no frame yet, or the requested
    lenslet currently falls off the sensor -- all of which happen routinely
    while the pitch is being adjusted, and none of which are errors.
    """
    stage = cam.mla_stage()
    frame = cam.latest()
    if stage is None or frame is None:
        return None
    h, w = frame.data.shape[:2]
    # geometry_for, not geometry: identical for the preview (which is the
    # reference resolution) but correct if this is ever handed a different
    # frame size. One conversion rule, used everywhere.
    geom = stage.geometry_for(w, h)
    scale = float(stage.params.crop_scale)
    derot = bool(getattr(stage.params, "derotate_views", True))
    idx = geom.named_indices(scale, derotate=derot).get(view)
    if idx is None:
        return None
    # Whether to resample into the lattice axes is a physical question, not a
    # display preference -- see the note at the top of processing/stages/
    # plenoptic.py. The stage owns the decision; this just honours it.
    if bool(getattr(stage.params, "derotate_views", True)):
        tile = geom.crop_derotated(frame.data, idx[0], idx[1], scale)
    else:
        tile = geom.crop(frame.data, idx[0], idx[1], scale)
    return tile if tile.size else None


def create_app(application: Application) -> FastAPI:
    api = FastAPI(title="TrilobiteVision", version="0.2.0")
    preview_fps = application.cfg.server.preview_fps
    quality = application.cfg.server.jpeg_quality

    def _cam(cam_id: str) -> CameraRuntime:
        try:
            return application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

    # -- pages ----------------------------------------------------------

    def _ui_build() -> str:
        """A short hash of the page as it currently sits on disk.

        Deployment here is `git pull` on the Pi with the browser left open on
        the dashboard, and a `FileResponse` carries an ETag but no
        `Cache-Control` -- so a browser applies heuristic freshness and can
        serve the page from cache for minutes without ever asking. The result
        is a UI missing controls the server already implements, which is
        indistinguishable from a bug in the feature and cost an exchange to
        work out. `no-cache` below fixes the cause; this makes the symptom
        self-diagnosing when it comes back in some other form.
        """
        try:
            return hashlib.sha256((STATIC / "index.html").read_bytes()).hexdigest()[:8]
        except OSError:                              # pragma: no cover - unreadable
            return "unknown"

    @api.get("/")
    def index() -> Response:
        text = (STATIC / "index.html").read_text(encoding="utf-8")
        # Stamped into the page so it knows which build it is, and can say so
        # when the server has moved on. Substitution, not a header, because the
        # comparison has to be made by the copy of the page that is running.
        return Response(
            text.replace("__UI_BUILD__", _ui_build()),
            media_type="text/html",
            # no-cache is "revalidate", not "do not store": the browser still
            # keeps it and still gets a 304 when nothing changed.
            headers={"Cache-Control": "no-cache"},
        )

    @api.get("/api/ui-build")
    def ui_build() -> dict[str, str]:
        """What the page on disk hashes to now. Polled; see index()."""
        return {"build": _ui_build()}

    @api.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    # -- status ---------------------------------------------------------

    @api.get("/api/status")
    def status() -> dict[str, Any]:
        return application.status()

    @api.get("/api/cameras")
    def cameras() -> list[dict[str, Any]]:
        # Config order, which is the order the UI lays them out left to right.
        # That is the reason the cameras are named "left" and "right" rather
        # than by index: the name states the physical arrangement, and the
        # config order is what places them on screen. Swapping ribbon cables
        # changes `index`; it does not change which camera is called "left".
        return [c.status() for c in application.cameras.values()]

    @api.get("/api/stage-types")
    def stage_types() -> dict[str, Any]:
        return catalogue()

    # -- preview stream --------------------------------------------------

    def _mjpeg(produce, min_interval: float, wait_for_new):
        """Shared MJPEG body. `produce` returns an ndarray or None.

        `wait_for_new` returns False when it timed out without a new frame. On
        that path nothing is sent, and that is a change: this used to fall
        through and re-encode the frame it had already sent. Two costs, and the
        second is the reason for the fix. It burned a JPEG encode per timeout
        on a camera that had stopped producing -- the worst moment to spend
        CPU. And it made the byte stream indistinguishable from a live one, so
        a frozen sensor arrived at the browser as a healthy stream of identical
        images.

        The picture on screen is still the last good one either way; MJPEG has
        no way to say "nothing here". Saying so is the page's job, using
        `published_age_s` from the status poll.
        """

        def frames():
            last_sent = 0.0
            while True:
                if wait_for_new() is False:
                    continue
                now = time.monotonic()
                if now - last_sent < min_interval:
                    continue
                image = produce()
                if image is None:
                    # Emit nothing but keep the connection open: the tile will
                    # reappear when the geometry is valid again.
                    time.sleep(min_interval)
                    continue
                last_sent = now
                try:
                    payload = encode_jpeg(image, quality=quality)
                except Exception:
                    log.exception("jpeg encode failed")
                    continue
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\n\r\n"
                    + payload
                    + b"\r\n"
                )

        return StreamingResponse(
            frames(), media_type="multipart/x-mixed-replace; boundary=frame"
        )

    @api.get("/stream/{cam_id}.mjpg")
    def stream(cam_id: str) -> StreamingResponse:
        cam = _cam(cam_id)
        state = {"seen": -1}

        def wait() -> bool:
            version, _ = cam.preview.wait_newer(state["seen"], timeout=2.0)
            fresh = version != state["seen"]
            state["seen"] = version
            return fresh

        return _mjpeg(lambda: (cam.latest().data if cam.latest() else None),
                      1.0 / max(preview_fps, 0.1), wait)

    @api.get("/subaperture/{cam_id}/{view}.jpg")
    def subaperture_tile(cam_id: str, view: str) -> Response:
        """One sub-aperture tile, single shot.

        Polled by the page rather than streamed: see the connection budget note
        at the top of this module. Six tiles as persistent streams would
        exhaust the browser's per-origin connection limit and hang every other
        request on the page.
        """
        cam = _cam(cam_id)
        if view not in SUBAPERTURE_VIEWS:
            raise HTTPException(404, f"unknown view {view!r}; have {list(SUBAPERTURE_VIEWS)}")
        tile = _subaperture_tile(cam, view)
        if tile is None:
            # Not an error: routine while the pitch is mid-adjustment and the
            # corner lenslet is momentarily off-sensor.
            raise HTTPException(204, "no tile")
        return Response(
            encode_jpeg(tile, quality=quality),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @api.get("/snapshot/{cam_id}.jpg")
    def snapshot(cam_id: str) -> Response:
        cam = _cam(cam_id)
        frame = cam.latest()
        if frame is None:
            raise HTTPException(503, f"{cam_id}: no frame yet")
        return Response(encode_jpeg(frame.data, quality=quality), media_type="image/jpeg")

    # -- MLA geometry ----------------------------------------------------

    @api.get("/api/subapertures/{cam_id}")
    def subapertures(cam_id: str) -> dict[str, Any]:
        """Which lenslet indices the named sub-apertures currently resolve to.

        The UI shows these next to the tiles so you can see, numerically, how
        far out the corner views are -- (7, -5) tells you more about whether
        the pitch is right than the picture does.
        """
        cam = _cam(cam_id)
        stage = cam.mla_stage()
        frame = cam.latest()
        if stage is None or frame is None:
            return {"available": False, "enabled": False, "views": {}}
        h, w = frame.data.shape[:2]
        geom = stage.geometry_for(w, h)
        scale = float(stage.params.crop_scale)
        named = geom.named_indices(scale)
        return {
            "available": True,
            "enabled": bool(stage.params.enabled),
            "stage": stage.name,
            "reference_shape": list(stage.reference_shape or (w, h)),
            "origin": [round(v, 2) for v in geom.origin],
            "centre_pixel": [round(v, 2) for v in geom.centre_pixel],
            "extent": list(geom.index_extent(scale)),
            "crop_px": round(geom.pitch * scale, 2),
            "views": {
                name: {
                    "index": list(named[name]),
                    "centre": [round(v, 2) for v in geom.centre_of(*named[name])],
                }
                for name in SUBAPERTURE_VIEWS
                if name in named
            },
        }

    # -- pipeline control -------------------------------------------------

    @api.get("/api/pipeline/{cam_id}")
    def get_pipeline(cam_id: str) -> list[dict[str, Any]]:
        return _cam(cam_id).pipeline.describe()

    @api.post("/api/pipeline/{cam_id}/{stage_name}")
    def set_params(cam_id: str, stage_name: str, body: ParamUpdate) -> dict[str, Any]:
        cam = _cam(cam_id)
        try:
            out = cam.pipeline.update_params(stage_name, body.values)
            application.mark_dirty()
            return out
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValidationError as exc:
            # Rejected parameters return 422 with the reason, so the UI can say
            # what was wrong rather than silently doing nothing.
            raise HTTPException(422, json.loads(exc.json())) from None

    @api.post("/api/pipeline/{cam_id}/_add")
    def add_stage(cam_id: str, body: StageAdd) -> list[dict[str, Any]]:
        cam = _cam(cam_id)
        try:
            cam.pipeline.add(
                StageConfig(type=body.type, name=body.name, params=body.params), body.index
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)) from None
        # A stage added at runtime does not know what else is in the pipeline;
        # re-run the wiring so a new presence stage finds the MLA geometry.
        cam.bind_pipeline()
        application.mark_dirty()
        return cam.pipeline.describe()

    @api.delete("/api/pipeline/{cam_id}/{stage_name}")
    def remove_stage(cam_id: str, stage_name: str) -> list[dict[str, Any]]:
        cam = _cam(cam_id)
        cam.pipeline.remove(stage_name)
        cam.bind_pipeline()
        application.mark_dirty()
        return cam.pipeline.describe()

    # -- sensor controls ---------------------------------------------------

    @api.get("/api/controls/{cam_id}")
    def read_controls(cam_id: str) -> dict[str, Any]:
        """Driver controls this sensor advertises, with its own limits.

        Ranges come from the sensor, not from a hardcoded guess: exposure
        limits differ per sensor and per mode, and a wrong range makes the
        slider useless over most of its travel.
        """
        cam = _cam(cam_id)
        # `requested_controls()`, not `cfg.controls`. The config is the value
        # the YAML shipped with; the source holds every value asked for since,
        # including the ones the state file restored at start-up. Reading the
        # config here is what made a restored exposure look unrestored: the
        # camera really was at 12000 us and the box really did say 8000, and
        # the first nudge of the slider sent the sensor back to the YAML.
        return {
            "spec": cam.control_spec(),
            "requested": cam.source.requested_controls(),
            "config": dict(cam.cfg.controls),
        }

    def _orientation_state(cam: CameraRuntime) -> dict[str, Any]:
        """Orientation plus whether it may currently be changed, in one payload.

        The lock and the values travel together so the page cannot render an
        enabled control against a locked camera: one request, one truth.
        """
        mla = cam.mla_stage()
        locked = bool(mla is not None and mla.params.enabled)
        return {
            **cam.source.orientation,
            "locked": locked,
            "lock_reason": (
                "the MLA grid is enabled, and its alignment was made against "
                "this frame. Turn the grid off to change the orientation; the "
                "alignment will be reset when you do."
                if locked else ""
            ),
        }

    @api.get("/api/orientation/{cam_id}")
    def read_orientation(cam_id: str) -> dict[str, Any]:
        return _orientation_state(_cam(cam_id))

    @api.post("/api/orientation/{cam_id}")
    def write_orientation(cam_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Rotate and mirror the sensor image, for the preview AND the saved data.

        A separate endpoint from the sensor controls because it is neither a
        driver control nor a pipeline parameter: it is applied at acquisition,
        before either. See CameraSource._orient.

        **This is a setup decision, and it is locked once the grid is on.** The
        order of operations for a calibration is: mount the camera, set the
        orientation, align the grid against the frame you are then looking at,
        and leave the frame alone for the rest of the session. Changing it
        later would invalidate the alignment, every recorded pose and the fit
        that follows -- so with the grid enabled this returns 409 rather than
        doing something clever.

        With the grid off it is allowed, and `bind_sensor` then RESETS the
        alignment: offsets and lattice rotation to zero, pitch kept. An earlier
        version transformed the offsets instead, which was arithmetically right
        and practically wrong -- it produced a grid that claimed to be aligned
        when nobody had looked at it.
        """
        cam = _cam(cam_id)
        wants = [k for k in ("rotate_deg", "flip_horizontal", "flip_vertical")
                 if k in body]
        state = _orientation_state(cam)
        if wants and state["locked"] and any(
            body[k] != state[k] for k in wants
        ):
            raise HTTPException(409, {
                "error": f"{cam.cam_id}: orientation is locked",
                "reason": state["lock_reason"],
            })

        changed = []
        if "rotate_deg" in body:
            try:
                deg = int(body["rotate_deg"]) % 360
            except (TypeError, ValueError):
                raise HTTPException(422, "rotate_deg must be a whole number") from None
            if deg not in (0, 90, 180, 270):
                raise HTTPException(
                    422,
                    f"rotate_deg must be 0, 90, 180 or 270 (clockwise), not {deg}. "
                    f"Anything else is a resample rather than a relabelling of "
                    f"pixels, and would cost resolution on every frame.",
                )
            if deg != int(cam.cfg.rotate_deg):
                cam.cfg.rotate_deg = deg
                changed.append("rotate_deg")
        for key in ("flip_horizontal", "flip_vertical"):
            if key in body:
                new_value = bool(body[key])
                if new_value != bool(getattr(cam.cfg, key)):
                    setattr(cam.cfg, key, new_value)
                    changed.append(key)

        mla = cam.mla_stage()
        rebased = ""
        if changed and mla is not None and cam.source.is_open:
            def alignment() -> tuple[float, float, float]:
                return (float(mla.params.offset_x), float(mla.params.offset_y),
                        float(mla.params.rotation_deg))

            had = alignment()
            w, h = cam.source.describe().full_resolution
            mla.bind_sensor(int(w), int(h), Orientation.of(cam.cfg))
            # Reported from what actually changed, not from what happened to be
            # there beforehand. Those differ -- a grid that was never bound has
            # nothing to reset -- and announcing a reset that did not happen
            # would send the operator off to re-align a grid that is still fine.
            rebased = (
                f"frame is now {w}x{h}"
                + (f"; grid alignment reset (offsets were {had[0]:.1f}, {had[1]:.1f}, "
                   f"rotation {had[2]:.2f}°), pitch kept at "
                   f"{mla.params.pitch_px:.1f} px"
                   if alignment() != had else "")
            )
        if changed:
            # Orientation is how the camera is BOLTED DOWN. It has to outlive
            # the process, and it was the one setting the snapshot never marked
            # dirty -- so a flip set in the UI survived until the next restart
            # and then quietly reverted to the YAML.
            application.mark_dirty()
        return {
            **_orientation_state(cam),
            "changed": changed,
            "rebased": rebased,
        }

    @api.post("/api/controls/{cam_id}")
    def write_controls(cam_id: str, body: ControlUpdate) -> dict[str, Any]:
        """Sensor controls: ExposureTime, AnalogueGain, AeEnable, ...

        Deliberately a different endpoint from pipeline parameters. These
        change what the sensor does; pipeline parameters change what happens to
        the numbers afterwards. Conflating them makes it impossible to tell
        whether a change you observed was optical or computational.
        """
        cam = _cam(cam_id)
        try:
            cam.set_controls(body.controls)
        except CommandRejected as exc:
            raise HTTPException(429, str(exc)) from None
        except CommandExpired as exc:
            raise HTTPException(504, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None
        application.mark_dirty()
        # Echo what the source actually settled on, not what was asked for.
        # Turning auto-exposure off pins the exposure AE had converged to, and
        # the caller needs that number to put in its box.
        return {
            "ok": True,
            "requested": body.controls,
            "effective": cam.source.requested_controls(),
            "live": cam.live_controls(),
        }

    # -- calibration -------------------------------------------------------
    #
    # The dashboard is a client-side mode, but its settings are server state:
    # they are frozen into the session record when a run starts, and they must
    # survive a browser reload mid-afternoon. Keeping them here also means the
    # readiness checks run against the live pipeline rather than against what
    # the page last happened to hear about it.

    @api.get("/api/calibration/settings")
    def get_calibration_settings() -> dict[str, Any]:
        return {
            "settings": application.calibration.model_dump(),
            "schema": CalibrationSettings.model_json_schema(),
            "derived": application.calibration_derived(),
        }

    @api.put("/api/calibration/settings")
    def put_calibration_settings(body: dict[str, Any]) -> dict[str, Any]:
        """Whole-object update. Returns the stored settings and what they imply.

        Whole-object rather than per-field because the derived optics depend on
        several at once: a partial update would report a square size computed
        from a mix of old and new values, which is worse than no number.
        """
        try:
            application.calibration = CalibrationSettings.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json())) from None
        application.mark_dirty()
        return {
            "settings": application.calibration.model_dump(),
            "derived": application.calibration_derived(),
        }

    @api.get("/api/calibration/readiness")
    def calibration_readiness() -> dict[str, Any]:
        """Preconditions, split into blocking and advisory."""
        return application.calibration_readiness()

    @api.post("/api/calibration/start")
    def calibration_start() -> dict[str, Any]:
        """Open a hands-free capture session.

        From here the rig decides when to take a shot: it watches the presence
        map, waits for the board to stop moving, checks a five-tile cross at
        full resolution, records the pose, and then refuses to record again
        until the board has moved. The operator's hands stay on the board.
        """
        readiness = application.calibration_readiness()
        if not readiness["ready"]:
            raise HTTPException(409, {
                "error": "preconditions not met",
                "blocking": readiness["blocking_failures"],
            })
        try:
            return application.session_start()
        except Exception as exc:
            log.exception("session failed to start")
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @api.post("/api/calibration/stop")
    def calibration_stop() -> dict[str, Any]:
        """Finish the session. Nothing else ends it -- coverage targets are
        reported, never acted on."""
        return application.session_stop()

    @api.get("/api/calibration/session")
    def calibration_session() -> dict[str, Any]:
        """Phase, banner text, coverage, pose count, and the event log.

        The event log carries a monotonic sequence number per entry so the
        console can play each tone exactly once. That matters more than it
        sounds: audio is the primary channel here, because the operator is
        looking at the board rather than at the screen.
        """
        return application.session_state()

    @api.post("/api/calibration/force")
    def calibration_force() -> dict[str, Any]:
        """Take a shot now, whatever the state machine thinks.

        The keyboard override. Some of the most valuable poses -- a board at an
        extreme angle, or in a dim corner of the field -- are ones the settle
        test will never arm on by itself.
        """
        return application.session_force()

    @api.post("/api/calibration/discard")
    def calibration_discard() -> dict[str, Any]:
        """Mark the most recent pose discarded.

        The files stay on disk and the index records the decision. A pose
        rejected by reflex and wanted back later is worth more than the 3 MB it
        occupies, and an offline tool can always ignore the flag.
        """
        return application.session_discard()

    @api.get("/calibration/peaks/{cam_id}.jpg")
    def calibration_peaks(cam_id: str) -> Response:
        """Every saddle peak the detector found, with each micro-image's count.

        The diagnostic for "no board is being noticed". Read it like this:
        peaks scattered over the image but the boxes in the wrong place is an
        alignment problem; a handful of peaks per box where thirty are expected
        means the board's squares are too large for a micro-image; no peaks at
        all is exposure, focus or a lens cap.
        """
        _cam(cam_id)
        try:
            view = application.presence_overlay(cam_id)
        except Exception as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None
        if view is None:
            raise HTTPException(
                204, "no presence map -- is the checkerboard_presence stage enabled?")
        return Response(encode_jpeg(view, quality=quality), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @api.get("/calibration/shot/{cam_id}.jpg")
    def calibration_shot(cam_id: str) -> Response:
        """The last recorded pose, with its cross and corners drawn.

        This is what the review pause shows: the frame that was actually
        measured, not a later preview frame that resembles it.
        """
        _cam(cam_id)
        shot = application.session_shot(cam_id)
        if shot is None:
            raise HTTPException(204, "no pose recorded yet")
        return Response(
            encode_jpeg(shot, quality=quality),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    # -- storage devices ----------------------------------------------------
    #
    # Hot-pluggable by design: a device that appears after startup must show up
    # here, and one that is pulled must stop being written to. See
    # storage/devices.py for why that needs polling rather than a config field.

    @api.get("/api/storage")
    def storage() -> dict[str, Any]:
        return application.storage_state()

    @api.post("/api/storage/verify")
    def storage_verify() -> dict[str, Any]:
        """Write a few MB to the active session directory, flush it, read it back.

        Exists because the alternative to checking a disk deliberately is
        finding out at the end of a session. It catches a mount that accepts
        writes and stores nothing, a full or read-only filesystem, and a device
        too slow to hold the capture rate. It does NOT prove the disk survives
        being unplugged -- nothing short of unplugging it does.
        """
        from ..storage.writer import verify_device  # noqa: PLC0415

        return verify_device(application.writer.session_dir)

    @api.post("/api/storage/target")
    def storage_target(body: StorageTarget) -> dict[str, Any]:
        """Send subsequent captures to a different filesystem.

        A new session directory is created on the target; nothing already
        written is moved or deleted. `path` is either a device mount point
        (the usual case -- the data subdirectory is appended) or an explicit
        directory, for anyone who wants to choose it exactly.
        """
        from ..storage.devices import DATA_SUBDIR  # noqa: PLC0415

        target = Path(body.path).expanduser()
        if body.append_subdir and target.name != DATA_SUBDIR:
            target = target / DATA_SUBDIR
        try:
            application.writer.retarget(target)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None
        return application.storage_state()

    @api.post("/api/storage/mount")
    def storage_mount(body: StorageDeviceRef) -> dict[str, Any]:
        """Mount a plugged-in but unmounted disk.

        A headless Pi runs no desktop session, so nothing auto-mounts removable
        media: a USB SSD plugged into a running rig is visible to the kernel
        and reachable by nothing. This is the button that closes that gap. It
        shells out to udisksctl, which needs no sudo and picks the mount point.
        """
        from ..storage.devices import mount_device  # noqa: PLC0415

        try:
            mount = mount_device(body.device)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            # The tool's own words. "unknown filesystem type 'exfat'" tells you
            # to install exfatprogs; anything vaguer wastes an afternoon.
            raise HTTPException(422, str(exc)) from None
        log.info("mounted %s at %s", body.device, mount)
        return application.storage_state()

    @api.post("/api/storage/unmount")
    def storage_unmount(body: StorageDeviceRef) -> dict[str, Any]:
        """Unmount so the disk can be pulled without corrupting it.

        Refuses, loudly, if anything still holds the filesystem open -- which
        includes this application, so release the output first. "Target is
        busy" is information, not an obstacle to route around.
        """
        from ..storage.devices import unmount_device  # noqa: PLC0415

        if Path(application.writer.root) == Path(body.mount or "\0"):
            raise HTTPException(409, "captures are still going there -- press Release first")
        try:
            unmount_device(body.device)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422, str(exc)) from None
        return application.storage_state()

    @api.get("/api/storage/diagnostics")
    def storage_diagnostics() -> dict[str, Any]:
        """Raw lsblk, /proc/mounts and udisks2 version, beside what the
        enumerator made of them.

        For the case that actually happens: "I plugged a disk in and nothing
        appeared". The three together localise it in one round trip -- present
        in lsblk but not in the list is an enumeration bug, absent from both is
        a kernel or cable problem, present and unmountable is a missing
        filesystem driver.
        """
        from ..storage.devices import diagnostics  # noqa: PLC0415

        return diagnostics()

    @api.post("/api/storage/measure")
    def storage_measure(body: MeasureRequest) -> dict[str, Any]:
        """Sustained write throughput, and what it allows to be recorded.

        Stage 5a and 5c's pre-flight. Distinct from `/verify`, which writes
        four megabytes and therefore measures the drive's write cache: this one
        writes until it has moved gigabytes or spent its time budget, and
        reports the **final quarter's** rate, which is the figure that predicts
        a multi-minute recording.

        Takes a while on purpose. The alternative is a configuration chosen
        from a number that describes the first twenty seconds of a five-minute
        job.
        """
        try:
            return application.recording.measure(
                budget_bytes=int(body.budget_mb) << 20,
                budget_seconds=float(body.budget_seconds))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        except StorageRefused as exc:
            raise HTTPException(409, {"state": exc.state, "detail": str(exc)}) from None
        except OSError as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @api.post("/api/storage/release")
    def storage_release(timeout: float = 10.0) -> dict[str, Any]:
        """Return output to the internal default so a device can be unplugged.

        There is no eject here on purpose -- unmounting someone's filesystem is
        not this application's business. The safe sequence is: release, check
        that the panel says the output is internal again, then pull the disk.

        **This now drains.** It waits for writes already in flight to finish
        and returns `released: false` if they do not, rather than reporting a
        disk as safe to pull while a write is inside the kernel -- which is
        what the old version did, because `save_still` held the lock only long
        enough to allocate a counter. 409 rather than 200, so a UI cannot show
        "released" on a refusal.
        """
        out = application.writer.release(timeout=timeout)
        if out.get("released") is False:
            raise HTTPException(409, {
                "detail": (f"{out.get('inflight')} write(s) are still in "
                           f"flight. The disk has NOT been released and must "
                           f"not be removed."),
                "storage": application.storage_state(),
            })
        return application.storage_state()

    # -- recording ---------------------------------------------------------
    #
    # Two recorders behind one set of routes, because the operator is choosing
    # between them rather than using both: `kind` selects. Stage 5b is the RAM
    # burst, 5c the continuous recording.

    def _rec(fn, *args, **kwargs) -> dict[str, Any]:
        """Call into the recorder and map its refusals onto status codes.

        Each refusal gets the code that says what the caller should do about
        it, which is the whole value of having distinct exception types:

          409  the rig is in a state that makes this wrong (an unsaved burst,
               another recorder running, a target that cannot take the write).
               Nothing was attempted; change something and retry.
          422  the request itself does not describe a possible recording.
          507  the target is full or below its reserve. Separated from 409 so a
               UI can say "the disk is full" without parsing a message.
        """
        try:
            return fn(*args, **kwargs)
        except StorageRefused as exc:
            code = 507 if exc.state in ("full", "internal-refused") else 409
            raise HTTPException(code, {
                "state": exc.state, "detail": str(exc),
                "recording": application.recording.status(),
            }) from None
        except PrefaultFailed as exc:
            raise HTTPException(507, {"state": "memory", "detail": str(exc)}) from None
        except MemoryError as exc:
            raise HTTPException(507, {"state": "memory", "detail": str(exc)}) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        except OSError as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @api.get("/api/recording")
    def recording_status() -> dict[str, Any]:
        return application.recording.status()

    @api.get("/api/recording/options")
    def recording_options() -> dict[str, Any]:
        """Every configuration this rig can offer, with its consequences.

        Each row carries its predicted drop fraction and the validity it
        caps the data at. Where no measurement is on file the prediction is
        **null, not zero** -- claiming no drops because nothing was measured is
        the one answer this must never give.
        """
        return application.recording.options()

    @api.post("/api/recording/feasibility")
    def recording_feasibility(body: RecordingPlanRequest) -> dict[str, Any]:
        """What would happen if this plan were started now. Changes nothing."""
        return _rec(application.recording.feasibility,
                    body.format, body.fps, body.duration_s)

    @api.post("/api/recording/arm")
    def recording_arm(body: RecordingArmRequest) -> dict[str, Any]:
        """Commit the memory and verify the target. Nothing is captured yet.

        Arming is where every refusal belongs: the memory is prefaulted here
        so a shortfall is a message rather than an OOM kill at frame 400, and
        the target is admitted here so a recording that cannot be written is
        refused before the operator has spent an afternoon taking it.
        """
        if body.kind == "burst":
            return _rec(application.recording.arm_burst, body.format, body.fps,
                        body.duration_s, body.frames)
        return _rec(application.recording.arm_continuous, body.format,
                    body.fps, body.duration_s, body.internal_ok,
                    body.max_drop_fraction, body.slots)

    @api.post("/api/recording/start")
    def recording_start(body: RecordingKind) -> dict[str, Any]:
        if body.kind == "burst":
            return _rec(application.recording.start_burst)
        return _rec(application.recording.start_continuous)

    @api.post("/api/recording/stop")
    def recording_stop(body: RecordingKind) -> dict[str, Any]:
        if body.kind == "burst":
            return _rec(application.recording.stop_burst)
        return _rec(application.recording.stop_continuous)

    @api.post("/api/recording/save")
    def recording_save(body: RecordingSaveRequest) -> dict[str, Any]:
        """Flush a RAM burst to the verified target.

        `internal_ok` is per call and is never remembered. The UI presents it
        unticked every time, because the point of the override is that saving
        multiple gigabytes onto the disk the operating system runs from is a
        deliberate act.
        """
        return _rec(application.recording.save_burst, body.internal_ok,
                    body.label)

    @api.post("/api/recording/discard")
    def recording_discard() -> dict[str, Any]:
        """Throw away an unsaved burst. The only way to lose one on purpose."""
        return _rec(application.recording.discard_burst)

    @api.post("/api/recording/release")
    def recording_release() -> dict[str, Any]:
        """Give the memory back. Refuses over an unsaved burst."""
        return _rec(application.recording.release_burst)

    # -- capture ----------------------------------------------------------

    @api.post("/api/capture/{cam_id}/raw")
    def capture_raw(cam_id: str) -> dict[str, Any]:
        """Full-resolution sensor data, ISP bypassed. This is measurement data.

        A buffer that cannot be admitted returns **422 and no file**, rather
        than a 200 naming a diagnostic capture. Supervisory review R4: the
        caller asked for measurement data, so "here is something else, and it
        worked" is the wrong answer twice over. The 422 body names
        `allow_unvalidated_raw`, which is the deliberate way to get the frame
        anyway and which then marks it for what it is.
        """
        cam = _cam(cam_id)
        try:
            return cam.capture_still(raw=True, tag="raw")
        except CommandRejected as exc:
            # The camera is busy or stopped. 429, not 500: nothing failed, the
            # work was never attempted, and retrying shortly is reasonable.
            raise HTTPException(429, str(exc)) from None
        except CommandExpired as exc:
            # 504, and the message says the capture may still have happened.
            # Nothing here can interrupt an SDK call.
            raise HTTPException(504, str(exc)) from None
        except RawFormatError as exc:
            log.warning("%s: raw capture refused: %s", cam_id, exc)
            raise HTTPException(422, {
                "error": "raw buffer not admitted as sensor data",
                "detail": str(exc),
                "hint": ("set 'allow_unvalidated_raw: true' for this camera to "
                         "capture it as a diagnostic frame, or fix the raw "
                         "format; scripts/probe_cameras.py lists what the "
                         "sensor offers"),
            }) from None
        except Exception as exc:
            log.exception("%s: raw capture failed", cam_id)
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @api.post("/api/capture/{cam_id}/view")
    def capture_view(cam_id: str) -> dict[str, Any]:
        """The processed preview exactly as displayed. A lab note, not data."""
        cam = _cam(cam_id)
        try:
            return cam.capture_preview(tag="view")
        except Exception as exc:
            log.exception("%s: view capture failed", cam_id)
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @api.post("/api/capture-all/{kind}")
    def capture_all(kind: str) -> dict[str, Any]:
        """Trigger every camera. kind is 'raw' or 'view'.

        This is NOT synchronised capture. The sensors free-run, so frames are
        tens of milliseconds apart. For stereo or plenoptic work needing real
        simultaneity, wire the IMX296 XVS pins together and drive an external
        trigger -- software cannot fix this.

        What software CAN fix, and this now does, is not making it worse. The
        old version completed each head's **disk write** before requesting the
        next frame, so storage latency landed inside the pair skew -- tens of
        milliseconds of sensor difference plus however long a USB stick took.
        Both heads are now asked before either is written.
        """
        if kind not in ("raw", "view"):
            raise HTTPException(404, "kind must be 'raw' or 'view'")
        if kind == "view":
            out: dict[str, Any] = {}
            for cam_id, cam in application.cameras.items():
                try:
                    out[cam_id] = cam.capture_preview(tag="view")
                except Exception as exc:
                    out[cam_id] = {"error": f"{type(exc).__name__}: {exc}"}
            return out

        # Ask every head first. Each submit blocks only until that camera's
        # owner services it, which is at most one frame period, and the two
        # owners are independent threads.
        frames: dict[str, Any] = {}
        out = {}
        for cam_id, cam in application.cameras.items():
            try:
                frames[cam_id] = cam.grab_still(raw=True)
            except Exception as exc:
                out[cam_id] = {"error": f"{type(exc).__name__}: {exc}"}
        # Then write. Slow storage now delays the files, not the exposures.
        for cam_id, frame in frames.items():
            try:
                out[cam_id] = application.camera(cam_id).save_frame(frame, "raw")
            except Exception as exc:
                out[cam_id] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    return api

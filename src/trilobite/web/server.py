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

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from ..app import Application, CameraRuntime
from ..config import StageConfig
from ..processing.registry import catalogue
from ..sinks.jpeg import encode_jpeg

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# The sub-apertures the UI shows, in display order. Centre plus two opposite
# corners: the corners are where pitch and rotation error accumulates, so they
# are what tells you whether the grid is right. A centre lenslet looks correct
# under almost any wrong pitch.
SUBAPERTURE_VIEWS: tuple[str, ...] = ("top_right", "centre", "bottom_left")


class ParamUpdate(BaseModel):
    values: dict[str, Any]


class ControlUpdate(BaseModel):
    controls: dict[str, Any]


class StageAdd(BaseModel):
    type: str
    name: str | None = None
    params: dict[str, Any] = {}
    index: int | None = None


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
    geom = stage.geometry(w, h)
    scale = float(stage.params.crop_scale)
    idx = geom.named_indices(scale).get(view)
    if idx is None:
        return None
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

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

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
        """Shared MJPEG body. `produce` returns an ndarray or None."""

        def frames():
            last_sent = 0.0
            while True:
                wait_for_new()
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

        def wait():
            version, _ = cam.preview.wait_newer(state["seen"], timeout=2.0)
            state["seen"] = version

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
        geom = stage.geometry(w, h)
        scale = float(stage.params.crop_scale)
        named = geom.named_indices(scale)
        return {
            "available": True,
            "enabled": bool(stage.params.enabled),
            "stage": stage.name,
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
            return cam.pipeline.update_params(stage_name, body.values)
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
        return cam.pipeline.describe()

    @api.delete("/api/pipeline/{cam_id}/{stage_name}")
    def remove_stage(cam_id: str, stage_name: str) -> list[dict[str, Any]]:
        cam = _cam(cam_id)
        cam.pipeline.remove(stage_name)
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
        return {"spec": cam.source.control_spec(), "requested": dict(cam.cfg.controls)}

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
            cam.source.set_controls(body.controls)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None
        return {"ok": True, "applied": body.controls}

    # -- capture ----------------------------------------------------------

    @api.post("/api/capture/{cam_id}/raw")
    def capture_raw(cam_id: str) -> dict[str, Any]:
        """Full-resolution sensor data, ISP bypassed. This is measurement data."""
        cam = _cam(cam_id)
        try:
            return cam.capture_still(raw=True, tag="raw")
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

        This is NOT synchronised capture. Requests go out sequentially from one
        thread and the sensors free-run, so frames may be tens of milliseconds
        apart. For stereo or plenoptic work needing real simultaneity, wire the
        IMX296 XVS pins together and drive an external trigger -- software
        cannot fix this.
        """
        if kind not in ("raw", "view"):
            raise HTTPException(404, "kind must be 'raw' or 'view'")
        out: dict[str, Any] = {}
        for cam_id, cam in application.cameras.items():
            try:
                out[cam_id] = (
                    cam.capture_still(raw=True, tag="raw")
                    if kind == "raw"
                    else cam.capture_preview(tag="view")
                )
            except Exception as exc:
                out[cam_id] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    return api

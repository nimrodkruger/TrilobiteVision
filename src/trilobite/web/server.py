"""FastAPI layer: MJPEG preview, parameter control, capture triggers.

MJPEG is the starting point because it works in every browser with no client
library and no negotiation. It is also bandwidth-hungry and has no audio or
timestamping, so it will eventually be replaced -- probably by WebRTC for the
live view. That replacement touches only this module and the static page,
which is the reason the encoder and the transport live behind the sink
boundary rather than inside the capture loop.

The streaming generator is a plain `def`, not `async def`. Starlette runs sync
generators on a threadpool, which is what we want: JPEG encoding is CPU-bound
and would block the event loop otherwise.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from ..app import Application
from ..config import StageConfig
from ..processing.registry import catalogue
from ..sinks.jpeg import encode_jpeg

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"


class ParamUpdate(BaseModel):
    values: dict[str, Any]


class ControlUpdate(BaseModel):
    controls: dict[str, Any]


class StageAdd(BaseModel):
    type: str
    name: str | None = None
    params: dict[str, Any] = {}
    index: int | None = None


def create_app(application: Application) -> FastAPI:
    api = FastAPI(title="TrilobiteVision", version="0.1.0")
    preview_fps = application.cfg.server.preview_fps
    quality = application.cfg.server.jpeg_quality

    # -- pages ----------------------------------------------------------

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # -- status ---------------------------------------------------------

    @api.get("/api/status")
    def status() -> dict[str, Any]:
        return application.status()

    @api.get("/api/cameras")
    def cameras() -> list[dict[str, Any]]:
        return [c.status() for c in application.cameras.values()]

    @api.get("/api/stage-types")
    def stage_types() -> dict[str, Any]:
        return catalogue()

    # -- preview stream --------------------------------------------------

    @api.get("/stream/{cam_id}.mjpg")
    def stream(cam_id: str) -> StreamingResponse:
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

        min_interval = 1.0 / max(preview_fps, 0.1)

        def frames():
            seen = -1
            last_sent = 0.0
            while True:
                version, frame = cam.preview.wait_newer(seen, timeout=2.0)
                if frame is None:
                    time.sleep(0.1)
                    continue
                seen = version
                # Rate-limit independently of sensor fps. Without this the
                # encoder becomes the bottleneck and steals CPU from capture.
                now = time.monotonic()
                if now - last_sent < min_interval:
                    continue
                last_sent = now
                try:
                    payload = encode_jpeg(frame.data, quality=quality)
                except Exception:
                    log.exception("%s: jpeg encode failed", cam_id)
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

    @api.get("/snapshot/{cam_id}.jpg")
    def snapshot(cam_id: str):
        """Single preview frame. Useful for scripts and for debugging the
        stream without holding a connection open."""
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        frame = cam.latest()
        if frame is None:
            raise HTTPException(503, f"{cam_id}: no frame yet")
        from fastapi import Response  # noqa: PLC0415

        return Response(encode_jpeg(frame.data, quality=quality), media_type="image/jpeg")

    # -- pipeline control -------------------------------------------------

    @api.get("/api/pipeline/{cam_id}")
    def get_pipeline(cam_id: str) -> list[dict[str, Any]]:
        try:
            return application.camera(cam_id).pipeline.describe()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

    @api.post("/api/pipeline/{cam_id}/{stage_name}")
    def set_params(cam_id: str, stage_name: str, body: ParamUpdate) -> dict[str, Any]:
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        try:
            return cam.pipeline.update_params(stage_name, body.values)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValidationError as exc:
            # Rejected parameters return 422 with the reason, so the UI can say
            # what was wrong instead of silently doing nothing.
            raise HTTPException(422, json.loads(exc.json())) from None

    @api.post("/api/pipeline/{cam_id}/_add")
    def add_stage(cam_id: str, body: StageAdd) -> list[dict[str, Any]]:
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        try:
            cam.pipeline.add(
                StageConfig(type=body.type, name=body.name, params=body.params), body.index
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc)) from None
        return cam.pipeline.describe()

    @api.delete("/api/pipeline/{cam_id}/{stage_name}")
    def remove_stage(cam_id: str, stage_name: str) -> list[dict[str, Any]]:
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        cam.pipeline.remove(stage_name)
        return cam.pipeline.describe()

    # -- camera controls --------------------------------------------------

    @api.post("/api/controls/{cam_id}")
    def set_controls(cam_id: str, body: ControlUpdate) -> dict[str, Any]:
        """Driver-level controls: ExposureTime, AnalogueGain, AeEnable, ...

        Distinct from pipeline parameters on purpose. These change what the
        sensor does; pipeline parameters change what happens to the numbers
        afterwards. Conflating the two is how you end up unable to tell
        whether a change was optical or computational.
        """
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        try:
            cam.source.set_controls(body.controls)
        except Exception as exc:
            raise HTTPException(422, f"{type(exc).__name__}: {exc}") from None
        return {"ok": True, "applied": body.controls}

    # -- capture ----------------------------------------------------------

    @api.post("/api/capture/{cam_id}")
    def capture(cam_id: str, raw: bool = True, tag: str = "still") -> dict[str, Any]:
        try:
            cam = application.camera(cam_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        try:
            return cam.capture_still(raw=raw, tag=tag)
        except Exception as exc:
            log.exception("%s: capture failed", cam_id)
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from None

    @api.post("/api/capture-all")
    def capture_all(raw: bool = True, tag: str = "pair") -> dict[str, Any]:
        """Trigger every camera.

        This is NOT synchronised capture. The requests go out sequentially from
        one thread and the two sensors are free-running, so the frames may be
        tens of milliseconds apart. For stereo or plenoptic work that needs
        real simultaneity, wire the IMX296 XVS sync pins together and drive an
        external trigger -- software cannot fix this.
        """
        out: dict[str, Any] = {}
        for cam_id, cam in application.cameras.items():
            try:
                out[cam_id] = cam.capture_still(raw=raw, tag=tag)
            except Exception as exc:
                out[cam_id] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    return api

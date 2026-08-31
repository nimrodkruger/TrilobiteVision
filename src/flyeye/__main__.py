"""Entry point:  python -m flyeye --config config/pi.yaml"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from .app import Application
from .config import load_config
from .web.server import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flyeye", description="Dual-camera capture and preview rig")
    ap.add_argument("--config", "-c", default="config/pi.yaml", help="path to the rig config YAML")
    ap.add_argument("--host", default=None, help="override server host")
    ap.add_argument("--port", type=int, default=None, help="override server port")
    ap.add_argument("--log-level", default=None)
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config not found: {cfg_path}", file=sys.stderr)
        return 2
    cfg = load_config(cfg_path)

    logging.basicConfig(
        level=(args.log_level or cfg.log_level).upper(),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
    )
    log = logging.getLogger("flyeye")

    application = Application(cfg)
    try:
        application.start()
    except Exception as exc:
        log.error("startup failed: %s", exc)
        return 1

    # Ctrl-C must release the cameras. libcamera does not always recover from a
    # process that dies holding a sensor, and the fix is a reboot.
    def shutdown(signum, _frame):
        log.info("signal %s, shutting down", signum)
        application.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    api = create_app(application)
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    log.info("serving on http://%s:%d", host, port)
    try:
        uvicorn.run(api, host=host, port=port, log_level="warning")
    finally:
        application.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

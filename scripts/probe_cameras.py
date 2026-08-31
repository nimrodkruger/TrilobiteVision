#!/usr/bin/env python3
"""Answer 'what cameras does this Pi actually have, and what can they do?'

Run this first, on the Pi, before touching the config file. It reports what
libcamera enumerates, the sensor modes each camera offers, and grabs one frame
from each to prove the whole path works. Everything the config file needs is
in its output.

    python3 scripts/probe_cameras.py
    python3 scripts/probe_cameras.py --grab   # also save a test frame each
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", action="store_true", help="capture one frame per camera")
    ap.add_argument("--out", default="probe_out", help="where to write grabbed frames")
    args = ap.parse_args()

    try:
        from picamera2 import Picamera2
    except ImportError:
        print(
            "picamera2 not importable.\n"
            "  On the Pi:      sudo apt install -y python3-picamera2\n"
            "  In a venv:      recreate it with  python3 -m venv --system-site-packages ...\n"
            "  On a desktop:   expected -- use the synthetic backend instead.",
            file=sys.stderr,
        )
        return 1

    cams = Picamera2.global_camera_info()
    if not cams:
        print(
            "libcamera sees no cameras.\n"
            "Check, in order:\n"
            "  1. rpicam-hello --list-cameras\n"
            "  2. /boot/firmware/config.txt contains:\n"
            "         camera_auto_detect=0\n"
            "         dtoverlay=imx296,cam0\n"
            "         dtoverlay=imx296,cam1\n"
            "  3. the ribbon cables are seated and the right way round\n"
            "  4. you rebooted after editing config.txt",
            file=sys.stderr,
        )
        return 1

    print(f"{len(cams)} camera(s) enumerated\n")
    for i, info in enumerate(cams):
        print(f"[{i}] " + json.dumps({k: str(v) for k, v in info.items()}, indent=6)[6:-1].strip())
        cam = Picamera2(i)
        try:
            print(f"      sensor_resolution : {cam.sensor_resolution}")
            print(f"      sensor_format     : {cam.sensor_format}")
            print("      modes:")
            for m in cam.sensor_modes:
                print(
                    f"        size={m.get('size')}  fmt={m.get('format')}  "
                    f"bit_depth={m.get('bit_depth')}  fps<={m.get('fps')}"
                )
            print("      controls (name: (min, max, default)):")
            for name in ("ExposureTime", "AnalogueGain", "FrameDurationLimits"):
                if name in cam.camera_controls:
                    print(f"        {name}: {cam.camera_controls[name]}")

            if args.grab:
                out = Path(args.out)
                out.mkdir(parents=True, exist_ok=True)
                cfg = cam.create_still_configuration(raw={})
                cam.configure(cfg)
                cam.start()
                req = cam.capture_request()
                try:
                    main_arr = req.make_array("main")
                    raw_arr = req.make_array("raw")
                    meta = req.get_metadata()
                finally:
                    req.release()
                cam.stop()
                import numpy as np

                np.save(out / f"cam{i}_main.npy", main_arr)
                np.save(out / f"cam{i}_raw.npy", raw_arr)
                (out / f"cam{i}_meta.json").write_text(
                    json.dumps({k: str(v) for k, v in meta.items()}, indent=2)
                )
                print(
                    f"      grabbed: main{main_arr.shape} {main_arr.dtype}, "
                    f"raw{raw_arr.shape} {raw_arr.dtype} -> {out}/"
                )
        finally:
            cam.close()
        print()

    print("Use the index in [brackets] as `index:` in the camera config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

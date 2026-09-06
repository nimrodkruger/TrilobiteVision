"""A live server and a real browser, for the checks a Python test cannot make.

Two bugs in the dashboard each cost an exchange and were invisible to the rest
of the suite: a control that existed but was not on screen (a cached page), and
a `<span>` missing its closing bracket that turned two buttons into attributes.
`tests/test_ui_page.py` now catches the second class by parsing the file. This
directory catches the first: it runs the application, opens the page in
Chromium, and asserts what is actually rendered.

These were ad-hoc scripts during development. Scripts get lost; the review
looked for them and found none in the repository. Now they are tests.

They **skip** when Playwright or a browser is absent, so the ordinary
`pytest -q` on a machine without either stays green. CI installs Chromium, so
they are executed there rather than perpetually skipped -- a suite of tests
that never runs is documentation with a worse format.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("playwright", reason="browser scenarios need playwright")

from playwright.sync_api import sync_playwright  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def browser():
    """Chromium, or a skip that says why.

    `chromium.launch()` raises when the browser has not been downloaded, which
    is the normal state on a fresh checkout. That is a skip, not a failure: the
    developer did not do anything wrong.
    """
    # TRILOBITE_CHROMIUM points at an existing Chromium when Playwright's own
    # download is unavailable or unwanted -- an air-gapped machine, a distro
    # package, a container that already ships one. Without it these skip, which
    # is correct but means the scenarios never actually run there.
    import os
    extra = {"executable_path": os.environ["TRILOBITE_CHROMIUM"]} \
        if os.environ.get("TRILOBITE_CHROMIUM") else {}

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--no-sandbox"], **extra)
        except Exception as exc:                       # not downloaded
            pytest.skip(f"no chromium available: {exc}")
        yield b
        b.close()


@pytest.fixture(scope="session")
def rig(tmp_path_factory):
    """The real application, two synthetic cameras, on a free port.

    Session-scoped: starting cameras and a server per test would dominate the
    runtime, and none of these scenarios mutate anything another one reads.
    """
    import uvicorn

    from trilobite.app import Application
    from trilobite.config import AppConfig, CameraConfig, StageConfig, StorageConfig
    from trilobite.web.server import create_app

    root = tmp_path_factory.mktemp("rig-data")

    def cam(cid: str) -> CameraConfig:
        return CameraConfig(
            cam_id=cid, backend="synthetic", fps=20, label=cid.title(),
            full_resolution=(1456, 1088), preview_resolution=(728, 544),
            synthetic_drift_px=0.0, synthetic_pattern="plenoptic_board",
            synthetic_pitch_px=100.0,
            pipeline=[
                StageConfig(type="stats", name="stats"),
                StageConfig(type="levels", name="display"),
                StageConfig(type="mla_grid_overlay", name="mla",
                            params={"enabled": False, "pitch_px": 100.0}),
                StageConfig(type="checkerboard_presence", name="presence",
                            params={"enabled": True, "min_corners": 20}),
            ],
        )

    port = _free_port()
    app = Application(
        AppConfig(storage=StorageConfig(root=str(root)),
                  cameras=[cam("left"), cam("right")]),
        state_path=None, restore=False,
    )
    app.start()
    server = uvicorn.Server(uvicorn.Config(
        create_app(app), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    import urllib.error
    import urllib.request
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(base + "/api/status", timeout=1).read()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    else:
        pytest.skip("the test server did not come up")

    yield base

    server.should_exit = True
    thread.join(timeout=5)
    app.stop()


@pytest.fixture
def page(browser, rig):
    """A page on the dashboard, with console errors collected.

    Any uncaught page error fails the test at teardown. Both of the bugs this
    directory exists for were the kind that leave the page looking fine and the
    console complaining, so an unread console is most of the value thrown away.
    """
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(rig)
    pg.wait_for_selector("#modes button", timeout=15000)
    pg.wait_for_timeout(1500)          # let the first status poll land
    yield pg
    ctx.close()
    # 409s are deliberate in the orientation-lock scenario and arrive as
    # console errors; anything else is not expected.
    unexpected = [e for e in errors if "409" not in e]
    assert not unexpected, unexpected

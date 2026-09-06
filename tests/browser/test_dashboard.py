"""What the dashboard actually renders, in a real browser.

Each of these was run by hand during development and then lost. They are the
checks that would have caught the two UI bugs the failure log records, plus the
behaviours a Python test can describe but not observe: which tabs stream, which
controls are where, and whether a slider's range follows another parameter.
"""

from __future__ import annotations


def tab(page, label):
    page.locator(f"#modes button:text-is('{label}')").click()
    page.wait_for_timeout(1500)


# -- the tabs --------------------------------------------------------------


def test_the_tabs_are_the_six_expected_in_order(page):
    """Per-camera tabs are generated from /api/cameras and inserted between
    System and Imaging, so this also checks the generation, not just a list."""
    assert page.locator("#modes button").all_text_contents() == [
        "System", "Left", "Right", "Imaging", "Video", "Calibration"]


def test_imaging_is_the_landing_tab_with_the_capture_actions(page):
    assert page.locator("#modes button.on").text_content() == "Imaging"
    # The stereo capture actions belong to the two-camera tab and nowhere else:
    # the space bar fires a stereo set, and a single-sensor screen is the wrong
    # place to offer that.
    assert page.locator("#live-actions").is_visible()
    assert page.locator("#all-raw").is_visible()


def test_only_the_tab_on_screen_streams(page):
    """The connection budget, asserted rather than asserted-about.

    A browser allows about six concurrent HTTP/1.1 connections per origin and
    an MJPEG stream holds one open forever. Two cameras with a preview and
    three tiles each is eight, at which point every button press queues behind
    them and never completes.
    """
    for label, expected in [("Imaging", 2), ("Left", 1), ("System", 0),
                            ("Video", 0), ("Calibration", 0)]:
        tab(page, label)
        assert page.locator("img.view").count() == expected, label


# -- which controls live where ---------------------------------------------


def test_the_setup_stages_are_on_the_camera_tab_only(page):
    tab(page, "Left")
    assert [s.get_attribute("data-stage")
            for s in page.locator(".panel[data-stage]").all()] == ["mla", "presence"]
    # And orientation, which is a per-sensor setup decision.
    assert page.locator(".row[data-key] , .row").filter(has_text="Rotate").count() >= 1


def test_imaging_has_the_capture_stages_and_no_orientation(page):
    """The rule the tab split exists to enforce: no way to turn a frame from
    the screen you capture a session on."""
    tab(page, "Imaging")
    assert [s.get_attribute("data-stage")
            for s in page.locator(".panel[data-stage]").all()] == [
        "stats", "display", "stats", "display"]
    assert page.locator("select").count() == 0, "orientation reached the Imaging tab"


def test_the_rotate_control_is_visible_and_has_a_usable_size(page):
    """The bug this file exists for. The control was present, styled and
    correct, and was not on screen -- the browser was serving a cached page.
    `count()` would have passed; only a rendered size catches it."""
    tab(page, "Left")
    sel = page.locator("select").first
    assert sel.is_visible()
    box = sel.bounding_box()
    assert box["width"] > 80 and box["height"] > 12, box


# -- behaviour -------------------------------------------------------------


def test_enabling_the_grid_locks_the_orientation_controls(page):
    """The lock lives on the server; this is the page agreeing with it without
    a reload."""
    tab(page, "Left")
    assert not page.locator("select").first.is_disabled()

    page.locator('.panel[data-stage="mla"] input[type=checkbox]').first.check()
    page.wait_for_timeout(1500)

    assert page.locator("select").first.is_disabled()
    assert page.locator(".row.locked").count() == 3

    status = page.evaluate(
        "(async()=>{const r=await fetch('/api/orientation/left',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({rotate_deg:90})});return r.status})()")
    assert status == 409

    page.locator('.panel[data-stage="mla"] input[type=checkbox]').first.uncheck()
    page.wait_for_timeout(1200)
    assert not page.locator("select").first.is_disabled()


def test_the_offset_slider_range_follows_the_pitch(page):
    """A bound that depends on another parameter, so it cannot live in the
    JSON schema. Half a pitch each way covers every distinct alignment."""
    tab(page, "Left")
    row = page.locator('.panel[data-stage="mla"] .row[data-key="offset_x"]')
    rng = row.locator("input[type=range]")
    assert (rng.get_attribute("min"), rng.get_attribute("max")) == ("-50", "50")

    box = page.locator('.panel[data-stage="mla"] .row[data-key="pitch_px"] input[type=number]')
    box.fill("40")
    box.press("Enter")
    page.wait_for_timeout(1200)
    assert (rng.get_attribute("min"), rng.get_attribute("max")) == ("-20", "20")


def test_the_camera_tab_puts_the_controls_beside_the_image(page):
    """Stacked is right for two cameras sharing the width and wrong for one
    sensor's alignment, where the image wants the full height AND a dozen
    controls have to be reachable without scrolling the image away."""
    tab(page, "Left")
    img = page.locator("img.view").bounding_box()
    controls = page.locator(".cam.aside > .controls").bounding_box()
    assert controls["x"] > img["x"] + img["width"] - 5, "controls are not beside the image"
    scroll, client = page.evaluate(
        "(() => {const c=document.querySelector('.cam.aside > .controls');"
        "return [c.scrollHeight, c.clientHeight]})()")
    assert scroll <= client + 1, "the control column needs scrolling at this size"


def test_the_page_does_not_report_itself_stale_when_it_is_current(page):
    """The staleness banner compares a hash stamped into the page against the
    file on disk. A page served by the same process must never show it -- a
    false positive here trains the operator to ignore the one warning that
    matters after a deploy."""
    page.wait_for_timeout(1000)
    assert page.locator("#stale").is_hidden()

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


# -- the status poll must age out, not hang (supervisory review R6) ----------


def _isolated(browser, rig):
    """A page in its own context, so an intercepted route cannot leak into the
    session-scoped fixture and so the aborted-fetch console noise is expected
    here rather than a failure everywhere else."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    return ctx, ctx.new_page()


def test_a_status_response_that_never_arrives_still_reads_as_stale(browser, rig):
    """The failure R6 names, and it is the ordinary way a bench rig disappears.

    A half-open TCP connection survives a Pi that has gone away: the request is
    neither answered nor refused. The poll awaited it with no timeout and
    scheduled the next poll only afterwards, so the loop stopped -- and the
    loop was the only thing that set the stale indicator. The page went quiet
    in exactly the case it exists to shout in, while MJPEG carried on showing
    the last frame it received.

    Asserted against a route that swallows the request entirely, because an
    error response is a different and much easier case.
    """
    ctx, page = _isolated(browser, rig)
    try:
        page.goto(rig)
        page.wait_for_selector("#modes button", timeout=15000)
        page.wait_for_timeout(1500)
        assert page.locator("#poll-dead").is_hidden(), "healthy rig, no banner"

        # Swallow every subsequent status request. Never fulfilled, never
        # aborted: the page must not depend on either happening.
        page.route("**/api/status", lambda route: None)

        page.wait_for_selector("#poll-dead:visible", timeout=15000)
        assert "no reply from the rig" in page.locator("#poll-dead").inner_text()
    finally:
        ctx.close()


def test_the_stale_banner_counts_up_rather_than_freezing(browser, rig):
    """It is driven by elapsed time, so it keeps getting worse while the rig is
    away. A banner stuck on one number reads as a stale banner rather than as a
    live measurement of how long the rig has been gone."""
    ctx, page = _isolated(browser, rig)
    try:
        page.goto(rig)
        page.wait_for_selector("#modes button", timeout=15000)
        page.wait_for_timeout(1500)
        page.route("**/api/status", lambda route: None)
        page.wait_for_selector("#poll-dead:visible", timeout=15000)

        first = page.locator("#poll-dead").inner_text()
        page.wait_for_timeout(3000)
        assert page.locator("#poll-dead").inner_text() != first
    finally:
        ctx.close()


def test_the_poll_recovers_when_the_rig_answers_again(browser, rig):
    """The other half. A banner that cannot clear is a banner that gets
    ignored, and the poll loop must still be alive after the timeouts."""
    ctx, page = _isolated(browser, rig)
    try:
        page.goto(rig)
        page.wait_for_selector("#modes button", timeout=15000)
        page.wait_for_timeout(1500)
        page.route("**/api/status", lambda route: None)
        page.wait_for_selector("#poll-dead:visible", timeout=15000)

        page.unroute("**/api/status")
        page.wait_for_selector("#poll-dead", state="hidden", timeout=15000)
    finally:
        ctx.close()


# -- the recording tab ------------------------------------------------------
#
# These exist because the Video tab's whole job is to put consequences in front
# of a person before they press Start, and "the number is on screen" is not
# something a Python test can check. The three below are the three claims the
# page makes that would be worst to get wrong.


def test_the_video_tab_renders_without_a_measurement(page):
    tab(page, "Video")
    assert page.locator("#rec-preflight").is_visible()
    assert page.locator("#rec-plans").is_visible()
    assert page.locator("#rec-live").is_visible()
    # Until the target has been measured the page says so, in the badge rather
    # than only in the text: a stale or absent measurement that looks like a
    # fresh one is how a configuration gets chosen off the wrong number.
    assert "NOT MEASURED" in page.locator("#rec-preflight").inner_text()


def test_an_unmeasured_target_shows_unknown_loss_and_never_zero(page):
    """The one answer this page must never give by omission."""
    tab(page, "Video")
    text = page.locator("#rec-plans").inner_text()
    assert "unknown" in text
    assert "0%" not in text


def test_a_lossy_configuration_is_offered_with_its_consequence_attached(page):
    tab(page, "Video")
    rows = page.locator("#rec-plans").inner_text()
    # Synthetic cameras advertise no 8-bit mode, so only the exact format is
    # offered here -- which is itself the rule under test: a rung that was not
    # established is not offered.
    assert "raw16" in rows
    assert "science" in rows


def test_the_internal_storage_override_is_never_pre_ticked(page):
    """The point of the override is that it is a deliberate act. A remembered
    preference is the opposite of one."""
    tab(page, "Video")
    assert page.locator("#rec-internal").is_checked() is False


def test_arming_a_burst_reports_the_duration_before_start(page):
    tab(page, "Video")
    page.locator("button[data-kind='burst']").click()
    page.wait_for_timeout(300)
    page.locator("#rec-arm").click()
    page.wait_for_selector("#rec-arm-note:not(:empty)", timeout=20000)
    note = page.locator("#rec-arm-note").inner_text()
    assert "armed" in note and "frames per head" in note


def test_an_unsaved_burst_says_in_words_that_ram_is_not_storage(page):
    tab(page, "Video")
    page.locator("button[data-kind='burst']").click()
    page.wait_for_timeout(300)
    page.locator("#rec-arm").click()
    page.wait_for_selector("#rec-arm-note:not(:empty)", timeout=20000)
    page.locator("#rec-start").click()
    page.wait_for_timeout(1200)
    page.locator("#rec-stop").click()
    page.wait_for_timeout(1500)
    live = page.locator("#rec-live").inner_text()
    assert "NOT SAVED" in live
    assert "lost on a process restart" in live
    # And the way out of it is on screen, with Discard distinct from Save.
    assert page.locator("#rec-save").is_visible()
    assert page.locator("#rec-discard").is_visible()

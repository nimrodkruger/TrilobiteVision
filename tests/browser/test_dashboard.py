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
    # Video holds two: an operator frames the shot and judges exposure before
    # pressing Start, and the first version of that tab showed numbers only,
    # so the only way to see the scene was to leave it. Two is the same as
    # Imaging and the cap is unchanged -- no tab holds more.
    for label, expected in [("Imaging", 2), ("Left", 1), ("System", 0),
                            ("Video", 2), ("Calibration", 0)]:
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
    assert page.locator("#rec-actions").is_visible()
    assert page.locator("#rec-live").is_visible()
    assert page.locator("#rec-preflight").is_visible()
    # Until the target has been measured the page says so, on the ACTION row
    # rather than only in the Target panel: the warning has to be where the
    # controls it affects are, not behind a heading somebody has to expand.
    assert "UNMEASURED" in page.locator("#rec-actions").inner_text()


def test_an_unmeasured_target_shows_unknown_loss_and_never_zero(page):
    """The one answer this page must never give by omission."""
    tab(page, "Video")
    # Lower-cased: the badge is styled with uppercase, which is a CSS choice
    # and not something a test should depend on either way.
    text = page.locator("#rec-actions").inner_text().lower()
    assert "unknown" in text
    assert "0%" not in text


def test_a_lossy_configuration_is_offered_with_its_consequence_attached(page):
    """A lossy rung is offered, and never without what it costs beside it."""
    tab(page, "Video")
    depths = page.locator("#rec-format button")
    labels = [b.inner_text() for b in depths.all()]
    assert any("lossy" in t.lower() for t in labels), labels
    # Choosing it changes the stated validity ceiling on the same row.
    before = page.locator("#rec-consequence").inner_text().lower()
    assert "science" in before
    for b in depths.all():
        if "lossy" in b.inner_text().lower():
            b.click()
            break
    page.wait_for_timeout(300)
    assert "diagnostic" in page.locator("#rec-consequence").inner_text().lower()


def test_the_selected_format_and_rate_are_shown_as_selected(page):
    """Conventional selection styling, the same `on` class the rest of the
    dashboard uses -- not a second vocabulary for the same idea."""
    tab(page, "Video")
    assert page.locator("#rec-format button.on").count() == 1
    assert page.locator("#rec-rate-pick button.on").count() == 1
    assert page.locator("#rec-mode button.on").count() == 1

    rates = page.locator("#rec-rate-pick button").all()
    assert len(rates) > 1, "only one frame rate on offer"
    target = next(b for b in rates if "on" not in (b.get_attribute("class") or ""))
    label = target.inner_text()
    target.click()
    page.wait_for_timeout(300)
    assert page.locator("#rec-rate-pick button.on").count() == 1
    assert page.locator("#rec-rate-pick button.on").inner_text() == label


def test_the_video_tab_shows_the_live_image_before_anything_is_armed(page):
    tab(page, "Video")
    assert page.locator(".panes img.view").count() == 2
    for img in page.locator(".panes img.view").all():
        assert "/stream/" in (img.get_attribute("src") or "")


def test_the_internal_storage_override_is_never_pre_ticked(page):
    """The point of the override is that it is a deliberate act. A remembered
    preference is the opposite of one."""
    tab(page, "Video")
    assert page.locator("#rec-internal").is_checked() is False


def test_arming_a_burst_reports_the_duration_before_start(page):
    tab(page, "Video")
    page.locator("#rec-mode button[data-kind='burst']").click()
    page.wait_for_timeout(300)
    page.locator("#rec-arm").click()
    page.wait_for_selector("#rec-arm-note:not(:empty)", timeout=20000)
    note = page.locator("#rec-arm-note").inner_text()
    assert "armed" in note and "frames per head" in note


def test_an_unsaved_burst_says_in_words_that_ram_is_not_storage(page):
    tab(page, "Video")
    page.locator("#rec-mode button[data-kind='burst']").click()
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

    # Then take it: an unsaved burst blocks arming anything else, deliberately,
    # so a test that leaves one behind breaks whatever runs after it on this
    # module-scoped rig. Clearing it here also exercises the way out.
    # Two clicks: discarding frames that exist nowhere else is deliberate, and
    # the first click only arms the button. It is not a native `confirm()` --
    # that blocks the page and cannot be driven from here, which left the one
    # destructive path in the UI untested.
    page.locator("#rec-discard").click()
    page.wait_for_timeout(300)
    assert "click again" in page.locator("#rec-discard").inner_text().lower()
    page.locator("#rec-discard").click()
    page.wait_for_timeout(800)
    page.locator("#rec-free").click()
    page.wait_for_function(
        """() => !(document.querySelector('#rec-live') || {})
                   .innerText?.includes('NOT SAVED')""",
        timeout=20000)


def test_the_recording_indicator_lights_and_then_clears_by_itself(page):
    """The requirement that costs something to get right.

    An indicator wired to the Start and Stop buttons is correct for the one
    path where the operator stops the recording, and wrong for every other
    way a recording ends -- the duration limit here, and in the field also the
    drop ceiling, a full disk and a pulled drive. None of those press Stop.

    So this arms a two-second recording, starts it, watches the indicator
    light, and then waits past the limit WITHOUT touching Stop. The indicator
    must go out on its own.
    """
    tab(page, "Video")
    page.locator("#rec-mode button[data-kind='continuous']").click()
    page.wait_for_timeout(200)
    page.fill("#rec-duration", "2")
    # The browser rig's target is the internal disk, so the arm is refused
    # without the override -- which is the storage protection doing its job.
    # Ticking it here is also the only place the override is exercised against
    # a continuous recording rather than a burst flush.
    page.check("#rec-internal")
    page.locator("#rec-arm").click()
    page.wait_for_selector("#rec-arm-note:not(:empty)", timeout=20000)
    assert "armed" in page.locator("#rec-arm-note").inner_text()

    # Not lit by arming: armed is not recording, and a light that cannot tell
    # the difference is worse than none.
    assert page.locator("#rec-on-air.live").count() == 0

    page.locator("#rec-start").click()
    page.wait_for_selector("#rec-on-air.live", timeout=20000)
    assert "REC" in page.locator("#rec-on-air").inner_text()
    # The tab itself carries it too, so it is visible from another tab.
    assert "live" in (page.locator("#mode-video").get_attribute("class") or "")

    # Past the two seconds, with nothing pressed.
    page.wait_for_selector("#rec-on-air.live", state="detached", timeout=25000)
    assert page.locator("#rec-on-air").inner_text().strip() == ""
    assert "live" not in (page.locator("#mode-video").get_attribute("class") or "")
    assert "recording" not in (
        page.locator("body").get_attribute("class") or "")
    # The live panel has its own one-second refresh and the stop is finished
    # on the rig's storage timer, so it trails the indicator slightly. The
    # indicator going out first is the correct order -- it says "not
    # capturing", which became true before the chunks were closed.
    page.wait_for_function(
        """() => (document.querySelector('#rec-live') || {}).innerText
                 ?.includes('requested duration reached')""",
        timeout=30000)

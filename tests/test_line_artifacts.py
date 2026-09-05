"""The line-artifact diagnostic actually discriminates.

`scripts/diagnose_lines.py` exists to answer one question -- are these lines the
sensor, or the link? -- and its whole value is that the answer is different for
different causes. A diagnostic that says "row noise" to everything is worse than
none, because it ends the investigation. So each cause is synthesised here with
a known signature and the tool has to tell them apart.

The three synthetic cases are built to be as similar as possible to each other
except in the one property being measured: same scene, same amplitude, same
number of affected rows. Anything that separates them has to be separating the
mechanism.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location(
    "diagnose_lines",
    Path(__file__).resolve().parent.parent / "scripts" / "diagnose_lines.py",
)
dl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dl)

H, W = 240, 320
BAD_ROWS = (40, 137, 200)


def scene() -> np.ndarray:
    """Something with real horizontal structure, so the tool has to work for it.

    A flat field would make every test here pass trivially: any row deviation
    at all would be the fault. The sinusoid down the frame is the part that
    `row_offsets` has to subtract before it can see anything.
    """
    x = np.arange(W)
    y = np.arange(H)
    return (128.0 + 60 * np.sin(x / 17.0)[None, :] + 20 * np.sin(y / 53.0)[:, None])


def frames_random(n=4, sigma=4.0, seed=0):
    rng = np.random.default_rng(seed)
    s = scene()
    return [s + rng.normal(0, sigma, (H, 1)) + rng.normal(0, 1, (H, W)) for _ in range(n)]


def frames_fixed(n=4, sigma=4.0, seed=1):
    rng = np.random.default_rng(seed)
    s, pattern = scene(), np.random.default_rng(99).normal(0, sigma, (H, 1))
    return [s + pattern + rng.normal(0, 1, (H, W)) for _ in range(n)]


def frames_displaced(n=4, shift=3, seed=2):
    """Rows whose pixels are all present but moved -- a dropped byte on the link."""
    rng = np.random.default_rng(seed)
    s = scene()
    out = []
    for _ in range(n):
        a = s.copy()
        for r in BAD_ROWS:
            a[r, :-shift] = s[r, shift:]
        out.append(a + rng.normal(0, 1, (H, W)))
    return out


# -- row offsets: the scene must not be mistaken for the sensor ---------


def test_a_clean_frame_reports_no_row_offset():
    """The floor. A frame with strong horizontal structure and no row fault has
    to read as zero, or every measurement above it is meaningless."""
    o = dl.row_offsets(scene())
    assert np.abs(o).max() < 0.05


def test_a_row_offset_is_recovered_at_about_the_amplitude_injected():
    """Within a few percent, and biased low, for a reason worth stating.

    The baseline is a 9-row median of the row-mean profile, and the window
    centred on the affected row contains that row. One outlier in nine does not
    move a median much, but it moves it a little and always towards the spike,
    so a measured offset is a slight underestimate. That is the right way round
    for a diagnostic -- it never invents amplitude -- and it is why the tests
    above are about correlation and displacement rather than about absolute
    counts.
    """
    s = scene()
    s[137] += 7.0
    o = dl.row_offsets(s)
    assert o[137] == pytest.approx(7.0, rel=0.1)
    assert o[137] < 7.0, "the median baseline biases the estimate low, not high"
    assert np.abs(np.delete(o, 137)).max() < 1.5


# -- displacement: the test that separates the link from the sensor -----


def test_a_displaced_row_is_found_at_the_shift_it_was_given():
    img = frames_displaced(n=1, shift=3)[0]
    s, gain = dl.best_shift(img, BAD_ROWS[1])
    assert s == -3, f"found {s:+d}, injected -3"
    assert gain > 0.5, gain


def test_a_row_with_a_dc_offset_is_not_called_displaced():
    """The false positive that would matter: row noise diagnosed as a cable
    fault sends the operator to buy hardware. best_shift removes each row's
    mean before comparing, which is what makes an offset invisible to it."""
    img = scene()
    img[BAD_ROWS[1]] += 12.0
    _, gain = dl.best_shift(img, BAD_ROWS[1])
    assert gain < 0.25, gain


def test_roughness_finds_a_displaced_row_that_the_offset_test_misses():
    """Why there are two probes rather than one.

    A displaced row has very nearly the right MEAN -- the same pixels are in
    it, just moved -- so it does not appear among the row-offset outliers at
    all. Probing by offset alone would have looked at the wrong rows and
    reported no displacement, which is the exact wrong answer.
    """
    img = frames_displaced(n=1)[0]
    offsets = np.abs(dl.row_offsets(img))
    rough = dl.row_roughness(img)
    assert set(np.argsort(rough)[-3:]) & set(BAD_ROWS), "roughness misses them"
    by_offset = set(int(v) for v in np.argsort(offsets)[-3:])
    assert not by_offset & set(BAD_ROWS), \
        "the offset probe found them after all -- this test no longer tests anything"


# -- fixed pattern versus random, which needs more than one frame -------


def test_fixed_pattern_row_noise_correlates_across_frames():
    o = [dl.row_offsets(f) for f in frames_fixed()]
    pairs = [float(np.corrcoef(o[i], o[j])[0, 1])
             for i in range(len(o)) for j in range(i + 1, len(o))]
    assert np.mean(pairs) > 0.9


def test_random_row_noise_does_not_correlate_across_frames():
    o = [dl.row_offsets(f) for f in frames_random()]
    pairs = [float(np.corrcoef(o[i], o[j])[0, 1])
             for i in range(len(o)) for j in range(i + 1, len(o))]
    assert abs(np.mean(pairs)) < 0.3


# -- end to end, on the verdict text -------------------------------------


def _verdict(tmp_path, frames, name):
    d = tmp_path / name
    d.mkdir()
    for k, f in enumerate(frames):
        np.save(d / f"raw_left_{k:06d}.npy", np.clip(f, 0, 255).astype(np.uint8))
    return sorted(d.glob("*.npy"))


def test_the_three_causes_get_three_different_verdicts(tmp_path, capsys, monkeypatch):
    """The claim the whole script is for, asserted on what it actually prints.

    dmesg is stubbed out: on a desktop it has nothing to say, and on the Pi it
    might, and either way this test is about the inference from pixels.
    """
    monkeypatch.setattr(dl, "dmesg_csi", lambda: [])

    dl.report(_verdict(tmp_path, frames_random(), "random"))
    random_out = capsys.readouterr().out
    dl.report(_verdict(tmp_path, frames_fixed(), "fixed"))
    fixed_out = capsys.readouterr().out
    dl.report(_verdict(tmp_path, frames_displaced(), "shift"))
    shift_out = capsys.readouterr().out

    assert "do not repeat across frames" in random_out
    assert "MOVED" not in random_out

    assert "repeat across frames" in fixed_out
    assert "dark frame" in fixed_out
    assert "MOVED" not in fixed_out

    assert "MOVED" in shift_out
    assert "CSI link" in shift_out
    for r in BAD_ROWS:
        assert str(r) in shift_out


def test_a_kernel_error_outranks_the_pixel_inference(tmp_path, capsys, monkeypatch):
    """Ordering, not just presence. Everything else here is inference from
    pixels; the driver counting real errors is evidence, and has to be said
    first so it is not buried under a paragraph about read noise."""
    monkeypatch.setattr(dl, "dmesg_csi", lambda: ["rp1-cfe: csi2 fifo overflow"])
    dl.report(_verdict(tmp_path, frames_random(), "kernel"))
    out = capsys.readouterr().out
    body = out.split("reading:")[1]
    assert body.index("camera-link errors") < body.index("row noise")

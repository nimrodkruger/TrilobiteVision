"""The dashboard is one HTML file, and nothing else checks that it parses.

Two bugs in this file have now cost an exchange each, and both were invisible
to every test that existed:

  * a rotate control that was present, styled and correct, on a page the
    browser was serving from its own cache;
  * a `<span id="live-actions"` missing its closing bracket, which made the
    browser read the two capture buttons as ATTRIBUTES of the span. The page
    rendered, the script loaded, and `$("#all-raw")` was null.

Neither is a Python bug and neither shows up in a Python test suite unless
something goes looking. These are the cheap checks that would have caught
them: the file parses, the elements the boot script reaches for exist, and the
server serves it revalidating and stamped.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "src/trilobite/web/static/index.html"

# The ids the script resolves at load time, before any tab is built. Anything
# a tab creates for itself is not here -- this is the boot contract.
BOOT_IDS = [
    "session", "health", "health-warn", "stale",
    "modes", "live-actions", "all-raw", "all-view",
    "quick-record", "quick-tally", "cams", "log",
]

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Structure(HTMLParser):
    """Ids, element nesting, and where each id sits in the tree."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ids: dict[str, list[str]] = {}       # id -> ancestor tags
        self.unclosed: list[str] = []
        self.attrs_seen: dict[str, set[str]] = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.ids[d["id"]] = list(self.stack)
        self.attrs_seen.setdefault(tag, set()).update(d)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            self.unclosed.append(tag)


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def structure(page: str) -> Structure:
    # The inline <script> is full of `<` and `>` in comparisons and arrow
    # functions, which HTMLParser would try to read as markup. The browser
    # does not, because CDATA rules apply inside a script element -- so it is
    # stripped here for the same reason.
    stripped = re.sub(r"<script>.*?</script>", "<script></script>", page, flags=re.S)
    s = Structure()
    s.feed(stripped)
    return s


def test_the_page_parses_with_every_tag_closed(structure: Structure):
    assert structure.unclosed == []
    assert structure.stack == [], f"still open at EOF: {structure.stack}"


@pytest.mark.parametrize("wanted", BOOT_IDS)
def test_the_boot_script_finds_the_element_it_asks_for(structure: Structure, wanted):
    assert wanted in structure.ids, (
        f"#{wanted} is not an element in the page. If it is in the source, the "
        f"most likely cause is an unclosed tag above it -- the browser will "
        f"have read it as an attribute of whatever came before."
    )


def test_the_capture_buttons_are_inside_the_actions_span(structure: Structure):
    """The exact shape of the bug: `<span id="live-actions"` without its
    bracket, which turned the two buttons into attributes of the span. The ids
    still 'appear' in the file, so a grep would have passed."""
    for btn in ("all-raw", "all-view", "quick-record"):
        assert "span" in structure.ids[btn], f"#{btn} is not nested inside a span"
    assert "button" not in structure.attrs_seen.get("span", set()), \
        "a <button> was parsed as an attribute of a <span>"


def test_every_tab_in_the_script_has_a_builder(page: str):
    """A tab whose `build` is missing renders an empty screen and logs nothing
    useful. Cheap to check by reading the table the tabs are declared in."""
    table = re.search(r"const TABS = \[(.*?)\n\];", page, re.S)
    assert table, "the TABS table has moved or changed shape"
    ids = re.findall(r'id:\s*"([a-z]+)"', table.group(1))
    assert ids == ["system", "imaging", "video", "cal"], ids
    for entry in re.finditer(r'id:\s*"([a-z]+)"[^}]*?build:\s*([A-Za-z(]+)',
                             table.group(1), re.S):
        assert entry.group(2), f"tab {entry.group(1)} has no builder"


def test_the_page_carries_a_build_placeholder_for_the_server_to_stamp(page: str):
    """The staleness check compares this against the file on disk. Left
    unsubstituted -- an older server, or a renamed placeholder -- the page must
    say nothing rather than claim to be out of date forever."""
    assert page.count("__UI_BUILD__") == 1
    assert 'UI_BUILD.startsWith("__")' in page, \
        "the guard against an unsubstituted placeholder has gone"

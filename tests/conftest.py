"""Test-suite guardrails.

One rule, enforced rather than remembered: **no test writes outside its own
temporary directory.**

This is not tidiness. The suite was run on a fresh machine during an external
review and seven tests failed — not because the code was wrong, but because
they constructed a `SessionWriter` with the default storage root and tried to
create `~/trilobite-data` in a home directory that would not have it. The
failures said nothing about the software and cost the reviewer time working out
that they were noise. On a developer's own machine the same tests pass *and*
silently scatter session directories through their home folder.

`AppConfig()` with no storage block defaults to `~/trilobite-data`, so the
mistake is one omitted line in a fixture and invisible when it works. The
autouse fixture below makes it loud instead: any writer rooted outside the
test's `tmp_path` fails the test that built it, naming the path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_writes_outside_tmp(tmp_path, monkeypatch, request):
    """Fail any test that points a SessionWriter outside its tmp_path.

    Wraps the constructor rather than watching the filesystem: it names the
    offending test and the path it asked for, at the moment of the mistake,
    instead of leaving a directory behind for someone to find later.

    Opt out with `@pytest.mark.allow_home_writes` for a test that is genuinely
    about the default location. Nothing uses it today; it exists so that the
    guard can be escaped deliberately rather than deleted.
    """
    if request.node.get_closest_marker("allow_home_writes"):
        return

    from trilobite.storage import writer as writer_module

    real_init = writer_module.SessionWriter.__init__
    allowed = (Path(tmp_path).resolve(), Path(os.environ.get("TMPDIR", "/tmp")).resolve())

    def guarded(self, cfg, root, *a, **kw):
        resolved = Path(os.path.expanduser(str(root))).resolve()
        if not any(resolved == p or p in resolved.parents for p in allowed):
            raise AssertionError(
                f"{request.node.nodeid} builds a SessionWriter rooted at "
                f"{resolved}, which is outside its tmp_path ({tmp_path}). Pass "
                f"StorageConfig(root=str(tmp_path / 'data')) — an AppConfig "
                f"with no storage block defaults to ~/trilobite-data and will "
                f"write to the developer's home directory."
            )
        return real_init(self, cfg, root, *a, **kw)

    monkeypatch.setattr(writer_module.SessionWriter, "__init__", guarded)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_home_writes: this test is about the default storage location",
    )

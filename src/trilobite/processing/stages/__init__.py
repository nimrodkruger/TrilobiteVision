"""Auto-import every stage module so decorators run.

Drop a new .py file in this directory and its @register'd stages become
available to the config file and the UI with no other edit.
"""

from __future__ import annotations

import importlib
import pkgutil

for _mod in pkgutil.iter_modules(__path__):
    if not _mod.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_mod.name}")

del importlib, pkgutil

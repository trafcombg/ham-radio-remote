"""Where to find editable, non-bundled files (config.json and friends) —
next to the installed .exe when frozen (PyInstaller), or next to the
calling module in dev mode. Bundled resources (server/web/*.html) don't
need this — PyInstaller keeps __file__-relative lookups working for those."""

import sys
from pathlib import Path


def app_dir(dev_file: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(dev_file).parent

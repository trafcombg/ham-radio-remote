"""Checks GitHub releases for a newer version and, when the user
confirms, silently downloads and runs the new installer — no manual
"find the installer, click through it" required. The installer itself
(packaging/installer/*.iss) closes the running app (AppMutex) before
replacing files, so this only needs to launch it and exit.

GITHUB_REPO is a placeholder — set it to the real "owner/repo" once this
project has actual GitHub releases before relying on this.
"""

import json
import logging
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

log = logging.getLogger("updater")

GITHUB_REPO = "your-org/ham-radio-remote"  # TODO: set to the real repo before shipping
CHECK_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _parse_version(v: str) -> tuple:
    nums = tuple(int(p) for p in re.findall(r"\d+", v)[:3])
    return nums or (0,)


def check_for_update(current_version: str, asset_name_contains: str, timeout: float = 5.0) -> dict | None:
    """Returns {"version": ..., "download_url": ...} if a newer release
    with a matching installer asset is found, else None. Never raises —
    a network hiccup or unexpected response just means "no update found"."""
    try:
        with urllib.request.urlopen(CHECK_URL, timeout=timeout) as resp:
            data = json.load(resp)
        latest = data["tag_name"].lstrip("v")
        if _parse_version(latest) <= _parse_version(current_version):
            return None
        for asset in data.get("assets", []):
            if asset_name_contains in asset["name"]:
                return {"version": latest, "download_url": asset["browser_download_url"]}
        return None
    except Exception:
        log.debug("update check failed (non-fatal)", exc_info=True)
        return None


def download_and_run_installer(download_url: str, timeout: float = 60.0) -> bool:
    """Downloads the new installer to a temp file and launches it in
    silent/unattended mode. Call this right before your own app exits —
    the installer's AppMutex handling takes care of waiting for it."""
    try:
        tmp = Path(tempfile.gettempdir()) / "ham-radio-remote-update.exe"
        urllib.request.urlretrieve(download_url, tmp)
        subprocess.Popen([str(tmp), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        return True
    except Exception:
        log.exception("update download/launch failed")
        return False


if __name__ == "__main__":
    import io
    import unittest.mock as mock

    assert _parse_version("1.2.3") == (1, 2, 3)
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("garbage") == (0,)
    assert _parse_version("1.2.3") > _parse_version("1.2.2")
    assert _parse_version("1.10.0") > _parse_version("1.9.0")  # numeric, not lexicographic

    def _fake_response(payload: dict):
        body = json.dumps(payload).encode()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(body)

    with mock.patch("urllib.request.urlopen", return_value=_fake_response({
        "tag_name": "v2.0.0",
        "assets": [{"name": "HAM-Radio-Client-Setup.exe", "browser_download_url": "https://example.invalid/client.exe"}],
    })):
        result = check_for_update("1.0.0", "Client-Setup")
        assert result == {"version": "2.0.0", "download_url": "https://example.invalid/client.exe"}

        assert check_for_update("3.0.0", "Client-Setup") is None  # already newer than "latest"

        result_none = check_for_update("1.0.0", "no-such-asset")
        assert result_none is None  # newer version exists but no matching asset

    with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")):
        assert check_for_update("1.0.0", "Client-Setup") is None  # never raises

    print("updater.py: ok")

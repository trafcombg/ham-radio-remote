"""Encrypts the saved login password at rest using Windows DPAPI
(CryptProtectData/CryptUnprotectData via ctypes — no new dependency), so
config.json holds ciphertext instead of a plaintext password.

ponytail: DPAPI ties the ciphertext to the Windows user account (and
usually machine) that encrypted it. Copy config.json to another PC or
user and decrypt() just returns "" — the login dialog falls back to an
empty password field instead of crashing, so re-entering it is the
"recovery" path, not a bug.
"""

import base64
import ctypes
import ctypes.wintypes as wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    data_in = _to_blob(plaintext.encode("utf-8"))
    data_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(data_out.pbData, data_out.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        raw = base64.b64decode(ciphertext)
    except (ValueError, ctypes.ArgumentError):
        return ""
    data_in = _to_blob(raw)
    data_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(data_in), None, None, None, None, 0, ctypes.byref(data_out)
    )
    if not ok:
        return ""  # different machine/user, or corrupted — caller re-prompts
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


if __name__ == "__main__":
    assert decrypt("") == ""
    assert decrypt("not-valid-base64!!!") == ""

    enc = encrypt("hunter2")
    assert enc != "hunter2" and enc != ""
    assert decrypt(enc) == "hunter2"

    # tampered ciphertext must fail closed, not raise
    tampered = base64.b64encode(base64.b64decode(enc)[:-1] + b"\x00").decode()
    assert decrypt(tampered) == ""

    print("credential_store.py: ok")

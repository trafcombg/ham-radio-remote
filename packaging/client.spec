# PyInstaller spec for the client. Build from the repo root:
#   pyinstaller packaging/client.spec
# Verified: this produces a working dist/HAM-Radio-Client/ that starts
# Qt + the background asyncio loop correctly when config.json is placed
# next to the exe.

from pathlib import Path

repo_root = Path(SPECPATH).parent

a = Analysis(
    [str(repo_root / "client" / "main.py")],
    pathex=[str(repo_root)],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HAM-Radio-Client",
    console=False,  # windowed GUI app, no console
)
COLLECT(exe, a.binaries, a.datas, name="HAM-Radio-Client")

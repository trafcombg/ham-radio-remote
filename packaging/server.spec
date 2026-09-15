# PyInstaller spec for the server. Build from the repo root:
#   pyinstaller packaging/server.spec
# Verified: this produces a working dist/HAM-Radio-Server/ that serves
# the admin panel correctly when config.json is placed next to the exe.

from pathlib import Path

repo_root = Path(SPECPATH).parent

a = Analysis(
    [str(repo_root / "server" / "main.py")],
    pathex=[str(repo_root)],
    datas=[(str(repo_root / "server" / "web"), "server/web")],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HAM-Radio-Server",
    console=True,  # the operator watches this window — server starts manually, not as a service
)
COLLECT(exe, a.binaries, a.datas, name="HAM-Radio-Server")

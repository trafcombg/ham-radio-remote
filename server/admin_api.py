"""FastAPI admin panel — remote radio configuration with device pickers,
Test/Verify, and hot-reload without restarting the server. Bound to
0.0.0.0 (see server/config.json "web") so it's reachable from any
computer on the LAN, not just the server machine.

Regular users never see this — they only ever use the desktop client
(Phase 1/2). Every route here requires an admin session cookie.
"""

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server.cat_bridge import probe_serial_port
from server.db import PostgresDb
from server.device_registry import (
    AmbiguousDeviceError,
    DeviceNotFoundError,
    resolve_serial_port,
    scan_audio_devices,
    scan_serial_devices,
)
from server.radio_manager import RadioBusyError, RadioManager
from server.web_auth import create_session_cookie, read_session_cookie

log = logging.getLogger("admin_api")
WEB_DIR = Path(__file__).with_name("web")

app = FastAPI(title="HAM Radio Remote — Admin")


def get_manager(request: Request) -> RadioManager:
    return request.app.state.manager


def get_db(request: Request):
    return request.app.state.db


def current_user(request: Request):
    return read_session_cookie(request.cookies.get("session"))


def require_admin(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=401, detail="admin login required")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


class RadioCatConfig(BaseModel):
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    location: str | None = None
    serial_port: str | None = None
    baud: int = 19200
    tcp_port: int


class RadioAudioConfig(BaseModel):
    name_contains: str | None = None
    endpoint_id: str | None = None
    udp_port: int


class RadioPttConfig(BaseModel):
    method: str  # civ | rts | dtr
    civ_address: int | None = None
    serial_port: str | None = None


class RadioConfigRequest(BaseModel):
    name: str
    model: str
    cat: RadioCatConfig
    control_port: int
    audio: RadioAudioConfig
    ptt: RadioPttConfig
    force: bool = False


class TestRequest(BaseModel):
    cat: RadioCatConfig


def _to_cfg_dict(body: RadioConfigRequest) -> dict:
    return {
        "name": body.name,
        "model": body.model,
        "cat": body.cat.model_dump(),
        "control_port": body.control_port,
        "audio": body.audio.model_dump(),
        "ptt": body.ptt.model_dump(),
    }


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/login")
async def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.post("/api/login")
async def login(body: LoginRequest, request: Request, response: Response):
    db = get_db(request)
    if not isinstance(db, PostgresDb):
        raise HTTPException(status_code=503, detail="PostgreSQL не е конфигуриран (db.dsn)")
    user = await db.authenticate(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="грешно потребителско име или парола")
    token = create_session_cookie(user["username"], user["is_admin"])
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return {"ok": True, "is_admin": user["is_admin"]}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    return {"user": current_user(request)}


@app.get("/api/devices")
async def list_devices(admin=Depends(require_admin)):
    return {
        "serial": [d.__dict__ for d in scan_serial_devices()],
        "audio": [d.__dict__ for d in scan_audio_devices()],
    }


@app.get("/api/radios")
async def list_radios(request: Request, admin=Depends(require_admin)):
    manager = get_manager(request)
    configs = await manager.list_configs()
    status = manager.status()
    for cfg in configs:
        cfg["status"] = status.get(cfg["name"], {"busy_by": None, "control_clients": 0})
    return {"radios": configs}


@app.post("/api/radios")
async def create_radio(body: RadioConfigRequest, request: Request, admin=Depends(require_admin)):
    manager = get_manager(request)
    cfg = _to_cfg_dict(body)
    try:
        await manager.reload_radio(cfg, force=True)  # a brand-new radio can't already be busy
    except (AmbiguousDeviceError, DeviceNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


@app.put("/api/radios/{name}")
async def update_radio(name: str, body: RadioConfigRequest, request: Request, admin=Depends(require_admin)):
    if body.name != name:
        raise HTTPException(status_code=400, detail="name mismatch")
    manager = get_manager(request)
    cfg = _to_cfg_dict(body)
    try:
        await manager.reload_radio(cfg, force=body.force)
    except RadioBusyError as e:
        raise HTTPException(status_code=409, detail={"busy_by": e.holder})
    except (AmbiguousDeviceError, DeviceNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


@app.delete("/api/radios/{name}")
async def delete_radio(name: str, request: Request, force: bool = False, admin=Depends(require_admin)):
    manager = get_manager(request)
    try:
        await manager.remove_radio(name, force=force)
    except RadioBusyError as e:
        raise HTTPException(status_code=409, detail={"busy_by": e.holder})
    return {"ok": True}


@app.post("/api/radios/{name}/test")
async def test_radio(name: str, body: TestRequest, request: Request, admin=Depends(require_admin)):
    manager = get_manager(request)
    bridge = manager.bridges.get(name)
    cat = body.cat
    if bridge and cat.serial_port and bridge.cfg["cat"].get("serial_port") == cat.serial_port:
        return {"ok": await bridge.test_cat()}

    try:
        port = cat.serial_port
        if cat.vid is not None or cat.pid is not None or cat.serial_number is not None:
            port = resolve_serial_port(
                scan_serial_devices(), vid=cat.vid, pid=cat.pid,
                serial_number=cat.serial_number, location=cat.location,
            )
        if not port:
            return {"ok": False, "error": "не е избран CAT порт"}
        return {"ok": await probe_serial_port(port, cat.baud)}
    except (AmbiguousDeviceError, DeviceNotFoundError) as e:
        return {"ok": False, "error": str(e)}

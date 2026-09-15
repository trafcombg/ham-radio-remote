"""FastAPI admin panel — remote radio configuration with device pickers,
Test/Verify, and hot-reload without restarting the server. Bound to
0.0.0.0 (see server/config.json "web") so it's reachable from any
computer on the LAN, not just the server machine.

Regular users never see this — they only ever use the desktop client
(Phase 1/2). Every route here requires an admin session cookie.
"""

import base64
import binascii
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common.version import APP_VERSION
from server.cat_bridge import probe_serial_port
from server.db import PostgresDb
from server.device_registry import (
    AmbiguousDeviceError,
    DeviceNotFoundError,
    resolve_serial_port,
    scan_audio_devices,
    scan_serial_devices,
)
from server.amplifier_manager import AmplifierManager
from server.radio_manager import RadioBusyError, RadioManager
from server.web_auth import create_session_cookie, read_session_cookie

log = logging.getLogger("admin_api")
WEB_DIR = Path(__file__).with_name("web")

app = FastAPI(title="HAM Radio Remote — Admin")


def get_manager(request: Request) -> RadioManager:
    return request.app.state.manager


def get_amp_manager(request: Request) -> AmplifierManager:
    return request.app.state.amp_manager


def get_db(request: Request):
    return request.app.state.db


def current_user(request: Request):
    return read_session_cookie(request.cookies.get("session"))


def require_admin(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=401, detail="admin login required")
    return user


async def client_user(request: Request) -> dict:
    """HTTP Basic Auth for the desktop client's own endpoints — it never
    holds a browser session cookie, just the username/password from its
    own config.json."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Basic auth required", headers={"WWW-Authenticate": "Basic"})
    try:
        username, password = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="bad auth header")
    db = get_db(request)
    if not isinstance(db, PostgresDb):
        return {"username": username, "is_admin": True}  # no accounts configured — degraded-open, same as radio_bridge
    user = await db.authenticate(username, password)
    if not user:
        raise HTTPException(status_code=401, detail="грешно потребителско име или парола")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupRequest(BaseModel):
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
    input_name_contains: str | None = None
    input_endpoint_id: str | None = None
    output_name_contains: str | None = None
    output_endpoint_id: str | None = None
    udp_port: int
    input_gain: float = 1.0
    output_gain: float = 1.0


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
    cw_udp_port: int
    ptt: RadioPttConfig
    cw: RadioPttConfig | None = None  # null = CW keys the same line as ptt
    active: bool = True
    force: bool = False


class TestRequest(BaseModel):
    cat: RadioCatConfig


class AmplifierConfigRequest(BaseModel):
    name: str
    model: str = "1200S"
    transport: str  # serial | tcp | http — see server/ebox_transport.py
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    username: str | None = None
    password: str | None = None
    linked_radio: str | None = None  # null = shared, no CAT mirror/PTT lockout


class AmplifierModeRequest(BaseModel):
    mode: str  # operate | standby | off


class UserRequest(BaseModel):
    username: str
    password: str | None = None  # None when editing without changing it — required for a brand-new user
    is_admin: bool = False
    radios: list[str] = []
    amplifiers: list[str] = []


def _to_cfg_dict(body: RadioConfigRequest) -> dict:
    return {
        "name": body.name,
        "model": body.model,
        "cat": body.cat.model_dump(),
        "control_port": body.control_port,
        "audio": body.audio.model_dump(),
        "cw_udp_port": body.cw_udp_port,
        "ptt": body.ptt.model_dump(),
        "cw": body.cw.model_dump() if body.cw else None,
        "active": body.active,
    }


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/login")
async def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.get("/setup")
async def setup_page():
    return FileResponse(WEB_DIR / "setup.html")


@app.get("/api/setup-status")
async def setup_status(request: Request):
    db = get_db(request)
    if not isinstance(db, PostgresDb):
        return {"db_configured": False, "needs_setup": False}
    return {"db_configured": True, "needs_setup": not await db.has_any_admin()}


@app.post("/api/setup")
async def setup(body: SetupRequest, request: Request):
    """Creates the first admin account from the browser — no command
    line needed. Only works once: refuses as soon as any admin exists,
    same as create_admin.py would need re-running for a second account."""
    db = get_db(request)
    if not isinstance(db, PostgresDb):
        raise HTTPException(status_code=503, detail="PostgreSQL не е конфигуриран (db.dsn)")
    if await db.has_any_admin():
        raise HTTPException(status_code=409, detail="вече има admin — влез през /login")
    if not body.username or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="потребител и парола (мин. 4 символа) са задължителни")
    await db.create_user(body.username, body.password, is_admin=True)
    return {"ok": True}


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


@app.get("/api/update")
async def get_update_status(request: Request, admin=Depends(require_admin)):
    return {"current_version": APP_VERSION, "pending": request.app.state.pending_update}


@app.post("/api/update/check")
async def check_update_now(request: Request, admin=Depends(require_admin)):
    await request.app.state.check_for_update_now()
    return {"current_version": APP_VERSION, "pending": request.app.state.pending_update}


@app.post("/api/update/apply")
async def apply_update(request: Request, admin=Depends(require_admin)):
    if not request.app.state.apply_update():
        raise HTTPException(status_code=409, detail="няма изтеглена версия за инсталиране")
    return {"ok": True}


@app.get("/api/devices")
async def list_devices(admin=Depends(require_admin)):
    return {
        "serial": [d.__dict__ for d in scan_serial_devices()],
        "audio": [d.__dict__ for d in scan_audio_devices()],
    }


@app.get("/api/client/radios")
async def list_client_radios(request: Request):
    """No admin auth — the desktop client never logs in, it just needs to
    know which radios exist and what ports to reach them on. Returns every
    radio (not just active ones) so the client can grey out inactive ones
    instead of silently hiding them."""
    manager = get_manager(request)
    configs = await manager.list_configs()
    return {
        "server_version": APP_VERSION,
        "radios": [
            {
                "name": c["name"],
                "active": c.get("active", True),
                "cat_port": c["cat"]["tcp_port"],
                "control_port": c["control_port"],
                "audio_port": c["audio"]["udp_port"],
                "cw_port": c["cw_udp_port"],
                "civ_address": (c.get("ptt") or {}).get("civ_address"),
            }
            for c in configs
        ]
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


@app.get("/api/amplifiers")
async def list_amplifiers(request: Request, admin=Depends(require_admin)):
    db = get_db(request)
    amp_manager = get_amp_manager(request)
    configs = await db.list_amplifier_configs()
    status = amp_manager.status()
    for cfg in configs:
        cfg["telemetry"] = status.get(cfg["name"])
    return {"amplifiers": configs}


@app.post("/api/amplifiers")
async def create_amplifier(body: AmplifierConfigRequest, request: Request, admin=Depends(require_admin)):
    amp_manager = get_amp_manager(request)
    await amp_manager.reload(body.model_dump())
    return {"ok": True}


@app.put("/api/amplifiers/{name}")
async def update_amplifier(name: str, body: AmplifierConfigRequest, request: Request, admin=Depends(require_admin)):
    if body.name != name:
        raise HTTPException(status_code=400, detail="name mismatch")
    amp_manager = get_amp_manager(request)
    await amp_manager.reload(body.model_dump())
    return {"ok": True}


@app.delete("/api/amplifiers/{name}")
async def delete_amplifier(name: str, request: Request, admin=Depends(require_admin)):
    amp_manager = get_amp_manager(request)
    await amp_manager.remove(name)
    return {"ok": True}


def _apply_amp_mode(bridge, mode: str):
    if mode == "operate":
        bridge.operate()
    elif mode == "standby":
        bridge.standby()
    elif mode == "off":
        bridge.power_off()
    else:
        raise HTTPException(status_code=400, detail="mode трябва да е operate/standby/off")


@app.post("/api/amplifiers/{name}/mode")
async def set_amplifier_mode(name: str, body: AmplifierModeRequest, request: Request, admin=Depends(require_admin)):
    amp_manager = get_amp_manager(request)
    bridge = amp_manager.bridges.get(name)
    if not bridge:
        raise HTTPException(status_code=404, detail="усилвателят не е свързан")
    _apply_amp_mode(bridge, body.mode)
    return {"ok": True}


@app.get("/api/client/amplifiers")
async def list_client_amplifiers(request: Request, user=Depends(client_user)):
    db = get_db(request)
    amp_manager = get_amp_manager(request)
    configs = await db.list_amplifier_configs() if isinstance(db, PostgresDb) else []
    status = amp_manager.status()
    result = []
    for cfg in configs:
        can_control = bool(user["is_admin"])
        if not can_control and isinstance(db, PostgresDb):
            can_control = await db.user_can_access_amplifier(user["username"], cfg["name"])
        result.append({
            "name": cfg["name"],
            "model": cfg.get("model"),
            "telemetry": status.get(cfg["name"]),
            "can_control": can_control,
        })
    return {"amplifiers": result}


@app.post("/api/client/amplifiers/{name}/mode")
async def set_client_amplifier_mode(name: str, body: AmplifierModeRequest, request: Request, user=Depends(client_user)):
    db = get_db(request)
    if not user["is_admin"]:
        allowed = isinstance(db, PostgresDb) and await db.user_can_access_amplifier(user["username"], name)
        if not allowed:
            raise HTTPException(status_code=403, detail="нямаш права за този усилвател")
    amp_manager = get_amp_manager(request)
    bridge = amp_manager.bridges.get(name)
    if not bridge:
        raise HTTPException(status_code=404, detail="усилвателят не е свързан")
    _apply_amp_mode(bridge, body.mode)
    return {"ok": True}


@app.get("/api/users")
async def list_users(request: Request, admin=Depends(require_admin)):
    db = get_db(request)
    return {"users": await db.list_users()}


@app.post("/api/users")
async def create_user(body: UserRequest, request: Request, admin=Depends(require_admin)):
    if not body.password or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="паролата трябва да е поне 4 символа")
    db = get_db(request)
    await db.upsert_user(body.username, body.password, body.is_admin, body.radios, body.amplifiers)
    return {"ok": True}


@app.put("/api/users/{username}")
async def update_user(username: str, body: UserRequest, request: Request, admin=Depends(require_admin)):
    if body.username != username:
        raise HTTPException(status_code=400, detail="name mismatch")
    if body.password and len(body.password) < 4:
        raise HTTPException(status_code=400, detail="паролата трябва да е поне 4 символа")
    db = get_db(request)
    await db.upsert_user(username, body.password or None, body.is_admin, body.radios, body.amplifiers)
    return {"ok": True}


@app.delete("/api/users/{username}")
async def delete_user(username: str, request: Request, admin=Depends(require_admin)):
    db = get_db(request)
    await db.delete_user(username)
    return {"ok": True}

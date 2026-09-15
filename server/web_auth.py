"""Signed-cookie admin sessions. Secret is generated fresh per server
process — sessions don't survive a server restart, which is fine for a
club/personal LAN tool; persist the secret if that ever matters."""

import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_MAX_AGE = 8 * 3600  # 8h

_secret = secrets.token_hex(32)
_serializer = URLSafeTimedSerializer(_secret)


def create_session_cookie(username: str, is_admin: bool) -> str:
    return _serializer.dumps({"username": username, "is_admin": is_admin})


def read_session_cookie(token: str | None):
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None

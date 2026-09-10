"""
Single-admin authentication: bcrypt password check + signed JWT sessions.

This app has exactly one admin account, configured entirely via env vars
(ADMIN_USERNAME, ADMIN_PASSWORD_HASH). There is no user collection.
"""

import os
import time
import bcrypt
import jwt

JWT_ALGORITHM = "HS256"


class InvalidCredentialsError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


def _get_jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable must be set.")
    return secret


def _get_expire_seconds() -> int:
    hours = float(os.getenv("JWT_EXPIRE_HOURS", "168"))
    return int(hours * 3600)


def verify_login(username: str, password: str) -> None:
    """Raise InvalidCredentialsError if username/password don't match the admin account."""
    admin_username = os.getenv("ADMIN_USERNAME")
    admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH")
    if not admin_username or not admin_password_hash:
        raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD_HASH environment variables must be set.")

    valid_password = bcrypt.checkpw(password.encode(), admin_password_hash.encode())
    if username != admin_username or not valid_password:
        raise InvalidCredentialsError("Invalid username or password")


def create_access_token(username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + _get_expire_seconds(),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str:
    """Return the username ('sub' claim) if the token is valid, else raise InvalidTokenError."""
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM], options={"require": ["sub", "iat", "exp"]})
    except jwt.PyJWTError as e:
        raise InvalidTokenError(str(e))
    if not isinstance(payload["sub"], str) or not payload["sub"]:
        raise InvalidTokenError("Invalid token subject")
    return payload["sub"]

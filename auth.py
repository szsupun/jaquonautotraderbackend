"""
Telegram WebApp initData verification.

Validates the `initData` string a Telegram Mini App sends with every
request, per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Falls back to allowing unauthenticated access only for requests coming
from localhost, so the UI can be developed/tested in a normal browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request

from config import ALLOWED_USER_IDS, DEV_BYPASS_AUTH, TELEGRAM_ADMIN_IDS, TELEGRAM_BOT_TOKEN
from profile_store import save_profile

logger = logging.getLogger(__name__)

MAX_AUTH_AGE_SECONDS = 24 * 3600

# Capture each user's real name/username once per backend run, not on every
# poll — names essentially never change mid-session, so re-writing the same
# file every 3s would just be wasted disk I/O.
_profile_seen_this_run: set = set()


def _save_profile_background(uid: int, user: dict) -> None:
    """save_profile() is a blocking pymongo write — this dependency runs on
    every single API call, including time-critical ones like Stop, so a
    slow/stalled Mongo round trip right here can block a request that has
    nothing to do with the profile cache. Fire it in the background and
    don't make the caller wait on it; a failed write here just means the
    admin dashboard shows a stale name until it's retried next run."""
    try:
        save_profile(uid, user)
    except Exception as e:
        logger.error(f"Background profile save failed for {uid}: {e}")


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def verify_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Return the parsed `user` dict if init_data is valid, else None."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(pairs.items())
    )
    computed_hash = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date and (time.time() - int(auth_date)) > MAX_AUTH_AGE_SECONDS:
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None


def _is_local(request: Request) -> bool:
    # Never trust this alone — a tunnel (ngrok/cloudflared) makes every
    # request, including real internet traffic, look like it's from
    # localhost. It only means anything when DEV_BYPASS_AUTH is explicitly
    # enabled, which you should only do with no tunnel running.
    if not DEV_BYPASS_AUTH:
        return False
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


async def require_admin(
    request: Request,
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """FastAPI dependency: only allow verified Telegram admin users (or localhost dev)."""
    user = verify_init_data(x_telegram_init_data, TELEGRAM_BOT_TOKEN)

    if user is None:
        if _is_local(request):
            return {"id": 0, "first_name": "Dev", "local": True}
        raise HTTPException(status_code=401, detail="Invalid or missing Telegram auth")

    if TELEGRAM_ADMIN_IDS and 0 not in TELEGRAM_ADMIN_IDS:
        if user.get("id") not in TELEGRAM_ADMIN_IDS:
            raise HTTPException(status_code=403, detail="Not authorized")

    return user


async def require_user(
    request: Request,
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """
    FastAPI dependency: allow any verified Telegram user (or localhost dev).

    Each caller gets identified by their own Telegram id — that id is what
    session_manager.py uses to key each user's isolated trading session.
    If ALLOWED_USER_IDS is non-empty, only those ids may pass (private beta).
    """
    user = verify_init_data(x_telegram_init_data, TELEGRAM_BOT_TOKEN)

    if user is None:
        if _is_local(request):
            # Distinct per browser tab for local multi-user testing:
            # open http://localhost:8001/?dev_user=123
            dev_user = request.query_params.get("dev_user", "0")
            return {"id": int(dev_user), "first_name": "Dev", "local": True}
        raise HTTPException(status_code=401, detail="Invalid or missing Telegram auth")

    if ALLOWED_USER_IDS and user.get("id") not in ALLOWED_USER_IDS:
        raise HTTPException(status_code=403, detail="Not authorized")

    uid = user.get("id")
    if uid is not None and uid not in _profile_seen_this_run and not user.get("local"):
        _profile_seen_this_run.add(uid)
        asyncio.create_task(asyncio.to_thread(_save_profile_background, uid, user))

    return user

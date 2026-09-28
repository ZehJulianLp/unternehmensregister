"""One-use authorization attempts bound to the initiating browser and local user."""

import secrets

from flask import session
from flask_login import current_user

from models import db

from .models import JulianverseAttempt, now
from .sessions import digest


def safe_next(value):
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and not any(ord(c) < 32 for c in value)
    ):
        return value
    return "/me"


def new_attempt(purpose, next_path=None, lifetime=600):
    state = secrets.token_urlsafe(32)
    browser = session.setdefault("register_auth_browser", secrets.token_urlsafe(32))
    user = current_user if current_user.is_authenticated else None
    db.session.execute(
        db.delete(JulianverseAttempt).where(JulianverseAttempt.expires_at < now())
    )
    db.session.add(
        JulianverseAttempt(
            id=digest(state),
            browser_hash=digest(browser),
            purpose=purpose,
            user_id=user.id if user else None,
            identity_stamp=digest(user.discord_id) if user else None,
            next_path=safe_next(next_path),
            expires_at=now() + lifetime,
        )
    )
    db.session.commit()
    return state


def get_attempt(state, prefix):
    browser = session.get("register_auth_browser", "")
    attempt = (
        db.session.get(JulianverseAttempt, digest(state)) if state and browser else None
    )
    if (
        not attempt
        or not attempt.purpose.startswith(prefix)
        or attempt.used
        or attempt.expires_at <= now()
        or attempt.browser_hash != digest(browser)
    ):
        return None
    if attempt.user_id is None:
        if current_user.is_authenticated:
            return None
    elif (
        not current_user.is_authenticated
        or current_user.id != attempt.user_id
        or not current_user.is_active
        or digest(current_user.discord_id) != attempt.identity_stamp
    ):
        return None
    return attempt


def take_attempt(state, prefix):
    attempt = get_attempt(state, prefix)
    if not attempt:
        return None
    result = db.session.execute(
        db.update(JulianverseAttempt)
        .where(
            JulianverseAttempt.id == attempt.id,
            JulianverseAttempt.used.is_(False),
            JulianverseAttempt.expires_at > now(),
        )
        .values(used=True)
    )
    db.session.commit()
    return attempt if result.rowcount == 1 else None


def discord_confirmed():
    return get_attempt(session.get("discord_confirmation"), "proof-discord") is not None


def consume_confirmation():
    if session.get("julianverse_session"):
        from .sessions import consume_confirmation as consume_sso

        return consume_sso()
    state = session.pop("discord_confirmation", None)
    return take_attempt(state, "proof-discord") is not None

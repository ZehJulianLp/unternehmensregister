import hashlib
import json
import secrets
import time

import requests
from flask import abort, current_app, flash, g, redirect, request, session, url_for
from flask_login import current_user, login_user, logout_user

from models import db

from .crypto import decrypt_secret, encrypt_secret
from .models import JulianverseSession, now


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def remote_request(path, **kwargs):
    with requests.Session() as client:
        client.trust_env = False
        return client.request(
            url=current_app.config["JULIANVERSE_ISSUER"] + path,
            timeout=(4, 8),
            allow_redirects=False,
            **kwargs,
        )


def client_auth():
    return (
        current_app.config["JULIANVERSE_CLIENT_ID"],
        current_app.config["JULIANVERSE_CLIENT_SECRET"],
    )


def token_data(token):
    if not isinstance(token.get("access_token"), str) or not token["access_token"]:
        raise ValueError("Missing access token")
    lifetime = int(token.get("expires_in", 0))
    if not 0 < lifetime <= 3600 or token.get("token_type", "").lower() != "bearer":
        raise ValueError("Invalid access token metadata")
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": now() + lifetime,
    }


def revoke_remote(token):
    value = token.get("refresh_token") or token.get("access_token")
    if value:
        try:
            remote_request(
                "/oauth/revoke",
                method="POST",
                data={"token": value},
                auth=client_auth(),
            )
        except requests.RequestException:
            current_app.logger.warning(
                "Julianverse token revocation could not reach Account."
            )


def clear_session(revoke=True):
    raw = session.pop("julianverse_session", None)
    if not raw:
        return
    record = db.session.get(JulianverseSession, digest(raw))
    if record:
        try:
            token = json.loads(decrypt_secret(record.secret) or "{}")
        except (ValueError, TypeError):
            token = {}
        db.session.delete(record)
        db.session.commit()
        if revoke:
            revoke_remote(token)


def start_session(user, token):
    values = token_data(token)
    if not values["refresh_token"]:
        raise ValueError("Missing refresh token")
    clear_session()
    logout_user()  # Also clear any older Flask-Login remember cookie.
    raw = secrets.token_urlsafe(48)
    db.session.add(
        JulianverseSession(
            id=digest(raw),
            user_id=user.id,
            secret=encrypt_secret(json.dumps(values)),
            expires_at=now() + 30 * 86400,
            checked_at=now(),
        )
    )
    db.session.commit()
    login_user(user, remember=False, fresh=True)
    session["julianverse_session"] = raw
    session.permanent = True


def expire():
    clear_session(revoke=False)
    logout_user()
    flash("Bitte melde dich erneut mit Julianverse an.", "warning")
    return redirect(url_for("login"))


def session_record():
    raw = session.get("julianverse_session")
    return (
        db.session.get(JulianverseSession, digest(raw), populate_existing=True)
        if raw
        else None
    )


def confirmed():
    record = session_record()
    return bool(
        record
        and current_user.is_authenticated
        and record.user_id == current_user.id
        and record.confirmed_at >= now() - 300
    )


def consume_confirmation():
    record = session_record()
    if not record or not current_user.is_authenticated:
        return False
    result = db.session.execute(
        db.update(JulianverseSession)
        .where(
            JulianverseSession.id == record.id,
            JulianverseSession.user_id == current_user.id,
            JulianverseSession.confirmed_at >= now() - 300,
        )
        .values(confirmed_at=0)
    )
    db.session.commit()
    return result.rowcount == 1


def check_session():
    if request.endpoint in ("static", "logout"):
        return
    if current_user.is_authenticated and not current_user.is_active:
        return expire()
    if not session.get("julianverse_session"):
        return
    if not current_app.config["JULIANVERSE_ENABLED"]:
        return expire()
    record = session_record()
    if (
        not record
        or not current_user.is_authenticated
        or record.user_id != current_user.id
        or record.expires_at <= now()
        or not record.identity
    ):
        return expire()
    g.julianverse_confirmed = confirmed()
    if record.checked_at > now() - 60:
        return
    key = secrets.token_hex(24)
    acquired = db.session.execute(
        db.update(JulianverseSession)
        .where(
            JulianverseSession.id == record.id,
            JulianverseSession.lease_until <= now(),
        )
        .values(lease_key=key, lease_until=now() + 40)
    )
    db.session.commit()
    if acquired.rowcount != 1:
        # Another request is renewing the one-use refresh token. Never replay it.
        for _ in range(20):
            time.sleep(0.05)
            record = session_record()
            if not record:
                return expire()
            if record.checked_at > now() - 60:
                return
        abort(
            503,
            "Die Anmeldung wird gerade geprüft. Bitte lade die Seite gleich erneut.",
        )
    record = session_record()
    if not record:
        return expire()
    refreshed = False
    try:
        token = json.loads(decrypt_secret(record.secret) or "{}")
        if token.get("expires_at", 0) <= now() + 30:
            refreshed = True
            response = remote_request(
                "/oauth/token",
                method="POST",
                auth=client_auth(),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token.get("refresh_token", ""),
                },
            )
            if response.status_code in (400, 401, 403):
                return expire()
            if response.status_code != 200:
                return expire()
            token = token_data(response.json())
            if not token.get("refresh_token"):
                return expire()
        response = remote_request(
            "/oauth/userinfo",
            method="GET",
            headers={
                "Authorization": "Bearer " + token["access_token"],
            },
        )
        if response.status_code in (401, 403):
            return expire()
        if response.status_code != 200:
            raise ValueError("Unexpected userinfo response")
        if response.json().get("sub") != record.identity.subject:
            return expire()
        saved = db.session.execute(
            db.update(JulianverseSession)
            .where(
                JulianverseSession.id == record.id,
                JulianverseSession.lease_key == key,
            )
            .values(
                secret=encrypt_secret(json.dumps(token)),
                checked_at=now(),
                lease_key=None,
                lease_until=0,
            )
        )
        db.session.commit()
        if saved.rowcount != 1:
            return expire()
    except (requests.RequestException, ValueError, KeyError):
        if refreshed:
            # The provider may already have consumed the refresh token.
            return expire()
        abort(
            503,
            "Julianverse Account ist gerade nicht erreichbar. Bitte versuche es gleich erneut.",
        )
    finally:
        db.session.execute(
            db.update(JulianverseSession)
            .where(
                JulianverseSession.id == record.id,
                JulianverseSession.lease_key == key,
            )
            .values(lease_key=None, lease_until=0)
        )
        db.session.commit()

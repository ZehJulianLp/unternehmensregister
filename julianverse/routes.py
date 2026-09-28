import secrets

import requests
from authlib.common.errors import AuthlibBaseError
from flask import Blueprint, abort, current_app, redirect, request, session, url_for
from flask_login import current_user, login_required
from joserfc.errors import JoseError
from sqlalchemy.exc import IntegrityError

from models import User, db

from .attempts import new_attempt, take_attempt
from .discord import begin as begin_discord
from .discord import notice
from .models import JulianverseIdentity, now
from .sessions import revoke_remote, session_record, start_session, token_data

bp = Blueprint("julianverse", __name__, url_prefix="/auth")


def oauth():
    return current_app.extensions["julianverse_oauth"].julianverse


@bp.before_request
def available():
    if (
        "/julianverse/" in request.path
        and not current_app.config["JULIANVERSE_ENABLED"]
    ):
        abort(404)
    if current_user.is_authenticated and not current_user.is_active:
        abort(403)


def begin_oidc(purpose, next_path=None):
    state = new_attempt("oidc-" + purpose, next_path)
    kwargs = {"prompt": "login", "max_age": 0} if purpose == "confirm" else {}
    try:
        return oauth().authorize_redirect(
            current_app.config["PUBLIC_BASE_URL"] + "/auth/julianverse/callback",
            state=state,
            **kwargs,
        )
    except (AuthlibBaseError, requests.RequestException, ValueError):
        return notice(
            "Julianverse Account ist gerade nicht erreichbar. Bitte versuche es später erneut."
        )


@bp.post("/julianverse/start")
def start():
    if current_user.is_authenticated:
        return redirect(url_for("settings"))
    return begin_oidc("login", request.form.get("next"))


@bp.post("/julianverse/register")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("settings"))
    return begin_oidc("register")


@bp.post("/julianverse/link")
@login_required
def link():
    if current_user.julianverse_identity:
        return notice("Dieses Registerkonto ist bereits mit Julianverse verknüpft.")
    if not current_user.has_discord:
        return notice("Verknüpfe zuerst einen Discord-Zugang in den Einstellungen.")
    return begin_discord("link")


@bp.post("/julianverse/unlink")
@login_required
def unlink():
    if not current_user.has_discord:
        return notice(
            "Verknüpfe zuerst einen Discord-Zugang, damit du dein Registerkonto weiter nutzen kannst."
        )
    return begin_discord("unlink")


@bp.get("/discord/login")
def discord_login():
    if current_user.is_authenticated:
        return redirect(url_for("settings"))
    return begin_discord("login", request.args.get("next"))


@bp.post("/discord/link")
@login_required
def discord_link():
    if current_user.has_discord:
        return notice("Dein Registerkonto hat bereits einen Discord-Zugang.")
    return begin_discord("attach")


@bp.post("/confirm")
@login_required
def confirm():
    if session.get("julianverse_session"):
        return begin_oidc("confirm")
    if current_user.has_discord:
        return begin_discord("confirm")
    return notice("Bitte melde dich erneut mit Julianverse an.")


@bp.get("/julianverse/callback")
def callback():
    attempt = take_attempt(request.args.get("state"), "oidc-")
    if not attempt:
        return notice(
            "Die Anmeldung ist abgelaufen oder wurde bereits verwendet. Bitte starte sie erneut."
        )
    token = None
    try:
        token = oauth().authorize_access_token(
            claims_options={
                "iss": {
                    "essential": True,
                    "value": current_app.config["JULIANVERSE_ISSUER"],
                }
            },
            leeway=30,
        )
        info = token.get("userinfo")
        if (
            not token.get("id_token")
            or not info
            or info.get("iss") != current_app.config["JULIANVERSE_ISSUER"]
            or not isinstance(info.get("sub"), str)
            or not 0 < len(info["sub"]) <= 255
        ):
            raise ValueError("Invalid identity")
        values = token_data(token)
        if not values.get("refresh_token"):
            raise ValueError("Missing refresh token")
        identity = db.session.scalar(
            db.select(JulianverseIdentity).where(
                JulianverseIdentity.issuer == info["iss"],
                JulianverseIdentity.subject == info["sub"],
            )
        )
        purpose = attempt.purpose.removeprefix("oidc-")
        if purpose == "confirm":
            record = session_record()
            if (
                not identity
                or identity.user_id != current_user.id
                or not record
                or record.user_id != current_user.id
                or record.expires_at <= now()
                or int(info.get("auth_time", 0)) < now() - 300
            ):
                raise ValueError("Fresh confirmation of the linked account required")
            record.confirmed_at = now()
            db.session.commit()
            revoke_remote(token)
            return notice(
                "Anmeldung bestätigt. Du kannst dein Registerkonto innerhalb von fünf Minuten löschen.",
                "success",
            )
        if purpose == "link":
            if current_user.julianverse_identity or identity:
                revoke_remote(token)
                return notice(
                    "Eines der Konten ist bereits verknüpft. Die bestehende Verknüpfung bleibt erhalten."
                )
            user = db.session.get(User, current_user.id)
        elif identity:
            user = identity.user
        elif purpose == "register":
            # The old schema requires a unique non-null Discord field. An internal
            # marker preserves the table and its foreign keys until Discord is linked.
            user = User(
                discord_id="jv-" + secrets.token_hex(14),
                username=str(
                    info.get("preferred_username")
                    or info.get("name")
                    or "Julianverse-Nutzer"
                )[:120],
                role="viewer",
            )
            db.session.add(user)
        else:
            revoke_remote(token)
            return notice(
                "Julianverse ist noch nicht verknüpft. Melde dich mit deinem bisherigen Discord-Zugang an und verknüpfe dein Konto in den Einstellungen. Falls du neu bist, wähle Neues Registerkonto erstellen."
            )
        if not user.is_active:
            raise ValueError("Account deleted")
        if not identity:
            identity = JulianverseIdentity(
                user=user,
                issuer=info["iss"],
                subject=info["sub"],
                name=str(
                    info.get("preferred_username") or info.get("name") or user.username
                )[:255],
            )
            db.session.add(identity)
            db.session.flush()
            current_app.extensions["register_auth_hooks"]["add_audit"](
                user,
                "julianverse_linked",
                "user",
                user.id,
                "Julianverse-Zugang ausdrücklich verknüpft.",
            )
        db.session.commit()
        start_session(user, token)
        if purpose in ("link", "register"):
            return notice(
                "Julianverse ist verknüpft. Deine Firmen und Berechtigungen bleiben in diesem Registerkonto.",
                "success",
            )
        return redirect(attempt.next_path)
    except (
        AuthlibBaseError,
        JoseError,
        requests.RequestException,
        ValueError,
        TypeError,
        IntegrityError,
    ):
        db.session.rollback()
        if token:
            revoke_remote(token)
        return notice(
            "Julianverse konnte die Anmeldung nicht bestätigen. Bitte versuche es mit deinem verknüpften Konto erneut."
        )

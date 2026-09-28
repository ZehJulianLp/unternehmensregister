"""Keep Discord login and prove both identities when linking existing accounts."""

import os
from urllib.parse import urlencode

import requests
from flask import current_app, flash, redirect, request, session, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from models import User, db

from .attempts import new_attempt, take_attempt
from .sessions import clear_session


def notice(message, category="error"):
    flash(message, category)
    return redirect(url_for("settings" if current_user.is_authenticated else "login"))


def begin(purpose="login", next_path=None):
    if not os.getenv("DISCORD_CLIENT_ID") or not os.getenv("DISCORD_CLIENT_SECRET"):
        return notice("Discord OAuth ist noch nicht konfiguriert.")
    state = new_attempt("discord-" + purpose, next_path)
    params = dict(
        client_id=os.environ["DISCORD_CLIENT_ID"],
        redirect_uri=os.getenv(
            "DISCORD_REDIRECT_URI", url_for("callback", _external=True)
        ),
        response_type="code",
        scope="identify",
        state=state,
        prompt="consent",
    )
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))


def callback():
    # The callable hooks come from the existing app, avoiding a second app import.
    hooks = current_app.extensions["register_auth_hooks"]
    attempt = take_attempt(request.args.get("state"), "discord-")
    if not attempt:
        return notice(
            "Die Discord-Anmeldung ist abgelaufen oder gehört zu einem anderen Konto. Bitte starte erneut."
        )
    if request.args.get("error") or not request.args.get("code"):
        return notice("Die Discord-Anmeldung wurde abgebrochen.")
    try:
        token = hooks["exchange_discord_code"](request.args["code"])
        profile = hooks["fetch_discord_user"](token["access_token"])
        discord_id = str(profile.get("id", ""))
        if not discord_id.isdigit() or not 5 <= len(discord_id) <= 32:
            raise ValueError("Invalid Discord ID")
    except (requests.RequestException, KeyError, ValueError):
        current_app.logger.warning("Discord authentication could not be verified.")
        return notice(
            "Discord konnte die Anmeldung nicht bestätigen. Bitte versuche es erneut."
        )

    purpose = attempt.purpose.removeprefix("discord-")
    if purpose == "login":
        user = hooks["upsert_user"](profile)
        clear_session()
        session.permanent = False
        login_user(user, remember=False, fresh=True)
        return redirect(attempt.next_path)
    user = db.session.get(User, current_user.id)
    if purpose == "attach":
        if user.has_discord:
            return notice("Dein Registerkonto hat bereits einen Discord-Zugang.")
        existing = db.session.scalar(
            db.select(User).where(User.discord_id == discord_id)
        )
        if existing:
            return notice(
                "Dieser Discord-Zugang gehört bereits zu einem Registerkonto. Melde dich damit an und verknüpfe Julianverse dort; bestehende Firmen werden nicht zusammengelegt."
            )
        try:
            user.discord_id = discord_id
            user.avatar = profile.get("avatar")
            hooks["add_audit"](
                user,
                "discord_linked",
                "user",
                user.id,
                "Discord-Zugang ausdrücklich verknüpft.",
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return notice("Dieser Discord-Zugang wurde bereits verknüpft.")
        # Existing role rules remain the source of permissions; merely linking grants no role.
        return notice(
            "Discord ist verknüpft. Du kannst jetzt bei Bedarf deine Discord-Mitgliedsrolle prüfen.",
            "success",
        )
    if not user.has_discord or user.discord_id != discord_id:
        return notice(
            "Bitte bestätige mit dem Discord-Konto, das zu diesem Registerkonto gehört."
        )
    if purpose == "link":
        if user.julianverse_identity:
            return notice("Dieses Registerkonto ist bereits mit Julianverse verknüpft.")
        from .routes import begin_oidc

        return begin_oidc("link")
    if purpose == "unlink":
        clear_session()
        if user.julianverse_identity:
            db.session.delete(user.julianverse_identity)
            hooks["add_audit"](
                user,
                "julianverse_unlinked",
                "user",
                user.id,
                "Julianverse-Verknüpfung nach Discord-Bestätigung getrennt.",
            )
            db.session.commit()
        logout_user()
        login_user(user, remember=False, fresh=True)
        session.permanent = False
        return notice(
            "Julianverse wurde getrennt. Dein Registerkonto bleibt über Discord erreichbar.",
            "success",
        )
    if purpose == "confirm":
        session["discord_confirmation"] = new_attempt("proof-discord", lifetime=300)
        return notice(
            "Anmeldung bestätigt. Du kannst dein Registerkonto innerhalb von fünf Minuten löschen.",
            "success",
        )
    return notice("Bitte starte die Anmeldung erneut.")

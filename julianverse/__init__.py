from datetime import timedelta
from urllib.parse import urlsplit

import click
from authlib.integrations.flask_client import OAuth
from flask import g, request

from models import db

from .attempts import discord_confirmed
from .models import JulianverseAttempt, JulianverseIdentity, JulianverseSession, now


def init_app(app):
    from .routes import bp
    from .sessions import check_session

    issuer = app.config["JULIANVERSE_ISSUER"].rstrip("/")
    enabled = bool(
        app.config["JULIANVERSE_CLIENT_ID"] and app.config["JULIANVERSE_CLIENT_SECRET"]
    )
    app.config["JULIANVERSE_ENABLED"] = enabled
    if enabled:
        for name, value in (
            ("JULIANVERSE_ISSUER", issuer),
            ("PUBLIC_BASE_URL", app.config["PUBLIC_BASE_URL"]),
        ):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise RuntimeError(name + " must be an HTTPS origin.")
        if app.config["SECRET_KEY"] == "dev-secret-change-me":
            raise RuntimeError(
                "Set a persistent private FLASK_SECRET_KEY before enabling SSO."
            )
    app.config["JULIANVERSE_ISSUER"] = issuer
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    oauth = OAuth(app)
    oauth.register(
        "julianverse",
        client_id=app.config["JULIANVERSE_CLIENT_ID"],
        client_secret=app.config["JULIANVERSE_CLIENT_SECRET"],
        server_metadata_url=issuer + "/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile",
            "code_challenge_method": "S256",
            "timeout": (4, 8),
        },
    )
    app.extensions["julianverse_oauth"] = oauth
    app.register_blueprint(bp)
    app.extensions["limiter"].limit("30 per minute")(bp)
    app.before_request(check_session)

    @app.context_processor
    def context():
        return dict(
            julianverse_enabled=enabled,
            julianverse_issuer=issuer,
            auth_confirmed=getattr(g, "julianverse_confirmed", False)
            or discord_confirmed(),
        )

    @app.after_request
    def protect_auth_response(response):
        if request.path.startswith("/auth/") or request.endpoint in (
            "login",
            "callback",
            "settings",
            "settings_export",
            "logout",
        ):
            response.headers["Cache-Control"] = "no-store"
        if request.endpoint in ("callback", "julianverse.callback"):
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.cli.command("init-julianverse")
    def init_tables():
        """Add only SSO tables; existing register records are kept in place."""
        for model in (JulianverseIdentity, JulianverseSession, JulianverseAttempt):
            model.__table__.create(db.engine, checkfirst=True)
        click.echo("Julianverse-Tabellen bereit.")

    @app.cli.command("prune-julianverse")
    def prune():
        """Remove expired local tokens and authorization attempts."""
        db.session.execute(
            db.delete(JulianverseAttempt).where(JulianverseAttempt.expires_at < now())
        )
        db.session.execute(
            db.delete(JulianverseSession).where(JulianverseSession.expires_at < now())
        )
        db.session.commit()
        click.echo("Abgelaufene Julianverse-Sitzungen entfernt.")

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests
from joserfc import jwt
from joserfc.jwk import RSAKey

from app import create_app
from julianverse.crypto import decrypt_secret, encrypt_secret
from julianverse.models import JulianverseIdentity, JulianverseSession, now
from models import Company, CompanyChange, CompanyManager, User, db

ISSUER = "https://account.example.org"
CALLBACK = "https://register.example.org/auth/julianverse/callback"


class Provider:
    def __init__(self):
        self.key = RSAKey.generate_key(2048, private=True)
        self.key.ensure_kid()
        self.pending = {}
        self.access = {}
        self.refresh = {}
        self.exchange_count = 0
        self.revoked = []
        self.refresh_count = 0
        self.unavailable = False
        self.bad_refresh = False
        self.overrides = {}
        self.discord_id = "123456789012345678"
        self.signing_key = None

    def response(self, data, request, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(data).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = request.url
        response.request = request
        return response

    def send(self, request, **kwargs):
        if urlsplit(request.url).hostname == "discord.com":
            if urlsplit(request.url).path.endswith("/oauth2/token"):
                return self.response({"access_token": "discord-test"}, request)
            if urlsplit(request.url).path.endswith("/users/@me"):
                return self.response(
                    {
                        "id": self.discord_id,
                        "username": "discord-collector",
                        "avatar": "original-avatar",
                    },
                    request,
                )
            raise AssertionError("Unexpected Discord operation")
        assert urlsplit(request.url).netloc == "account.example.org", (
            "Unexpected network request"
        )
        path = urlsplit(request.url).path
        if self.unavailable:
            raise requests.ConnectionError("offline")
        if path == "/.well-known/openid-configuration":
            return self.response(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/oauth/authorize",
                    "token_endpoint": ISSUER + "/oauth/token",
                    "jwks_uri": ISSUER + "/oauth/jwks",
                    "userinfo_endpoint": ISSUER + "/oauth/userinfo",
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
                request,
            )
        if path == "/oauth/jwks":
            return self.response({"keys": [self.key.as_dict(private=False)]}, request)
        raw = request.body or ""
        fields = parse_qs(raw.decode() if isinstance(raw, bytes) else raw)
        if path == "/oauth/token":
            assert (
                request.headers["Authorization"]
                == "Basic "
                + base64.b64encode(b"register-client:client-secret").decode()
            )
            if fields["grant_type"] == ["refresh_token"]:
                self.refresh_count += 1
                previous = fields["refresh_token"][0]
                subject = self.refresh.pop(previous, None)
                if not subject or self.bad_refresh:
                    return self.response({"error": "invalid_grant"}, request, 400)
                return self.response(self.tokens(subject), request)
            self.exchange_count += 1
            auth = self.pending.pop(fields["code"][0])
            verifier = fields["code_verifier"][0]
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert auth["code_challenge"] == [challenge]
            assert auth["code_challenge_method"] == ["S256"]
            assert fields["redirect_uri"] == [CALLBACK]
            info = {
                "iss": ISSUER,
                "sub": "central-user",
                "aud": "register-client",
                "iat": now(),
                "exp": now() + 600,
                "auth_time": now(),
                "nonce": auth["nonce"][0],
                "email": "collector@example.org",
                "email_verified": True,
                "preferred_username": "collector",
                "name": "Collector",
                "locale": "de",
            }
            info.update(self.overrides)
            token = self.tokens(info["sub"])
            token["id_token"] = jwt.encode(
                {"alg": "RS256", "kid": self.key.kid},
                info,
                self.signing_key or self.key,
            )
            return self.response(token, request)
        if path == "/oauth/userinfo":
            subject = self.access.get(
                request.headers.get("Authorization", "").removeprefix("Bearer ")
            )
            return self.response(
                {"sub": subject} if subject else {}, request, 200 if subject else 401
            )
        if path == "/oauth/revoke":
            value = fields["token"][0]
            self.revoked.append(value)
            self.refresh.pop(value, None)
            return self.response({}, request)
        raise AssertionError(path)

    def tokens(self, subject):
        index = len(self.access) + 1
        access, refresh = f"access-{index}", f"refresh-{index}"
        self.access[access] = subject
        self.refresh[refresh] = subject
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 600,
            "token_type": "Bearer",
        }

    def finish(self, client, response):
        assert response.status_code == 302
        auth = parse_qs(urlsplit(response.location).query)
        assert urlsplit(response.location).path == "/oauth/authorize"
        code = "code-" + str(self.exchange_count)
        self.pending[code] = auth
        callback = "/auth/julianverse/callback?" + urlencode(
            {"code": code, "state": auth["state"][0]}
        )
        return client.get(callback), callback

    def discord_finish(self, client, response):
        assert response.status_code == 302
        params = parse_qs(urlsplit(response.location).query)
        assert urlsplit(response.location).hostname == "discord.com"
        return client.get(
            "/callback?"
            + urlencode({"code": "discord-code", "state": params["state"][0]})
        )


@pytest.fixture
def setup_sso(tmp_path, monkeypatch):
    provider = Provider()
    for key, value in {
        "DISCORD_CLIENT_ID": "test-discord-client",
        "DISCORD_CLIENT_SECRET": "test-discord-secret",
        "DISCORD_REDIRECT_URI": "https://register.example.org/callback",
        "DISCORD_BOT_TOKEN": "",
        "DISCORD_GUILD_ID": "",
        "DISCORD_MEMBER_ROLE_IDS": "",
        "ADMIN_DISCORD_IDS": "",
        "DISCORD_ADMIN_CHANNEL_ID": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        requests.Session,
        "send",
        lambda self, request, **kw: provider.send(request, **kw),
    )
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'register.db'}",
            "LOGO_UPLOAD_FOLDER": str(tmp_path / "logos"),
            "RATELIMIT_ENABLED": False,
            "JULIANVERSE_ISSUER": ISSUER,
            "JULIANVERSE_CLIENT_ID": "register-client",
            "JULIANVERSE_CLIENT_SECRET": "client-secret",
            "PUBLIC_BASE_URL": "https://register.example.org",
            "SERVER_NAME": "register.example.org",
            "PREFERRED_URL_SCHEME": "https",
            "SESSION_COOKIE_SECURE": True,
        }
    )
    yield app, app.test_client(), provider
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def seed_user(app, role="owner"):
    with app.app_context():
        user = User(
            discord_id="123456789012345678",
            username="existing-owner",
            role=role,
            avatar="original-avatar",
        )
        manager = User(
            discord_id="234567890123456789", username="manager", role="member"
        )
        db.session.add_all([user, manager])
        db.session.flush()
        company = Company(
            name="Existing Railway",
            short_name="ER",
            description="Keep this",
            industry="Railway",
            company_type="profit",
            status="active",
            headquarters="Main",
            district="North",
            owner_id=user.id,
            register_id="RR-0001",
            logo_filename="original.webp",
        )
        db.session.add(company)
        db.session.flush()
        db.session.add(CompanyManager(company_id=company.id, user_id=manager.id))
        db.session.add(
            CompanyChange(
                company_id=company.id,
                user_id=user.id,
                action="created",
                description="Original history",
            )
        )
        db.session.commit()
        return user.id, company.id


def test_restart_preserves_existing_admin_rows(setup_sso, monkeypatch):
    from deploy.setup_julianverse import fingerprint

    app, _, _ = setup_sso
    seed_user(app, role="admin")
    monkeypatch.setenv("ADMIN_DISCORD_IDS", "123456789012345678")
    with app.app_context():
        path = db.engine.url.database
    before = fingerprint(path)
    restarted = create_app(dict(app.config))
    try:
        assert fingerprint(path) == before
    finally:
        with restarted.app_context():
            db.session.remove()
            db.engine.dispose()


def test_startup_still_grants_configured_admin_role(setup_sso, monkeypatch):
    app, _, _ = setup_sso
    user_id, _ = seed_user(app, role="viewer")
    monkeypatch.setenv("ADMIN_DISCORD_IDS", "123456789012345678")
    restarted = create_app(dict(app.config))
    try:
        with restarted.app_context():
            assert db.session.get(User, user_id).role == "admin"
            assert (
                db.session.scalar(
                    db.select(User).where(User.discord_id == "234567890123456789")
                ).role
                == "member"
            )
    finally:
        with restarted.app_context():
            db.session.remove()
            db.engine.dispose()


def login_discord(client, provider):
    return provider.discord_finish(client, client.get("/auth/discord/login"))


def register_sso(client, provider):
    return provider.finish(client, client.post("/auth/julianverse/register"))


def link_sso(client, provider):
    second = provider.discord_finish(client, client.post("/auth/julianverse/link"))
    return provider.finish(client, second)


def age_session(app, expired_token=False):
    with app.app_context():
        record = db.session.scalar(db.select(JulianverseSession))
        record.checked_at = now() - 70
        if expired_token:
            token = json.loads(decrypt_secret(record.secret))
            token["expires_at"] = now() - 1
            record.secret = encrypt_secret(json.dumps(token))
        db.session.commit()


def test_new_account_requires_explicit_registration_and_starts_as_viewer(setup_sso):
    app, client, provider = setup_sso
    response, _ = provider.finish(client, client.post("/auth/julianverse/start"))
    assert response.location.endswith("/login")
    with app.app_context():
        assert db.session.scalar(db.select(User)) is None
    response, callback = register_sso(client, provider)
    assert response.location.endswith("/settings")
    with app.app_context():
        user = db.session.scalar(db.select(User))
        assert user.role == "viewer" and not user.has_discord and user.is_active
        assert user.julianverse_identity.subject == "central-user"
        record = db.session.scalar(db.select(JulianverseSession))
        assert "access-" not in record.secret and "refresh-" not in record.secret
        assert json.loads(decrypt_secret(record.secret))["access_token"] == "access-2"
    assert client.get("/settings").status_code == 200
    with client.session_transaction() as cookie:
        assert cookie["_permanent"]
        assert "access-" not in str(dict(cookie))
    client.get(callback)
    assert provider.exchange_count == 2
    assert client.get("/admin").status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example.org"},
        {"aud": "other-app"},
        {"nonce": "wrong"},
        {"exp": 1},
        {"sub": ""},
    ],
)
def test_invalid_id_token_rejected(setup_sso, overrides):
    app, client, provider = setup_sso
    provider.overrides = overrides
    assert register_sso(client, provider)[0].location.endswith("/login")
    with app.app_context():
        assert db.session.scalar(db.select(User)) is None


def test_wrong_signing_key_rejected(setup_sso):
    app, client, provider = setup_sso
    provider.signing_key = RSAKey.generate_key(2048, private=True)
    assert register_sso(client, provider)[0].location.endswith("/login")
    with app.app_context():
        assert db.session.scalar(db.select(User)) is None


def test_existing_discord_link_preserves_company_roles_history_and_managers(setup_sso):
    app, client, provider = setup_sso
    user_id, company_id = seed_user(app, role="admin")
    login_discord(client, provider)
    with app.app_context():
        user = db.session.get(User, user_id)
        username = user.username
    response, _ = link_sso(client, provider)
    assert response.location.endswith("/settings")
    with app.app_context():
        user = db.session.get(User, user_id)
        company = db.session.get(Company, company_id)
        assert (
            user.role == "admin"
            and user.username == username
            and user.discord_id == provider.discord_id
        )
        assert (
            company.owner_id == user_id
            and company.short_name == "ER"
            and company.register_id == "RR-0001"
        )
        assert company.logo_filename == "original.webp" and company.status == "active"
        assert len(company.managers) == 1 and len(company.changes) == 1
        assert db.session.scalar(db.select(db.func.count(User.id))) == 2
    client.post("/logout")
    response, _ = provider.finish(client, client.post("/auth/julianverse/start"))
    assert response.location.endswith("/me")
    with client.session_transaction() as cookie:
        assert cookie["_user_id"] == str(user_id)
    assert client.get("/admin").status_code == 200


def test_link_requires_exact_existing_discord_identity(setup_sso):
    app, client, provider = setup_sso
    user_id, _ = seed_user(app)
    login_discord(client, provider)
    provider.discord_id = "999999999999999999"
    response = provider.discord_finish(client, client.post("/auth/julianverse/link"))
    assert response.location.endswith("/settings")
    assert provider.exchange_count == 0
    with app.app_context():
        assert db.session.get(User, user_id).julianverse_identity is None


def test_callback_cannot_move_between_browsers_or_replay(setup_sso):
    app, client, provider = setup_sso
    started = client.post("/auth/julianverse/register")
    fields = parse_qs(urlsplit(started.location).query)
    stolen = "/auth/julianverse/callback?" + urlencode(
        {"state": fields["state"][0], "code": "stolen"}
    )
    assert app.test_client().get(stolen).location.endswith("/login")
    assert provider.exchange_count == 0
    _, callback = provider.finish(client, started)
    client.get(callback)
    assert provider.exchange_count == 1


def test_discord_callback_requires_state_and_once_only(setup_sso):
    app, client, provider = setup_sso
    assert client.get("/callback?code=stolen").location.endswith("/login")
    started = client.get("/auth/discord/login")
    fields = parse_qs(urlsplit(started.location).query)
    callback = "/callback?" + urlencode(
        {"state": fields["state"][0], "code": "one-use"}
    )
    client.get(callback)
    client.post("/logout")
    client.get(callback)
    with client.session_transaction() as cookie:
        assert "_user_id" not in cookie
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(User.id))) == 1


def test_unlink_requires_discord_confirmation_and_preserves_access(setup_sso):
    app, client, provider = setup_sso
    user_id, company_id = seed_user(app)
    login_discord(client, provider)
    link_sso(client, provider)
    provider.discord_id = "999999999999999999"
    provider.discord_finish(client, client.post("/auth/julianverse/unlink"))
    with app.app_context():
        assert db.session.get(User, user_id).julianverse_identity
    provider.discord_id = "123456789012345678"
    provider.discord_finish(client, client.post("/auth/julianverse/unlink"))
    with app.app_context():
        assert db.session.get(User, user_id).julianverse_identity is None
        assert db.session.get(Company, company_id).owner_id == user_id
        assert db.session.scalar(db.select(JulianverseSession)) is None
    assert client.get("/me").status_code == 200


def test_sso_only_cannot_unlink_until_discord_proven(setup_sso):
    app, client, provider = setup_sso
    register_sso(client, provider)
    assert client.post("/auth/julianverse/unlink").location.endswith("/settings")
    response = provider.discord_finish(client, client.post("/auth/discord/link"))
    assert response.location.endswith("/settings")
    with app.app_context():
        user = db.session.scalar(db.select(User))
        assert user.has_discord and user.role == "viewer"
    provider.discord_finish(client, client.post("/auth/julianverse/unlink"))
    client.post("/logout")
    login_discord(client, provider)
    assert client.get("/me").status_code == 200


def test_discord_cannot_be_taken_from_existing_account(setup_sso):
    app, client, provider = setup_sso
    user_id, company_id = seed_user(app)
    register_sso(client, provider)
    provider.discord_finish(client, client.post("/auth/discord/link"))
    with app.app_context():
        identity = db.session.scalar(db.select(JulianverseIdentity))
        assert not identity.user.has_discord
        assert db.session.get(Company, company_id).owner_id == user_id


def test_logout_revokes_server_session_and_copied_cookie(setup_sso):
    app, client, provider = setup_sso
    register_sso(client, provider)
    copied = app.test_client()
    copied.set_cookie(
        "session",
        client.get_cookie("session", domain="register.example.org").value,
        domain="register.example.org",
    )
    assert client.get("/logout").status_code == 405
    client.post("/logout")
    assert copied.get("/settings").location.endswith("/login")
    assert provider.revoked


def test_central_revocation_also_blocks_attaching_discord(setup_sso):
    app, client, provider = setup_sso
    register_sso(client, provider)
    age_session(app)
    provider.access.clear()
    assert client.post("/auth/discord/link").location.endswith("/login")
    with app.app_context():
        assert db.session.scalar(db.select(JulianverseSession)) is None


def test_refresh_rotation_and_provider_outage(setup_sso):
    app, client, provider = setup_sso
    register_sso(client, provider)
    age_session(app, True)
    assert client.get("/settings").status_code == 200
    assert provider.refresh_count == 1 and "refresh-1" not in provider.refresh
    age_session(app)
    provider.unavailable = True
    assert client.get("/settings").status_code == 503
    provider.unavailable = False
    assert client.get("/settings").status_code == 200


def test_sso_delete_needs_fresh_identity_and_soft_deletes_companies(setup_sso):
    app, client, provider = setup_sso
    user_id, company_id = seed_user(app)
    login_discord(client, provider)
    link_sso(client, provider)
    data = {"confirmation": "KONTO LÖSCHEN"}
    client.post("/settings/delete-account", data=data)
    with app.app_context():
        assert db.session.get(User, user_id).is_active
    provider.overrides = {"sub": "wrong-user"}
    provider.finish(client, client.post("/auth/confirm"))
    client.post("/settings/delete-account", data=data)
    with app.app_context():
        assert db.session.get(User, user_id).is_active
    provider.overrides = {}
    provider.finish(client, client.post("/auth/confirm"))
    client.post("/settings/delete-account", data=data)
    with app.app_context():
        assert not db.session.get(User, user_id).is_active
        assert db.session.get(Company, company_id).deleted_at
        assert db.session.scalar(db.select(JulianverseIdentity)) is None
        assert db.session.scalar(db.select(JulianverseSession)) is None
    assert client.get("/settings").status_code == 302


def test_legacy_discord_confirmation_and_export(setup_sso):
    app, client, provider = setup_sso
    user_id, company_id = seed_user(app)
    login_discord(client, provider)
    result = client.get("/settings/export")
    assert result.status_code == 200
    assert result.json["user"]["discord_id"] == provider.discord_id
    assert result.json["user"]["julianverse"] is None
    assert result.headers["Cache-Control"] == "no-store"
    provider.discord_finish(client, client.post("/auth/confirm"))
    client.post("/settings/delete-account", data={"confirmation": "KONTO LÖSCHEN"})
    with app.app_context():
        assert not db.session.get(User, user_id).is_active


def test_csrf_and_security_headers(setup_sso):
    app, client, provider = setup_sso
    app.config["WTF_CSRF_ENABLED"] = True
    assert client.post("/auth/julianverse/register").status_code == 400
    page = client.get("/login")
    assert page.status_code == 200
    assert b'<meta name="referrer" content="same-origin">' in page.data
    assert ISSUER in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"


def test_revocation_during_discord_link_blocks_callback(setup_sso):
    app, client, provider = setup_sso
    register_sso(client, provider)
    started = client.post("/auth/discord/link")
    age_session(app)
    provider.access.clear()
    result = provider.discord_finish(client, started)
    assert result.location.endswith("/login")
    with app.app_context():
        assert not db.session.scalar(db.select(User)).has_discord

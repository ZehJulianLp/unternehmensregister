import json
import sqlite3
import urllib.error
from contextlib import contextmanager

import pytest

from deploy import setup_julianverse as deploy


@pytest.mark.parametrize("initial", ["maintenance", "upstream", "network", "old_page"])
def test_https_check_waits_until_new_app_is_served(monkeypatch, initial):
    calls = []

    class Response:
        status = 200
        headers = {}

        def read(self):
            return (
                b"old page"
                if initial == "old_page" and len(calls) == 1
                else b"Mit Julianverse anmelden Discord"
            )

    @contextmanager
    def urlopen(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            if initial in ("maintenance", "upstream"):
                raise urllib.error.HTTPError(
                    request.full_url,
                    503 if initial == "maintenance" else 502,
                    "not ready",
                    {},
                    None,
                )
            if initial == "network":
                raise urllib.error.URLError("not ready")
        yield Response()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    deploy.check_https()
    assert len(calls) == 2
    assert all(request.get_header("Connection") == "close" for request in calls)


def test_https_check_fails_with_last_status_after_deadline(monkeypatch):
    clock = [0]

    def urlopen(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 503, "not ready", {}, None)

    monkeypatch.setattr(deploy.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(deploy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        deploy.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    with pytest.raises(RuntimeError, match="HTTPS.*HTTP 503"):
        deploy.check_https(timeout=2)


def test_maintenance_check_waits_for_our_own_marker(monkeypatch):
    calls = []

    def urlopen(request, **kwargs):
        calls.append(request)
        headers = {"X-Julianverse-Maintenance": "register"} if len(calls) == 2 else {}
        raise urllib.error.HTTPError(request.full_url, 503, "not ready", headers, None)

    monkeypatch.setattr(deploy.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    deploy.check_https(maintenance=True)
    assert len(calls) == 2


def test_data_guard_keeps_checking_rows_and_reports_table_without_contents(tmp_path):
    path = tmp_path / "register.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO user VALUES (1, 'private name')")
    before = deploy.fingerprint(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE julianverse_identity (user_id INTEGER)")
    deploy.require_unchanged(before, deploy.fingerprint(path), "Probelauf")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE user SET name='another private name' WHERE id=1")
    with pytest.raises(RuntimeError, match="user: Datensätze geändert") as error:
        deploy.require_unchanged(before, deploy.fingerprint(path), "Probelauf")
    assert "private name" not in str(error.value)


def test_proxy_changes_only_register_blocks_and_is_repeatable():
    original = (
        "server {\n"
        + deploy.MARKER
        + "}\nserver {\n"
        + deploy.MARKER
        + "}\nserver {\n    server_name example.org;\n}\n"
    )
    updated = deploy.nginx_config(original)
    assert updated.count("access_log off;") == 2
    assert updated.endswith("server {\n    server_name example.org;\n}\n")
    assert deploy.nginx_config(updated) == updated
    assert deploy.nginx_config(original, maintenance=True).count("return 503;") == 2
    with pytest.raises(RuntimeError):
        deploy.nginx_config("server_name unexpected.example;")


@pytest.mark.parametrize(
    "failure", ["none", "transient_https", "before_release", "after_release"]
)
def test_deploy_keeps_data_and_rolls_back_without_losing_new_writes(
    tmp_path, monkeypatch, failure
):
    root, backups, data, uploads = [
        tmp_path / name for name in ("repo", "backups", "data", "uploads")
    ]
    for directory in (root, backups, data, uploads):
        directory.mkdir()
    (root / ".env").write_text("FLASK_SECRET_KEY=test-only\n")
    nginx = tmp_path / "nginx.conf"
    original = "server {\n" + deploy.MARKER + "}\nserver {\n" + deploy.MARKER + "}\n"
    nginx.write_text(original)
    (uploads / "logo.png").write_bytes(b"original-logo")
    database = data / "register.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE company (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO company VALUES (1, 'Existing company')")
    commands = []
    container = {
        "Id": "existing",
        "Image": "old-image",
        "Config": {
            "Labels": {
                "com.docker.compose.project.working_dir": str(root),
                "com.docker.compose.project": "existing-project",
            },
            "Env": ["DATABASE_URL=sqlite:////app/instance/register.db"],
        },
        "Mounts": [
            {"Destination": "/app/instance", "Source": str(data)},
            {"Destination": "/app/static/uploads", "Source": str(uploads)},
        ],
    }

    def run(args, **kwargs):
        commands.append(args)
        if args[:2] == ["docker", "ps"]:
            return "existing"
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([container])
        if args[-2:] == ["config", "--images"]:
            return "existing-project-web"
        if "up" in args and any(str(a).endswith("new.json") for a in args):
            assert "return 503;" in nginx.read_text()
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE julianverse_identity (user_id INTEGER)")
        return ""

    def check_local():
        if failure == "before_release":
            # Simulate a failed startup that changed existing data while maintenance is active.
            with sqlite3.connect(database) as db:
                db.execute("UPDATE company SET name='bad migration'")
            raise RuntimeError("startup failed")

    class Response:
        status = 200

        def read(self):
            return b"Mit Julianverse anmelden Discord"

    public_calls = []

    @contextmanager
    def urlopen(*args, **kwargs):
        if "return 503;" in nginx.read_text():
            raise urllib.error.HTTPError(
                "https://amt.julianverse.de/login",
                503,
                "maintenance",
                {"X-Julianverse-Maintenance": "register"},
                None,
            )
        assert "return 503;" not in nginx.read_text()
        public_calls.append(True)
        if failure == "transient_https" and len(public_calls) == 1:
            raise urllib.error.HTTPError(
                "https://amt.julianverse.de/login", 503, "old worker", {}, None
            )
        if failure == "after_release":
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO company VALUES (2, 'New user write')")
            raise RuntimeError("external health check failed")
        yield Response()

    real_mkdtemp = deploy.tempfile.mkdtemp
    monkeypatch.setattr(deploy, "ROOT", root)
    monkeypatch.setattr(deploy, "NGINX", nginx)
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deploy.os, "chown", lambda *args: None)
    monkeypatch.setattr(deploy.os, "umask", lambda *args: 0)
    monkeypatch.setattr(deploy.signal, "signal", lambda *args: None)
    monkeypatch.setattr(deploy.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        deploy.tempfile,
        "mkdtemp",
        lambda **kw: real_mkdtemp(prefix=kw["prefix"], dir=backups),
    )
    monkeypatch.setattr(deploy, "run", run)
    monkeypatch.setattr(deploy, "check_local", check_local)
    monkeypatch.setattr(deploy.urllib.request, "urlopen", urlopen)
    if failure in ("none", "transient_https"):
        deploy.main()
        assert "access_log off;" in nginx.read_text()
        assert not any(
            "up" in cmd and any(str(a).endswith("old.json") for a in cmd)
            for cmd in commands
        )
    else:
        with pytest.raises(RuntimeError):
            deploy.main()
        assert nginx.read_text() == original
        assert any(
            "up" in cmd and any(str(a).endswith("old.json") for a in cmd)
            for cmd in commands
        )
    with sqlite3.connect(database) as db:
        assert (
            db.execute("SELECT name FROM company WHERE id=1").fetchone()[0]
            == "Existing company"
        )
        assert db.execute("SELECT count(*) FROM company").fetchone()[0] == (
            2 if failure == "after_release" else 1
        )
    backup = next(backups.iterdir())
    assert (backup / "uploads.tar.gz").is_file()
    assert (uploads / "logo.png").read_bytes() == b"original-logo"
    assert (backup / "register.env").stat().st_mode & 0o777 == 0o600

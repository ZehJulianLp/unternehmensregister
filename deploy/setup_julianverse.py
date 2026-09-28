"""Deploy into the existing Compose project; retain its database and upload volumes."""

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NGINX = Path("/etc/nginx/nginx.conf")
MARKER = "        server_name amt.julianverse.de;\n"
LOGGING = """        # Julianverse Register: OAuth codes must not enter logs.
        access_log off;
        error_log /var/log/nginx/register.error.log crit;
"""
MAINTENANCE = """        add_header X-Julianverse-Maintenance "register" always;
        return 503; # Register wird aktualisiert
"""


def run(args, timeout=120):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} fehlgeschlagen: {result.stderr[-2000:]}")
    return result.stdout.strip()


def nginx_config(original, maintenance=False):
    if original.count(MARKER) != 2:
        raise RuntimeError(
            "Erwartete zwei amt.julianverse.de-Serverblöcke nicht gefunden."
        )
    # Only insert settings in this domain's two blocks; other hosts remain unchanged.
    result = original.replace(LOGGING, "").replace(MAINTENANCE, "")
    extra = LOGGING + (MAINTENANCE if maintenance else "")
    return result.replace(MARKER, MARKER + extra)


def fingerprint(path):
    """Compare every existing row without printing personal data."""
    result = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite-Integritätsprüfung fehlgeschlagen.")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Ungültige Datenbankverknüpfung gefunden.")
        tables = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for name, schema in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = sorted(repr(row) for row in db.execute(f"SELECT * FROM {quoted}"))
            result[name] = (
                schema,
                hashlib.sha256("\n".join(rows).encode()).hexdigest(),
            )
    return result


def backup_database(source, target):
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src,
        sqlite3.connect(target) as dst,
    ):
        src.backup(dst)
    os.chmod(target, 0o600)


def require_unchanged(before, after, phase):
    differences = []
    for name, (schema, rows) in before.items():
        current = after.get(name)
        if current is None:
            differences.append(f"{name}: Tabelle fehlt")
        elif current[0] != schema:
            differences.append(f"{name}: Tabellenschema geändert")
        elif current[1] != rows:
            differences.append(f"{name}: Datensätze geändert")
    if differences:
        raise RuntimeError(
            f"{phase}: Bestehende Registerdaten wurden verändert; Abbruch. "
            + "; ".join(differences)
        )


def check_local():
    for _ in range(45):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:5000/login",
                headers={"Host": "amt.julianverse.de", "X-Forwarded-Proto": "https"},
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                body = response.read().decode()
                if (
                    response.status == 200
                    and "Mit Julianverse anmelden" in body
                    and "Discord" in body
                ):
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError("Die neue Anmeldeseite wurde nicht erreichbar.")


def check_https(
    timeout=45, *, maintenance=False, url="https://amt.julianverse.de/login"
):
    """A successful reload command only signals Nginx; wait for its public response."""
    deadline = time.monotonic() + timeout
    last = "keine Antwort"
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            url, headers={"Connection": "close", "Cache-Control": "no-cache"}
        )
        try:
            with urllib.request.urlopen(
                request, timeout=min(3, max(0.1, deadline - time.monotonic()))
            ) as response:
                body = response.read().decode(errors="replace")
                last = f"HTTP {response.status}"
                if not maintenance and response.status == 200:
                    if "Mit Julianverse anmelden" in body and "Discord" in body:
                        return
                    last += " ohne die neue Anmeldeseite"
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
            ready = (
                maintenance
                and error.code == 503
                and error.headers.get("X-Julianverse-Maintenance") == "register"
            )
            error.close()
            if ready:
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last = f"Verbindungsfehler ({type(error).__name__})"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    target = "Wartungsmodus" if maintenance else "Julianverse-Anmeldeseite"
    raise RuntimeError(
        f"HTTPS-Prüfung: {target} nach {timeout} Sekunden nicht erreichbar ({last})."
    )


def main():
    if os.geteuid() != 0:
        raise SystemExit(
            f"Bitte ausführen: sudo bash {ROOT}/deploy/setup-julianverse.sh"
        )
    os.umask(0o077)
    original = NGINX.read_text()
    nginx_config(original)  # Check shape before changing anything.
    run(["nginx", "-t"])
    candidates = run(
        ["docker", "ps", "--filter", "label=com.docker.compose.service=web", "-q"]
    ).split()
    containers = (
        json.loads(run(["docker", "inspect", *candidates])) if candidates else []
    )
    matches = [
        c
        for c in containers
        if c["Config"]["Labels"].get("com.docker.compose.project.working_dir")
        == str(ROOT)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Der laufende Register-Container konnte nicht eindeutig zugeordnet werden."
        )
    current = matches[0]
    project = current["Config"]["Labels"]["com.docker.compose.project"]
    mounts = {m["Destination"]: Path(m["Source"]) for m in current["Mounts"]}
    data_dir, uploads = mounts["/app/instance"], mounts["/app/static/uploads"]
    database = data_dir / "register.db"
    if not database.is_file() or not uploads.is_dir():
        raise RuntimeError("Vorhandene Datenbank oder Uploads fehlen; Abbruch.")
    # This script is for the current SQLite layout, not an implicit database move.
    env = dict(
        value.split("=", 1) for value in current["Config"]["Env"] if "=" in value
    )
    if env.get("DATABASE_URL") != "sqlite:////app/instance/register.db":
        raise RuntimeError("Unerwarteter Datenbankpfad; keine Änderung vorgenommen.")
    backup = Path(tempfile.mkdtemp(prefix="register-sso.", dir="/var/backups"))
    os.chmod(backup, 0o700)
    shutil.copy2(NGINX, backup / "nginx.conf")
    shutil.copy2(ROOT / ".env", backup / "register.env")
    os.chmod(backup / "register.env", 0o600)
    os.chmod(ROOT / ".env", 0o600)
    stamp = backup.name.removeprefix("register-sso.").lower()
    old_tag, new_tag = f"register-before-sso:{stamp}", f"register-julianverse:{stamp}"
    run(["docker", "tag", current["Image"], old_tag])
    (backup / "previous-image.txt").write_text(old_tag + "\n")
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "-p",
        project,
        "-f",
        str(ROOT / "docker-compose.yml"),
    ]
    default_image = run([*compose, "config", "--images"]).splitlines()
    if len(default_image) != 1:
        raise RuntimeError("Unerwartete Compose-Services; keine Änderung vorgenommen.")
    old_override, new_override = backup / "old.json", backup / "new.json"
    for path, tag in [(old_override, old_tag), (new_override, new_tag)]:
        path.write_text(
            json.dumps({"services": {"web": {"image": tag, "pull_policy": "never"}}})
        )
    print(f"Sicherung: {backup}", flush=True)
    print("Baue das neue Image; das Register bleibt dabei erreichbar.", flush=True)
    run(["docker", "build", "-t", new_tag, str(ROOT)], timeout=1200)
    # Validate the additive schema against a copy before the service is stopped.
    stage = backup / "check"
    stage.mkdir()
    backup_database(database, stage / "register.db")
    before = fingerprint(stage / "register.db")
    run(
        [
            "docker",
            "run",
            "--rm",
            "--env-file",
            str(ROOT / ".env"),
            "-e",
            "DATABASE_URL=sqlite:////check/register.db",
            "-v",
            f"{stage}:/check",
            new_tag,
            "python",
            "-c",
            "from app import create_app; a=create_app(); assert a.config['JULIANVERSE_ENABLED']; assert a.test_client().get('/login').status_code == 200",
        ],
        timeout=90,
    )
    after = fingerprint(stage / "register.db")
    require_unchanged(before, after, "Probelauf")
    changed = False
    stopped = False
    released = False

    def interrupted(signum, frame):
        raise RuntimeError("Einrichtung unterbrochen.")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        print(
            "Aktiviere kurz den Wartungsmodus nur für das Register und sichere seine Daten.",
            flush=True,
        )
        changed = True
        NGINX.write_text(nginx_config(original, maintenance=True))
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
        check_https(maintenance=True)
        stopped = True
        run(["docker", "stop", "--time", "30", current["Id"]], timeout=45)
        backup_database(database, backup / "register.db")
        baseline = fingerprint(backup / "register.db")
        with tarfile.open(backup / "uploads.tar.gz", "w:gz") as archive:
            archive.add(uploads, arcname="uploads")
        print(
            "Starte das Register mit Julianverse und den bestehenden Volumes.",
            flush=True,
        )
        run(
            [
                *compose,
                "-f",
                str(new_override),
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "web",
            ],
            timeout=120,
        )
        check_local()
        live = fingerprint(database)
        require_unchanged(baseline, live, "Start")
        NGINX.write_text(nginx_config(original))
        run(["nginx", "-t"])
        # Once a public reload is requested, new writes may arrive even if the
        # reload command itself times out. Never restore an older database then.
        released = True
        run(["systemctl", "reload", "nginx"])
        print(
            "Warte auf die öffentliche Julianverse-Anmeldeseite (maximal 45 Sekunden).",
            flush=True,
        )
        check_https()
    except BaseException:
        print("Einrichtung fehlgeschlagen; starte das vorherige Image.", flush=True)
        try:
            if stopped:
                run([*compose, "-f", str(old_override), "stop", "web"], timeout=60)
                # Never roll back data after reopening the site to user writes.
                if not released and (backup / "register.db").is_file():
                    owner = database.stat()
                    for suffix in ("-wal", "-shm"):
                        Path(str(database) + suffix).unlink(missing_ok=True)
                    shutil.copyfile(backup / "register.db", database)
                    os.chown(database, owner.st_uid, owner.st_gid)
                    os.chmod(database, owner.st_mode & 0o777)
                run(
                    [
                        *compose,
                        "-f",
                        str(old_override),
                        "up",
                        "-d",
                        "--no-build",
                        "--no-deps",
                        "web",
                    ],
                    timeout=120,
                )
        finally:
            if changed:
                shutil.copy2(backup / "nginx.conf", NGINX)
                run(["nginx", "-t"])
                run(["systemctl", "reload", "nginx"])
        raise
    run(["docker", "tag", new_tag, default_image[0]])
    print("Fertig: https://amt.julianverse.de/login", flush=True)
    print(
        "Bestehende Benutzer, Firmen, Rollen und Uploads wurden beibehalten.",
        flush=True,
    )
    print(f"Sicherung und vorheriges Image: {backup}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        raise SystemExit(f"FEHLER: {error}") from None

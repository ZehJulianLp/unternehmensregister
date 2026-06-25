# Redstone & Rails Unternehmensregister

Eine klassische Flask-Webapp für ein fiktives Unternehmensregister mit Discord OAuth2 Login, Rollenverwaltung, Firmenanträgen, Admin-Freigabe, Audit-Log und Discord-Benachrichtigungen.

## Features

- Flask Backend mit Jinja Templates
- SQLite-Datenbank via SQLAlchemy
- Discord OAuth2 Login
- Session-Handling mit Flask-Login
- Rollen: Zuschauer, Mitglied, Eigentümer, Admin
- Öffentliche Firmenübersicht mit Suche, Filtern und Pagination
- Firmen beantragen, bearbeiten, freigeben, ablehnen und löschen
- Soft Delete für Firmen
- Register-ID pro Firma, z. B. `RR-0001`
- Firmenlogos mit serverseitiger Bildprüfung
- Miteigentümer pro Firma
- Tochterunternehmen und Mutterunternehmen pro Firma
- Admin-Dashboard mit Statistiken, Audit-Log und Benutzerverwaltung
- Optional Discord-DMs und Admin-Channel-Benachrichtigungen
- CSRF-Schutz, Security Headers und Rate Limiting
- Light-/Darkmode
- Docker-Setup mit persistenten Volumes

## Tech Stack

- Python 3.11+
- Flask
- Flask-SQLAlchemy
- Flask-Login
- Flask-WTF
- Flask-Limiter
- Pillow
- SQLite
- Gunicorn

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Kopiere danach die Beispielkonfiguration:

```powershell
Copy-Item .env.example .env
```

Trage deine Werte in `.env` ein.

## Docker

Mit Docker Compose:

```powershell
docker compose up --build
```

Danach:

```text
http://localhost:5000/
```

Die Compose-Konfiguration nutzt persistente Volumes für:

- SQLite-Datenbank: `/app/instance`
- Uploads: `/app/static/uploads`

## Konfiguration

```env
FLASK_SECRET_KEY=change-me
DISCORD_CLIENT_ID=your-discord-client-id
DISCORD_CLIENT_SECRET=your-discord-client-secret
DISCORD_REDIRECT_URI=http://localhost:5000/callback
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_ADMIN_CHANNEL_ID=
DATABASE_URL=sqlite:///register.db
ADMIN_DISCORD_IDS=123456789012345678
SESSION_COOKIE_SECURE=false
RATELIMIT_STORAGE_URI=memory://
```

`DISCORD_ADMIN_CHANNEL_ID` ist optional. Leer lassen deaktiviert Channel-Posts.

Für Produktion mit HTTPS:

```env
SESSION_COOKIE_SECURE=true
```

## Discord Setup

1. Öffne das Discord Developer Portal: https://discord.com/developers/applications
2. Erstelle oder wähle eine Application.
3. Unter `OAuth2` füge als Redirect URL hinzu:

```text
http://localhost:5000/callback
```

4. Kopiere `Client ID` und `Client Secret` in `.env`.
5. Unter `Bot` erstelle einen Bot und kopiere den Bot Token nach `DISCORD_BOT_TOKEN`.
6. Lade den Bot auf deinen Server ein:
   - `OAuth2` -> `URL Generator`
   - Scope: `bot`
   - Permission: `Send Messages`
7. Aktiviere in Discord den Entwicklermodus und kopiere deine User-ID nach `ADMIN_DISCORD_IDS`.

## Starten ohne Docker

```powershell
python -m flask --app app run
```

Danach:

```text
http://localhost:5000/
```

## Projektstruktur

```text
.
|-- app.py
|-- models.py
|-- requirements.txt
|-- Dockerfile
|-- docker-compose.yml
|-- .env.example
|-- LICENSE
|-- templates/
|-- static/
|   |-- css/
|   `-- uploads/
`-- instance/
```

## Rollen und Rechte

- Nicht eingeloggte Nutzer können Firmen ansehen.
- Zuschauer können Firmen ansehen, aber nichts beantragen.
- Mitglieder können Firmen beantragen.
- Eigentümer und Miteigentümer können ihre Firmen bearbeiten.
- Admins können alle Firmen verwalten, freigeben, ablehnen, löschen und Nutzerrollen ändern.
- Alle mutierenden Routen prüfen Rechte serverseitig.

## Sicherheit

Bereits enthalten:

- CSRF-Schutz für Formulare
- Flask-Login Sessions
- OAuth `state` Prüfung
- Security Headers
- Rate Limiting
- Serverseitige Rechteprüfung
- Upload-Prüfung mit Pillow
- Secrets nur über `.env`
- `.gitignore` für `.env`, Logs, DB, Uploads und Cache

Wichtig für Produktion:

- Nicht mit Flask Development Server betreiben
- HTTPS verwenden
- `SESSION_COOKIE_SECURE=true` setzen
- Starkes `FLASK_SECRET_KEY` nutzen
- Datenbankmigrationen professionell mit Flask-Migrate/Alembic verwalten

## Entwicklung

Die App erstellt fehlende SQLite-Tabellen und einfache Spalten automatisch beim Start. Für größere Produktionseinsätze sollte das durch richtige Migrationen ersetzt werden.

## Lizenz

Dieses Projekt steht unter der MIT-Lizenz. Siehe [LICENSE](LICENSE).

# Redstone & Rails Unternehmensregister

Eine klassische Flask-Webapp für ein fiktives Unternehmensregister mit Julianverse- und Discord-Login, Rollenverwaltung, Firmenanträgen, Admin-Freigabe, Audit-Log und Discord-Benachrichtigungen.

## Features

- Flask Backend mit Jinja Templates
- SQLite-Datenbank via SQLAlchemy
- Julianverse SSO (OpenID Connect mit PKCE) und bestehender Discord OAuth2 Login
- Session-Handling mit Flask-Login
- Rollen: Zuschauer, Mitglied, Eigentümer, Admin
- Öffentliche Firmenübersicht mit Suche, Filtern und wählbarer Pagination
- Firmen beantragen, bearbeiten, freigeben, ablehnen und löschen
- Firmen nach Verein und Profitunternehmen unterscheiden
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
DISCORD_PUBLIC_KEY=your-discord-application-public-key
DISCORD_REDIRECT_URI=http://localhost:5000/callback
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_ADMIN_CHANNEL_ID=
DISCORD_GUILD_ID=
DISCORD_MEMBER_ROLE_IDS=
DATABASE_URL=sqlite:///register.db
ADMIN_DISCORD_IDS=123456789012345678
SESSION_COOKIE_SECURE=false
RATELIMIT_DEFAULT=1000 per hour
RATELIMIT_STORAGE_URI=memory://
```

`DISCORD_ADMIN_CHANNEL_ID` ist optional. Leer lassen deaktiviert Channel-Posts.
`DISCORD_GUILD_ID` und `DISCORD_MEMBER_ROLE_IDS` sind optional. Wenn beide gesetzt sind, bekommen Nutzer beim Login automatisch die Rolle `Mitglied`, sobald sie auf dem Discord-Server eine der angegebenen Rollen haben. Mehrere Rollen-IDs werden kommagetrennt eingetragen.
`RATELIMIT_DEFAULT` steuert das globale Standardlimit. Routen mit sensiblen Aktionen haben zusätzlich eigene Limits.

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
5. Kopiere unter `General Information` den `Public Key` nach `DISCORD_PUBLIC_KEY`.
6. Unter `Bot` erstelle einen Bot und kopiere den Bot Token nach `DISCORD_BOT_TOKEN`.
7. Lade den Bot auf deinen Server ein:
   - `OAuth2` -> `URL Generator`
   - Scope: `bot`
   - Permission: `Send Messages`
8. Optional für automatische Mitgliederrollen:
   - Trage deine Server-ID in `DISCORD_GUILD_ID` ein.
   - Trage erlaubte Rollen-IDs in `DISCORD_MEMBER_ROLE_IDS` ein.
   - Aktiviere im Developer Portal beim Bot den Server Members Intent, falls Discord das Abfragen von Servermitgliedern blockiert.
9. Aktiviere in Discord den Entwicklermodus und kopiere deine User-ID nach `ADMIN_DISCORD_IDS`.

### Discord Slash Command

Die App stellt einen Discord-Interactions-Endpoint bereit:

```text
https://deine-domain.example/discord/interactions
```

Diesen Endpoint trägst du im Discord Developer Portal unter `Interactions Endpoint URL` ein. Der enthaltene Slash Command heißt `/bremsweg` und gibt den Mindestsignalabstand für die Option `geschwindigkeit` in km/h aus.

Für einen schnellen Guild-Test kannst du den Command so registrieren:

```bash
curl -X POST \
  -H "Authorization: Bot $DISCORD_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  "https://discord.com/api/v10/applications/$DISCORD_CLIENT_ID/guilds/$DISCORD_GUILD_ID/commands" \
  -d '{
    "name": "bremsweg",
    "description": "Berechnet den Mindestsignalabstand für eine Geschwindigkeit.",
    "options": [
      {
        "type": 4,
        "name": "geschwindigkeit",
        "description": "Geschwindigkeit in km/h",
        "required": true,
        "min_value": 0,
        "max_value": 160
      }
    ]
  }'
```

## Starten ohne Docker

```powershell
python -m flask --app wsgi run
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


## Julianverse Account

Auf der Anmeldeseite steht **Mit Julianverse anmelden** zur Verfügung. Bestehende
Nutzer melden sich einmal mit Discord an und wählen in **Einstellungen →
Julianverse Account** die Verknüpfung. Sie bestätigen zuerst ihren bisherigen
Discord-Zugang und danach ihren Julianverse Account. Firmen, Miteigentümer,
Register-IDs, Logos, Verlauf und Registerrollen bleiben beim bisherigen Nutzer.
Gleiche Namen führen zu keiner automatischen Zusammenführung.

Neue Nutzer wählen ausdrücklich **Neues Registerkonto erstellen**. Sie starten
als Zuschauer. Register-Admins vergeben die lokalen Rechte wie bisher;
Julianverse-Adminrechte werden nicht als Registerrechte übernommen. Discord kann
später hinzugefügt werden, sofern dieser Discord-Zugang noch keinem anderen
Registerkonto gehört. Zum Trennen von Julianverse ist ein bestätigter
Discord-Zugang nötig. Die Löschung des Registerkontos erfordert eine frische,
einmal verwendbare Anmeldebestätigung und löscht keinen Julianverse Account.

### Einrichtung

In Account einen vertraulichen OIDC-Client mit folgenden Werten anlegen:

- Callback: `https://amt.julianverse.de/auth/julianverse/callback`
- Scopes: `openid profile`
- Grants: `authorization_code`, `refresh_token`
- Token-Authentifizierung: `client_secret_basic`

In der privaten `.env` ergänzen:

```env
PUBLIC_BASE_URL=https://amt.julianverse.de
JULIANVERSE_ISSUER=https://account.julianverse.de
JULIANVERSE_CLIENT_ID=client-id
JULIANVERSE_CLIENT_SECRET=private-client-secret
```

`FLASK_SECRET_KEY` bleibt dauerhaft gleich; er schützt auch die verschlüsselten
SSO-Tokens. Produktionsstart: `gunicorn --bind 0.0.0.0:5000 --workers 2 wsgi:app`.
Fehlende Client-Werte deaktivieren Julianverse, der Discord-Zugang bleibt nutzbar.

Auf dem bestehenden Server führt das folgende Skript die Bereitstellung aus:

```bash
sudo bash /home/srvmgr/unternehmensregister/deploy/setup-julianverse.sh
```

Es baut das Image, prüft die Erweiterung an einer Datenbankkopie und schaltet dann
nur das Register kurz in den Wartungsmodus. Es sichert die SQLite-Datenbank,
Uploads, Konfiguration und das vorherige Image. Die bestehenden Compose-Volumes
bleiben erhalten; vor der Freigabe werden alle alten Datensätze verglichen.
Nach dem Nginx-Neuladen wartet das Skript auf den bestätigten Wartungsmodus bzw.
bis zu 45 Sekunden auf die neue HTTPS-Anmeldeseite. Kurze 503-Antworten während
der Umschaltung lösen dadurch keine vorzeitige Rückkehr zur alten Version aus.
Bei einem anhaltenden Fehler wird das vorherige Image gestartet. Nginx protokolliert für
`amt.julianverse.de` keine OAuth-Codes mehr. Andere Domains werden nicht geändert.
Sicherungen liegen geschützt unter `/var/backups/register-sso.*`.

Die Erweiterung fügt drei Tabellen für Identitäten, Sitzungen und Anmeldeversuche
hinzu. Das bisherige User-Schema bleibt erhalten; Nutzer ohne Discord haben darin
eine interne Kennung, die nicht als Discord-ID angezeigt oder weitergegeben wird.
SSO-Tokens liegen verschlüsselt in SQLite; der Browser bekommt nur eine signierte
Sitzung mit zufälliger Referenz. SSO-Sitzungen gelten bis zu 30 Tage. Spätestens
bei der nächsten Anfrage nach 60 Sekunden wird der zentrale Zugang erneut geprüft.
Ein Ausfall bei der Tokenrotation kann eine erneute Anmeldung erfordern, weil
Refresh-Tokens nur einmal verwendet werden dürfen.

### Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Die Tests verwenden temporäre Datenbanken und einen isolierten OAuth-Anbieter.
Sie prüfen signierte ID-Tokens, PKCE, CSRF, gebundene und einmalige Callbacks,
Verknüpfung und Trennung, unveränderte Firmenrechte, Tokenrotation, Widerruf
sowie die erneute Identitätsprüfung vor einer Kontolöschung.

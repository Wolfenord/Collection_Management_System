#!/usr/bin/env bash
# ============================================================================
# CMS: PostgreSQL-Einrichtung für Arch Linux — idempotent, mehrfach ausführbar.
#
# Was das Skript tut (fragt vor jedem sudo-Schritt das sudo-Passwort ab):
#   1. Installiert PostgreSQL (pacman), falls es fehlt.
#   2. Initialisiert den Cluster unter /var/lib/postgres/data (nur beim ersten
#      Mal; Auth: peer für lokale Unix-Sockets, scram-sha-256 für TCP).
#   3. Startet & aktiviert den systemd-Dienst "postgresql".
#   4. Legt Rolle "cms" (mit Zufallspasswort) und Datenbank "cms" an bzw.
#      setzt das Passwort neu, wenn es die Rolle schon gibt.
#   5. Exportiert optional die vorhandenen SQLite-Daten (dumpdata) …
#   6. … stellt .env auf DB_ENGINE=postgres um (bestehende Werte bleiben,
#      DB_PASSWORD wird wiederverwendet, wenn schon eines in .env steht),
#      führt die Migrationen aus …
#   7. … und importiert die SQLite-Daten (loaddata), falls exportiert.
#   8. Verbindungstest + Testsuite-Hinweis.
#
# Aufruf (im Projektverzeichnis, als normaler Benutzer mit sudo-Rechten):
#   ./scripts/setup_postgres.sh              # fragt bei SQLite-Daten nach
#   ./scripts/setup_postgres.sh --with-data  # SQLite-Daten ohne Nachfrage übernehmen
#   ./scripts/setup_postgres.sh --no-data    # nur leere Datenbank einrichten
#
# Rückweg jederzeit: in .env  DB_ENGINE=sqlite  setzen — die SQLite-Datei
# bleibt unangetastet liegen.
# ============================================================================
set -euo pipefail

# --- Konfiguration ----------------------------------------------------------
DB_NAME="cms"
DB_USER="cms"
DB_HOST="127.0.0.1"
DB_PORT="5432"
DATA_DIR="/var/lib/postgres/data"

# --- Projektpfade -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PY="$PROJECT_DIR/.venv/bin/python"
ENV_FILE="$PROJECT_DIR/.env"
DUMP_FILE="$PROJECT_DIR/sqlite-dump-$(date +%Y%m%d-%H%M%S).json"

MIGRATE_DATA="ask"
case "${1:-}" in
    --with-data) MIGRATE_DATA="yes" ;;
    --no-data)   MIGRATE_DATA="no" ;;
    "" )         ;;
    * ) echo "Unbekannte Option: $1 (erlaubt: --with-data | --no-data)"; exit 1 ;;
esac

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    ✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    ✘ %s\033[0m\n' "$*" >&2; exit 1; }

[[ -x "$PY" ]] || die "venv nicht gefunden: $PY — Skript aus dem Projekt heraus ausführen."
[[ $EUID -ne 0 ]] || die "Bitte als normaler Benutzer ausführen (das Skript nutzt sudo selbst)."
command -v sudo >/dev/null || die "sudo wird benötigt."

# --- 1. PostgreSQL installieren --------------------------------------------
say "PostgreSQL-Installation prüfen"
if command -v postgres >/dev/null 2>&1 || [[ -x /usr/bin/postgres ]]; then
    ok "PostgreSQL ist installiert ($(postgres --version 2>/dev/null || echo unbekannt))"
else
    sudo pacman -S --needed --noconfirm postgresql
    ok "PostgreSQL installiert"
fi

# --- 2. Cluster initialisieren ----------------------------------------------
say "Datenbank-Cluster prüfen ($DATA_DIR)"
if sudo test -f "$DATA_DIR/PG_VERSION"; then
    ok "Cluster existiert bereits (Version $(sudo cat "$DATA_DIR/PG_VERSION"))"
else
    sudo -u postgres initdb --locale=C.UTF-8 --encoding=UTF8 -D "$DATA_DIR" \
        --auth-local=peer --auth-host=scram-sha-256
    ok "Cluster initialisiert (peer lokal, scram-sha-256 über TCP)"
fi

# --- 3. Dienst starten -------------------------------------------------------
say "Dienst starten & aktivieren"
sudo systemctl enable --now postgresql
for _ in $(seq 1 30); do
    if sudo -u postgres pg_isready -q 2>/dev/null; then break; fi
    sleep 1
done
sudo -u postgres pg_isready -q || die "PostgreSQL antwortet nicht (journalctl -u postgresql)."
ok "PostgreSQL läuft"

# --- 4. Rolle + Datenbank ----------------------------------------------------
say "Rolle „$DB_USER“ und Datenbank „$DB_NAME“ einrichten"
# Bestehendes Passwort aus .env wiederverwenden, sonst neues erzeugen (hex ist
# shell-/sed-sicher).
DB_PASSWORD="$(grep -s -oP '(?<=^DB_PASSWORD=).*' "$ENV_FILE" || true)"
if [[ -z "$DB_PASSWORD" ]]; then
    DB_PASSWORD="$(openssl rand -hex 24)"
    ok "Neues Datenbank-Passwort erzeugt"
else
    ok "Datenbank-Passwort aus .env übernommen"
fi

sudo -u postgres psql -v ON_ERROR_STOP=1 --quiet <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
    ELSE
        ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';
    END IF;
END
\$\$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
    ok "Datenbank angelegt"
else
    ok "Datenbank existiert bereits"
fi

# --- 5. SQLite-Daten exportieren (optional) ----------------------------------
DO_DATA="no"
if [[ -f "$PROJECT_DIR/db.sqlite3" ]]; then
    if [[ "$MIGRATE_DATA" == "ask" ]]; then
        read -r -p "Vorhandene SQLite-Daten nach PostgreSQL übernehmen? [J/n] " answer
        [[ "${answer,,}" =~ ^(n|nein|no)$ ]] || DO_DATA="yes"
    else
        DO_DATA="$MIGRATE_DATA"
    fi
fi
if [[ "$DO_DATA" == "yes" ]]; then
    say "SQLite-Daten exportieren → $DUMP_FILE"
    ( cd "$PROJECT_DIR" && DB_ENGINE=sqlite "$PY" manage.py dumpdata \
        --natural-foreign --natural-primary \
        -e contenttypes -e auth.permission -e admin.logentry -e sessions.session \
        --indent 1 -o "$DUMP_FILE" )
    ok "Export fertig ($(du -h "$DUMP_FILE" | cut -f1))"
fi

# --- 6. .env umstellen + Migrationen -----------------------------------------
say ".env auf PostgreSQL umstellen"
touch "$ENV_FILE"
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
set_env DB_ENGINE postgres
set_env DB_NAME "$DB_NAME"
set_env DB_USER "$DB_USER"
set_env DB_PASSWORD "$DB_PASSWORD"
set_env DB_HOST "$DB_HOST"
set_env DB_PORT "$DB_PORT"
ok ".env aktualisiert (DB_ENGINE=postgres)"
if [[ -n "${DB_ENGINE:-}" ]]; then
    warn "Achtung: In dieser Shell ist DB_ENGINE=${DB_ENGINE} exportiert und überstimmt .env."
fi

say "Migrationen ausführen"
( cd "$PROJECT_DIR" && "$PY" manage.py migrate )
ok "Schema steht"

# --- 7. Daten importieren -----------------------------------------------------
if [[ "$DO_DATA" == "yes" ]]; then
    say "Daten importieren (loaddata)"
    ( cd "$PROJECT_DIR" && "$PY" manage.py loaddata "$DUMP_FILE" )
    ok "Daten übernommen — Dump bleibt als Sicherung liegen: $DUMP_FILE"
fi

# --- 8. Verbindungstest -------------------------------------------------------
say "Verbindung prüfen"
( cd "$PROJECT_DIR" && "$PY" manage.py shell -c "
from django.db import connection
connection.ensure_connection()
from django.contrib.auth import get_user_model
from Collection_Management_System.models import Collection, Item
print('Backend :', connection.vendor, connection.pg_version if hasattr(connection, 'pg_version') else '')
print('Benutzer:', get_user_model().objects.count())
print('Sammlungen:', Collection.objects.count(), '· Gegenstände:', Item.objects.count())
" )
ok "PostgreSQL ist eingerichtet."

cat <<EOF

Fertig! Nächste Schritte:
  • Testsuite gegen PostgreSQL: .venv/bin/python manage.py test
  • Entwicklungsserver:        .venv/bin/python manage.py runserver
  • Zurück zu SQLite:          in .env DB_ENGINE=sqlite setzen (Datei blieb unangetastet).
EOF

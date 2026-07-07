# Sicherheitskonzept — Collection Management System

Stand: Juli 2026. Dieses Dokument beschreibt die umgesetzten Schutzmaßnahmen,
die DSGVO-relevanten Eigenschaften und die Checkliste für den Produktivbetrieb.

## Grundprinzip

Das CMS verwaltet private Sammlungsdaten. Nichts davon ist öffentlich:
jede Seite außer Login/Registrierung/Passwort-Reset erfordert eine Anmeldung,
Zugriff auf Sammlungen ist zusätzlich pro Benutzer autorisiert
(Eigentümer / geteilte Freigaben mit `view`/`edit`).

## Authentifizierung

- **Registrierungs-Whitelist**: Neue Konten sind inaktiv (`is_active=False`),
  bis ein Administrator sie freigibt. Die Sperre greift im Auth-Backend selbst.
  Die Registrierung kann per Laufzeit-Einstellung komplett geschlossen werden.
- **Passkeys (WebAuthn/FIDO2)** als passwortlose Anmelde-Option
  (`accounts/passkeys.py`, `static/js/passkeys.js`):
  - Registrierung auf der Profilseite, Anmeldung per Button auf der Login-Seite.
  - Es wird nur der **öffentliche** Schlüssel gespeichert; der private bleibt
    auf dem Gerät (Handy, Sicherheitsschlüssel, Passwort-Manager).
  - Discoverable Credentials (kein Benutzername nötig, keiner wird geleakt),
    User-Verification (PIN/Biometrie) ist **erforderlich**, Challenge ist
    Einmal-Wert in der Session, Origin + RP-ID werden serverseitig geprüft,
    der Signaturzähler erkennt geklonte Authenticatoren.
  - Die Freigabe-Whitelist gilt auch für Passkey-Logins.
  - Browser erlauben WebAuthn nur in sicheren Kontexten → HTTPS nötig
    (localhost ausgenommen).
- **Brute-Force-Schutz** (`accounts/throttling.py`, Cache-basiert):
  - Login: 5 Fehlversuche pro Konto bzw. 20 pro IP → 15 Minuten Sperre;
    auch das richtige Passwort wird während der Sperre abgelehnt.
    Erfolgreiche Anmeldung setzt die Zähler zurück.
  - Registrierung: max. 10 POSTs/Stunde/IP. Passwort-Reset: max. 5
    Mails/Stunde/IP. Passkey-Login: max. 20 Versuche/15 min/IP. Antwort: 429.
  - Externe Datenbanksuche (Lookup/Suche): max. 30 Anfragen/Minute pro
    Benutzer — der Server lässt sich nicht als Anfrage-Kanone gegen
    DNB/Google/Open Library missbrauchen.
  - Sicherheitsereignisse (Lockouts, Rate-Limit-Treffer, fehlgeschlagene
    Passkey-Anmeldungen) werden im Logger ``cms.security`` protokolliert
    (stderr; in Produktion auf Datei/Syslog zeigen lassen).
  - Hinweis: Mit dem Standard-LocMemCache gelten die Limits pro Prozess.
    Für strikte globale Limits bei mehreren Gunicorn-Workern `CACHES` auf
    Redis/Memcached zeigen lassen.
- **Argon2** (memory-hard, OWASP-Empfehlung) ist der primäre Passwort-Hasher;
  bestehende PBKDF2-Hashes werden beim nächsten Login transparent migriert.
- Passwort-Validierung (Länge, Ähnlichkeit, Common-Passwords, numerisch) aktiv;
  Anmeldefehler sind generisch (kein User-Enumeration über die Fehlermeldung,
  auch nicht beim Passkey-Login oder Passwort-Reset).
- **API-Tokens werden nur als SHA-256-Hash gespeichert** — der Klartext-Schlüssel
  existiert genau einmal (Anzeige nach dem Erstellen); ein DB-Diebstahl liefert
  keine verwendbaren Tokens.

## Transport & Header

- **CSP** (Django-6-nativ, erzwungen): `default-src 'self'`; Skripte nur von
  eigener Origin + per-Request-Nonce für die wenigen Inline-Blöcke; keine
  Inline-Event-Handler (durch `data-confirm`-Muster ersetzt); Bilder von
  eigener Origin + den vier Cover-Hosts der Buchdatenbanken; `frame-ancestors
  'none'`, `object-src 'none'`, `base-uri`/`form-action 'self'`.
- **X-Frame-Options: DENY**, **nosniff**, **Referrer-Policy: same-origin**.
- **Permissions-Policy**: nur Kamera (Barcode-Scan/Fotoaufnahme) für die eigene
  Origin; Mikrofon, Standort, Payment, USB aus.
- Session-/CSRF-Cookies: `HttpOnly` (Session), `SameSite=Lax`; Session-Ablauf
  standardmäßig 14 Tage (`SESSION_COOKIE_AGE` konfigurierbar).
- HSTS/`Secure`-Cookies/SSL-Redirect sind env-gesteuert (siehe Checkliste) —
  lokal aus, in Produktion an.

## Crawler & Indexierung

Dreifach abgesichert, gilt auch für Nicht-HTML-Antworten (Exporte, QR-Codes):

1. `/robots.txt` → `Disallow: /` für alle User-Agents.
2. HTTP-Header `X-Robots-Tag: noindex, nofollow, noarchive` auf jeder Antwort.
3. `<meta name="robots" content="noindex, nofollow">` in jeder Seite.

Alles Inhaltliche liegt ohnehin hinter dem Login; aggressive Crawler laufen
zusätzlich in die Rate-Limits der öffentlichen Endpunkte.

## DSGVO / deutsche Anforderungen

- **Keine Drittanbieter-Requests**: Bootstrap, Icons (inkl. Fonts), Intro.js,
  html5-qrcode, SortableJS und Chart.js werden lokal aus `static/vendor/`
  ausgeliefert — keine Besucher-IP erreicht ein CDN (vgl. Google-Fonts-Urteil
  LG München I, 3 O 17493/20). Kein Analytics, kein Tracking, keine Cookies
  außer Session/CSRF/Sprache (alle technisch notwendig, kein Cookie-Banner
  erforderlich).
- Ausnahme, bewusst und nutzerinitiiert: Bei der externen Datenbanksuche lädt
  der Browser Cover-Vorschaubilder direkt von DNB/Google Books/Open Library
  (nur diese vier Hosts, per CSP erzwungen). Gespeicherte Cover lädt der
  Server (Host-Whitelist gegen SSRF) und liefert sie danach selbst aus.
- **Datenminimierung**: Konto = Benutzername, E-Mail, optionaler Anzeigename.
  Passwörter als PBKDF2-Hash, API-Tokens nur einmal sichtbar, Passkeys nur als
  öffentlicher Schlüssel.
- **Löschbarkeit**: Konten können im Admin gelöscht werden (Sammlungen,
  Gegenstände, Dateien, Tokens, Passkeys hängen per `CASCADE` daran).
  Papierkorb-Inhalte werden nach Ablauffrist automatisch endgültig entfernt.
- Für den öffentlichen Betrieb bereitstellen (inhaltlich, außerhalb des Codes):
  Impressum & Datenschutzerklärung, ggf. AVV mit dem Hoster, Backup-Konzept.

## Upload- & Eingabesicherheit

- Uploads: Größen-Limit und Erweiterungs-Whitelist (Laufzeit-Einstellungen),
  Auslieferung mit `nosniff`; Cover-Downloads nur von der Host-Whitelist,
  Größenlimit, Content-Type-Prüfung (SSRF-/Malware-Schutz).
- ORM/Templates verhindern SQLi/XSS; CSRF-Schutz auf allen Formularen und
  JSON-POSTs (Header `X-CSRFToken`); Redirects nach Login werden mit
  `url_has_allowed_host_and_scheme` geprüft (kein Open Redirect).
- Excel-Import parst nur Zellwerte (openpyxl, keine Makros/Formeln-Ausführung).

## Checkliste Produktivbetrieb (`.env` / `config.ini`)

| Einstellung | Produktionswert |
|---|---|
| `DEBUG` | `False` (Pflicht) |
| `SECRET_KEY` | langer Zufallswert (Pflicht, nie der Code-Default) |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | eigene Domain |
| `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `True` |
| `SECURE_HSTS_SECONDS` | z. B. `31536000` (+ Subdomains/Preload nach Bedarf) |
| `USE_PROXY_SSL_HEADER` | `True` hinter TLS-terminierendem Proxy |
| `DB_ENGINE` | `postgres` |
| `EMAIL_BACKEND` + `EMAIL_*` | SMTP für Reset-/Freigabe-Mails |
| `STATIC_MANIFEST` | `True` (gehashte, komprimierte Assets via WhiteNoise) |
| `SESSION_COOKIE_AGE` | nach Bedarf kürzen (Sekunden) |

Zusätzlich: Reverse-Proxy so konfigurieren, dass `REMOTE_ADDR` die echte
Client-IP ist (die Rate-Limits binden daran; `X-Forwarded-For` wird bewusst
nicht blind vertraut), regelmäßige Updates (`pip install -U -r
requirements.txt`), Backups verschlüsselt ablegen.

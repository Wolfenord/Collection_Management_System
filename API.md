# JSON API (v1)

A small token-authenticated API mirroring the web UI's row-level permissions.
It exchanges items as plain `{"values": {field_key: value}}` mappings — the
schema is whatever the collection's dynamic fields define — and validates
writes through the same form logic as the UI.

## Enabling & authentication

1. A staff user enables the API: *Systemeinstellungen → JSON-API aktivieren*
   (runtime setting `api_enabled`, off by default — all requests get 403 while
   disabled).
2. Each user creates personal tokens on their profile page (*Mein Profil →
   API-Tokens*). The key is shown **once** at creation; revoke = delete.
3. Send the token on every request:

```
Authorization: Bearer <token>        # or:  X-Api-Key: <token>
```

## Endpoints

| Method | Path | Meaning |
|---|---|---|
| GET | `/api/collections/` | accessible collections (with own `permission`) |
| GET | `/api/collections/<id>/` | schema: fields (key/label/type/required/config) + item types |
| GET | `/api/collections/<id>/items/` | items; supports the UI filter params (`q`, `type`, per-field filters) and `page` |
| POST | `/api/collections/<id>/items/` | create item (edit permission) |
| GET | `/api/collections/<id>/items/<item_id>/` | one item |
| PUT | `/api/collections/<id>/items/<item_id>/` | replace values (full validation) |
| PATCH | `/api/collections/<id>/items/<item_id>/` | merge values into the existing ones |
| DELETE | `/api/collections/<id>/items/<item_id>/` | move to the collection's trash (restorable in the UI) |

File/image fields cannot be written through the API (upload via the web UI);
their stored references appear read-only in `values`.

## Examples

```bash
TOKEN=…; BASE=https://cms.example.com

curl -H "Authorization: Bearer $TOKEN" $BASE/api/collections/

curl -H "Authorization: Bearer $TOKEN" \
     "$BASE/api/collections/<id>/items/?q=dune&page=1"

curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"values": {"name": "Dune", "preis": 12.5}, "item_type": 3}' \
     $BASE/api/collections/<id>/items/

curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"values": {"ort": "Regal B"}}' \
     $BASE/api/collections/<id>/items/<item_id>/
```

Errors are JSON: `{"error": "…"}` with 400/401/403/404/405; validation errors
additionally carry `{"fields": {field_key: [messages]}}`.

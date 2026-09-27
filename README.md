# Al Rams Laundry / مغسلة الرمس

Independent staff and administration system for Al Rams Laundry. It has no public customer-ordering page.

## Start locally

Run `start-laundry.cmd`, then open:

- Staff: http://127.0.0.1:8875/staff.html
- Admin: http://127.0.0.1:8875/admin.html

On the first start, random passwords are written to `runtime/credentials.txt`. This file is ignored by Git. Change the accounts before production use.

## Data and backup

The SQLite database is `data/laundry.sqlite3`. Stop the server before a file backup, or use SQLite's backup command. Verify a restored backup with `PRAGMA integrity_check;` before starting the application.

## WhatsApp

WhatsApp is outgoing-only. Orders are never accepted from incoming messages. Copy `.env.example` to `.env` in production and configure approved Meta templates. Secrets must be injected into the server process; `.env` is ignored by Git. The initial local build queues notification events and records webhook delivery updates. A Meta account, approved templates, public HTTPS webhook, access token, phone number ID, verification token and app secret are required for real delivery.

## Tests

Run:

```powershell
python -m unittest -v test_system.py
```

## Production checklist

1. Put the application behind HTTPS and a reverse proxy.
2. Replace bootstrap credentials and protect `runtime/` and `data/`.
3. Configure automated encrypted backups and test restoration.
4. Configure Meta WhatsApp credentials and approved Arabic/English templates.
5. Restrict network access, monitor logs, and rotate secrets.
6. Run the full test suite before every release.

The server recalculates catalog prices, VAT and balances. Client totals are never trusted. Payment and WhatsApp event idempotency keys prevent repeated submissions.

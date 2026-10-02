# Al Rams Laundry / مغسلة الرمس

Independent staff and administration system for Al Rams Laundry. It has no public customer-ordering page.

## Start locally

Run `start-laundry.cmd`, then open:

- Staff: http://127.0.0.1:8875/staff.html
- Admin: http://127.0.0.1:8875/admin.html

On the first start, random passwords are written to `runtime/credentials.txt`. This file is ignored by Git. Change the accounts before production use.

## Operations included

- Fast photo-based order entry with popular/recent items, search, direct service buttons, quantity controls, and a sticky basket.
- Received, washing, ironing, ready, collected, due-today, and overdue order views.
- Customer lookup by normalized full UAE phone number, visit/spend/balance history, preferences, and important notes.
- Partial payments, admin-approved discounts, refunds, 5% VAT, receipt numbers, and 80 mm receipt printing.
- Daily/weekly/monthly reporting, cash/card reconciliation, unpaid orders, top services, staff results, expenses, and net profit.
- Staff activation, password reset, granular permissions, and audit history.
- Outgoing-only WhatsApp queue with deduplication, retries, delivery/read status, and uncollected-order reminders.

## Data and backup

The local database is `data/laundry.sqlite3`. `backup.py` creates an integrity-checked AES-GCM encrypted backup. Generate and securely retain a urlsafe base64 32-byte key, set it as `BACKUP_ENCRYPTION_KEY`, then run:

```powershell
python backup.py
python backup.py --restore backups\encrypted\laundry-YYYYMMDD-HHMMSS.sqlite3.aes --target restored.sqlite3
```

Verify the restored database before replacing production data. `install-backup-task.ps1` installs a daily 02:00 Windows task; run it from an elevated PowerShell after setting the key. Losing the encryption key makes the backups unrecoverable.

## Supabase migration

The production PostgreSQL schema is prepared in `supabase/migrations/`. Every public table has RLS enabled and direct anonymous/authenticated table access is revoked; the current browser never receives the service-role key. The running local application still uses SQLite until a Supabase project is linked and the server data adapter is switched and verified. Apply the migration only to a reviewed project and run Supabase security/performance advisors before launch.

## WhatsApp

WhatsApp is outgoing-only. Orders are never accepted from incoming messages. Copy `.env.example` to `.env` in production and configure approved Meta templates. Secrets must be injected into the server process; `.env` is ignored by Git. The initial local build queues notification events and records webhook delivery updates. A Meta account, approved templates, public HTTPS webhook, access token, phone number ID, verification token and app secret are required for real delivery.

## Tests

Run:

```powershell
python -m unittest -v test_system.py
```

## Production checklist

1. Link the intended Supabase project, apply the reviewed migration, then migrate and reconcile the SQLite data.
2. Deploy behind HTTPS and configure the required environment variables in the hosting provider.
3. Replace the local development passwords with strong unique production passwords.
4. Protect `runtime/`, `data/`, backup files, and the encryption key.
5. Configure and test automated encrypted backups and a complete restoration.
6. Configure Meta WhatsApp credentials, approved Arabic/English templates, and a public HTTPS webhook.
7. Run Supabase advisors, the automated tests, and a live end-to-end order/payment/refund/notification test.

The server recalculates catalog prices, VAT and balances. Client totals are never trusted. Payment and WhatsApp event idempotency keys prevent repeated submissions.

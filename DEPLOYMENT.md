# Production deployment

## 1. Supabase

1. Create or select the production project in the correct region.
2. Link the CLI: `pnpm dlx supabase@latest link --project-ref <project-ref>`.
3. Review and apply `supabase/migrations/20261002092125_initial_laundry_schema.sql`.
4. Run the Supabase security and performance advisors and resolve findings.
5. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` only in the server environment. Never place the service-role key in frontend files.
6. Run a dry-run data migration, compare row counts and financial totals, then perform the final migration during a maintenance window.

The current local runtime uses SQLite. Deployment is not complete until the server repository layer is connected to Supabase and the full flow is tested against the production project.

## 2. Hosting and HTTPS

Deploy the Python server to a host that supports persistent server processes, scheduled work, and HTTPS. Set all variables from `.env.example` in the host's encrypted environment settings. Do not upload `.env`, `data/`, `runtime/`, or backup files. Restrict CORS to the production origin and place rate limiting in front of authentication and webhook endpoints.

## 3. WhatsApp

Configure the official Meta Cloud API phone number, permanent server token, app secret, verify token, and approved Arabic/English templates. Expose `/api/webhooks/whatsapp` over HTTPS. Subscribe to message status events and verify the challenge and request signature. The integration is outgoing-only; incoming booking commands are not accepted.

## 4. Backup and recovery

Set a securely stored `BACKUP_ENCRYPTION_KEY`, install the daily backup task, copy encrypted backups to separate protected storage, and retain the key separately. Restore the newest backup to a temporary database and require `PRAGMA integrity_check` to return `ok`. Record each recovery test.

## 5. Release verification

Run Python/JavaScript syntax checks and `python -m unittest -v` twice. Verify unauthorized admin requests return 401, staff permissions return 403 where applicable, duplicate payment/refund/WhatsApp requests are rejected or deduplicated, receipt printing fits 80 mm, and dashboard totals reconcile as: net collected = payments - refunds; profit = net collected - expenses.

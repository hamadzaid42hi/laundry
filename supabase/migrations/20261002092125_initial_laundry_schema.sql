-- Al Rams Laundry production schema. Access is server-only through the service role.
-- No anon/authenticated policies are intentionally created.
create extension if not exists pgcrypto;

create table public.staff_profiles (
  id uuid primary key references auth.users(id) on delete restrict,
  username text unique not null check (username ~ '^[a-z0-9_.-]{2,40}$'),
  display_name text not null,
  role text not null check (role in ('staff','admin')),
  permissions jsonb not null default '[]'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table public.customers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  phone text unique not null check (phone ~ '^\+9715[0-9]{8}$'),
  important_notes text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table public.orders (
  id uuid primary key default gen_random_uuid(),
  order_ref text unique not null,
  customer_id uuid not null references public.customers(id) on delete restrict,
  status text not null check (status in ('received','washing','drying','ironing','ready','collected','cancelled')),
  subtotal_cents bigint not null check (subtotal_cents>=0),
  vat_cents bigint not null check (vat_cents>=0),
  discount_cents bigint not null default 0 check (discount_cents>=0),
  total_cents bigint not null check (total_cents>=0),
  expected_at timestamptz,
  notes text not null default '',
  client_key uuid unique not null,
  created_by uuid not null references public.staff_profiles(id),
  updated_by uuid references public.staff_profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table public.order_items (
  id uuid primary key default gen_random_uuid(),order_id uuid not null references public.orders(id) on delete cascade,
  category text not null,item text not null,size text,service text not null,quantity integer not null check(quantity between 1 and 999),
  width numeric,height numeric,unit_cents bigint not null check(unit_cents>=0),line_cents bigint not null check(line_cents>=0)
);
create table public.order_events (
 id bigint generated always as identity primary key,order_id uuid not null references public.orders(id) on delete cascade,status text not null,note text not null default '',actor uuid not null references public.staff_profiles(id),created_at timestamptz not null default now()
);
create table public.payments (
 id uuid primary key default gen_random_uuid(),order_id uuid not null references public.orders(id) on delete restrict,amount_cents bigint not null check(amount_cents>0),method text not null check(method in('cash','card','bank')),client_key uuid unique not null,actor uuid not null references public.staff_profiles(id),created_at timestamptz not null default now()
);
create table public.refunds (
 id uuid primary key default gen_random_uuid(),order_id uuid not null references public.orders(id) on delete restrict,amount_cents bigint not null check(amount_cents>0),method text not null check(method in('cash','card','bank')),reason text not null,client_key uuid unique not null,actor uuid not null references public.staff_profiles(id),created_at timestamptz not null default now()
);
create table public.expenses (
 id uuid primary key default gen_random_uuid(),expense_date date not null,category text not null,description text not null default '',amount_cents bigint not null check(amount_cents>0),actor uuid not null references public.staff_profiles(id),created_at timestamptz not null default now()
);
create table public.whatsapp_events (
 id uuid primary key default gen_random_uuid(),order_id uuid not null references public.orders(id) on delete restrict,customer_id uuid not null references public.customers(id) on delete restrict,recipient_masked text not null,message_type text not null,template_name text not null,template_language text not null,idempotency_key text unique not null,provider_message_id text unique,status text not null default 'queued' check(status in('queued','sent','delivered','read','failed')),retry_count integer not null default 0 check(retry_count between 0 and 10),failure_reason text,created_at timestamptz not null default now(),updated_at timestamptz not null default now()
);
create table public.audit_events (
 id bigint generated always as identity primary key,actor uuid references public.staff_profiles(id),action text not null,entity_type text not null,entity_id text,details jsonb not null default '{}'::jsonb,created_at timestamptz not null default now()
);
create table public.business_settings(key text primary key,value text not null,updated_at timestamptz not null default now());

create index orders_status_created_idx on public.orders(status,created_at desc);
create index orders_customer_idx on public.orders(customer_id,created_at desc);
create index orders_expected_idx on public.orders(expected_at) where status not in ('collected','cancelled');
create index order_items_order_idx on public.order_items(order_id);
create index payments_order_idx on public.payments(order_id);
create index whatsapp_status_idx on public.whatsapp_events(status,created_at) where status in ('queued','failed');

alter table public.staff_profiles enable row level security;
alter table public.customers enable row level security;
alter table public.orders enable row level security;
alter table public.order_items enable row level security;
alter table public.order_events enable row level security;
alter table public.payments enable row level security;
alter table public.refunds enable row level security;
alter table public.expenses enable row level security;
alter table public.whatsapp_events enable row level security;
alter table public.audit_events enable row level security;
alter table public.business_settings enable row level security;

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;

-- Web Push subscriptions, one row per browser that opted in.
--
-- The gap this closes: notifications were browser-only and tab-bound — a job that
-- parked on a question or an approval was invisible the moment every tab was closed,
-- which is the normal state. A Web Push subscription is what lets the server reach an
-- opted-in browser (and its device's lock screen) with the site shut. Fan-out is
-- global by design: anyone who visits and opts in gets pinged on any blocked job,
-- because there is no user model to target and "is anything waiting for me" is a
-- question every operator of this box shares.
--
-- Only the subscription's public halves live here. The endpoint is a URL at the
-- browser's own push service; `p256dh`/`auth` are the client public keys the payload
-- is encrypted to. The server's VAPID *private* key is never in this table — it stays
-- in data/vapid.json (0600) or an env var, like every other secret in this system.
create table push_subscriptions (
  id         text primary key,
  -- The browser's push endpoint. Unique so re-subscribing the same browser upserts
  -- rather than piling up dead rows — a browser silently rotates its subscription and
  -- re-POSTs, and two rows for one browser would double every notification.
  endpoint   text not null unique,
  p256dh     text not null,
  auth       text not null,
  -- The User-Agent at subscribe time, so the settings panel can say "3 browsers" in
  -- terms a person recognises. Advisory only.
  ua         text,
  created_at real not null,
  -- Last successful send, and a running count of consecutive-ish failures. A dead
  -- endpoint (404/410 from the push service) is deleted outright; `failures` is for
  -- the transient errors that do not justify dropping a subscription yet.
  last_ok    real,
  failures   integer not null default 0
);

create index push_subscriptions_created on push_subscriptions(created_at);

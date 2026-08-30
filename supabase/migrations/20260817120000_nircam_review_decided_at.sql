-- Last-writer-wins guard for NIRCam triage review writes. The web triage
-- outbox retries dropped/hung saves and flushes with keepalive on unload, so
-- the same decision can legitimately arrive more than once and out of order.
-- Each staged decision carries the client-side time it was made; the review
-- API applies an update only when this column is null or <= that stamp, so a
-- delayed duplicate of an older decision can never overwrite a newer one.
alter table "public"."nircam_exposures"
    add column "review_decided_at" timestamp with time zone;

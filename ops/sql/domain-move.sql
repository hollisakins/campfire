-- Domain move (ops/README.md, "Domain move checklist"): rewrite the stored
-- public tile hostnames. These two columns are the only rows in the database
-- that carry a hostname (checked 2026-10-25: programs.config, fields.config and
-- everything else are hostname-free). Everything else the browser loads is
-- built from NEXT_PUBLIC_* at image build time or from web.env at runtime.
--
--   docker exec -i campfire-db psql -U postgres -d postgres \
--     -v old=https://campfire-tiles.hollisakins.com \
--     -v new=https://tiles.campfire-jwst.org \
--     -f - < ops/sql/domain-move.sql
--
-- Idempotent: rows already on :new are untouched. Run it again with the
-- arguments swapped to roll back. The R2 custom domain :new must be attached
-- to the tiles bucket first, or every map tile and cutout 404s.
\set ON_ERROR_STOP on
begin;

update public.map_layers
   set tile_base_url = replace(tile_base_url, :'old', :'new')
 where tile_base_url like :'old' || '%';

update public.fitsgl_datasets
   set fitsgl_json_url = replace(fitsgl_json_url, :'old', :'new')
 where fitsgl_json_url like :'old' || '%';

select 'map_layers' as t, regexp_replace(tile_base_url, '^(https?://[^/]+).*$', '\1') as host, count(*)
  from public.map_layers group by 1, 2
union all
select 'fitsgl_datasets', regexp_replace(fitsgl_json_url, '^(https?://[^/]+).*$', '\1'), count(*)
  from public.fitsgl_datasets group by 1, 2;

commit;

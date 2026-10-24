-- Schema/ACL fingerprint of the public schema: one md5 per aspect. Run the same
-- file against the hosted project (dump-hosted.sh) and the restored database
-- (restore-hosted.sh); identical lines mean the restore reproduced the grants,
-- RLS, policies, triggers, indexes, extensions and default privileges exactly.
-- Reports a count and a hash per line so a difference names the aspect to dig into.
-- ACL arrays are sorted before hashing: pg_dump re-grants in a different order
-- than the migrations did, which permutes the aclitem[] without changing it.
with acl as (
  select oid, (select string_agg(a::text, ',' order by a::text) from unnest(relacl) a) as acl
    from pg_class where relnamespace = 'public'::regnamespace
), pacl as (
  select oid, (select string_agg(a::text, ',' order by a::text) from unnest(proacl) a) as acl
    from pg_proc where pronamespace = 'public'::regnamespace
), aacl as (
  select attrelid, attname, (select string_agg(a::text, ',' order by a::text) from unnest(attacl) a) as acl
    from pg_attribute where attacl is not null
     and attrelid in (select oid from pg_class where relnamespace = 'public'::regnamespace)
)
select 'relacl' k, count(*) n, md5(string_agg(c.relname||' '||coalesce(acl.acl,'-'), E'\n' order by c.relname)) h
  from pg_class c join acl using (oid)
 where c.relnamespace = 'public'::regnamespace and c.relkind in ('r','v','m','S','p')
union all select 'proacl', count(*), md5(string_agg(p.proname||'('||pg_get_function_identity_arguments(p.oid)||') '||coalesce(pacl.acl,'-'), E'\n' order by p.proname, pg_get_function_identity_arguments(p.oid)))
  from pg_proc p join pacl using (oid) where p.pronamespace = 'public'::regnamespace
union all select 'attacl', count(*), md5(string_agg(attrelid::regclass::text||'.'||attname||' '||acl, E'\n' order by attrelid::regclass::text, attname)) from aacl
union all select 'rls', count(*), md5(string_agg(relname||' '||relrowsecurity||relforcerowsecurity, E'\n' order by relname)) from pg_class where relnamespace = 'public'::regnamespace and relkind in ('r','p')
union all select 'policies', count(*), md5(string_agg(tablename||'.'||policyname||' '||coalesce(roles::text,'')||' '||cmd||' '||coalesce(qual,'')||' '||coalesce(with_check,''), E'\n' order by tablename, policyname)) from pg_policies where schemaname = 'public'
union all select 'func_owners', count(*), md5(string_agg(proname||' '||proowner::regrole::text||' '||prosecdef, E'\n' order by proname, pg_get_function_identity_arguments(oid))) from pg_proc where pronamespace = 'public'::regnamespace
union all select 'triggers', count(*), md5(string_agg(tgrelid::regclass::text||'.'||tgname||' '||tgenabled::text, E'\n' order by tgrelid::regclass::text, tgname)) from pg_trigger where not tgisinternal and tgrelid in (select oid from pg_class where relnamespace = 'public'::regnamespace)
union all select 'indexes', count(*), md5(string_agg(indexname||' '||indexdef, E'\n' order by indexname)) from pg_indexes where schemaname = 'public'
union all select 'extensions', count(*), md5(string_agg(extname||' '||extversion, E'\n' order by extname)) from pg_extension
union all select 'default_acl_public', count(*), md5(string_agg(defaclrole::regrole::text||'/'||defaclobjtype::text||': '||(select string_agg(a::text, ',' order by a::text) from unnest(defaclacl) a), E'\n' order by defaclrole::regrole::text, defaclobjtype::text)) from pg_default_acl where defaclnamespace = 'public'::regnamespace

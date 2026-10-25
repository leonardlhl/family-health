-- Run this WHOLE file in the intended project's SQL Editor as postgres.
-- Apply 001 first. This is a verification transaction, NOT a migration.
-- No Auth users, passwords, files, or medical records are created.
-- Only synthetic DB rows exist until ROLLBACK. The one FK timing change also
-- rolls back. It takes a brief table lock; stop if the 3-second lock limit fires.
-- If ANY assertion fails, run ROLLBACK; immediately before investigating.
-- Never replace ROLLBACK with COMMIT or execute selected fragments.
begin;
set local lock_timeout = '3s';
set local statement_timeout = '30s';

do $$
begin
  if current_user <> 'postgres' then
    raise exception 'Run the complete verification as postgres';
  end if;
  if exists (select 1 from public.family_archives a
      where a.id in ('verify-family-a', 'verify-family-b'))
    or exists (select 1 from auth.users u where u.id in (
      '00000000-0000-4000-8000-000000000201'::uuid,
      '00000000-0000-4000-8000-000000000202'::uuid))
    or exists (select 1 from public.family_members m where m.user_id in (
      '00000000-0000-4000-8000-000000000201'::uuid,
      '00000000-0000-4000-8000-000000000202'::uuid))
    or exists (select 1 from storage.objects o
      where o.bucket_id = 'family-health' and o.name like 'verify-family-%') then
    raise exception 'Synthetic fixture identifiers already exist; nothing changed';
  end if;
  if exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
      where n.nspname = 'public' and c.relname in
        ('family_archives','family_members','family_snapshots','family_media')
        and not c.relrowsecurity) then
    raise exception 'FAIL: RLS is disabled on a family table';
  end if;
  if not exists (select 1 from storage.buckets b
      where b.id = 'family-health' and b.public = false) then
    raise exception 'FAIL: family-health bucket missing or public';
  end if;
  if exists (select 1 from pg_roles r where r.rolname in ('anon','authenticated')
      and (r.rolsuper or r.rolbypassrls)) then
    raise exception 'FAIL: client role can bypass RLS';
  end if;
end $$;

-- Defer ONLY this FK, solely so a synthetic JWT subject needs no Auth account.
-- The deferred reference would prevent COMMIT; the required ROLLBACK restores
-- the original constraint and removes the synthetic member.
alter table public.family_members alter constraint family_members_user_id_fkey
  deferrable initially deferred;
set constraints public.family_members_user_id_fkey deferred;

insert into public.family_archives (id, current_version) values
  ('verify-family-a', '00000000-0000-4000-8000-000000000101'),
  ('verify-family-b', '00000000-0000-4000-8000-000000000101');
-- Same current version in two archives catches missing archive correlation.
insert into public.family_snapshots (archive_id, version_id, snapshot) values
  ('verify-family-a', '00000000-0000-4000-8000-000000000101',
    '{"schema_version":1,"patients":[],"visits":[],"attachments":[]}'),
  ('verify-family-a', '00000000-0000-4000-8000-000000000102',
    '{"schema_version":1,"patients":[],"visits":[],"attachments":[]}'),
  ('verify-family-b', '00000000-0000-4000-8000-000000000101',
    '{"schema_version":1,"patients":[],"visits":[],"attachments":[]}');
insert into public.family_members (archive_id, user_id) values
  ('verify-family-a', '00000000-0000-4000-8000-000000000201');
insert into public.family_media (archive_id, version_id, attachment_id, object_path) values
  ('verify-family-a', '00000000-0000-4000-8000-000000000101', 'synthetic-current',
    'verify-family-a/00000000-0000-4000-8000-000000000101/placeholder.png'),
  ('verify-family-a', '00000000-0000-4000-8000-000000000102', 'synthetic-old',
    'verify-family-a/00000000-0000-4000-8000-000000000102/placeholder.png'),
  ('verify-family-b', '00000000-0000-4000-8000-000000000101', 'synthetic-other',
    'verify-family-b/00000000-0000-4000-8000-000000000101/placeholder.png');
-- Storage registry rows only: no corresponding file bytes are uploaded.
insert into storage.objects (bucket_id, name)
  select 'family-health', m.object_path from public.family_media m
  where m.archive_id in ('verify-family-a', 'verify-family-b');

-- Anonymous: no table grants, no publishing RPC, no visible storage registry.
set local role anon;
select set_config('request.jwt.claims', '{"role":"anon"}', true);
select set_config('request.jwt.claim.sub', '', true);
do $$
declare table_name text; visible_count bigint;
begin
  if current_user <> 'anon' or auth.uid() is not null then
    raise exception 'FAIL: anonymous simulation was not installed';
  end if;
  foreach table_name in array array['family_archives','family_members','family_snapshots','family_media'] loop
    if has_table_privilege(current_user, 'public.' || table_name,
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') then
      raise exception 'FAIL: anonymous has a grant on %', table_name;
    end if;
  end loop;
  if has_function_privilege(current_user,
      'public.publish_family_snapshot(text,uuid,jsonb,jsonb)', 'EXECUTE') then
    raise exception 'FAIL: anonymous can execute publication';
  end if;
  begin
    select count(*) into visible_count from storage.objects o
      where o.bucket_id = 'family-health' and o.name like 'verify-family-%';
    if visible_count <> 0 then raise exception 'FAIL: anonymous can see private media'; end if;
  exception when insufficient_privilege then null;
  end;
end $$;

-- A valid JWT subject without membership must see nothing.
set local role authenticated;
select set_config('request.jwt.claims',
  '{"role":"authenticated","sub":"00000000-0000-4000-8000-000000000202"}', true);
select set_config('request.jwt.claim.sub', '00000000-0000-4000-8000-000000000202', true);
do $$
begin
  if current_user <> 'authenticated'
    or auth.uid() is distinct from '00000000-0000-4000-8000-000000000202'::uuid then
    raise exception 'FAIL: outsider simulation was not installed';
  end if;
  if exists (select 1 from public.family_archives a where a.id in ('verify-family-a','verify-family-b'))
    or exists (select 1 from public.family_members m where m.archive_id in ('verify-family-a','verify-family-b'))
    or exists (select 1 from public.family_snapshots s where s.archive_id in ('verify-family-a','verify-family-b'))
    or exists (select 1 from public.family_media m where m.archive_id in ('verify-family-a','verify-family-b'))
    or exists (select 1 from storage.objects o where o.bucket_id = 'family-health'
      and o.name like 'verify-family-%') then
    raise exception 'FAIL: outsider can see family data or media';
  end if;
end $$;

-- Allowed family member: own CURRENT version only, never the other archive.
select set_config('request.jwt.claims',
  '{"role":"authenticated","sub":"00000000-0000-4000-8000-000000000201"}', true);
select set_config('request.jwt.claim.sub', '00000000-0000-4000-8000-000000000201', true);
do $$
declare table_name text; affected_count bigint;
begin
  if current_user <> 'authenticated'
    or auth.uid() is distinct from '00000000-0000-4000-8000-000000000201'::uuid then
    raise exception 'FAIL: family member simulation was not installed';
  end if;
  if (select count(*) from public.family_archives a
      where a.id in ('verify-family-a','verify-family-b')) <> 1
    or not exists (select 1 from public.family_archives a where a.id = 'verify-family-a') then
    raise exception 'FAIL: member archive visibility';
  end if;
  if (select count(*) from public.family_members m
      where m.archive_id in ('verify-family-a','verify-family-b')) <> 1 then
    raise exception 'FAIL: member allowlist visibility';
  end if;
  if (select count(*) from public.family_snapshots s
      where s.archive_id in ('verify-family-a','verify-family-b')) <> 1
    or not exists (select 1 from public.family_snapshots s where s.archive_id = 'verify-family-a'
      and s.version_id = '00000000-0000-4000-8000-000000000101') then
    raise exception 'FAIL: old snapshot or other archive visible, or current snapshot missing';
  end if;
  if (select count(*) from public.family_media m
      where m.archive_id in ('verify-family-a','verify-family-b')) <> 1
    or not exists (select 1 from public.family_media m where m.attachment_id = 'synthetic-current'
      and m.archive_id = 'verify-family-a') then
    raise exception 'FAIL: media manifest version/archive isolation';
  end if;
  if (select count(*) from storage.objects o where o.bucket_id = 'family-health'
      and o.name like 'verify-family-%') <> 1
    or not exists (select 1 from storage.objects o where o.bucket_id = 'family-health'
      and o.name = 'verify-family-a/00000000-0000-4000-8000-000000000101/placeholder.png') then
    raise exception 'FAIL: storage version/archive isolation';
  end if;
  foreach table_name in array array['family_archives','family_members','family_snapshots','family_media'] loop
    if not has_table_privilege(current_user, 'public.' || table_name, 'SELECT')
      or has_table_privilege(current_user, 'public.' || table_name,
        'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') then
      raise exception 'FAIL: authenticated grants are not SELECT-only on %', table_name;
    end if;
  end loop;
  if has_function_privilege(current_user,
      'public.publish_family_snapshot(text,uuid,jsonb,jsonb)', 'EXECUTE') then
    raise exception 'FAIL: family can execute publication';
  end if;
  -- Storage has shared table grants, so test actual writes against its RLS.
  begin
    insert into storage.objects (bucket_id, name) values ('family-health',
      'verify-family-a/00000000-0000-4000-8000-000000000101/forbidden.png');
    raise exception 'FAIL: family can insert storage metadata';
  exception when insufficient_privilege then null;
  end;
  begin
    update storage.objects o set metadata = '{"synthetic":"forbidden"}'::jsonb
      where o.bucket_id = 'family-health' and o.name like 'verify-family-%';
    get diagnostics affected_count = row_count;
    if affected_count <> 0 then raise exception 'FAIL: family can update storage metadata'; end if;
  exception when insufficient_privilege then null;
  end;
  begin
    delete from storage.objects o
      where o.bucket_id = 'family-health' and o.name like 'verify-family-%';
    get diagnostics affected_count = row_count;
    if affected_count <> 0 then raise exception 'FAIL: family can delete storage metadata'; end if;
  exception when insufficient_privilege then null;
  end;
end $$;

rollback;
select 'PASS: anonymous and outsider denied; member current-version SELECT only; all fixtures and FK timing changes rolled back' as verification_result;

-- Apply once in the Supabase SQL editor. Contains schema only, never patient data.
begin;

create table if not exists public.family_archives (
  id text primary key check (id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  current_version uuid,
  published_at timestamptz
);
create table if not exists public.family_members (
  archive_id text not null references public.family_archives(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  primary key (archive_id, user_id)
);
create table if not exists public.family_snapshots (
  archive_id text not null references public.family_archives(id) on delete cascade,
  version_id uuid not null,
  snapshot jsonb not null check (snapshot->>'schema_version' = '1'),
  published_at timestamptz not null default now(),
  primary key (archive_id, version_id)
);
create table if not exists public.family_media (
  archive_id text not null,
  version_id uuid not null,
  attachment_id text not null,
  object_path text not null unique,
  primary key (archive_id, version_id, attachment_id),
  foreign key (archive_id, version_id) references public.family_snapshots(archive_id, version_id) on delete cascade
);

alter table public.family_archives enable row level security;
alter table public.family_members enable row level security;
alter table public.family_snapshots enable row level security;
alter table public.family_media enable row level security;

revoke all on public.family_archives, public.family_members, public.family_snapshots, public.family_media from anon, authenticated;
grant select on public.family_archives, public.family_members, public.family_snapshots, public.family_media to authenticated;
grant all on public.family_archives, public.family_members, public.family_snapshots, public.family_media to service_role;

drop policy if exists family_member_self on public.family_members;
create policy family_member_self on public.family_members for select to authenticated
  using (user_id = (select auth.uid()));
drop policy if exists family_archive_read on public.family_archives;
create policy family_archive_read on public.family_archives for select to authenticated
  using (exists (select 1 from public.family_members m where m.archive_id = id and m.user_id = (select auth.uid())));
drop policy if exists family_snapshot_read on public.family_snapshots;
create policy family_snapshot_read on public.family_snapshots for select to authenticated
  using (exists (select 1 from public.family_archives a where a.id = archive_id and a.current_version = version_id));
drop policy if exists family_media_read on public.family_media;
create policy family_media_read on public.family_media for select to authenticated
  using (exists (select 1 from public.family_archives a where a.id = archive_id and a.current_version = version_id));

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('family-health', 'family-health', false, 26214400, array['image/png','image/jpeg','image/webp'])
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists family_private_media_read on storage.objects;
create policy family_private_media_read on storage.objects for select to authenticated
  using (bucket_id = 'family-health' and exists (
    select 1 from public.family_media m where m.object_path = name
  ));
-- No INSERT/UPDATE/DELETE policies exist for family readers.

create or replace function public.publish_family_snapshot(
  p_archive_id text, p_version_id uuid, p_snapshot jsonb, p_media jsonb
) returns void language plpgsql security invoker set search_path = public, pg_temp as $$
declare item jsonb;
begin
  if p_archive_id is null or p_archive_id !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
    or p_snapshot->>'schema_version' is distinct from '1'
    or jsonb_typeof(p_snapshot->'visits') is distinct from 'array'
    or jsonb_typeof(p_snapshot->'attachments') is distinct from 'array'
    or jsonb_typeof(p_media) is distinct from 'array' then
    raise exception 'Invalid archive snapshot';
  end if;
  if exists (select 1 from jsonb_array_elements(p_snapshot->'visits') v
    where v->>'status' is distinct from 'confirmed' or v->>'privacy_reviewed' is distinct from 'true') then
    raise exception 'Only confirmed visits can be published';
  end if;
  if exists (select 1 from jsonb_array_elements(p_snapshot->'attachments') a
    where a->>'privacy_checked' is distinct from 'true') then
    raise exception 'All attachments require privacy review';
  end if;
  insert into public.family_archives (id) values (p_archive_id) on conflict do nothing;
  insert into public.family_snapshots (archive_id, version_id, snapshot)
    values (p_archive_id, p_version_id, p_snapshot);
  for item in select * from jsonb_array_elements(p_media) loop
    if left(item->>'object_path', length(p_archive_id || '/' || p_version_id::text || '/'))
      <> p_archive_id || '/' || p_version_id::text || '/' then
      raise exception 'Invalid media path';
    end if;
    insert into public.family_media (archive_id, version_id, attachment_id, object_path)
      values (p_archive_id, p_version_id, item->>'id', item->>'object_path');
  end loop;
  -- One transaction makes the new complete version visible. Failed uploads never reach here.
  update public.family_archives set current_version = p_version_id, published_at = now() where id = p_archive_id;
end $$;
revoke all on function public.publish_family_snapshot(text,uuid,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.publish_family_snapshot(text,uuid,jsonb,jsonb) to service_role;

insert into public.family_archives (id) values ('family') on conflict do nothing;
commit;

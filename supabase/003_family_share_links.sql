-- Capability links: store only SHA-256 hashes of 32 random bytes encoded as
-- unpadded base64url. Hash the 43-character token's UTF-8 text, not decoded bytes.
-- Apply after 001. No links or patient content are created by this migration.
begin;

create table if not exists public.family_share_links (
  id uuid primary key default gen_random_uuid(),
  archive_id text not null references public.family_archives(id) on delete cascade,
  token_hash text not null unique check (token_hash ~ '^[a-f0-9]{64}$'),
  created_at timestamptz not null default now(),
  revoked_at timestamptz,
  expires_at timestamptz
);
create index if not exists family_share_links_archive_id_idx
  on public.family_share_links (archive_id);
alter table public.family_share_links enable row level security;
revoke all on public.family_share_links from public, anon, authenticated;
grant all on public.family_share_links to service_role;
-- No client policies: a share token is checked only by the server-side function.

create or replace function public.rotate_family_share_link(
  p_archive_id text, p_previous_id uuid, p_id uuid, p_token_hash text
) returns jsonb language plpgsql security invoker set search_path = public, pg_temp as $$
declare result public.family_share_links%rowtype;
begin
  if p_archive_id is null or p_archive_id !~ '^[a-z0-9][a-z0-9_-]{0,63}$'
    or p_id is null or p_token_hash is null or p_token_hash !~ '^[a-f0-9]{64}$'
    or p_previous_id = p_id then
    raise exception 'Invalid share-link request';
  end if;
  -- Serialize create/rotate operations for this archive without broad revocation.
  perform 1 from public.family_archives a where a.id = p_archive_id for update;
  if not found then raise exception 'Archive unavailable'; end if;
  select s.* into result from public.family_share_links s where s.id = p_id;
  if found then
    if result.archive_id <> p_archive_id or result.token_hash <> p_token_hash then
      raise exception 'Share-link request conflicts with an existing identifier';
    end if;
    return to_jsonb(result); -- Replay recovery never reactivates a revoked link.
  end if;
  if p_previous_id is null then
    if exists (select 1 from public.family_share_links s
      where s.archive_id = p_archive_id and s.revoked_at is null) then
      raise exception 'An active share link already exists';
    end if;
  else
    select s.* into result from public.family_share_links s
      where s.id = p_previous_id and s.archive_id = p_archive_id for update;
    if not found or result.revoked_at is not null then
      raise exception 'Previous share link unavailable';
    end if;
    update public.family_share_links s set revoked_at = now()
      where s.id = p_previous_id and s.archive_id = p_archive_id;
  end if;
  insert into public.family_share_links (id, archive_id, token_hash)
    values (p_id, p_archive_id, p_token_hash) returning * into result;
  return to_jsonb(result);
end $$;

create or replace function public.revoke_family_share_link(
  p_archive_id text, p_id uuid, p_token_hash text
) returns jsonb language plpgsql security invoker set search_path = public, pg_temp as $$
declare result public.family_share_links%rowtype;
begin
  -- Lock in the same order as rotation, then affect only the exact requested link.
  perform 1 from public.family_archives a where a.id = p_archive_id for update;
  if not found then raise exception 'Archive unavailable'; end if;
  update public.family_share_links s set revoked_at = coalesce(s.revoked_at, now())
    where s.archive_id = p_archive_id and s.id = p_id and s.token_hash = p_token_hash
    returning s.* into result;
  if not found then raise exception 'Share link unavailable'; end if;
  return to_jsonb(result);
end $$;

revoke all on function public.rotate_family_share_link(text,uuid,uuid,text) from public, anon, authenticated;
revoke all on function public.revoke_family_share_link(text,uuid,text) from public, anon, authenticated;
grant execute on function public.rotate_family_share_link(text,uuid,uuid,text) to service_role;
grant execute on function public.revoke_family_share_link(text,uuid,text) to service_role;
commit;

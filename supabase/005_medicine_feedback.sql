-- Apply after 004. Family observations are independent of clinical snapshots.
-- No patient rows, share tokens, or Auth accounts are created here.
begin;

create table if not exists public.family_medicine_feedback (
  id uuid primary key default gen_random_uuid(),
  event_no bigint generated always as identity unique,
  archive_id text not null references public.family_archives(id),
  snapshot_version uuid not null,
  patient_id text not null check (patient_id in ('grandpa','grandma')),
  visit_id text not null check (visit_id ~ '^[A-Za-z0-9_-]{1,96}$'),
  medicine_id text not null check (medicine_id ~ '^[A-Za-z0-9_-]{1,96}$'),
  rating text not null check (rating in ('effective','ineffective','undecided')),
  notes text not null default '' check (char_length(notes) <= 500 and octet_length(notes) <= 2000),
  revision integer not null check (revision > 0),
  request_id uuid not null,
  -- Internal authorization scope, never a claimed human identity or public field.
  actor_scope text not null check (actor_scope ~ '^(share|member):[a-f0-9-]{36}$'),
  created_at timestamptz not null default clock_timestamp(),
  unique (archive_id, request_id),
  unique (archive_id, patient_id, visit_id, medicine_id, revision)
);
create index if not exists family_medicine_feedback_actor_time_idx
  on public.family_medicine_feedback (archive_id,actor_scope,created_at);
alter table public.family_medicine_feedback enable row level security;
revoke all on public.family_medicine_feedback from public, anon, authenticated, service_role;
grant select, insert on public.family_medicine_feedback to service_role;
revoke all on sequence public.family_medicine_feedback_event_no_seq from public, anon, authenticated;
grant usage, select on sequence public.family_medicine_feedback_event_no_seq to service_role;
-- No browser policies; even service_role has no UPDATE/DELETE grant on revisions.

create or replace function public.family_feedback_public_row(p_row public.family_medicine_feedback)
returns jsonb language sql immutable security invoker set search_path = public, pg_temp as $$
  select case when p_row.id is null then null else jsonb_build_object(
    'id',p_row.id,'patient_id',p_row.patient_id,'visit_id',p_row.visit_id,
    'medicine_id',p_row.medicine_id,'rating',p_row.rating,'notes',p_row.notes,
    'revision',p_row.revision,'created_at',p_row.created_at) end
$$;

create or replace function public.family_feedback_medicine_is_current(
  p_snapshot jsonb, p_patient_id text, p_visit_id text, p_medicine_id text
) returns boolean language sql immutable security invoker set search_path = public, pg_temp as $$
  select p_patient_id in ('grandpa','grandma')
    and (select count(*) from jsonb_array_elements(coalesce(p_snapshot->'patients','[]'::jsonb)) p
      where p->>'id' = p_patient_id
        and p->>'name' = case p_patient_id when 'grandpa' then '爺爺' else '嬷嬷' end) = 1
    and (select count(*) from jsonb_array_elements(coalesce(p_snapshot->'visits','[]'::jsonb)) v
      where v->>'id' = p_visit_id) = 1
    and exists (select 1 from jsonb_array_elements(coalesce(p_snapshot->'visits','[]'::jsonb)) v
      where v->>'id' = p_visit_id and v->>'patient_id' = p_patient_id
        and v->>'status' = 'confirmed' and v->>'privacy_reviewed' = 'true'
        and (select count(*) from jsonb_array_elements(coalesce(v->'medicines','[]'::jsonb)) m
          where m->>'id' = p_medicine_id) = 1)
$$;

create or replace function public.family_medicine_feedback_access(
  p_action text,
  p_token_hash text default null,
  p_user_id uuid default null,
  p_archive_id text default 'family',
  p_patient_id text default null,
  p_visit_id text default null,
  p_medicine_id text default null,
  p_rating text default null,
  p_notes text default '',
  p_expected_revision integer default null,
  p_request_id uuid default null,
  p_snapshot_version uuid default null
) returns jsonb language plpgsql security invoker set search_path = public, pg_temp as $$
declare
  v_archive_id text;
  v_actor text;
  v_link public.family_share_links%rowtype;
  v_archive public.family_archives%rowtype;
  v_snapshot jsonb;
  v_existing public.family_medicine_feedback%rowtype;
  v_latest public.family_medicine_feedback%rowtype;
  v_result public.family_medicine_feedback%rowtype;
  v_items jsonb;
begin
  if p_action is null or p_action not in ('list','save')
    or ((p_token_hash is null) = (p_user_id is null)) then
    return jsonb_build_object('status','denied');
  end if;
  if p_token_hash is not null then
    if p_token_hash !~ '^[a-f0-9]{64}$' then return jsonb_build_object('status','denied'); end if;
    select s.* into v_link from public.family_share_links s where s.token_hash = p_token_hash;
    if not found then return jsonb_build_object('status','denied'); end if;
    v_archive_id := v_link.archive_id;
  else
    -- The Edge verifies this UUID with Auth; direct clients cannot execute this RPC.
    if p_archive_id is null or p_archive_id <> 'family' then return jsonb_build_object('status','denied'); end if;
    v_archive_id := p_archive_id;
  end if;

  -- Same archive-first order as publication and share rotation/revocation. The
  -- lock lasts through the insert, so neither current content nor access can
  -- change between validation and persistence. Saves serialize per archive.
  select a.* into v_archive from public.family_archives a where a.id = v_archive_id for update;
  if not found then return jsonb_build_object('status','denied'); end if;
  if p_token_hash is not null then
    select s.* into v_link from public.family_share_links s
      where s.id = v_link.id and s.archive_id = v_archive_id and s.token_hash = p_token_hash for update;
    if not found or v_link.revoked_at is not null
      or (v_link.expires_at is not null and v_link.expires_at <= clock_timestamp()) then
      return jsonb_build_object('status','denied');
    end if;
    v_actor := 'share:' || v_link.id::text;
  else
    perform 1 from public.family_members m where m.archive_id = v_archive_id and m.user_id = p_user_id for update;
    if not found then return jsonb_build_object('status','denied'); end if;
    v_actor := 'member:' || p_user_id::text;
  end if;
  select s.snapshot into v_snapshot from public.family_snapshots s
    where s.archive_id = v_archive_id and s.version_id = v_archive.current_version;
  if not found then
    if p_action = 'list' then return jsonb_build_object('status','ok','items','[]'::jsonb); end if;
    return jsonb_build_object('status','stale');
  end if;

  if p_action = 'list' then
    select coalesce(jsonb_agg(public.family_feedback_public_row(latest::public.family_medicine_feedback)
      order by latest.patient_id,latest.visit_id,latest.medicine_id),'[]'::jsonb) into v_items
    from (select distinct on (f.patient_id,f.visit_id,f.medicine_id) f.*
      from public.family_medicine_feedback f where f.archive_id = v_archive_id
        and public.family_feedback_medicine_is_current(v_snapshot,f.patient_id,f.visit_id,f.medicine_id)
      order by f.patient_id,f.visit_id,f.medicine_id,f.revision desc) latest;
    return jsonb_build_object('status','ok','items',v_items);
  end if;

  if p_patient_id is null or p_patient_id not in ('grandpa','grandma')
    or p_visit_id is null or p_visit_id !~ '^[A-Za-z0-9_-]{1,96}$'
    or p_medicine_id is null or p_medicine_id !~ '^[A-Za-z0-9_-]{1,96}$'
    or p_rating is null or p_rating not in ('effective','ineffective','undecided')
    or p_notes is null or char_length(p_notes) > 500 or octet_length(p_notes) > 2000
    or p_expected_revision is null or p_expected_revision < 0 or p_expected_revision >= 2147483647
    or p_request_id is null or p_snapshot_version is null then return jsonb_build_object('status','invalid'); end if;
  if not public.family_feedback_medicine_is_current(v_snapshot,p_patient_id,p_visit_id,p_medicine_id) then
    return jsonb_build_object('status','stale');
  end if;
  -- Replay is allowed only for the same authorized scope and exact payload.
  select f.* into v_existing from public.family_medicine_feedback f
    where f.archive_id = v_archive_id and f.request_id = p_request_id;
  if found then
    if v_existing.actor_scope <> v_actor or v_existing.patient_id <> p_patient_id
      or v_existing.visit_id <> p_visit_id or v_existing.medicine_id <> p_medicine_id
      or v_existing.rating <> p_rating or v_existing.notes <> p_notes
      or v_existing.snapshot_version <> p_snapshot_version
      or v_existing.revision <> p_expected_revision + 1 then
      return jsonb_build_object('status','invalid');
    end if;
    return jsonb_build_object('status','ok','item',public.family_feedback_public_row(v_existing));
  end if;
  if v_archive.current_version is distinct from p_snapshot_version then
    return jsonb_build_object('status','stale');
  end if;
  select f.* into v_latest from public.family_medicine_feedback f
    where f.archive_id = v_archive_id and f.patient_id = p_patient_id
      and f.visit_id = p_visit_id and f.medicine_id = p_medicine_id
    order by f.revision desc limit 1;
  if coalesce(v_latest.revision,0) <> p_expected_revision then
    return jsonb_build_object('status','conflict','item',public.family_feedback_public_row(v_latest));
  end if;
  if (select count(*) from public.family_medicine_feedback f where f.archive_id = v_archive_id
    and f.actor_scope = v_actor and f.created_at > clock_timestamp() - interval '1 minute') >= 30 then
    return jsonb_build_object('status','rate_limited');
  end if;
  if p_token_hash is not null and v_link.expires_at is not null and v_link.expires_at <= clock_timestamp() then
    return jsonb_build_object('status','denied');
  end if;
  insert into public.family_medicine_feedback
    (archive_id,snapshot_version,patient_id,visit_id,medicine_id,rating,notes,revision,request_id,actor_scope)
    values (v_archive_id,p_snapshot_version,p_patient_id,p_visit_id,p_medicine_id,p_rating,p_notes,
      p_expected_revision+1,p_request_id,v_actor) returning * into v_result;
  return jsonb_build_object('status','ok','item',public.family_feedback_public_row(v_result));
end $$;

revoke all on function public.family_feedback_public_row(public.family_medicine_feedback) from public, anon, authenticated;
revoke all on function public.family_feedback_medicine_is_current(jsonb,text,text,text) from public, anon, authenticated;
revoke all on function public.family_medicine_feedback_access(text,text,uuid,text,text,text,text,text,text,integer,uuid,uuid) from public, anon, authenticated;
grant execute on function public.family_feedback_public_row(public.family_medicine_feedback) to service_role;
grant execute on function public.family_feedback_medicine_is_current(jsonb,text,text,text) to service_role;
grant execute on function public.family_medicine_feedback_access(text,text,uuid,text,text,text,text,text,text,integer,uuid,uuid) to service_role;
commit;

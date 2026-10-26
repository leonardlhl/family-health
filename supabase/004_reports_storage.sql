-- Optional reports on schema v1, reviewed raster PDFs, immutable shared assets.
-- Existing published snapshots and capability links are not modified.
begin;
update storage.buckets set public = false, file_size_limit = 50000000,
  allowed_mime_types = array['image/png','image/jpeg','image/webp','application/pdf']
  where id = 'family-health';

-- One immutable object may belong to several retained versions. The current
-- manifest RLS policies from 001 remain the read authority, including Storage.
alter table public.family_media drop constraint if exists family_media_object_path_key;
create index if not exists family_media_object_path_idx on public.family_media(object_path);

create or replace function public.publish_family_snapshot(
  p_archive_id text, p_version_id uuid, p_snapshot jsonb, p_media jsonb
) returns void language plpgsql security invoker set search_path = public, pg_temp as $$
declare item jsonb; record_item jsonb; source_id jsonb; med jsonb; preview jsonb;
  kind text; extension text; expected_path text; legacy_path text; manifest_id text;
  linked_ids text[] := array[]::text[]; expected_assets integer := 0; max_bytes bigint;
begin
  if p_archive_id is null or p_archive_id !~ '^[a-z0-9][a-z0-9_-]{0,63}$' or p_version_id is null
    or p_snapshot->>'schema_version' is distinct from '1'
    or jsonb_typeof(p_snapshot->'patients') is distinct from 'array'
    or jsonb_typeof(p_snapshot->'visits') is distinct from 'array'
    or jsonb_typeof(coalesce(p_snapshot->'reports','[]'::jsonb)) is distinct from 'array'
    or jsonb_typeof(p_snapshot->'attachments') is distinct from 'array'
    or jsonb_typeof(p_media) is distinct from 'array' then
    raise exception 'Invalid archive snapshot';
  end if;
  if exists (select 1 from jsonb_array_elements(p_snapshot->'patients') p
      where p->>'id' not in ('grandpa','grandma') or p->>'id' is null
        or p->>'name' is distinct from case p->>'id' when 'grandpa' then '爺爺' else '嬷嬷' end)
    or (select count(*) <> count(distinct p->>'id') from jsonb_array_elements(p_snapshot->'patients') p)
    or (select count(*) <> count(distinct a->>'id') from jsonb_array_elements(p_snapshot->'attachments') a)
    or (select count(*) <> count(distinct m->>'id') from jsonb_array_elements(p_media) m) then
    raise exception 'Invalid or duplicate archive identifiers';
  end if;
  foreach kind in array array['visits','reports'] loop
    for record_item in select value from jsonb_array_elements(coalesce(p_snapshot->kind,'[]'::jsonb)) loop
      if record_item->>'status' is distinct from 'confirmed'
        or record_item->>'privacy_reviewed' is distinct from 'true'
        or coalesce(record_item->>'reviewed_at','') = ''
        or coalesce(record_item->>'id','') !~ '^[A-Za-z0-9_-]{1,96}$'
        or not exists (select 1 from jsonb_array_elements(p_snapshot->'patients') p
          where p->>'id' = record_item->>'patient_id')
        or jsonb_typeof(record_item->'source_file_ids') is distinct from 'array' then
        raise exception 'Only reviewed same-person records can be published';
      end if;
      if kind = 'reports' and (btrim(coalesce(record_item->>'title','')) = ''
        or coalesce(record_item->>'date','') !~ '^\d{4}-\d{2}-\d{2}$'
        or jsonb_array_length(record_item->'source_file_ids') = 0
        or coalesce(record_item->>'review_notes','') <> '') then
        raise exception 'Reports require reviewed source files, title and date';
      end if;
      for source_id in select value from jsonb_array_elements(record_item->'source_file_ids') loop
        if jsonb_typeof(source_id) <> 'string' or not exists (
          select 1 from jsonb_array_elements(p_snapshot->'attachments') a
          where a->>'id' = source_id #>> '{}' and a->>'patient_id' = record_item->>'patient_id') then
          raise exception 'Record source ownership mismatch';
        end if;
        linked_ids := array_append(linked_ids, source_id #>> '{}');
      end loop;
      if kind = 'visits' then
        if jsonb_typeof(record_item->'medicines') is distinct from 'array' then
          raise exception 'Invalid medicines';
        end if;
        for med in select value from jsonb_array_elements(record_item->'medicines') loop
          if jsonb_typeof(med->'source_file_ids') is distinct from 'array' then
            raise exception 'Invalid medicine sources';
          end if;
          for source_id in select value from jsonb_array_elements(med->'source_file_ids') loop
            if jsonb_typeof(source_id) <> 'string' or not exists (
              select 1 from jsonb_array_elements(p_snapshot->'attachments') a
              where a->>'id' = source_id #>> '{}' and a->>'patient_id' = record_item->>'patient_id') then
              raise exception 'Medicine source ownership mismatch';
            end if;
            linked_ids := array_append(linked_ids, source_id #>> '{}');
          end loop;
        end loop;
      end if;
    end loop;
  end loop;
  for item in select value from jsonb_array_elements(p_snapshot->'attachments') loop
    extension := case item->>'mime' when 'image/png' then '.png' when 'image/jpeg' then '.jpg'
      when 'image/webp' then '.webp' when 'application/pdf' then '.pdf' else null end;
    max_bytes := case when item->>'mime' = 'application/pdf' then 50000000 else 26214400 end;
    if extension is null or item->>'privacy_checked' is distinct from 'true'
      or coalesce(item->>'id','') !~ '^[A-Za-z0-9_-]{1,96}$'
      or not (item->>'id' = any(linked_ids))
      or coalesce(item->>'sha256','') !~ '^[a-f0-9]{64}$'
      or coalesce(item->>'size','') !~ '^[0-9]{1,9}$' then
      raise exception 'Invalid reviewed attachment';
    end if;
    if (item->>'size')::bigint < 1 or (item->>'size')::bigint > max_bytes then
      raise exception 'Attachment exceeds its size limit';
    end if;
    expected_path := p_archive_id || '/objects/' || (item->>'id') || '-' || (item->>'sha256') || extension;
    legacy_path := p_archive_id || '/' || p_version_id::text || '/' || (item->>'id') || extension;
    if item->>'object_path' is null or item->>'object_path' not in (expected_path, legacy_path) then
      raise exception 'Invalid immutable attachment path';
    end if;
    if not exists (select 1 from jsonb_array_elements(p_media) m
      where m->>'id' = item->>'id' and m->>'object_path' = item->>'object_path') then
      raise exception 'Attachment missing from current manifest';
    end if;
    expected_assets := expected_assets + 1;
    preview := item->'thumbnail';
    if item->>'mime' = 'application/pdf' and preview is null then
      raise exception 'PDF reports require a raster preview';
    end if;
    if preview is not null then
      if jsonb_typeof(preview) is distinct from 'object' or preview->>'mime' is distinct from 'image/jpeg'
        or coalesce(preview->>'sha256','') !~ '^[a-f0-9]{64}$'
        or coalesce(preview->>'size','') !~ '^[0-9]{1,9}$'
        or coalesce(preview->>'width','') !~ '^[0-9]{1,3}$'
        or coalesce(preview->>'height','') !~ '^[0-9]{1,3}$' then
        raise exception 'Invalid raster preview';
      end if;
      if (preview->>'size')::bigint not between 1 and 3145728
        or (preview->>'width')::int not between 1 and 960 or (preview->>'height')::int not between 1 and 960 then
        raise exception 'Invalid raster preview limits';
      end if;
      expected_path := p_archive_id || '/objects/' || (item->>'id') || '-' || (preview->>'sha256') || '.thumb.jpg';
      legacy_path := p_archive_id || '/' || p_version_id::text || '/' || (item->>'id') || '.thumb.jpg';
      manifest_id := (item->>'id') || ':thumbnail';
      if preview->>'object_path' is null or preview->>'object_path' not in (expected_path, legacy_path)
        or not exists (select 1 from jsonb_array_elements(p_media) m
          where m->>'id' = manifest_id and m->>'object_path' = preview->>'object_path') then
        raise exception 'Preview missing from current manifest';
      end if;
      expected_assets := expected_assets + 1;
    end if;
  end loop;
  if expected_assets <> jsonb_array_length(p_media) then
    raise exception 'Unexpected media manifest entries';
  end if;
  insert into public.family_archives (id) values (p_archive_id) on conflict do nothing;
  perform 1 from public.family_archives a where a.id = p_archive_id for update;
  insert into public.family_snapshots (archive_id, version_id, snapshot)
    values (p_archive_id, p_version_id, p_snapshot);
  insert into public.family_media (archive_id,version_id,attachment_id,object_path)
    select p_archive_id,p_version_id,m->>'id',m->>'object_path' from jsonb_array_elements(p_media) m;
  update public.family_archives set current_version = p_version_id, published_at = now() where id = p_archive_id;
end $$;
revoke all on function public.publish_family_snapshot(text,uuid,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.publish_family_snapshot(text,uuid,jsonb,jsonb) to service_role;

create or replace function public.family_storage_usage(p_archive_id text default 'family')
returns jsonb language plpgsql stable security invoker set search_path = public, pg_temp as $$
declare result jsonb;
begin
  if p_archive_id is distinct from 'family' then raise exception 'Unsupported archive'; end if;
  with objects as (
    select o.name,
      case when o.metadata->>'size' ~ '^[0-9]{1,18}$' then (o.metadata->>'size')::bigint else null end as bytes
      from storage.objects o where o.bucket_id = 'family-health'
  ), scoped as (
    select o.*,
      left(o.name,length(p_archive_id || '/')) = p_archive_id || '/' as owned,
      exists (select 1 from public.family_media m join public.family_archives a
        on a.id = m.archive_id and a.current_version = m.version_id
        where a.id = p_archive_id and m.object_path = o.name) as current_asset,
      exists (select 1 from public.family_media m where m.archive_id = p_archive_id and m.object_path = o.name) as referenced
      from objects o
  ) select jsonb_build_object(
    'bucket_bytes',coalesce(sum(s.bytes),0),'bucket_files',count(*),
    'archive_bytes',coalesce(sum(s.bytes) filter(where s.owned),0),'archive_files',count(*) filter(where s.owned),
    'current_bytes',coalesce(sum(s.bytes) filter(where s.current_asset),0),'current_files',count(*) filter(where s.current_asset),
    'unreferenced_bytes',coalesce(sum(s.bytes) filter(where s.owned and not s.referenced),0),
    'unreferenced_files',count(*) filter(where s.owned and not s.referenced),
    'unknown_size_files',count(*) filter(where s.bytes is null),
    'retained_versions',(select count(*) from public.family_snapshots v where v.archive_id = p_archive_id),
    'database_bytes',pg_database_size(current_database()),'measured_at',statement_timestamp()
  ) into result from scoped s;
  return result;
end $$;
revoke all on function public.family_storage_usage(text) from public, anon, authenticated;
grant execute on function public.family_storage_usage(text) to service_role;
commit;

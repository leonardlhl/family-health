-- Run after 001_family_archive.sql on the intended fresh Family Health project.
-- No patient records or source images are read or uploaded.
-- An existing published version is preserved, including on repeated execution.
begin;
do $$
declare active_version uuid;
begin
  select current_version into active_version
    from public.family_archives where id = 'family' for update;
  if not found then
    raise exception 'Missing family archive: apply 001_family_archive.sql first';
  end if;
  if active_version is null then
    perform public.publish_family_snapshot(
      'family',
      gen_random_uuid(),
      '{
        "schema_version":1,
        "patients":[
          {"id":"grandpa","name":"爺爺","age":null,"sex":"unknown"},
          {"id":"grandma","name":"嬷嬷","age":null,"sex":"unknown"}
        ],
        "visits":[],
        "attachments":[]
      }'::jsonb,
      '[]'::jsonb
    );
    raise notice 'Empty family archive initialized';
  else
    raise notice 'Existing published family archive preserved';
  end if;
end $$;
commit;

import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {createHash, webcrypto} from 'node:crypto';

const source = await readFile(new URL('../supabase/functions/family-share/index.ts', import.meta.url), 'utf8');
const {createHandler} = await import('data:text/javascript;base64,' + Buffer.from(source+'\n//# sourceURL=family-share-test.js').toString('base64'));
const ORIGIN = 'https://leonardlhl.github.io';
const TOKEN = 'A'.repeat(43);
const TOKEN_HASH = createHash('sha256').update(TOKEN).digest('hex');
const VERSION = '00000000-0000-4000-8000-000000000101';
const OTHER_VERSION = '00000000-0000-4000-8000-000000000102';
const SHARE_ID = '00000000-0000-4000-8000-000000000201';
const KEY = 'sb_secret_offline_test_only';
const FULL = new Uint8Array([137,80,78,71]);
const PREVIEW = new Uint8Array([255,216,255]);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const json = (body,status=200) => new Response(JSON.stringify(body), {status,headers:{'Content-Type':'application/json'}});

function fixture() {
  const archive = {id:'family',current_version:VERSION,published_at:'2026-10-25T00:00:00Z'};
  const link = {id:SHARE_ID,archive_id:'family',revoked_at:null,expires_at:null};
  const file = {id:'file1',patient_id:'grandpa',privacy_checked:true,mime:'image/png',size:FULL.length,
    sha256:sha(FULL),object_path:`family/${VERSION}/file1.png`,thumbnail:{mime:'image/jpeg',size:PREVIEW.length,
      sha256:sha(PREVIEW),width:120,height:80,object_path:`family/${VERSION}/file1.thumb.jpg`}};
  const snapshot = {schema_version:1,patients:[{id:'grandpa',name:'爺爺',age:null,sex:'unknown'}],
    visits:[{id:'visit1',patient_id:'grandpa',status:'confirmed',privacy_reviewed:true,
      source_file_ids:['file1'],medicines:[]}],attachments:[file]};
  return {archive,link,snapshot};
}

function harness(change = () => {}) {
  const state = fixture();
  const calls = [];
  let linkReads = 0;
  let archiveReads = 0;
  change(state);
  const fetch = async (url, options) => {
    calls.push({url,options});
    assert.equal(new URL(url).origin,'https://example.supabase.co');
    assert.equal(options.method,'GET');
    assert.equal(options.redirect,'error');
    assert.equal(options.headers.apikey,KEY);
    assert.equal(options.headers.Authorization,undefined);
    assert.equal(url.includes(TOKEN),false);
    const parsed = new URL(url);
    const query = parsed.searchParams;
    if (parsed.pathname.endsWith('/family_share_links')) {
      assert.equal(query.get('token_hash'),`eq.${TOKEN_HASH}`);
      linkReads++;
      if (state.revokedDuringRead && linkReads > 1) return json([{...state.link,revoked_at:'2026-10-25T00:00:01Z'}]);
      return json(state.absent ? [] : [state.link]);
    }
    if (parsed.pathname.endsWith('/family_archives')) {
      assert.equal(query.get('id'),`eq.${state.link.archive_id}`);
      archiveReads++;
      return json([{...state.archive,current_version:state.changedDuringRead && archiveReads > 1 ? OTHER_VERSION : state.archive.current_version}]);
    }
    if (parsed.pathname.endsWith('/family_snapshots')) {
      assert.equal(query.get('archive_id'),`eq.${state.link.archive_id}`);
      assert.equal(query.get('version_id'),`eq.${state.archive.current_version}`);
      return json([{archive_id:state.snapshotArchive || state.link.archive_id,
        version_id:state.snapshotVersion || state.archive.current_version,snapshot:state.snapshot}]);
    }
    if (parsed.pathname.endsWith('/family_media')) {
      assert.equal(query.get('archive_id'),`eq.${state.link.archive_id}`);
      assert.equal(query.get('version_id'),`eq.${state.archive.current_version}`);
      const preview = query.get('attachment_id') === 'eq.file1:thumbnail';
      return json(state.noManifest ? [] : [{archive_id:state.manifestArchive || state.link.archive_id,
        version_id:state.manifestVersion || state.archive.current_version,
        attachment_id:preview ? 'file1:thumbnail' : 'file1',
        object_path:state.manifestPath || (preview ? state.snapshot.attachments[0].thumbnail.object_path : state.snapshot.attachments[0].object_path)}]);
    }
    if (parsed.pathname.startsWith('/storage/')) {
      const preview = parsed.pathname.endsWith('.thumb.jpg');
      return new Response(state.badBytes ? new Uint8Array([0]) : preview ? PREVIEW : state.fileBytes || FULL,
        {headers:{'Content-Type':state.storageMime || (preview ? 'image/jpeg' : state.snapshot.attachments[0].mime)}});
    }
    throw new Error('Unexpected offline fetch');
  };
  const handler = createHandler({fetch,crypto:webcrypto,now:()=>Date.parse('2026-10-25T01:00:00Z'),
    getEnv:name => ({SUPABASE_URL:'https://example.supabase.co',SUPABASE_SERVICE_ROLE_KEY:KEY})[name]});
  const request = (body={token:TOKEN,action:'snapshot'},headers={},method='POST') => handler(new Request('https://example.supabase.co/functions/v1/family-share', {
    method,headers:{Origin:ORIGIN,'Content-Type':'application/json',...headers},...(method === 'POST' ? {body:typeof body === 'string' ? body : JSON.stringify(body)} : {})
  }));
  return {state,calls,request,handler};
}

test('snapshot uses token-derived archive and responds without caching or credentials', async () => {
  const h = harness();
  const result = await h.request();
  assert.equal(result.status,200);
  assert.equal(result.headers.get('Access-Control-Allow-Origin'),ORIGIN);
  assert.match(result.headers.get('Cache-Control'),/no-store/);
  const body = await result.text();
  assert.equal(body.includes(TOKEN),false);
  assert.equal(body.includes(KEY),false);
  assert.deepEqual(JSON.parse(body),{...h.state.snapshot,reports:[],published_at:h.state.archive.published_at,snapshot_version:VERSION});
  assert.equal(h.calls.filter(c=>c.url.includes('family_share_links')).length,2);
});

test('files and thumbnails require exact current manifest and proxy verified bytes', async () => {
  for (const thumbnail of [false,true]) {
    const h = harness();
    const result = await h.request({token:TOKEN,action:'file',attachmentId:'file1',thumbnail});
    assert.equal(result.status,200);
    assert.deepEqual(new Uint8Array(await result.arrayBuffer()),thumbnail ? PREVIEW : FULL);
    assert.equal(result.headers.get('Content-Type'),thumbnail ? 'image/jpeg' : 'image/png');
    assert.match(result.headers.get('Cache-Control'),/no-store/);
    assert.equal(h.calls.some(c=>c.url.includes('/object/sign/')),false);
    assert.equal(h.calls.filter(c=>c.url.includes('family_share_links')).length,2);
  }
});

test('missing, malformed, revoked, expired and unknown links return identical 403', async () => {
  const cases = [
    [()=>{}, {action:'snapshot'}],
    [()=>{}, {token:'short',action:'snapshot'}],
    [s=>{s.absent=true;}],
    [s=>{s.link.revoked_at='2026-10-24T00:00:00Z';}],
    [s=>{s.link.expires_at='2026-10-25T00:00:00Z';}],
    [s=>{s.link.expires_at='invalid';}],
  ];
  const results=[];
  for (const [change,body] of cases) {
    const h=harness(change); const result=await h.request(body);
    assert.equal(result.status,403); results.push(await result.text());
    assert.equal(h.calls.some(c=>c.url.includes('family_snapshots')),false);
  }
  assert.equal(new Set(results).size,1);
});

test('arbitrary paths, write actions, extra parameters and oversized bodies are rejected before DB access', async () => {
  for (const body of [
    {token:TOKEN,action:'delete'},
    {token:TOKEN,action:'snapshot',archiveId:'other'},
    {token:TOKEN,action:'file',attachmentId:'../other'},
    {token:TOKEN,action:'file',attachmentId:'file1',object_path:'other/photo.png'},
    {token:TOKEN,action:'file',attachmentId:'file1',thumbnail:'true'},
    ' '.repeat(4097), '{broken',
  ]) {
    const h=harness(); assert.equal((await h.request(body)).status,403); assert.equal(h.calls.length,0);
  }
  for (const method of ['GET','PUT','PATCH','DELETE']) {
    const h=harness(); assert.equal((await h.request(undefined,{},method)).status,405); assert.equal(h.calls.length,0);
  }
});

test('CORS rejects foreign, missing and opaque origins; preflight permits JSON POST only', async () => {
  for (const origin of ['https://leonardlhl.github.io.attacker.invalid','https://evil.invalid','null','']) {
    const h=harness(); const result=await h.request(undefined,{Origin:origin});
    assert.equal(result.status,403); assert.equal(result.headers.has('Access-Control-Allow-Origin'),false); assert.equal(h.calls.length,0);
  }
  const h=harness();
  const missing = new Request('https://example.supabase.co/functions/v1/family-share', {method:'POST',body:JSON.stringify({token:TOKEN,action:'snapshot'})});
  assert.equal((await h.handler(missing)).status,403);
  for (const origin of [ORIGIN,'http://localhost:8767','http://127.0.0.1:8767']) {
    const result=await h.request(undefined,{Origin:origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'},'OPTIONS');
    assert.equal(result.status,204); assert.equal(result.headers.get('Access-Control-Allow-Origin'),origin);
  }
  assert.equal((await h.request(undefined,{'Access-Control-Request-Method':'DELETE'},'OPTIONS')).status,403);
  assert.equal((await h.request(undefined,{'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization'},'OPTIONS')).status,204);
  assert.equal((await h.request(undefined,{'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'x-actor'},'OPTIONS')).status,403);
});

test('cross-archive snapshot, stale path, patient mismatch and unreviewed data cannot be returned', async () => {
  for (const change of [
    s=>{s.snapshotArchive='other';}, s=>{s.snapshotVersion=OTHER_VERSION;},
    s=>{s.snapshot.attachments[0].object_path=`other/${VERSION}/file1.png`;},
    s=>{s.snapshot.attachments[0].object_path=`family/${OTHER_VERSION}/file1.png`;},
    s=>{s.snapshot.attachments[0].thumbnail.object_path=`other/${VERSION}/file1.thumb.jpg`;},
    s=>{s.snapshot.attachments[0].patient_id='grandma';},
    s=>{s.snapshot.attachments[0].privacy_checked=false;},
    s=>{s.snapshot.visits[0].status='draft';},
    s=>{s.snapshot.visits[0].source_file_ids=[];},
  ]) {
    const h=harness(change); assert.equal((await h.request()).status,403);
    assert.equal(h.calls.some(c=>c.url.includes('/storage/')),false);
  }
});

test('file manifests block other archives, previous versions, missing and different object paths', async () => {
  for (const change of [s=>{s.manifestArchive='other';},s=>{s.manifestVersion=OTHER_VERSION;},
    s=>{s.noManifest=true;},s=>{s.manifestPath=`family/${VERSION}/other.png`;}]) {
    const h=harness(change);
    assert.equal((await h.request({token:TOKEN,action:'file',attachmentId:'file1'})).status,403);
    assert.equal(h.calls.some(c=>c.url.includes('/storage/')),false);
  }
  const h=harness();
  assert.equal((await h.request({token:TOKEN,action:'file',attachmentId:'removed-since-last-snapshot'})).status,503);
  assert.equal(h.calls.some(c=>c.url.includes('/storage/')),false);
});

test('revocation or publication during reads prevents late snapshot and image delivery', async () => {
  for (const [change,status] of [[s=>{s.revokedDuringRead=true;},403],[s=>{s.changedDuringRead=true;},503]]) {
    for (const action of ['snapshot','file']) {
      const h=harness(change);
      const result=await h.request({token:TOKEN,action,...(action==='file'?{attachmentId:'file1'}:{})});
      assert.equal(result.status,status);
    }
  }
});

test('image MIME and checksum mismatch cannot be proxied', async () => {
  for (const change of [s=>{s.storageMime='text/html';},s=>{s.badBytes=true;}]) {
    const h=harness(change);
    assert.equal((await h.request({token:TOKEN,action:'file',attachmentId:'file1'})).status,403);
  }
});

test('empty bootstrap schema works and has no storage access', async () => {
  const sql=await readFile(new URL('../supabase/002_bootstrap_empty.sql',import.meta.url),'utf8');
  const payload=JSON.parse(sql.match(/'(\{\s*"schema_version"[\s\S]*?\})'::jsonb/)[1]);
  const h=harness(s=>{s.snapshot=payload;});
  const result=await h.request(); assert.equal(result.status,200);
  assert.deepEqual((await result.json()).visits,[]);
  assert.equal(h.calls.some(c=>c.url.includes('/storage/')),false);
});

test('share table and management RPCs have no browser grants; gateway JWT verification disabled', async () => {
  const sql=await readFile(new URL('../supabase/003_family_share_links.sql',import.meta.url),'utf8');
  assert.match(sql,/alter table public\.family_share_links enable row level security/i);
  assert.match(sql,/revoke all on public\.family_share_links from public, anon, authenticated/i);
  assert.match(sql,/revoke all on function public\.rotate_family_share_link\(text,uuid,uuid,text\) from public, anon, authenticated/i);
  assert.match(sql,/revoke all on function public\.revoke_family_share_link\(text,uuid,text\) from public, anon, authenticated/i);
  assert.doesNotMatch(sql,/create policy/i);
  const config=await readFile(new URL('../supabase/config.toml',import.meta.url),'utf8');
  assert.match(config,/\[functions\.family-share\][\s\S]*verify_jwt = false/);
});

function reportOnly(state,pdf=false,cas=false){
  state.snapshot.visits=[];
  state.snapshot.reports=[{id:'report1',patient_id:'grandpa',date:'2026-10-26',title:'SYNTHETIC REPORT',
    status:'confirmed',privacy_reviewed:true,reviewed_at:'2026-10-26T00:00:00Z',source_file_ids:['file1']}];
  const file=state.snapshot.attachments[0];
  if(pdf){
    state.fileBytes=new TextEncoder().encode('%PDF-1.7 synthetic sanitized fixture');
    file.mime='application/pdf';file.size=state.fileBytes.length;file.sha256=sha(state.fileBytes);
    file.object_path=`family/${VERSION}/file1.pdf`;
  }
  if(cas){
    file.object_path=`family/objects/file1-${file.sha256}${pdf?'.pdf':'.png'}`;
    file.thumbnail.object_path=`family/objects/file1-${file.thumbnail.sha256}.thumb.jpg`;
  }
}

test('confirmed reports and PDF original/first-page JPEG work on legacy and immutable paths',async()=>{
  for(const cas of [false,true]){
    const h=harness(s=>reportOnly(s,true,cas));
    const snapshot=await h.request();assert.equal(snapshot.status,200);
    assert.equal((await snapshot.json()).reports[0].id,'report1');
    const full=await h.request({token:TOKEN,action:'file',attachmentId:'file1',thumbnail:false});
    assert.equal(full.status,200);assert.equal(full.headers.get('Content-Type'),'application/pdf');
    assert.deepEqual(new Uint8Array(await full.arrayBuffer()),h.state.fileBytes);
    const preview=await h.request({token:TOKEN,action:'file',attachmentId:'file1',thumbnail:true});
    assert.equal(preview.status,200);assert.equal(preview.headers.get('Content-Type'),'image/jpeg');
    assert.deepEqual(new Uint8Array(await preview.arrayBuffer()),PREVIEW);
  }
});

test('reports cannot expose other patients, drafts, unchecked PDFs or mismatched immutable hashes',async()=>{
  for(const change of [
    s=>{s.snapshot.reports[0].patient_id='grandma';},s=>{s.snapshot.reports[0].status='draft';},
    s=>{s.snapshot.reports[0].privacy_reviewed=false;},s=>{s.snapshot.reports[0].source_file_ids=[];},
    s=>{s.snapshot.reports[0].review_notes='UNRESOLVED';},
    s=>{delete s.snapshot.attachments[0].thumbnail;},s=>{s.snapshot.attachments[0].size=50000001;},
    s=>{s.snapshot.attachments[0].object_path=`family/objects/file1-${'f'.repeat(64)}.pdf`;},
  ]){
    const h=harness(s=>{reportOnly(s,true,true);change(s);});
    assert.equal((await h.request()).status,403);
    assert.equal(h.calls.some(c=>c.url.includes('/storage/')),false);
  }
});

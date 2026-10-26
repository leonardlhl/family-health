import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {createHash,webcrypto} from 'node:crypto';
import {createCloudClient,createShareClient} from '../web/cloud.js';

const source=await readFile(new URL('../supabase/functions/family-share/index.ts',import.meta.url),'utf8');
const {createHandler}=await import('data:text/javascript;base64,'+Buffer.from(source+'\n//# sourceURL=feedback-edge-test.js').toString('base64'));
const ORIGIN='https://leonardlhl.github.io',BASE='https://example.supabase.co';
const TOKEN='B'.repeat(43),KEY='sb_secret_offline_feedback_only';
const VERSION='10000000-0000-4000-8000-000000000001';
const USER='10000000-0000-4000-8000-000000000002';
const REQUEST='10000000-0000-4000-8000-000000000003';
const ROW='10000000-0000-4000-8000-000000000004';
const INPUT={patient_id:'grandpa',visit_id:'visit1',medicine_id:'med1',rating:'effective',notes:'食後感受',expected_revision:0,request_id:REQUEST};
const ITEM={id:ROW,patient_id:'grandpa',visit_id:'visit1',medicine_id:'med1',rating:'effective',notes:'食後感受',revision:1,created_at:'2026-10-26T06:00:00Z'};
const snapshot={schema_version:1,snapshot_version:VERSION,patients:[{id:'grandpa',name:'爺爺'}],
  visits:[{id:'visit1',patient_id:'grandpa',status:'confirmed',privacy_reviewed:true,source_file_ids:[],medicines:[{id:'med1',source_file_ids:[]}]}],attachments:[]};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});

function edgeHarness() {
  const state={result:{status:'ok',items:[{...ITEM,actor_scope:'private',request_id:REQUEST,event_no:1}]},authStatus:200,user:{id:USER,is_anonymous:false},upstreamStatus:200};
  const calls=[];
  const handler=createHandler({crypto:webcrypto,getEnv:name=>({SUPABASE_URL:BASE,SUPABASE_SERVICE_ROLE_KEY:KEY})[name],fetch:async(url,options)=>{
    calls.push({url,options});
    assert.equal(new URL(url).origin,BASE);assert.equal(options.redirect,'error');assert.equal(options.cache,'no-store');
    assert.equal(options.headers.apikey,KEY);assert.ok(!url.includes(TOKEN));
    if(url===BASE+'/auth/v1/user') {
      assert.equal(options.method,'GET');assert.equal(options.headers.Authorization,'Bearer valid.jwt.token');
      return json(state.user,state.authStatus);
    }
    assert.equal(url,BASE+'/rest/v1/rpc/family_medicine_feedback_access');
    assert.equal(options.method,'POST');assert.equal(options.headers.Authorization,undefined);
    const payload=JSON.parse(options.body);assert.equal(payload.p_archive_id,'family');assert.equal(options.body.includes(TOKEN),false);
    return json(state.result,state.upstreamStatus);
  }});
  const request=(body={token:TOKEN,action:'feedback-list'},headers={})=>handler(new Request(BASE+'/functions/v1/family-share',{
    method:'POST',headers:{Origin:ORIGIN,'Content-Type':'application/json',...headers},body:JSON.stringify(body),
  }));
  return {state,calls,handler,request};
}

test('capability feedback reaches only the scoped transaction RPC and strips internal fields',async()=>{
  const h=edgeHarness(),res=await h.request();
  assert.equal(res.status,200);assert.match(res.headers.get('Cache-Control'),/no-store/);
  assert.deepEqual(await res.json(),{items:[ITEM]});assert.equal(h.calls.length,1);
  const body=JSON.parse(h.calls[0].options.body);
  assert.equal(body.p_token_hash,createHash('sha256').update(TOKEN).digest('hex'));
  assert.equal(body.p_user_id,null);assert.equal(body.p_action,'list');
});

test('feedback save forwards exact version, revision, idempotency UUID, and up to500 Unicode characters',async()=>{
  const h=edgeHarness();h.state.result={status:'ok',item:{...ITEM,notes:'😀'.repeat(500)}};
  const res=await h.request({token:TOKEN,action:'feedback-save',...INPUT,notes:'😀'.repeat(500),snapshot_version:VERSION});
  assert.equal(res.status,200);assert.equal([...(await res.json()).item.notes].length,500);
  const body=JSON.parse(h.calls[0].options.body);
  assert.equal(body.p_expected_revision,0);assert.equal(body.p_snapshot_version,VERSION);assert.equal(body.p_request_id,REQUEST);
  assert.equal(body.p_rating,'effective');assert.equal(body.p_notes,'😀'.repeat(500));
});

test('malformed feedback and injected actor/archive/clinical fields never reach upstream',async()=>{
  for(const change of [{rating:'cured'},{rating:1},{notes:'字'.repeat(501)},{notes:12},{notes:'bad\u0000text'},
    {expected_revision:-1},{expected_revision:1.5},{expected_revision:2147483647},{request_id:'bad'},
    {snapshot_version:'old'},{patient_id:'other'},{medicine_id:'../other'},{visit_id:null},
    {actor:'granddaughter'},{user_id:USER},{archive_id:'other'},{diagnosis:'changed'}]) {
    const h=edgeHarness();assert.equal((await h.request({token:TOKEN,action:'feedback-save',...INPUT,snapshot_version:VERSION,...change})).status,400);
    assert.equal(h.calls.length,0);
  }
  const h=edgeHarness();assert.equal((await h.request({token:TOKEN,action:'feedback-save',...INPUT,snapshot_version:VERSION},{Authorization:'Bearer valid.jwt.token'})).status,403);
  assert.equal(h.calls.length,0);
});

test('invited login feedback verifies user with Auth before allowlist RPC; expired/anonymous users never reach RPC',async()=>{
  const h=edgeHarness();assert.equal((await h.request({action:'feedback-list'},{Authorization:'Bearer valid.jwt.token'})).status,200);
  assert.equal(h.calls.length,2);const body=JSON.parse(h.calls[1].options.body);assert.equal(body.p_user_id,USER);assert.equal(body.p_token_hash,null);
  for(const change of [{authStatus:401},{authStatus:403},{user:{id:USER,is_anonymous:true}},{user:{id:'forged'}}]) {
    const denied=edgeHarness();Object.assign(denied.state,change);
    assert.equal((await denied.request({action:'feedback-list'},{Authorization:'Bearer valid.jwt.token'})).status,403);
    assert.equal(denied.calls.length,1);
  }
  const outsider=edgeHarness();outsider.state.result={status:'denied'};
  assert.equal((await outsider.request({action:'feedback-list'},{Authorization:'Bearer valid.jwt.token'})).status,403);
});

test('transaction denial/revocation, stale context, conflict, invalid replay and server failure stay distinct and safe',async()=>{
  for(const [result,status,code] of [[{status:'denied'},403,null],[{status:'stale'},409,'feedback_stale'],
    [{status:'conflict',item:{...ITEM,actor_scope:'hidden',request_id:REQUEST}},409,'feedback_conflict'],
    [{status:'invalid'},400,'feedback_invalid'],[{status:'rate_limited'},429,'feedback_rate_limited']]) {
    const h=edgeHarness();h.state.result=result;
    const res=await h.request({token:TOKEN,action:'feedback-save',...INPUT,snapshot_version:VERSION});
    assert.equal(res.status,status);const value=await res.json();if(code)assert.equal(value.error,code);
    if(result.status==='conflict')assert.deepEqual(value.item,ITEM);
    assert.equal(JSON.stringify(value).includes('hidden'),false);
  }
  const h=edgeHarness();h.state.upstreamStatus=500;h.state.result={message:'SECRET DATABASE DETAIL'};
  const res=await h.request();assert.equal(res.status,503);assert.equal((await res.text()).includes('SECRET'),false);
});

function browserHarness() {
  const state={feedback:{items:[ITEM]},feedbackStatus:200,failed:false,calls:[],snapshot:structuredClone(snapshot)};
  globalThis.sessionStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
  globalThis.fetch=async(url,options={})=>{
    state.calls.push({url,options});
    if(url.includes('/auth/v1/token'))return json({access_token:'valid.jwt.token',refresh_token:'refresh',expires_in:3600,user:{id:USER}});
    if(url.includes('/auth/v1/logout'))return json({});
    if(url.includes('/family_archives?'))return json([{id:'family',current_version:state.snapshot.snapshot_version}]);
    if(url.includes('/family_snapshots?'))return json([{snapshot:state.snapshot}]);
    assert.equal(url,BASE+'/functions/v1/family-share');
    const payload=JSON.parse(options.body);
    if(payload.action==='snapshot')return json(state.snapshot);
    if(state.failed)throw new Error('offline');
    return json(state.feedback,state.feedbackStatus);
  };
  return state;
}

test('both browser clients send scoped feedback with loaded version and preserve safe response fields',async()=>{
  for(const mode of ['share','login']) {
    const h=browserHarness();const client=mode==='share'?createShareClient({supabaseUrl:BASE},TOKEN)
      :createCloudClient({supabaseUrl:BASE,supabaseAnonKey:'sb_publishable_offline',archiveId:'family'});
    if(mode==='login')await client.signIn('synthetic@example.invalid','test');
    await client.getData();assert.deepEqual(await client.getFeedback(),{items:[ITEM]});
    h.feedback={item:{...ITEM,actor_scope:'excluded',event_no:4}};
    assert.deepEqual(await client.saveFeedback(INPUT),{item:ITEM});
    const call=h.calls.at(-1),body=JSON.parse(call.options.body);
    assert.equal(body.snapshot_version,VERSION);assert.equal(body.request_id,REQUEST);assert.equal(body.action,'feedback-save');
    assert.equal(call.options.redirect,'error');assert.equal(call.options.referrerPolicy,'no-referrer');
    assert.equal(body.token,mode==='share'?TOKEN:undefined);
    assert.equal(call.options.headers.Authorization,mode==='login'?'Bearer valid.jwt.token':undefined);
    assert.equal(call.options.headers.apikey,undefined);
    client.destroy();
  }
});

test('browser conflict exposes safe latest item; unknown network retries retain exact request identity',async()=>{
  for(const mode of ['share','login']) {
    const h=browserHarness();const client=mode==='share'?createShareClient({supabaseUrl:BASE},TOKEN)
      :createCloudClient({supabaseUrl:BASE,supabaseAnonKey:'sb_publishable_offline'});
    if(mode==='login')await client.signIn('synthetic@example.invalid','test');
    await client.getData();h.feedbackStatus=409;h.feedback={error:'feedback_conflict',item:{...ITEM,revision:2,actor_scope:'secret'}};
    await assert.rejects(client.saveFeedback(INPUT),error=>error.status===409&&error.code==='feedback_conflict'&&error.item.revision===2&&!error.item.actor_scope);
    h.failed=true;await assert.rejects(client.saveFeedback(INPUT));const retryBody=h.calls.at(-1).options.body;
    h.snapshot.snapshot_version='10000000-0000-4000-8000-000000000005';
    await client.getData(); // An uncertain earlier write retains its original version on retry.
    h.failed=false;h.feedbackStatus=200;h.feedback={item:ITEM};await client.saveFeedback(INPUT);
    assert.equal(h.calls.at(-1).options.body,retryBody);
    const count=h.calls.length;
    await assert.rejects(client.saveFeedback({...INPUT,notes:'changed after uncertainty'}),error=>error.status===400);
    assert.equal(h.calls.length,count);
    h.feedback={items:[{...ITEM,revision:3}]};assert.equal((await client.getFeedback()).items[0].revision,3);
    client.destroy();
  }
});

test('browser invalid input/stale unloaded version does not send; revoked feedback permanently clears capability',async()=>{
  const h=browserHarness();let denied=0;const client=createShareClient({supabaseUrl:BASE},TOKEN,{onDenied:()=>denied++});
  await assert.rejects(client.saveFeedback(INPUT),error=>error.status===409&&error.code==='feedback_stale');
  assert.equal(h.calls.length,0);await client.getData();const count=h.calls.length;
  await assert.rejects(client.saveFeedback({...INPUT,notes:'字'.repeat(501)}),error=>error.status===400);
  assert.equal(h.calls.length,count);
  h.feedbackStatus=403;h.feedback={error:'denied'};await assert.rejects(client.getFeedback());assert.equal(denied,1);
  const last=h.calls.length;await assert.rejects(client.getFeedback());assert.equal(h.calls.length,last);
});

test('signed-in feedback denial clears the session and notifies the view',async()=>{
  const h=browserHarness();const client=createCloudClient({supabaseUrl:BASE,supabaseAnonKey:'sb_publishable_offline'});
  await client.signIn('synthetic@example.invalid','test');await client.getData();let cleared=0;
  client.onAuthChange(value=>{if(value===null)cleared++;});h.feedbackStatus=403;h.feedback={error:'denied'};
  await assert.rejects(client.getFeedback(),error=>error.status===403&&error.code==='feedback_denied');
  assert.equal(await client.getSession(),null);assert.equal(cleared,1);
  const count=h.calls.length;await assert.rejects(client.getFeedback());assert.equal(h.calls.length,count);
});

test('SQL retains append-only service-only revisions, idempotency, archive locks and current prescription checks',async()=>{
  const sql=await readFile(new URL('../supabase/005_medicine_feedback.sql',import.meta.url),'utf8');
  assert.match(sql,/enable row level security/i);assert.match(sql,/grant select, insert on public\.family_medicine_feedback to service_role/i);
  assert.doesNotMatch(sql,/grant\s+(?:all|update|delete).*family_medicine_feedback/i);
  assert.match(sql,/event_no bigint generated always as identity unique/i);
  assert.match(sql,/unique \(archive_id, request_id\)/i);assert.match(sql,/clock_timestamp\(\)/);
  assert.match(sql,/where a\.id = v_archive_id for update/);assert.match(sql,/m\.user_id = p_user_id for update/);
  assert.match(sql,/v_archive\.current_version is distinct from p_snapshot_version/);
  assert.match(sql,/v_existing\.snapshot_version <> p_snapshot_version/);
  assert.ok(sql.indexOf("v_link.revoked_at is not null")<sql.indexOf('where f.archive_id = v_archive_id and f.request_id = p_request_id'));
  assert.doesNotMatch(sql,/(update|delete from) public\.family_(?:snapshots|media|medicine_feedback)\b/i);
});

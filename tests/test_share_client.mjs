/** Capability-reader tests: synthetic tokens and records, no browser or live network. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import {createHash, webcrypto} from 'node:crypto';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const source=fs.readFileSync(path.join(root,'web/cloud.js'),'utf8').replaceAll('export function','function');
const TOKEN='A'.repeat(43);
const BASE='https://synthetic.supabase.co';
const checksum=bytes=>createHash('sha256').update(new Uint8Array(bytes)).digest('hex');

function fixture({preview=false}={}) {
  const file={id:'file-one',patient_id:'grandpa',privacy_checked:true,mime:'image/png',size:3,
    sha256:checksum([1,2,3]),object_path:'family/synthetic/file-one.png'};
  if(preview)file.thumbnail={mime:'image/jpeg',size:2,sha256:checksum([1,2]),
    object_path:'family/synthetic/file-one.thumb.jpg',width:32,height:32};
  return {schema_version:1,patients:[{id:'grandpa',name:'爺爺'}],
    visits:[{id:'visit-one',patient_id:'grandpa',status:'confirmed',privacy_reviewed:true,
      source_file_ids:['file-one'],medicines:[]}],attachments:[file],published_at:'2026-10-25T00:00:00Z'};
}
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const image=(mime='image/png',bytes=[1,2,3])=>new Response(new Uint8Array(bytes),{headers:{'Content-Type':mime}});

function harness() {
  const calls=[],created=[],revoked=[];
  let reply=async()=>{throw Error('Unexpected network request');};
  const context={Map,Set,Uint8Array,TextDecoder,URLSearchParams,Blob,crypto:webcrypto,
    URL:{createObjectURL:blob=>{const url='blob:synthetic-'+(created.length+1);created.push({url,blob});return url;},
      revokeObjectURL:url=>revoked.push(url)},
    fetch:async(url,options)=>{calls.push({url,options,body:JSON.parse(options.body)});return reply(url,options);},
    console:{log(){throw Error('Logging is prohibited');},error(){throw Error('Logging is prohibited');}},
  };
  for(const key of ['localStorage','sessionStorage','indexedDB']) {
    Object.defineProperty(context,key,{get(){throw Error('Capability persistence is prohibited');}});
  }
  vm.createContext(context);
  vm.runInContext(source+'\nglobalThis.share={readShareToken,createShareClient};',context,{filename:'cloud.js'});
  return {share:context.share,calls,created,revoked,setReply:fn=>{reply=fn;},
    client:options=>context.share.createShareClient({supabaseUrl:BASE,supabaseAnonKey:'MUST_NOT_BE_SENT'},TOKEN,options)};
}

test('fragment parser distinguishes absent, malformed and duplicate capabilities',()=>{
  const h=harness(),parse=h.share.readShareToken;
  assert.equal(parse(''),null);assert.equal(parse('#other=value'),null);
  assert.equal(parse('#share='+TOKEN),TOKEN);
  for(const fragment of ['#share','#share=','#share=short','#share='+TOKEN+'=',
    '#share='+TOKEN+'&share='+TOKEN,'#share=%E0%A4%A','#share='+TOKEN.slice(1),
    '#share='+TOKEN+'A','#share='+TOKEN.slice(1)+'+']) {
    assert.throws(()=>parse(fragment),error=>!error.message.includes(TOKEN)&&/分享/.test(error.message));
  }
  assert.equal(h.calls.length,0);
});

test('invalid service URLs and tokens fail before any request',()=>{
  const h=harness();
  assert.throws(()=>h.share.createShareClient({supabaseUrl:'https://attacker.example'},TOKEN));
  assert.throws(()=>h.share.createShareClient({supabaseUrl:'http://synthetic.supabase.co'},TOKEN));
  assert.throws(()=>h.share.createShareClient({supabaseUrl:BASE},'bad-token'));
  assert.equal(h.calls.length,0);
});

test('capability travels only in a protected POST body without account keys or persistence',async()=>{
  const h=harness();h.setReply(()=>json(fixture()));const client=h.client();
  const data=await client.getData();assert.equal(data.visits.length,1);
  const call=h.calls[0];assert.equal(call.url,BASE+'/functions/v1/family-share');
  assert.ok(!call.url.includes(TOKEN));assert.deepEqual(call.body,{token:TOKEN,action:'snapshot'});
  assert.equal(call.options.method,'POST');assert.equal(call.options.credentials,'omit');
  assert.equal(call.options.cache,'no-store');assert.equal(call.options.redirect,'error');
  assert.equal(call.options.referrerPolicy,'no-referrer');
  assert.deepEqual(Object.keys(call.options.headers),['Content-Type']);
  assert.equal(call.options.headers['Content-Type'],'application/json');
  client.destroy();
});

test('only manifest-known IDs can request files and caller paths are ignored',async()=>{
  const h=harness();h.setReply((url,options)=>JSON.parse(options.body).action==='snapshot'?json(fixture()):image());
  const client=h.client();await client.getData();
  const url=await client.fileUrl({id:'file-one',object_path:'../../secret',mime:'text/html'});
  assert.equal(url,'blob:synthetic-1');assert.equal(h.created[0].blob.type,'image/png');
  assert.deepEqual(h.calls[1].body,{token:TOKEN,action:'file',attachmentId:'file-one',thumbnail:false});
  const count=h.calls.length;await assert.rejects(client.fileUrl({id:'../../secret'}));
  assert.equal(h.calls.length,count);client.destroy();
});

test('thumbnails and full images remain distinct, with full-image fallback when no preview exists',async()=>{
  const h=harness();h.setReply((url,options)=>{
    const body=JSON.parse(options.body);
    return body.action==='snapshot'?json(fixture({preview:true})):body.thumbnail?image('image/jpeg',[1,2]):image();
  });
  const client=h.client();const data=await client.getData();const file=data.attachments[0];
  assert.notEqual(await client.thumbnailUrl(file),await client.fileUrl(file));
  assert.equal(h.created[0].blob.type,'image/jpeg');assert.equal(h.created[1].blob.type,'image/png');
  client.destroy();
  const other=harness();other.setReply((url,options)=>JSON.parse(options.body).action==='snapshot'?json(fixture()):image());
  const fallback=other.client();await fallback.getData();
  assert.equal(await fallback.thumbnailUrl({id:'file-one'}),await fallback.fileUrl({id:'file-one'}));
  assert.equal(other.calls.filter(c=>c.body.action==='file').length,1);fallback.destroy();
});

test('unreviewed, cross-patient, orphaned and duplicate attachment snapshots are rejected',async()=>{
  const changes=[
    data=>{data.attachments[0].privacy_checked=false;},
    data=>{data.attachments[0].patient_id='grandma';},
    data=>{data.visits[0].status='draft';},
    data=>{data.visits[0].privacy_reviewed=false;},
    data=>{data.visits[0].source_file_ids=[];},
    data=>{data.attachments.push({...data.attachments[0]});},
    data=>{data.attachments[0].mime='text/html';},
  ];
  for(const change of changes){
    const h=harness(),data=fixture();change(data);h.setReply(()=>json(data));const client=h.client();
    await assert.rejects(client.getData());await assert.rejects(client.fileUrl({id:'file-one'}));
    assert.equal(h.calls.length,1);assert.equal(h.created.length,0);client.destroy();
  }
});

test('403 revocation clears blobs and capability, invokes denial once, and blocks retries',async()=>{
  const h=harness();let deny=false,callbacks=0;
  h.setReply((url,options)=>deny?json({error:'PRIVATE_UPSTREAM_MESSAGE'},403):
    JSON.parse(options.body).action==='snapshot'?json(fixture()):image());
  const client=h.client({onDenied:()=>{callbacks++;}});await client.getData();
  const url=await client.fileUrl({id:'file-one'});deny=true;
  await assert.rejects(client.getData(),error=>!error.message.includes('PRIVATE_UPSTREAM_MESSAGE'));
  assert.equal(callbacks,1);assert.ok(h.revoked.includes(url));const count=h.calls.length;
  await assert.rejects(client.getData());await assert.rejects(client.fileUrl({id:'file-one'}));
  assert.equal(callbacks,1);assert.equal(h.calls.length,count);client.destroy();
});

test('network and 503 failures remain generic and permit retry of a valid capability',async()=>{
  const h=harness();let mode='network',denials=0;
  h.setReply(()=>{if(mode==='network')throw Error('SECRET_TOKEN='+TOKEN);
    return mode==='unavailable'?json({error:'SENSITIVE_SERVER_BODY'},503):json(fixture());});
  const client=h.client({onDenied:()=>{denials++;}});
  await assert.rejects(client.getData(),error=>!error.message.includes(TOKEN));
  mode='unavailable';await assert.rejects(client.getData(),error=>!error.message.includes('SENSITIVE_SERVER_BODY'));
  mode='ready';assert.equal((await client.getData()).visits.length,1);assert.equal(denials,0);client.destroy();
});

test('refresh revokes old blobs and a late old-version image cannot repopulate the cache',async()=>{
  const h=harness();let release,delay=false;
  h.setReply((url,options)=>{
    const body=JSON.parse(options.body);
    if(body.action==='snapshot')return json(fixture({preview:true}));
    if(delay)return new Promise(resolve=>{release=resolve;});
    return image();
  });
  const client=h.client();await client.getData();const oldUrl=await client.fileUrl({id:'file-one'});
  delay=true;const late=client.thumbnailUrl({id:'file-one'});
  await client.getData();assert.ok(h.revoked.includes(oldUrl));
  release(image('image/jpeg',[1,2]));await assert.rejects(late);assert.equal(h.created.length,1);client.destroy();
});

test('destroy prevents delayed snapshots and image responses from reviving a share view',async()=>{
  for(const stage of ['snapshot','file']){
    const h=harness();let release;
    h.setReply((url,options)=>JSON.parse(options.body).action===stage?
      new Promise(resolve=>{release=resolve;}):json(fixture()));
    const client=h.client();if(stage==='file')await client.getData();
    const pending=stage==='snapshot'?client.getData():client.fileUrl({id:'file-one'});
    client.destroy();release(stage==='snapshot'?json(fixture()):image());
    await assert.rejects(pending);assert.equal(h.created.length,0);
  }
});

test('image MIME, exact size and maximum response size are checked before creating blobs',async()=>{
  const responses=[()=>image('text/html'),()=>image('image/png',[1,2]),
    ()=>new Response(new Uint8Array([1,2,3]),{headers:{'Content-Type':'image/png','Content-Length':String(25*1024*1024+1)}})];
  for(const response of responses){
    const h=harness();h.setReply((url,options)=>JSON.parse(options.body).action==='snapshot'?json(fixture()):response());
    const client=h.client();await client.getData();await assert.rejects(client.fileUrl({id:'file-one'}));
    assert.equal(h.created.length,0);client.destroy();
  }
});

test('same-size changed image bytes cannot appear beside an earlier snapshot',async()=>{
  const h=harness();let changed=true,denials=0;
  h.setReply((url,options)=>JSON.parse(options.body).action==='snapshot'?json(fixture()):
    image('image/png',changed?[4,5,6]:[1,2,3]));
  const client=h.client({onDenied:()=>{denials++;}});await client.getData();
  await assert.rejects(client.fileUrl({id:'file-one'}));
  assert.equal(h.created.length,0);assert.equal(denials,0);
  changed=false;assert.equal(await client.fileUrl({id:'file-one'}),'blob:synthetic-1');
  client.destroy();
});

test('concurrent requests for one image share one request and one revocable blob',async()=>{
  const h=harness();let release;
  h.setReply((url,options)=>JSON.parse(options.body).action==='snapshot'?json(fixture()):new Promise(resolve=>{release=resolve;}));
  const client=h.client();await client.getData();
  const first=client.fileUrl({id:'file-one'}),second=client.fileUrl({id:'file-one'});
  release(image());const urls=await Promise.all([first,second]);assert.equal(urls[0],urls[1]);
  assert.equal(h.calls.filter(c=>c.body.action==='file').length,1);assert.equal(h.created.length,1);
  client.destroy();assert.deepEqual(h.revoked,[urls[0]]);
});

function reportFixture(){
  const data=fixture({preview:true});
  data.visits=[];
  data.reports=[{id:'report-one',patient_id:'grandpa',date:'2026-10-26',title:'SYNTHETIC REPORT',
    status:'confirmed',privacy_reviewed:true,reviewed_at:'2026-10-26T00:00:00Z',source_file_ids:['file-one']}];
  Object.assign(data.attachments[0],{mime:'application/pdf',size:4,sha256:checksum([37,80,68,70]),
    object_path:'family/objects/file-one-'+checksum([37,80,68,70])+'.pdf'});
  return data;
}

test('report-only snapshots expose verified PDF blobs and JPEG previews separately',async()=>{
  const h=harness(),data=reportFixture();
  h.setReply((url,options)=>{
    const request=JSON.parse(options.body);
    return request.action==='snapshot'?json(data):request.thumbnail?image('image/jpeg',[1,2]):image('application/pdf',[37,80,68,70]);
  });
  const client=h.client(),loaded=await client.getData();
  assert.equal(loaded.reports[0].id,'report-one');assert.equal(loaded.visits.length,0);
  assert.notEqual(await client.fileUrl({id:'file-one'}),await client.thumbnailUrl({id:'file-one'}));
  assert.deepEqual(h.created.map(item=>item.blob.type),['application/pdf','image/jpeg']);
  assert.ok(h.calls.every(call=>!call.url.includes('/objects/')));
  client.destroy();
});

test('reports require reviewed same-patient sources and PDF safe preview under decimal50MB',async()=>{
  for(const change of [
    data=>{data.reports[0].status='draft';},data=>{data.reports[0].patient_id='grandma';},
    data=>{data.reports[0].privacy_reviewed=false;},data=>{data.reports[0].source_file_ids=[];},
    data=>{data.reports[0].review_notes='UNRESOLVED';},data=>{delete data.attachments[0].thumbnail;},
    data=>{data.attachments[0].size=50000001;},
  ]){
    const h=harness(),data=reportFixture();change(data);h.setReply(()=>json(data));
    const client=h.client();await assert.rejects(client.getData());assert.equal(h.created.length,0);client.destroy();
  }
});

test('PDF checksum mismatch and image body posing as PDF never produce blobs',async()=>{
  for(const response of [()=>image('application/pdf',[37,80,68,71]),()=>image('image/png',[37,80,68,70])]){
    const h=harness();h.setReply((url,options)=>JSON.parse(options.body).action==='snapshot'?json(reportFixture()):response());
    const client=h.client();await client.getData();await assert.rejects(client.fileUrl({id:'file-one'}));
    assert.equal(h.created.length,0);client.destroy();
  }
});

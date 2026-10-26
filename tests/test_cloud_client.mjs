import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createCloudClient} from '../web/cloud.js';

const assetMetadata = {mime:'image/png',size:3,sha256:createHash('sha256').update(new Uint8Array([1,2,3])).digest('hex')};
const memory = new Map();
globalThis.sessionStorage = { getItem: k => memory.get(k) || null, setItem: (k,v) => memory.set(k,v), removeItem: k => memory.delete(k) };
let requests = [];
const response = (value, status=200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
globalThis.fetch = async () => { throw new Error('Network must remain disabled for missing configuration'); };
const closed = createCloudClient({});
assert.equal(closed.configured, false);
await assert.rejects(closed.signIn('nobody@example.invalid', 'test'), /尚未連接/);

const config = { supabaseUrl: 'https://example.supabase.co', supabaseAnonKey: 'sb_publishable_test', archiveId: 'family' };
let permitted = false;
let expireOnce = false;
let refreshCount = 0;
let thumbnailMode = false;
let thumbnailPath = 'family/version1/photo.thumb.jpg';
const session = () => ({ access_token: 'test-access', refresh_token: 'test-refresh', expires_in: 3600, user: { id: 'viewer', email: 'viewer@example.invalid' } });
globalThis.fetch = async (url, options={}) => {
  requests.push({ url, method: options.method || 'GET' });
  if (url.includes('grant_type=refresh_token')) { refreshCount++; return response(session()); }
  if (url.includes('/auth/v1/token')) return response(session());
  if (url.includes('/auth/v1/logout')) return response({});
  if (expireOnce) { expireOnce=false; return response({}, 401); }
  if (url.includes('/family_archives?')) return response(permitted ? [{ id: 'family', current_version: 'version1', published_at: '2026-10-25' }] : []);
  if (url.includes('/family_snapshots?')) return response([{ published_at: '2026-10-25', snapshot: {
    schema_version: 1, patients: [], visits: [], attachments: [{ id:'photo',privacy_checked:true,...assetMetadata,object_path:'family/version1/photo.png',
      ...(thumbnailMode ? { thumbnail: { ...assetMetadata,mime: 'image/jpeg', object_path: thumbnailPath, width: 960, height: 640 } } : {}),
    }],
  } }]);
  if (url.includes('/storage/')) return new Response(new Uint8Array([1,2,3]), { headers: { 'Content-Type': url.endsWith('.thumb.jpg') ? 'image/jpeg' : 'image/png' } });
  throw new Error('Unexpected request');
};
const client = createCloudClient(config);
await assert.rejects(client.signIn('viewer@example.invalid', 'test'), /未獲授權/);
assert.equal(await client.getSession(), null);
assert.equal(memory.size, 0);
permitted=true;
await client.signIn('viewer@example.invalid', 'test');
expireOnce=true;
const data = await client.getData();
assert.equal(data.schema_version, 1);
assert.equal(refreshCount, 1);
assert.equal((await client.fileUrl(data.attachments[0])).startsWith('blob:'), true);
await assert.rejects(client.fileUrl({id:'not-authorized',object_path:'other/photo.png'}), /未獲授權/);
await client.signOut();
assert.equal(await client.getSession(), null);
assert.equal(memory.size, 0);
assert.equal(requests.filter(r => !r.url.includes('/auth/')).every(r => r.method === 'GET'), true);

// A slow refresh must never resurrect a session after the viewer logs out.
const normalFetch = globalThis.fetch;
const refreshing = createCloudClient(config);
await refreshing.signIn('viewer@example.invalid', 'test');
(await refreshing.getSession()).expires_at = 0;
let releaseRefresh;
let refreshStarted;
const startedRefresh = new Promise(resolve => { refreshStarted = resolve; });
globalThis.fetch = async (url, options) => {
  if (url.includes('grant_type=refresh_token')) {
    refreshStarted();
    return new Promise(resolve => { releaseRefresh = () => resolve(response(session())); });
  }
  return normalFetch(url, options);
};
const inFlightRefresh = refreshing.getSession();
await startedRefresh;
await refreshing.signOut();
releaseRefresh();
await assert.rejects(inFlightRefresh, /過期/);
assert.equal(await refreshing.getSession(), null);
assert.equal(memory.size, 0);

// A slow private photo response is discarded after logout, before a Blob URL is made.
globalThis.fetch = normalFetch;
const photos = createCloudClient(config);
await photos.signIn('viewer@example.invalid', 'test');
const photoData = await photos.getData();
let releasePhoto;
let photoStarted;
const startedPhoto = new Promise(resolve => { photoStarted = resolve; });
globalThis.fetch = async (url, options) => {
  if (url.includes('/storage/')) {
    photoStarted();
    return new Promise(resolve => { releasePhoto = () => resolve(new Response(new Uint8Array([1,2,3]), { headers: { 'Content-Type': 'image/png' } })); });
  }
  return normalFetch(url, options);
};
const inFlightPhoto = photos.fileUrl(photoData.attachments[0]);
await startedPhoto;
await photos.signOut();
releasePhoto();
await assert.rejects(inFlightPhoto, /登入狀態已更改/);
assert.equal(await photos.getSession(), null);
assert.equal(memory.size, 0);

// A photo from an older snapshot cannot be cached after a successful data reload.
globalThis.fetch = normalFetch;
const versions = createCloudClient(config);
await versions.signIn('viewer@example.invalid', 'test');
const older = await versions.getData();
let releaseOldPhoto;
let oldPhotoStarted;
const startedOldPhoto = new Promise(resolve => { oldPhotoStarted = resolve; });
globalThis.fetch = async (url, options) => {
  if (url.includes('/storage/')) {
    oldPhotoStarted();
    return new Promise(resolve => { releaseOldPhoto = () => resolve(new Response(new Uint8Array([1,2,3]), { headers: { 'Content-Type': 'image/png' } })); });
  }
  return normalFetch(url, options);
};
const oldPhoto = versions.fileUrl(older.attachments[0]);
await startedOldPhoto;
await versions.getData();
releaseOldPhoto();
await assert.rejects(oldPhoto, /紀錄已更新/);
await versions.signOut();

// Either a slow archive pointer or a slow snapshot must lose to the latest complete load.
for (const delayedStage of ['archive', 'snapshot']) {
  globalThis.fetch = normalFetch;
  const concurrent = createCloudClient(config);
  await concurrent.signIn('viewer@example.invalid', 'test');
  let newest = false, releaseOldLoad, markOldStarted;
  const oldStarted = new Promise(resolve => { markOldStarted = resolve; });
  const fileFor = version => ({id:version, privacy_checked:true, ...assetMetadata,
    object_path:`family/${version}/${version}.png`});
  const snapshotFor = version => [{snapshot:{schema_version:1,patients:[],visits:[],attachments:[fileFor(version)]}}];
  globalThis.fetch = async (url, options) => {
    if (url.includes('/family_archives?')) {
      const value = [{id:'family',current_version:newest ? 'new' : 'old'}];
      if (!newest && delayedStage === 'archive') {
        markOldStarted();
        return new Promise(resolve => { releaseOldLoad = () => resolve(response(value)); });
      }
      return response(value);
    }
    if (url.includes('/family_snapshots?')) {
      const version = url.includes('version_id=eq.old') ? 'old' : 'new';
      if (version === 'old' && delayedStage === 'snapshot') {
        markOldStarted();
        return new Promise(resolve => { releaseOldLoad = () => resolve(response(snapshotFor(version))); });
      }
      return response(snapshotFor(version));
    }
    return normalFetch(url, options);
  };
  const oldLoad = concurrent.getData();
  await oldStarted;
  newest = true;
  const current = await concurrent.getData();
  releaseOldLoad();
  await assert.rejects(oldLoad, /紀錄已更新/);
  await assert.rejects(concurrent.fileUrl(fileFor('old')), /未獲授權/);
  assert.ok((await concurrent.fileUrl(current.attachments[0])).startsWith('blob:'));
  await concurrent.signOut();
}

// Compressed previews and full-size images have independent, authenticated URLs.
globalThis.fetch = normalFetch;
thumbnailMode = true;
const thumbnails = createCloudClient(config);
await thumbnails.signIn('viewer@example.invalid', 'test');
const thumbData = await thumbnails.getData();
requests = [];
const previewUrl = await thumbnails.thumbnailUrl(thumbData.attachments[0]);
const fullUrl = await thumbnails.fileUrl(thumbData.attachments[0]);
assert.notEqual(previewUrl, fullUrl);
assert.equal(await thumbnails.thumbnailUrl(thumbData.attachments[0]), previewUrl);
assert.equal(requests.filter(r => r.url.includes('/storage/')).length, 2);
assert.ok(requests.some(r => r.url.endsWith('/photo.thumb.jpg')));
assert.ok(requests.some(r => r.url.endsWith('/photo.png')));
thumbnailPath = 'other/version1/private.thumb.jpg';
const unsafeMetadata = await thumbnails.getData();
requests = [];
await thumbnails.thumbnailUrl(unsafeMetadata.attachments[0]);
assert.equal(requests.some(r => r.url.includes('/other/')), false);
assert.equal(requests.some(r => r.url.endsWith('/photo.png')), true);
await thumbnails.signOut();

// The exact empty bootstrap SQL payload must load as a valid, empty family archive.
const bootstrapSql = await readFile(new URL('../supabase/002_bootstrap_empty.sql', import.meta.url), 'utf8');
const bootstrapMatch = bootstrapSql.match(/'(\{\s*"schema_version"[\s\S]*?\})'::jsonb/);
assert.ok(bootstrapMatch, 'Bootstrap must contain a static empty schema payload');
const emptySnapshot = JSON.parse(bootstrapMatch[1]);
globalThis.fetch = async (url, options) => {
  if (url.includes('/family_snapshots?')) return response([{ published_at: '2026-10-25', snapshot: emptySnapshot }]);
  return normalFetch(url, options);
};
const emptyArchive = createCloudClient(config);
await emptyArchive.signIn('viewer@example.invalid', 'test');
requests = [];
const emptyData = await emptyArchive.getData();
assert.deepEqual(emptyData.patients, [
  {id:'grandpa',name:'爺爺',age:null,sex:'unknown'},
  {id:'grandma',name:'嬷嬷',age:null,sex:'unknown'},
]);
assert.deepEqual(emptyData.visits, []);
assert.deepEqual(emptyData.attachments, []);
assert.equal(requests.some(r => r.url.includes('/storage/')), false);
await assert.rejects(emptyArchive.fileUrl({id:'absent'}), /未獲授權/);
await emptyArchive.signOut();

// The optional login reader keeps legacy paths and also opens current-manifest CAS PDFs.
const pdfBytes=new Uint8Array([37,80,68,70]);
const pdfHash=createHash('sha256').update(pdfBytes).digest('hex');
const pdfFile={id:'pdf',patient_id:'grandpa',privacy_checked:true,mime:'application/pdf',size:4,sha256:pdfHash,
  object_path:`family/objects/pdf-${pdfHash}.pdf`,thumbnail:{...assetMetadata,mime:'image/jpeg',
    object_path:`family/objects/pdf-${assetMetadata.sha256}.thumb.jpg`,width:120,height:80}};
let badPdf=false;
globalThis.fetch=async(url,options)=>{
  if(url.includes('/family_snapshots?'))return response([{snapshot:{schema_version:1,patients:[],visits:[],
    reports:[{id:'report1'}],attachments:[pdfFile]}}]);
  if(url.includes('/storage/'))return new Response(url.endsWith('.pdf')?(badPdf?new Uint8Array([37,80,68,71]):pdfBytes):new Uint8Array([1,2,3]),
    {headers:{'Content-Type':url.endsWith('.pdf')?'application/pdf':'image/jpeg'}});
  return normalFetch(url,options);
};
const pdfClient=createCloudClient(config);await pdfClient.signIn('viewer@example.invalid','test');
const pdfData=await pdfClient.getData();assert.equal(pdfData.reports[0].id,'report1');
assert.notEqual(await pdfClient.fileUrl(pdfFile),await pdfClient.thumbnailUrl(pdfFile));
await pdfClient.getData();badPdf=true;
await assert.rejects(pdfClient.fileUrl(pdfFile),/內容已更新/);
await pdfClient.signOut();

// Reject oversized PDF declarations before reading; stop an overlong stream at its first excess chunk.
const pdfFetch = globalThis.fetch;
let streamMode = 'declared', cancelled = false, pulls = 0, blobsCreated = 0;
const realCreateObjectURL = URL.createObjectURL;
URL.createObjectURL = value => { blobsCreated++; return realCreateObjectURL(value); };
globalThis.fetch = async (url, options) => {
  if (!url.includes('/storage/')) return pdfFetch(url, options);
  const bytes = streamMode === 'short' ? new Uint8Array([37,80]) : new Uint8Array([37,80,68,70,0]);
  const body = new ReadableStream({
    pull(controller) { pulls++; controller.enqueue(bytes); if (streamMode === 'short') controller.close(); },
    cancel() { cancelled = true; },
  }, {highWaterMark:0});
  return new Response(body, {headers:{'Content-Type':'application/pdf',
    ...(streamMode === 'declared' ? {'Content-Length':'50000001'} : {})}});
};
const boundedClient = createCloudClient(config);
await boundedClient.signIn('viewer@example.invalid','test');
try {
  for (const mode of ['declared','stream','short']) {
    streamMode = mode; cancelled = false; pulls = 0;
    await boundedClient.getData();
    await assert.rejects(boundedClient.fileUrl(pdfFile), /檔案格式或大小不符/);
    assert.equal(blobsCreated, 0);
    if (mode !== 'short') assert.equal(cancelled, true, `${mode} response must be cancelled`);
    assert.equal(pulls, mode === 'declared' ? 0 : 1);
  }
} finally {
  URL.createObjectURL = realCreateObjectURL;
  await boundedClient.signOut();
}
console.log('Cloud client: auth/privacy/read-only races, concurrent loads, private previews, CAS PDF checksums, and bounded streams passed.');

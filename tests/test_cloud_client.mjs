import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../web/cloud.js', import.meta.url), 'utf8');
const { createCloudClient } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
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
    schema_version: 1, patients: [], visits: [], attachments: [{ id:'photo',privacy_checked:true,object_path:'family/version1/photo.png',
      ...(thumbnailMode ? { thumbnail: { mime: 'image/jpeg', object_path: thumbnailPath, width: 960, height: 640 } } : {}),
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
console.log('Cloud client: auth/privacy/read-only/race checks and private compressed-preview/original separation passed.');

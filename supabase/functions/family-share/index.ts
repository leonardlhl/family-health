// @ts-nocheck
// Self-contained, dependency-free Deno Edge Function. Also valid JavaScript for
// offline tests. Never log requests, tokens, credentials, or backend error bodies.
const ORIGINS = new Set([
  'https://leonardlhl.github.io',
  'http://127.0.0.1:8767',
  'http://localhost:8767',
]);
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const ID = /^[A-Za-z0-9_-]{1,96}$/;
const ARCHIVE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const HASH = /^[a-f0-9]{64}$/;
const ALIASES = {grandpa:'爺爺',grandma:'嬷嬷'};
const EXTENSIONS = {'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','application/pdf':'.pdf'};
const DENIED = 'This private link is unavailable.';
const MAX_BODY = 4096;
const MAX_SNAPSHOT = 16 * 1024 * 1024;
const MAX_IMAGE = 25 * 1024 * 1024;
const MAX_PDF = 50000000;
const MAX_PREVIEW = 3 * 1024 * 1024;

class Denied extends Error {}
class Unavailable extends Error {}
class InvalidFeedback extends Error {}
const requireAccess = condition => { if (!condition) throw new Denied(); };

function publicFeedback(value) {
  if (!value || !UUID.test(value.id) || !Object.hasOwn(ALIASES,value.patient_id)
    || !ID.test(value.visit_id) || !ID.test(value.medicine_id)
    || !['effective','ineffective','undecided'].includes(value.rating)
    || typeof value.notes !== 'string' || [...value.notes].length > 500
    || !Number.isInteger(value.revision) || value.revision < 1
    || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))) throw new Unavailable();
  return Object.fromEntries(['id','patient_id','visit_id','medicine_id','rating','notes','revision','created_at']
    .map(key => [key,value[key]]));
}

async function boundedBytes(value, limit) {
  const length = value.headers.get('content-length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) throw new Denied();
  if (!value.body) return new Uint8Array();
  const reader = value.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      size += result.value.byteLength;
      if (size > limit) { await reader.cancel(); throw new Denied(); }
      chunks.push(result.value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

function validAsset(asset, expectedPaths, preview = false) {
  return asset && expectedPaths.includes(asset.object_path) && HASH.test(asset.sha256)
    && Number.isInteger(asset.size) && asset.size > 0
    && asset.size <= (preview ? MAX_PREVIEW : asset.mime === 'application/pdf' ? MAX_PDF : MAX_IMAGE)
    && (preview ? asset.mime === 'image/jpeg' : Object.hasOwn(EXTENSIONS, asset.mime));
}

function checkedSnapshot(value, archiveId, version) {
  requireAccess(value?.schema_version === 1 && Array.isArray(value.patients)
    && Array.isArray(value.visits) && Array.isArray(value.attachments)
    && (value.reports === undefined || Array.isArray(value.reports)));
  const patients = new Set();
  for (const patient of value.patients) {
    requireAccess(Object.hasOwn(ALIASES, patient?.id) && !patients.has(patient.id)
      && patient.name === ALIASES[patient.id]);
    patients.add(patient.id);
  }
  const files = new Map();
  for (const file of value.attachments) {
    requireAccess(file && typeof file.id === 'string' && ID.test(file.id) && !files.has(file.id) && patients.has(file.patient_id)
      && file.privacy_checked === true && Object.hasOwn(EXTENSIONS, file.mime));
    requireAccess(validAsset(file, [`${archiveId}/${version}/${file.id}${EXTENSIONS[file.mime]}`,
      `${archiveId}/objects/${file.id}-${file.sha256}${EXTENSIONS[file.mime]}`]));
    if (file.mime === 'application/pdf') requireAccess(file.thumbnail !== undefined);
    if (file.thumbnail !== undefined) {
      requireAccess(validAsset(file.thumbnail, [`${archiveId}/${version}/${file.id}.thumb.jpg`,
        `${archiveId}/objects/${file.id}-${file.thumbnail?.sha256}.thumb.jpg`], true)
        && ['width','height'].every(key => Number.isInteger(file.thumbnail[key])
          && file.thumbnail[key] >= 1 && file.thumbnail[key] <= 960));
    }
    files.set(file.id, file);
  }
  const linked = new Set();
  const checkSources = (sources, patientId) => {
    requireAccess(Array.isArray(sources));
    for (const id of sources) {
      requireAccess(files.has(id) && files.get(id).patient_id === patientId);
      linked.add(id);
    }
  };
  const visits = new Set();
  for (const visit of value.visits) {
    requireAccess(visit && typeof visit.id === 'string' && ID.test(visit.id) && !visits.has(visit.id) && patients.has(visit.patient_id)
      && visit.status === 'confirmed' && visit.privacy_reviewed === true && Array.isArray(visit.medicines));
    visits.add(visit.id);
    for (const sources of [visit.source_file_ids, ...visit.medicines.map(medicine => medicine?.source_file_ids)]) {
      checkSources(sources, visit.patient_id);
    }
  }
  const reports = new Set();
  for (const report of value.reports || []) {
    requireAccess(report && typeof report.id === 'string' && ID.test(report.id) && !reports.has(report.id)
      && patients.has(report.patient_id) && report.status === 'confirmed' && report.privacy_reviewed === true
      && typeof report.reviewed_at === 'string' && !!report.reviewed_at
      && typeof report.title === 'string' && !!report.title.trim()
      && typeof report.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(report.date)
      && Array.isArray(report.source_file_ids) && report.source_file_ids.length > 0 && !report.review_notes);
    reports.add(report.id);
    checkSources(report.source_file_ids, report.patient_id);
  }
  requireAccess(linked.size === files.size);
  return {...value,reports:value.reports || []};
}

export function createHandler(options = {}) {
  const getEnv = options.getEnv || (name => globalThis.Deno?.env.get(name));
  const send = options.fetch || globalThis.fetch;
  const cryptography = options.crypto || globalThis.crypto;
  const now = options.now || (() => Date.now());

  function response(body, status, origin, extra = {}) {
    return new Response(body, {status, headers:{
      'Cache-Control':'no-store, max-age=0', 'Pragma':'no-cache', 'Expires':'0',
      'Vary':'Origin', 'X-Content-Type-Options':'nosniff',
      ...(ORIGINS.has(origin) ? {'Access-Control-Allow-Origin':origin} : {}), ...extra,
    }});
  }
  const jsonResponse = (value, status, origin) => response(JSON.stringify(value), status, origin,
    {'Content-Type':'application/json; charset=utf-8'});

  return async function handle(request) {
    const origin = request.headers.get('origin');
    if (!ORIGINS.has(origin)) return jsonResponse({error:DENIED}, 403, origin);
    if (request.method === 'OPTIONS') {
      const method = request.headers.get('access-control-request-method');
      const headers = (request.headers.get('access-control-request-headers') || '')
        .split(',').map(value => value.trim().toLowerCase()).filter(Boolean);
      if (method !== 'POST' || headers.some(value => !['content-type','authorization'].includes(value))) return jsonResponse({error:DENIED}, 403, origin);
      return response(null, 204, origin, {'Access-Control-Allow-Methods':'POST, OPTIONS',
        'Access-Control-Allow-Headers':'content-type, authorization'});
    }
    if (request.method !== 'POST') return jsonResponse({error:DENIED}, 405, origin);
    try {
      requireAccess(request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() === 'application/json');
      requireAccess(!new URL(request.url).search);
      let body;
      try { body = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(await boundedBytes(request, MAX_BODY))); }
      catch { throw new Denied(); }
      requireAccess(body && !Array.isArray(body) && typeof body === 'object'
        && ['snapshot','file','feedback-list','feedback-save'].includes(body.action));
      const feedback = body.action.startsWith('feedback-');
      const authorization = request.headers.get('authorization');
      const hasToken = body.token !== undefined;
      requireAccess(hasToken ? typeof body.token === 'string' && /^[A-Za-z0-9_-]{43}$/.test(body.token)
        && !authorization : feedback && typeof authorization === 'string'
        && authorization.length <= 8192 && /^Bearer [A-Za-z0-9._-]+$/i.test(authorization));
      const feedbackFields = ['patient_id','visit_id','medicine_id','rating','notes','expected_revision','request_id','snapshot_version'];
      const permittedKeys = ['token','action',...(body.action === 'file' ? ['attachmentId','thumbnail']
        : body.action === 'feedback-save' ? feedbackFields : [])];
      if (!Object.keys(body).every(key => permittedKeys.includes(key))) {
        if (feedback) throw new InvalidFeedback();
        throw new Denied();
      }
      if (body.action === 'feedback-save' && (!Object.hasOwn(ALIASES,body.patient_id)
        || typeof body.visit_id !== 'string' || !ID.test(body.visit_id)
        || typeof body.medicine_id !== 'string' || !ID.test(body.medicine_id)
        || !['effective','ineffective','undecided'].includes(body.rating)
        || typeof body.notes !== 'string' || [...body.notes].length > 500
        || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(body.notes)
        || !Number.isInteger(body.expected_revision) || body.expected_revision < 0 || body.expected_revision >= 2147483647
        || typeof body.request_id !== 'string' || !UUID.test(body.request_id)
        || typeof body.snapshot_version !== 'string' || !UUID.test(body.snapshot_version))) throw new InvalidFeedback();
      if (body.action === 'file') requireAccess(typeof body.attachmentId === 'string' && ID.test(body.attachmentId)
        && (body.thumbnail === undefined || typeof body.thumbnail === 'boolean'));

      const base = String(getEnv('SUPABASE_URL') || '').replace(/\/$/, '');
      const secret = getEnv('SUPABASE_SERVICE_ROLE_KEY') || getEnv('FAMILY_SHARE_SECRET_KEY') || '';
      if (!/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(base) || typeof secret !== 'string' || !secret) throw new Unavailable();
      const headers = {'apikey':secret};
      if (!secret.startsWith('sb_secret_')) {
        try {
          const part = secret.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
          if (JSON.parse(atob(part)).role !== 'service_role') throw new Error();
        } catch { throw new Unavailable(); }
        headers.Authorization = `Bearer ${secret}`;
      }
      const digest = async bytes => Array.from(new Uint8Array(await cryptography.subtle.digest('SHA-256', bytes)))
        .map(byte => byte.toString(16).padStart(2,'0')).join('');
      const tokenHash = hasToken ? await digest(new TextEncoder().encode(body.token)) : null;
      if (feedback) {
        let userId = null;
        if (!hasToken) {
          let auth;
          try { auth = await send(base + '/auth/v1/user', {method:'GET',headers:{apikey:secret,Authorization:authorization},
            cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(15000)}); }
          catch { throw new Unavailable(); }
          if ([401,403].includes(auth.status)) throw new Denied();
          if (!auth.ok) throw new Unavailable();
          let user;
          try { user = JSON.parse(new TextDecoder().decode(await boundedBytes(auth,65536))); }
          catch { throw new Unavailable(); }
          requireAccess(typeof user.id === 'string' && UUID.test(user.id) && user.is_anonymous !== true);
          userId = user.id;
        }
        const payload = {p_action:body.action === 'feedback-list' ? 'list' : 'save',
          p_token_hash:tokenHash,p_user_id:userId,p_archive_id:'family'};
        if (body.action === 'feedback-save') for (const field of feedbackFields) payload[`p_${field}`] = body[field];
        let result;
        try { result = await send(base + '/rest/v1/rpc/family_medicine_feedback_access', {
          method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify(payload),
          cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(15000)}); }
        catch { throw new Unavailable(); }
        if (!result.ok) throw new Unavailable();
        let value;
        try { value = JSON.parse(new TextDecoder().decode(await boundedBytes(result,MAX_SNAPSHOT))); }
        catch { throw new Unavailable(); }
        if (value?.status === 'denied') throw new Denied();
        if (value?.status === 'invalid') throw new InvalidFeedback();
        if (value?.status === 'rate_limited') return jsonResponse({error:'feedback_rate_limited'},429,origin);
        if (value?.status === 'stale') return jsonResponse({error:'feedback_stale',item:null},409,origin);
        if (value?.status === 'conflict') return jsonResponse({error:'feedback_conflict',
          item:value.item === null ? null : publicFeedback(value.item)},409,origin);
        if (value?.status !== 'ok') throw new Unavailable();
        if (body.action === 'feedback-list') {
          if (!Array.isArray(value.items)) throw new Unavailable();
          return jsonResponse({items:value.items.map(publicFeedback)},200,origin);
        }
        return jsonResponse({item:publicFeedback(value.item)},200,origin);
      }
      const upstream = async path => {
        let result;
        try { result = await send(base + path, {method:'GET', headers, cache:'no-store',
          credentials:'omit', redirect:'error', signal:AbortSignal.timeout(15000)}); }
        catch { throw new Unavailable(); }
        if (!result.ok) throw new Unavailable();
        return result;
      };
      const rows = async (table, filters, limit = MAX_SNAPSHOT) => {
        const result = await upstream(`/rest/v1/${table}?${new URLSearchParams({...filters,limit:'2'})}`);
        try {
          const value = JSON.parse(new TextDecoder().decode(await boundedBytes(result, limit)));
          if (!Array.isArray(value)) throw new Error();
          return value;
        } catch { throw new Unavailable(); }
      };
      const activeShare = async () => {
        const links = await rows('family_share_links', {select:'id,archive_id,revoked_at,expires_at',token_hash:`eq.${tokenHash}`}, 4096);
        requireAccess(links.length === 1);
        const link = links[0];
        requireAccess(typeof link.id === 'string' && UUID.test(link.id)
          && typeof link.archive_id === 'string' && ARCHIVE.test(link.archive_id) && link.revoked_at === null
          && (link.expires_at === null || (typeof link.expires_at === 'string'
            && Number.isFinite(Date.parse(link.expires_at)) && Date.parse(link.expires_at) > now())));
        return link;
      };
      const share = await activeShare();
      const readArchive = async () => {
        const archives = await rows('family_archives', {select:'id,current_version,published_at',id:`eq.${share.archive_id}`}, 4096);
        requireAccess(archives.length === 1 && archives[0].id === share.archive_id
          && (archives[0].current_version === null || UUID.test(archives[0].current_version)));
        return archives[0];
      };
      const archive = await readArchive();
      let snapshot = {schema_version:1,patients:[],visits:[],reports:[],attachments:[]};
      let publishedAt = archive.published_at;
      if (archive.current_version !== null) {
        const versions = await rows('family_snapshots', {select:'archive_id,version_id,snapshot,published_at',
          archive_id:`eq.${share.archive_id}`,version_id:`eq.${archive.current_version}`});
        requireAccess(versions.length === 1 && versions[0].archive_id === share.archive_id
          && versions[0].version_id === archive.current_version);
        snapshot = checkedSnapshot(versions[0].snapshot, share.archive_id, archive.current_version);
        publishedAt = versions[0].published_at || publishedAt;
      }
      const recheck = async () => {
        const currentShare = await activeShare();
        requireAccess(currentShare.id === share.id && currentShare.archive_id === share.archive_id);
        // A normal publication is retryable; it must not invalidate a valid link.
        if ((await readArchive()).current_version !== archive.current_version) throw new Unavailable();
      };
      if (body.action === 'snapshot') {
        await recheck();
        return jsonResponse({...snapshot,published_at:publishedAt || null,snapshot_version:archive.current_version}, 200, origin);
      }
      const file = snapshot.attachments.find(item => item.id === body.attachmentId);
      // A previously displayed attachment can disappear after publication.
      // Its absence does not revoke an otherwise valid capability.
      if (!file) throw new Unavailable();
      requireAccess(file.privacy_checked === true);
      const usePreview = body.thumbnail === true && !!file.thumbnail;
      const asset = usePreview ? file.thumbnail : file;
      const manifestId = file.id + (usePreview ? ':thumbnail' : '');
      const manifests = await rows('family_media', {select:'archive_id,version_id,attachment_id,object_path',
        archive_id:`eq.${share.archive_id}`,version_id:`eq.${archive.current_version}`,attachment_id:`eq.${manifestId}`}, 4096);
      requireAccess(manifests.length === 1 && manifests[0].archive_id === share.archive_id
        && manifests[0].version_id === archive.current_version && manifests[0].attachment_id === manifestId
        && manifests[0].object_path === asset.object_path);
      const image = await upstream('/storage/v1/object/authenticated/family-health/'
        + asset.object_path.split('/').map(encodeURIComponent).join('/'));
      requireAccess(image.headers.get('content-type')?.split(';')[0].trim().toLowerCase() === asset.mime);
      const bytes = await boundedBytes(image, usePreview ? MAX_PREVIEW : asset.mime === 'application/pdf' ? MAX_PDF : MAX_IMAGE);
      requireAccess(bytes.byteLength === asset.size && await digest(bytes) === asset.sha256);
      await recheck();
      return response(bytes, 200, origin, {'Content-Type':asset.mime,
        'Content-Disposition':`inline; filename="image${EXTENSIONS[asset.mime]}"`});
    } catch (error) {
      if (error instanceof InvalidFeedback) return jsonResponse({error:'feedback_invalid'},400,origin);
      return jsonResponse({error:error instanceof Denied ? DENIED : 'Private records are temporarily unavailable.'},
        error instanceof Denied ? 403 : 503, origin);
    }
  };
}

if (globalThis.Deno?.serve) globalThis.Deno.serve(createHandler());

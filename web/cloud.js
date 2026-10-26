const FEEDBACK_UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const FEEDBACK_ID = /^[A-Za-z0-9_-]{1,96}$/;
function feedbackError(status, code, item = null) {
  const message = code === 'feedback_conflict' ? '其他家人已更新回饋，請先查看最新內容。'
    : code === 'feedback_stale' ? '藥單已更新，請重新整理後再填回饋。'
    : code === 'feedback_invalid' ? '請選擇效果，備註最多 500 字。'
    : code === 'feedback_rate_limited' ? '回饋提交得太密，請等一分鐘再試。'
    : '暫時未能讀取或儲存回饋，請稍後再試。';
  const error = new Error(message);
  error.status = status; error.code = code; error.item = item;
  return error;
}
function feedbackPayload(value, snapshotVersion) {
  const fields = ['patient_id','visit_id','medicine_id','rating','notes','expected_revision','request_id'];
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || !Object.keys(value).every(key => fields.includes(key))
    || !['grandpa','grandma'].includes(value.patient_id)
    || typeof value.visit_id !== 'string' || !FEEDBACK_ID.test(value.visit_id)
    || typeof value.medicine_id !== 'string' || !FEEDBACK_ID.test(value.medicine_id)
    || !['effective','ineffective','undecided'].includes(value.rating)
    || typeof value.notes !== 'string' || [...value.notes].length > 500
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value.notes)
    || !Number.isInteger(value.expected_revision) || value.expected_revision < 0 || value.expected_revision >= 2147483647
    || typeof value.request_id !== 'string' || !FEEDBACK_UUID.test(value.request_id)) throw feedbackError(400,'feedback_invalid');
  if (typeof snapshotVersion !== 'string' || !FEEDBACK_UUID.test(snapshotVersion)) throw feedbackError(409,'feedback_stale');
  return {...Object.fromEntries(fields.map(key => [key,value[key]])),snapshot_version:snapshotVersion};
}
function feedbackItem(value) {
  if (!value || typeof value.id !== 'string' || !FEEDBACK_UUID.test(value.id)
    || !['grandpa','grandma'].includes(value.patient_id)
    || typeof value.visit_id !== 'string' || !FEEDBACK_ID.test(value.visit_id)
    || typeof value.medicine_id !== 'string' || !FEEDBACK_ID.test(value.medicine_id)
    || !['effective','ineffective','undecided'].includes(value.rating)
    || typeof value.notes !== 'string' || [...value.notes].length > 500
    || !Number.isInteger(value.revision) || value.revision < 1
    || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))) throw feedbackError(null,'feedback_unavailable');
  return Object.fromEntries(['id','patient_id','visit_id','medicine_id','rating','notes','revision','created_at'].map(key => [key,value[key]]));
}
function rememberedFeedback(value, snapshotVersion, requests) {
  const previous = requests.get(value?.request_id?.toLowerCase?.());
  const payload = feedbackPayload(value,previous?.snapshot_version || snapshotVersion);
  payload.request_id = payload.request_id.toLowerCase();
  if (previous && JSON.stringify(previous) !== JSON.stringify(payload)) throw feedbackError(400,'feedback_invalid');
  if (!previous) requests.set(payload.request_id,payload);
  return payload;
}
async function feedbackResponse(response, requireCurrent, action) {
  const limit = 16 * 1024 * 1024;
  const declared = response.headers.get('content-length');
  if (!response.body || (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase() !== 'application/json'
    || (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > limit))) throw feedbackError(null,'feedback_unavailable');
  const reader = response.body.getReader(), chunks = []; let size = 0;
  try {
    while (true) {
      const result = await reader.read(); requireCurrent();
      if (result.done) break;
      size += result.value.byteLength;
      if (size > limit) throw feedbackError(null,'feedback_unavailable');
      chunks.push(result.value);
    }
  } catch (error) { try { await reader.cancel(); } catch {} throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.byteLength; }
  let value; try { value = JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)); }
  catch { throw feedbackError(null,'feedback_unavailable'); }
  requireCurrent();
  if (!response.ok) {
    const code = response.status === 409 && ['feedback_conflict','feedback_stale'].includes(value?.error) ? value.error
      : response.status === 400 ? 'feedback_invalid'
      : response.status === 429 ? 'feedback_rate_limited' : 'feedback_unavailable';
    throw feedbackError(response.status,code,code === 'feedback_conflict' && value.item ? feedbackItem(value.item) : null);
  }
  if (action === 'feedback-list' && Array.isArray(value?.items)) return {items:value.items.map(feedbackItem)};
  if (action === 'feedback-save' && value?.item) return {item:feedbackItem(value.item)};
  throw feedbackError(null,'feedback_unavailable');
}

/** Clinical data is read-only; separate family observations use scoped feedback actions. */
export function createCloudClient(config = {}) {
  const base = String(config.supabaseUrl || '').replace(/\/$/, '');
  const key = String(config.supabaseAnonKey || '');
  const archiveId = String(config.archiveId || 'family');
  const configured = /^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(base)
    && /^(?:eyJ|sb_publishable_)/.test(key) && !key.startsWith('sb_secret_')
    && /^[a-z0-9][a-z0-9_-]{0,63}$/.test(archiveId);
  if (key.startsWith('eyJ')) {
    try {
      const part = key.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      if (JSON.parse(atob(part)).role !== 'anon') throw new Error('not-public');
    } catch { throw new Error('網站設定錯誤：只能使用公開的 anon / publishable key。'); }
  }
  const storageKey = `family-health-session:${base}:${archiveId}`;
  const listeners = new Set();
  const blobUrls = new Map();
  let session = null;
  let refreshPromise = null;
  let authEpoch = 0;
  let fileEpoch = 0;
  let availableFiles = new Map();
  let snapshotVersion = null;
  const feedbackRequests = new Map();
  try { session = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { /* storage unavailable */ }
  function ensureConfigured() {
    if (!configured) throw new Error('家人網站尚未連接私人資料庫，請聯絡管理人。');
  }
  function requireEpoch(epoch) {
    if (epoch !== authEpoch) throw new Error('登入狀態已更改，請重新開啟紀錄。');
  }
  function clearFiles() {
    ++fileEpoch;
    for (const url of blobUrls.values()) URL.revokeObjectURL(url);
    blobUrls.clear(); availableFiles.clear();
    snapshotVersion = null;
  }
  function saveSession(value) {
    session = value;
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { /* memory-only sessions still work */ }
    if (!value) { clearFiles(); feedbackRequests.clear(); }
    for (const fn of listeners) { try { fn(value); } catch { /* one listener cannot break auth */ } }
  }
  function normalizedSession(value) {
    if (!value?.access_token || !value?.refresh_token) throw new Error('登入回應不完整，請再試一次。');
    return {
      access_token: value.access_token,
      refresh_token: value.refresh_token,
      expires_at: Number(value.expires_at || (Date.now() / 1000 + Number(value.expires_in || 3600))),
      user: { id: value.user?.id, email: value.user?.email },
    };
  }
  async function authRequest(path, body, accessToken) {
    ensureConfigured();
    const response = await fetch(base + path, {
      method: 'POST', cache: 'no-store', credentials: 'omit',
      headers: { apikey: key, 'Content-Type': 'application/json', ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
      body: JSON.stringify(body),
    });
    let value = {}; try { value = await response.json(); } catch { /* logout can return an empty body */ }
    if (!response.ok) {
      if (response.status === 429) throw new Error('登入次數太多，請稍後再試。');
      throw new Error('登入未成功，請檢查電郵及密碼，或聯絡管理人。');
    }
    return value;
  }
  async function currentSession() {
    if (!session) return null;
    ensureConfigured();
    if (session.expires_at > Date.now() / 1000 + 60) return session;
    if (!refreshPromise) {
      const epoch = authEpoch;
      const refreshToken = session.refresh_token;
      refreshPromise = (async () => {
        try {
          const value = await authRequest('/auth/v1/token?grant_type=refresh_token', { refresh_token: refreshToken });
          requireEpoch(epoch);
          saveSession(normalizedSession(value)); return session;
        } catch (error) { if (epoch === authEpoch) saveSession(null); throw new Error('登入已過期，請重新登入。'); }
        finally { refreshPromise = null; }
      })();
    }
    return refreshPromise;
  }
  async function request(path, options = {}, retry = true) {
    ensureConfigured();
    const epoch = authEpoch;
    const active = await currentSession();
    requireEpoch(epoch);
    if (!active) throw new Error('請先登入，才可查看家庭紀錄。');
    const response = await fetch(base + path, {
      ...options, cache: 'no-store', credentials: 'omit',
      headers: { apikey: key, Authorization: `Bearer ${active.access_token}`, ...(options.headers || {}) },
    });
    requireEpoch(epoch);
    if (response.status === 401 && retry) {
      session.expires_at = 0; await currentSession(); return request(path, options, false);
    }
    if (!response.ok) {
      if ([401, 403].includes(response.status)) throw new Error('你未獲授權查看呢份紀錄，請聯絡管理人。');
      throw new Error('暫時未能讀取紀錄，請檢查網絡後再試。');
    }
    return response;
  }
  async function getArchive() {
    const epoch = authEpoch;
    const query = new URLSearchParams({ select: 'id,current_version,published_at', id: `eq.${archiveId}` });
    const rows = await (await request(`/rest/v1/family_archives?${query}`)).json();
    requireEpoch(epoch);
    if (!Array.isArray(rows) || rows.length !== 1) throw new Error('你未獲授權查看呢份紀錄，請聯絡管理人。');
    return rows[0];
  }
  async function imageUrl(attachment, thumbnail = false) {
    const epoch = authEpoch;
    const sourceEpoch = fileEpoch;
    const file = availableFiles.get(attachment?.id);
    if (!file) throw new Error('相片未獲授權或未完成私隱檢查。');
    const preview = thumbnail && file.thumbnail;
    const asset = preview || file;
    const cacheKey = `${file.id}:${preview ? 'thumbnail' : 'full'}`;
    if (blobUrls.has(cacheKey)) return blobUrls.get(cacheKey);
    const objectPath = asset.object_path.split('/').map(encodeURIComponent).join('/');
    const response = await request(`/storage/v1/object/authenticated/family-health/${objectPath}`);
    const mime = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    const maximum = preview ? 3 * 1024 * 1024 : mime === 'application/pdf' ? 50000000 : 25 * 1024 * 1024;
    const declared = response.headers.get('content-length');
    if (!['image/png', 'image/jpeg', 'image/webp', 'application/pdf'].includes(mime)
      || mime !== asset.mime || (preview && mime !== 'image/jpeg')
      || !Number.isInteger(asset.size) || asset.size < 1 || asset.size > maximum
      || (declared !== null && (!/^\d+$/.test(declared) || Number(declared) !== asset.size))
      || !response.body) {
      try { await response.body?.cancel(); } catch { /* Invalid content must not continue downloading. */ }
      throw new Error('檔案格式或大小不符。');
    }
    const reader = response.body.getReader(), chunks = [];
    let size = 0;
    try {
      while (true) {
        const part = await reader.read();
        requireEpoch(epoch);
        if (sourceEpoch !== fileEpoch) throw new Error('紀錄已更新，請重新開啟相片。');
        if (part.done) break;
        size += part.value.byteLength;
        if (size > asset.size || size > maximum) throw new Error('檔案格式或大小不符。');
        chunks.push(part.value);
      }
    } catch (error) {
      try { await reader.cancel(); } catch { /* Preserve the original failure. */ }
      throw error;
    } finally { reader.releaseLock(); }
    if (size !== asset.size) throw new Error('檔案格式或大小不符。');
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const checksum = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
      .map(value => value.toString(16).padStart(2, '0')).join('');
    if (checksum !== asset.sha256) throw new Error('檔案內容已更新，請重新整理。');
    requireEpoch(epoch);
    if (sourceEpoch !== fileEpoch) throw new Error('紀錄已更新，請重新開啟相片。');
    const url = URL.createObjectURL(new Blob([bytes], {type:mime})); blobUrls.set(cacheKey, url); return url;
  }
  async function feedbackRequest(action, value) {
    const epoch = authEpoch, generation = fileEpoch;
    const payload = {action,...(action === 'feedback-save' ? rememberedFeedback(value,snapshotVersion,feedbackRequests) : {})};
    const check = () => {
      requireEpoch(epoch);
      if (generation !== fileEpoch) throw feedbackError(null,'feedback_unavailable');
    };
    const active = await currentSession(); check();
    if (!active) throw feedbackError(403,'feedback_unavailable');
    let response;
    try { response = await fetch(base + '/functions/v1/family-share', {
      method:'POST',cache:'no-store',credentials:'omit',redirect:'error',referrerPolicy:'no-referrer',
      headers:{'Content-Type':'application/json',Authorization:`Bearer ${active.access_token}`},body:JSON.stringify(payload),
    }); } catch { throw feedbackError(null,'feedback_unavailable'); }
    check();
    if ([401,403].includes(response.status)) {
      ++authEpoch; refreshPromise = null; saveSession(null);
      throw feedbackError(response.status,'feedback_denied');
    }
    return feedbackResponse(response,check,action);
  }
  return {
    configured,
    async signIn(email, password) {
      const epoch = ++authEpoch;
      refreshPromise = null;
      saveSession(null);
      const result = await authRequest('/auth/v1/token?grant_type=password', { email: String(email).trim(), password: String(password) });
      requireEpoch(epoch);
      saveSession(normalizedSession(result));
      try { await getArchive(); } catch (error) { if (epoch === authEpoch) saveSession(null); throw error; }
      return session;
    },
    async signOut() {
      ++authEpoch;
      refreshPromise = null;
      const token = session?.access_token;
      saveSession(null);
      if (token && configured) {
        try { await authRequest('/auth/v1/logout?scope=local', {}, token); } catch { /* local logout is unconditional */ }
      }
    },
    async getSession() { return currentSession(); },
    async getData() {
      const epoch = authEpoch;
      clearFiles();
      const generation = fileEpoch;
      const requireGeneration = () => {
        requireEpoch(epoch);
        if (generation !== fileEpoch) throw new Error('紀錄已更新，請重新整理。');
      };
      const archive = await getArchive();
      requireGeneration();
      if (!archive.current_version) return { schema_version: 1, patients: [], visits: [], reports: [], attachments: [], published_at: null };
      const query = new URLSearchParams({ select: 'snapshot,published_at', archive_id: `eq.${archiveId}`, version_id: `eq.${archive.current_version}` });
      const rows = await (await request(`/rest/v1/family_snapshots?${query}`)).json();
      requireGeneration();
      if (!Array.isArray(rows) || rows.length !== 1) throw new Error('紀錄正在更新，請重新整理。');
      const data = rows[0].snapshot;
      if (data?.schema_version !== 1 || !Array.isArray(data.visits) || !Array.isArray(data.attachments) || !Array.isArray(data.patients)
        || (data.reports !== undefined && !Array.isArray(data.reports))) {
        throw new Error('紀錄格式未能讀取，請聯絡管理人。');
      }
      const extensions = {'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','application/pdf':'.pdf'};
      const isScopedPath = (asset, file, preview=false) => {
        const suffix = preview ? '.thumb.jpg' : extensions[asset?.mime];
        return typeof file.id === 'string' && /^[A-Za-z0-9_-]{1,96}$/.test(file.id)
          && typeof asset?.sha256 === 'string' && /^[a-f0-9]{64}$/.test(asset.sha256) && suffix
          && [ `${archiveId}/${archive.current_version}/${file.id}${suffix}`,
            `${archiveId}/objects/${file.id}-${asset.sha256}${suffix}` ].includes(asset.object_path);
      };
      for (const file of data.attachments) {
        if (file.privacy_checked === true && isScopedPath(file,file)) {
          const preview = file.thumbnail;
          if (file.mime === 'application/pdf' && !(preview?.mime === 'image/jpeg' && isScopedPath(preview,file,true))) continue;
          availableFiles.set(file.id, { ...file, thumbnail: preview?.mime === 'image/jpeg' && isScopedPath(preview,file,true) ? preview : null });
        }
      }
      snapshotVersion = archive.current_version;
      return { ...data, reports:data.reports || [], published_at: rows[0].published_at || archive.published_at,
        snapshot_version:snapshotVersion };
    },
    getFeedback() { return feedbackRequest('feedback-list'); },
    saveFeedback(value) { return feedbackRequest('feedback-save',value); },
    async fileUrl(attachment) { return imageUrl(attachment, false); },
    async thumbnailUrl(attachment) { return imageUrl(attachment, true); },
    onAuthChange(callback) { listeners.add(callback); return () => listeners.delete(callback); },
    destroy() { clearFiles(); feedbackRequests.clear(); listeners.clear(); },
  };
}

const SHARE_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const SHARE_DENIED = '分享連結無效或已失效，請向管理人索取新連結。';
const SHARE_UNAVAILABLE = '暫時未能讀取分享紀錄，請稍後再試。';
const SHARE_MAX_IMAGE = 25 * 1024 * 1024;
const SHARE_MAX_PDF = 50000000;
const SHARE_MAX_SNAPSHOT = 16 * 1024 * 1024;

/** Only fragments carry the capability. Do not inspect a query string or persist it. */
export function readShareToken(hash) {
  const fragment = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
  const values = new URLSearchParams(fragment).getAll('share');
  if (!values.length) return null;
  if (values.length !== 1 || !SHARE_TOKEN_PATTERN.test(values[0])) throw new Error(SHARE_DENIED);
  return values[0];
}

/** Scoped capability client; the token exists only in this instance's memory. */
export function createShareClient(config, token, {onDenied} = {}) {
  const base = String(config?.supabaseUrl || '').replace(/\/$/, '');
  if (!/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(base)) {
    throw new Error('分享服務尚未設定，請聯絡管理人。');
  }
  if (typeof token !== 'string' || !SHARE_TOKEN_PATTERN.test(token)) throw new Error(SHARE_DENIED);
  const endpoint = base + '/functions/v1/family-share';
  let capability = token;
  token = undefined;
  let epoch = 0;
  let destroyed = false;
  let denied = false;
  let availableFiles = new Map();
  let snapshotVersion = null;
  const feedbackRequests = new Map();
  const blobUrls = new Map();
  const pendingImages = new Map();
  const safeId = /^[A-Za-z0-9_-]{1,96}$/;
  const safeHash = /^[a-f0-9]{64}$/;
  const imageTypes = new Set(['image/png', 'image/jpeg', 'image/webp', 'application/pdf']);

  function clearFiles() {
    for (const url of blobUrls.values()) URL.revokeObjectURL(url);
    blobUrls.clear();
    pendingImages.clear();
    availableFiles.clear();
    snapshotVersion = null;
  }
  function requireEpoch(expected) {
    if (destroyed || denied || !capability) throw new Error(SHARE_DENIED);
    if (expected !== epoch) throw new Error(SHARE_UNAVAILABLE);
  }
  function denyAccess() {
    if (destroyed || denied) return;
    denied = true;
    capability = null;
    feedbackRequests.clear();
    ++epoch;
    clearFiles();
    if (typeof onDenied === 'function') {
      try { onDenied(); } catch { /* The view cannot prevent local credential cleanup. */ }
    }
  }
  async function request(payload, expected) {
    requireEpoch(expected);
    let response;
    try {
      response = await fetch(endpoint, {
        method: 'POST', cache: 'no-store', credentials: 'omit', redirect: 'error',
        referrerPolicy: 'no-referrer', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: capability, ...payload}),
      });
    } catch {
      if (payload.action.startsWith('feedback-')) throw feedbackError(null,'feedback_unavailable');
      throw new Error(SHARE_UNAVAILABLE);
    }
    if (response.status === 403) {
      denyAccess();
      const error = new Error(SHARE_DENIED);
      if (payload.action.startsWith('feedback-')) { error.status = 403; error.code = 'feedback_denied'; }
      throw error;
    }
    requireEpoch(expected);
    if (!response.ok && !payload.action.startsWith('feedback-')) throw new Error(SHARE_UNAVAILABLE);
    return response;
  }
  async function boundedBytes(response, limit, expected) {
    const declared = response.headers.get('content-length');
    if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > limit)) {
      throw new Error(SHARE_UNAVAILABLE);
    }
    if (!response.body) throw new Error(SHARE_UNAVAILABLE);
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const result = await reader.read();
        requireEpoch(expected);
        if (result.done) break;
        size += result.value.byteLength;
        if (size > limit) {
          await reader.cancel();
          throw new Error(SHARE_UNAVAILABLE);
        }
        chunks.push(result.value);
      }
    } catch (error) {
      try { await reader.cancel(); } catch { /* Cleanup must not hide the original failure. */ }
      if (destroyed || denied) throw new Error(SHARE_DENIED);
      throw new Error(SHARE_UNAVAILABLE);
    } finally { reader.releaseLock(); }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return bytes;
  }
  const mimeOf = response => (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
  function checkedAsset(value, thumbnail = false) {
    if (!value || !imageTypes.has(value.mime) || (thumbnail && value.mime !== 'image/jpeg')
      || !Number.isInteger(value.size) || value.size < 1 || value.size > (thumbnail ? 3 * 1024 * 1024 : value.mime === 'application/pdf' ? SHARE_MAX_PDF : SHARE_MAX_IMAGE)
      || typeof value.sha256 !== 'string' || !safeHash.test(value.sha256)) throw new Error(SHARE_UNAVAILABLE);
    // Paths from the snapshot are deliberately excluded: image requests use IDs only.
    return {mime: value.mime, size: value.size, sha256: value.sha256};
  }
  function checkedSnapshot(data) {
    if (data?.schema_version !== 1 || !Array.isArray(data.patients)
      || !Array.isArray(data.visits) || !Array.isArray(data.attachments)
      || (data.reports !== undefined && !Array.isArray(data.reports))) throw new Error(SHARE_UNAVAILABLE);
    const patients = new Set();
    for (const patient of data.patients) {
      if (!['grandpa', 'grandma'].includes(patient?.id) || patients.has(patient.id)
        || patient.name !== (patient.id === 'grandpa' ? '爺爺' : '嬷嬷')) throw new Error(SHARE_UNAVAILABLE);
      patients.add(patient.id);
    }
    const files = new Map();
    for (const file of data.attachments) {
      if (!file || typeof file.id !== 'string' || !safeId.test(file.id) || files.has(file.id)
        || !patients.has(file.patient_id) || file.privacy_checked !== true
        || (file.mime === 'application/pdf' && file.thumbnail === undefined)) throw new Error(SHARE_UNAVAILABLE);
      files.set(file.id, {id: file.id, patient_id: file.patient_id, ...checkedAsset(file),
        thumbnail: file.thumbnail === undefined ? null : checkedAsset(file.thumbnail, true)});
    }
    const visits = new Set();
    const linked = new Set();
    const checkSources = (sources, patientId) => {
      if (!Array.isArray(sources)) throw new Error(SHARE_UNAVAILABLE);
      for (const id of sources) {
        if (!files.has(id) || files.get(id).patient_id !== patientId) throw new Error(SHARE_UNAVAILABLE);
        linked.add(id);
      }
    };
    for (const visit of data.visits) {
      if (!visit || typeof visit.id !== 'string' || !safeId.test(visit.id) || visits.has(visit.id)
        || !patients.has(visit.patient_id) || visit.status !== 'confirmed'
        || visit.privacy_reviewed !== true || !Array.isArray(visit.medicines)) throw new Error(SHARE_UNAVAILABLE);
      visits.add(visit.id);
      for (const sources of [visit.source_file_ids, ...visit.medicines.map(medicine => medicine?.source_file_ids)]) {
        checkSources(sources, visit.patient_id);
      }
    }
    const reports = new Set();
    for (const report of data.reports || []) {
      if (!report || typeof report.id !== 'string' || !safeId.test(report.id) || reports.has(report.id)
        || !patients.has(report.patient_id) || report.status !== 'confirmed' || report.privacy_reviewed !== true
        || !report.reviewed_at || !report.date || typeof report.title !== 'string' || !report.title.trim()
        || !Array.isArray(report.source_file_ids) || !report.source_file_ids.length || report.review_notes) throw new Error(SHARE_UNAVAILABLE);
      reports.add(report.id);
      checkSources(report.source_file_ids, report.patient_id);
    }
    if (linked.size !== files.size) throw new Error(SHARE_UNAVAILABLE);
    return files;
  }

  async function getData() {
    requireEpoch(epoch);
    const expected = ++epoch;
    clearFiles();
    const response = await request({action: 'snapshot'}, expected);
    if (mimeOf(response) !== 'application/json') throw new Error(SHARE_UNAVAILABLE);
    const bytes = await boundedBytes(response, SHARE_MAX_SNAPSHOT, expected);
    let data;
    try { data = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)); }
    catch { throw new Error(SHARE_UNAVAILABLE); }
    const files = checkedSnapshot(data);
    requireEpoch(expected);
    availableFiles = files;
    snapshotVersion = data.snapshot_version || null;
    return {...data,reports:data.reports || []};
  }

  async function imageUrl(attachment, thumbnail) {
    const expected = epoch;
    requireEpoch(expected);
    const file = availableFiles.get(attachment?.id);
    if (!file) throw new Error(SHARE_UNAVAILABLE);
    const asset = thumbnail && file.thumbnail ? file.thumbnail : file;
    const key = `${file.id}:${thumbnail && file.thumbnail ? 'thumbnail' : 'original'}`;
    if (blobUrls.has(key)) return blobUrls.get(key);
    if (pendingImages.has(key)) return pendingImages.get(key);
    const pending = (async () => {
      const response = await request({action: 'file', attachmentId: file.id, thumbnail}, expected);
      const mime = mimeOf(response);
      if (!imageTypes.has(mime) || mime !== asset.mime) throw new Error(SHARE_UNAVAILABLE);
      const bytes = await boundedBytes(response, asset.mime === 'application/pdf' ? SHARE_MAX_PDF : SHARE_MAX_IMAGE, expected);
      if (bytes.byteLength !== asset.size) throw new Error(SHARE_UNAVAILABLE);
      let checksum;
      try {
        const digest = await crypto.subtle.digest('SHA-256', bytes);
        checksum = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
      } catch { throw new Error(SHARE_UNAVAILABLE); }
      requireEpoch(expected);
      if (checksum !== asset.sha256) throw new Error(SHARE_UNAVAILABLE);
      const url = URL.createObjectURL(new Blob([bytes], {type: mime}));
      blobUrls.set(key, url);
      return url;
    })();
    pendingImages.set(key, pending);
    try { return await pending; }
    finally { if (pendingImages.get(key) === pending) pendingImages.delete(key); }
  }

  return {
    getData,
    async getFeedback() {
      const expected = epoch;
      const response = await request({action:'feedback-list'},expected);
      return feedbackResponse(response,() => requireEpoch(expected),'feedback-list');
    },
    async saveFeedback(value) {
      const expected = epoch;
      const payload = rememberedFeedback(value,snapshotVersion,feedbackRequests);
      const response = await request({action:'feedback-save',...payload},expected);
      return feedbackResponse(response,() => requireEpoch(expected),'feedback-save');
    },
    fileUrl(attachment) { return imageUrl(attachment, false); },
    thumbnailUrl(attachment) { return imageUrl(attachment, true); },
    destroy() {
      destroyed = true;
      capability = null;
      feedbackRequests.clear();
      ++epoch;
      clearFiles();
    },
  };
}

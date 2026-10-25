/** Authenticated, read-only family client. No records are persisted in browser storage. */
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
  }
  function saveSession(value) {
    session = value;
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { /* memory-only sessions still work */ }
    if (!value) clearFiles();
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
    const blob = await response.blob();
    requireEpoch(epoch);
    if (sourceEpoch !== fileEpoch) throw new Error('紀錄已更新，請重新開啟相片。');
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(blob.type)) throw new Error('相片格式不受支援。');
    if (preview && blob.type !== 'image/jpeg') throw new Error('預覽相片格式不受支援。');
    const url = URL.createObjectURL(blob); blobUrls.set(cacheKey, url); return url;
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
      const archive = await getArchive();
      requireEpoch(epoch);
      clearFiles();
      if (!archive.current_version) return { schema_version: 1, patients: [], visits: [], attachments: [], published_at: null };
      const query = new URLSearchParams({ select: 'snapshot,published_at', archive_id: `eq.${archiveId}`, version_id: `eq.${archive.current_version}` });
      const rows = await (await request(`/rest/v1/family_snapshots?${query}`)).json();
      requireEpoch(epoch);
      if (!Array.isArray(rows) || rows.length !== 1) throw new Error('紀錄正在更新，請重新整理。');
      const data = rows[0].snapshot;
      if (data?.schema_version !== 1 || !Array.isArray(data.visits) || !Array.isArray(data.attachments) || !Array.isArray(data.patients)) {
        throw new Error('紀錄格式未能讀取，請聯絡管理人。');
      }
      const prefix = `${archiveId}/${archive.current_version}/`;
      const isScopedPath = path => typeof path === 'string' && path.startsWith(prefix) && !path.includes('..') && !path.includes('\\');
      for (const file of data.attachments) {
        if (file.privacy_checked === true && isScopedPath(file.object_path)) {
          const preview = file.thumbnail;
          availableFiles.set(file.id, { ...file, thumbnail: preview?.mime === 'image/jpeg' && isScopedPath(preview.object_path) ? preview : null });
        }
      }
      return { ...data, published_at: rows[0].published_at || archive.published_at };
    },
    async fileUrl(attachment) { return imageUrl(attachment, false); },
    async thumbnailUrl(attachment) { return imageUrl(attachment, true); },
    onAuthChange(callback) { listeners.add(callback); return () => listeners.delete(callback); },
    destroy() { clearFiles(); listeners.clear(); },
  };
}

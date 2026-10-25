// Public connection details only. Never put a service-role/secret key here.
// tools/build_site.py can inject HEALTH_SUPABASE_URL / HEALTH_SUPABASE_ANON_KEY.
export const cloudConfig = Object.freeze({
  supabaseUrl: '',
  supabaseAnonKey: '',
  archiveId: 'family',
});
export default cloudConfig;

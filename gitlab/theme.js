/* Use GitLab's native application colors and its current-user preference endpoint. */
(() => {
  'use strict';
  const config = window.FantasyGitLabThemeConfig;
  if (!config || !/^\/[^?#]*$/.test(config.preferencesPath || '')) return;
  const valid = value => value === 'light' || value === 'dark';
  const key = 'fantasy-community:gitlab-theme:v1:' + (config.userId || 'guest');
  const defaultKey = 'fantasy-community:gitlab-default-theme:v1';
  let storage;
  try { storage = window.localStorage; } catch (_) { /* Window-local state still works. */ }
  let state = { default: 'dark', preference: 'auto', lastApplied: null };
  try {
    const saved = JSON.parse(storage?.getItem(key) || 'null');
    if (saved && valid(saved.default)) state.default = saved.default;
    if (saved && (valid(saved.preference) || saved.preference === 'auto')) state.preference = saved.preference;
    if (saved && [1, 2].includes(saved.lastApplied)) state.lastApplied = saved.lastApplied;
  } catch (_) { /* Ignore invalid local state. */ }
  try { const shared = storage?.getItem(defaultKey); if (valid(shared)) state.default = shared; } catch (_) {}
  // A change made with GitLab's original preference page takes precedence.
  if (state.lastApplied && [1, 2, 3].includes(config.currentMode) && config.currentMode !== state.lastApplied) {
    state.preference = config.currentMode === 1 ? 'light' : config.currentMode === 2 ? 'dark' : 'auto';
    state.lastApplied = null;
  }
  const url = new URL(location.href);
  const incoming = url.searchParams.get('fantasy_theme');
  if (valid(incoming)) {
    state.default = incoming;
    try { storage?.setItem(defaultKey, incoming); } catch (_) {}
    url.searchParams.delete('fantasy_theme');
    history.replaceState(history.state, '', url);
  }
  function saveLocal() { try { storage?.setItem(key, JSON.stringify(state)); } catch (_) {} }
  const current = () => state.preference === 'auto' ? state.default : state.preference;
  let select, status, pending = Promise.resolve(), revision = 0;
  function apply() {
    const theme = current();
    document.documentElement.classList.remove('gl-light', 'gl-dark', 'gl-system');
    document.documentElement.classList.add(theme === 'dark' ? 'gl-dark' : 'gl-light');
    document.documentElement.style.colorScheme = theme;
    const light = document.getElementById('fantasy-gitlab-light');
    const dark = document.getElementById('fantasy-gitlab-dark');
    if (light) light.media = theme === 'light' ? 'all' : 'not all';
    if (dark) dark.media = theme === 'dark' ? 'all' : 'not all';
    if (select) select.value = state.preference;
    saveLocal();
  }
  function persist() {
    if (!config.signedIn) return;
    const desired = current() === 'dark' ? 2 : 1;
    const requestRevision = ++revision;
    pending = pending.then(async () => {
      if (requestRevision !== revision) return;
      if (config.currentMode === desired) {
        state.lastApplied = desired; saveLocal(); return;
      }
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
      if (!csrf) { if (status) status.textContent = '当前窗口已应用；登录后可保存 GitLab 偏好。'; return; }
      try {
        const response = await fetch(config.preferencesPath, { method: 'PUT', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({ user: { color_mode_id: desired } }) });
        if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('preference rejected');
        const result = await response.json();
        if (result.type !== 'notice') throw new Error('preference rejected');
        config.currentMode = desired;
        state.lastApplied = desired; saveLocal();
        if (status) status.textContent = '';
      } catch (_) {
        // A connection failure must not claim a remote preference was saved.
        if (status) status.textContent = '当前窗口已应用；GitLab 偏好保存失败，可在偏好设置中重试。';
      }
    });
  }
  apply();
  function mount() {
    if (document.getElementById('fantasy-gitlab-theme-control')) return;
    const box = document.createElement('div'); box.id = 'fantasy-gitlab-theme-control';
    box.style.cssText = 'position:fixed;right:18px;bottom:16px;z-index:1000;max-width:360px;padding:8px 10px;border:1px solid var(--gl-border-color-default,#8795a5);border-radius:6px;background:var(--gl-background-color-default,#fff);color:var(--gl-text-color-default,#333);font:12px/1.5 sans-serif;box-shadow:0 2px 12px #0002';
    const label = document.createElement('label'); label.textContent = '主题 '; label.htmlFor = 'fantasy-gitlab-theme';
    select = document.createElement('select'); select.id = 'fantasy-gitlab-theme'; select.setAttribute('aria-label', '代码仓库主题');
    select.style.cssText = 'background:inherit;color:inherit;border:1px solid currentColor;border-radius:4px;padding:4px 7px;margin-left:5px';
    for (const [value, text] of [['auto', '跟随仪表盘'], ['light', '白天'], ['dark', '黑夜']]) {
      const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option);
    }
    status = document.createElement('span'); status.setAttribute('role', 'status'); status.style.cssText = 'display:block;max-width:300px;font-size:11px';
    select.addEventListener('change', () => { state.preference = select.value; apply(); persist(); });
    label.append(select); box.append(label, status); document.body.append(box);
    apply(); persist();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, { once: true });
  else mount();
})();

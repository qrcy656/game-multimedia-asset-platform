/* One dashboard default, explicit per-module overrides. Loaded before page styles. */
(() => {
  'use strict';
  if (window.FantasyTheme) return;
  const key = 'fantasy-community:theme:v1';
  const modules = ['knowledge', 'animation', 'image', 'speech', 'music', 'model3d', 'narrative', 'effects', 'gitlab'];
  const segment = location.pathname.split('/')[1];
  const scope = modules.includes(segment) ? segment : 'portal';
  const colorScope = ['image', 'speech', 'music', 'model3d'].includes(scope) ? 'media' : scope;
  const valid = value => value === 'light' || value === 'dark';
  // Capture the real storage before the effects adapter scopes its own project drafts.
  let storage;
  try { storage = window.localStorage; } catch (_) { /* A window-local preference still works. */ }
  let state = { default: 'dark', modules: {} };
  function read() {
    try {
      const value = JSON.parse(storage?.getItem(key) || 'null');
      if (value && typeof value === 'object') {
        const overrides = {};
        for (const name of modules) if (valid(value.modules?.[name])) overrides[name] = value.modules[name];
        state = { default: valid(value.default) ? value.default : 'dark', modules: overrides };
      }
    } catch (_) { /* Keep the last valid setting. */ }
    return state;
  }
  function preference(name = scope) { return name === 'portal' ? state.default : state.modules[name] || 'auto'; }
  function themeFor(name = scope) { return name === 'portal' ? state.default : state.modules[name] || state.default; }
  function apply() {
    const theme = themeFor();
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.fantasyModule = colorScope;
    document.documentElement.style.colorScheme = theme;
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = theme;
    document.querySelectorAll('[data-fantasy-theme-select]').forEach(select => {
      const name = select.dataset.fantasyThemeSelect;
      select.value = preference(name);
      select.title = name === 'portal' ? '此设置作为各工作模块的默认主题' :
        preference(name) === 'auto' ? `跟随仪表盘：${state.default === 'dark' ? '黑夜' : '白天'}` : '仅对此模块生效';
    });
    window.dispatchEvent(new CustomEvent('fantasy-theme-change', {
      detail: { scope, theme, preference: preference(), defaultTheme: state.default }
    }));
  }
  function setPreference(value, name = scope) {
    if (name !== 'portal' && !modules.includes(name)) return;
    if (!(valid(value) || (value === 'auto' && name !== 'portal'))) return;
    read();
    if (name === 'portal') state.default = value;
    else if (value === 'auto') delete state.modules[name];
    else state.modules[name] = value;
    try { storage?.setItem(key, JSON.stringify(state)); } catch (_) { /* Retain this window's choice. */ }
    apply();
  }
  let sequence = 0;
  function mount() {
    document.querySelectorAll('[data-theme-slot]').forEach(slot => {
      if (slot.querySelector('[data-fantasy-theme-select]')) return;
      const name = slot.dataset.themeModule || scope;
      if (name !== 'portal' && !modules.includes(name)) return;
      const label = document.createElement('label');
      label.className = 'fantasy-theme-control';
      const title = document.createElement('span');
      title.textContent = name === 'portal' ? '默认主题' : name === 'gitlab' ? '仓库主题' : '主题';
      const select = document.createElement('select');
      select.id = 'fantasy-theme-' + (++sequence);
      select.dataset.fantasyThemeSelect = name;
      select.setAttribute('aria-label', name === 'portal' ? '仪表盘与模块默认主题' : name === 'gitlab' ? '代码仓库主题' : '当前模块主题');
      label.htmlFor = select.id;
      for (const [value, text] of [...(name === 'portal' ? [] : [['auto', '跟随仪表盘']]), ['light', '白天'], ['dark', '黑夜']]) {
        const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option);
      }
      select.addEventListener('change', () => setPreference(select.value, name));
      label.append(title, select); slot.append(label);
      select.value = preference(name);
    });
  }
  window.FantasyTheme = Object.freeze({
    getTheme: themeFor, getPreference: preference, setPreference,
    getDefault: () => state.default, scope, storageKey: key
  });
  read();
  apply();
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    if (event.newValue === null) state = { default: 'dark', modules: {} };
    read(); apply();
  });
  const ready = () => {
    mount();
    apply();
    // React and the embedded editor create their headers after the document loads.
    new MutationObserver(records => {
      if (records.some(record => record.addedNodes.length)) mount();
    }).observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true });
  else ready();
})();

/* Run with node --test tests/test_theme_controls.cjs; no server or browser required. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'portal/static/theme.js'), 'utf8');
const gitlabScript = fs.readFileSync(path.join(root, 'gitlab/theme.js'), 'utf8');
const key = 'fantasy-community:theme:v1';

function environment(pathname, saved = {}, options = {}) {
  const data = new Map(Object.entries(saved));
  const events = new Map();
  const documentEvents = new Map();
  const nodes = new Map();
  const requests = [];
  const storage = {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value)};
  const classes = new Set();
  function node(tag) {
    return {tag, style: {}, dataset: {}, children: [], value: '', textContent: '',
      append(...children) { this.children.push(...children); for (const child of children) if (child.id) nodes.set(child.id, child); },
      setAttribute(name, value) { this[name] = value; },
      addEventListener(name, callback) { this['on' + name] = callback; }, querySelector() { return null; }};
  }
  const document = {readyState: 'loading', documentElement: {dataset: {}, style: {}, classList: {
      remove(...values) { values.forEach(value => classes.delete(value)); }, add(value) { classes.add(value); }
    }}, body: node('body'), createElement: node,
    querySelector: query => query.includes('csrf-token') ? {content: 'test-csrf'} : null,
    querySelectorAll: () => [], getElementById: id => nodes.get(id),
    addEventListener(name, callback) { documentEvents.set(name, callback); }};
  const window = {document, localStorage: storage,
    addEventListener(name, callback) { events.set(name, callback); },
    dispatchEvent(event) { events.get(event.type)?.(event); }};
  if (options.storageBlocked) Object.defineProperty(window, 'localStorage', {get() { throw new Error('disabled'); }});
  const context = vm.createContext({window, document, location: {pathname, href: 'https://example.test' + pathname},
    history: {state: {}, replaceState(_state, _unused, url) { context.replacedURL = String(url); }},
    URL, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    MutationObserver: class { observe() {} }, fetch: async (url, request) => {
      requests.push({url, request});
      return {ok: !options.saveFails, headers: {get: () => 'application/json'}, json: async () => ({type: 'notice'})};
    }});
  return {context, window, document, data, events, documentEvents, nodes, classes, requests};
}

test('first paint follows dashboard default across every editor', () => {
  for (const module of ['knowledge', 'animation', 'image', 'speech', 'music', 'model3d', 'narrative', 'effects']) {
    const page = environment('/' + module + '/', {[key]: JSON.stringify({default: 'light', modules: {}})});
    vm.runInContext(script, page.context);
    assert.equal(page.document.documentElement.dataset.theme, 'light');
    assert.equal(page.window.FantasyTheme.getPreference(), 'auto');
    assert.equal(page.document.documentElement.style.colorScheme, 'light');
  }
});
test('module override persists and can be reset without changing the dashboard or siblings', () => {
  const page = environment('/image/', {[key]: JSON.stringify({default: 'dark', modules: {music: 'light'}})});
  vm.runInContext(script, page.context);
  page.window.FantasyTheme.setPreference('light');
  assert.deepEqual(JSON.parse(page.data.get(key)), {default: 'dark', modules: {music: 'light', image: 'light'}});
  page.window.FantasyTheme.setPreference('auto');
  assert.deepEqual(JSON.parse(page.data.get(key)), {default: 'dark', modules: {music: 'light'}});
  assert.equal(page.document.documentElement.dataset.theme, 'dark');
});
test('dashboard change updates following tabs while respecting independent choices', () => {
  for (const override of [undefined, 'dark']) {
    const page = environment('/knowledge/', {[key]: JSON.stringify({default: 'dark', modules: {knowledge: override}})});
    vm.runInContext(script, page.context);
    page.data.set(key, JSON.stringify({default: 'light', modules: {knowledge: override}}));
    page.events.get('storage')({key, newValue: page.data.get(key)});
    assert.equal(page.document.documentElement.dataset.theme, override || 'light');
  }
});
test('effects draft storage adapter cannot capture or hide shared theme preferences', () => {
  const page = environment('/effects/');
  vm.runInContext(script, page.context);
  page.window.localStorage = {setItem() { throw new Error('project draft namespace'); }};
  page.window.FantasyTheme.setPreference('light');
  assert.equal(JSON.parse(page.data.get(key)).modules.effects, 'light');
});
test('blocked storage and malformed preferences keep controls usable', () => {
  const page = environment('/', {}, {storageBlocked: true});
  vm.runInContext(script, page.context);
  page.window.FantasyTheme.setPreference('light');
  assert.equal(page.document.documentElement.dataset.theme, 'light');
  page.window.FantasyTheme.setPreference('arbitrary-value');
  assert.equal(page.document.documentElement.dataset.theme, 'light');
});
test('all entry pages load theme selection before their own styles', () => {
  for (const filename of ['portal/static/index.html', 'knowledge/static/index.html', 'animation/static/index.html',
      'media/static/index.html', 'narrative/index.html', 'effects/vendor/index.html']) {
    const source = fs.readFileSync(path.join(root, filename), 'utf8');
    assert.ok(source.indexOf('/portal-static/theme.js') < source.indexOf('rel="stylesheet"'), filename);
  }
});

function gitlabPage(pathname, saved = {}, options = {}) {
  const page = environment(pathname, saved, options);
  page.nodes.set('fantasy-gitlab-light', {media: 'not all'});
  page.nodes.set('fantasy-gitlab-dark', {media: 'not all'});
  page.window.FantasyGitLabThemeConfig = {userId: 7, signedIn: true, currentMode: 3, preferencesPath: '/-/profile/preferences', ...options.config};
  vm.runInContext(gitlabScript, page.context);
  return page;
}
test('managed GitLab applies its native stylesheet and classes from dashboard default', () => {
  const page = gitlabPage('/projects?fantasy_theme=light');
  assert.ok(page.classes.has('gl-light'));
  assert.equal(page.nodes.get('fantasy-gitlab-light').media, 'all');
  assert.equal(page.nodes.get('fantasy-gitlab-dark').media, 'not all');
  assert.equal(page.context.replacedURL, 'https://example.test/projects');
});
test('managed GitLab independent override wins over a new dashboard default', () => {
  const page = gitlabPage('/projects?fantasy_theme=light', {
    'fantasy-community:gitlab-theme:v1:7': JSON.stringify({default: 'dark', preference: 'dark', lastApplied: 2})
  }, {config: {currentMode: 2}});
  assert.ok(page.classes.has('gl-dark'));
});
test('GitLab original preference changes are respected on the next page', () => {
  const page = gitlabPage('/projects?fantasy_theme=dark', {
    'fantasy-community:gitlab-theme:v1:7': JSON.stringify({default: 'dark', preference: 'dark', lastApplied: 2})
  }, {config: {currentMode: 1}});
  assert.ok(page.classes.has('gl-light'));
});
test('GitLab preference writes use current-user endpoint and CSRF, without OAuth token scope changes', async () => {
  const page = gitlabPage('/projects?fantasy_theme=light');
  page.documentEvents.get('DOMContentLoaded')();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.requests.length, 1);
  const {url, request} = page.requests[0];
  assert.equal(url, '/-/profile/preferences');
  assert.equal(request.method, 'PUT');
  assert.equal(request.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(request.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(request.body), {user: {color_mode_id: 1}});
});
test('GitLab failed preference save does not claim server persistence', async () => {
  const page = gitlabPage('/projects?fantasy_theme=light', {}, {saveFails: true});
  page.documentEvents.get('DOMContentLoaded')();
  await new Promise(resolve => setImmediate(resolve));
  const controls = page.document.body.children[0];
  assert.match(controls.children[1].textContent, /保存失败/);
  assert.equal(JSON.parse(page.data.get('fantasy-community:gitlab-theme:v1:7')).lastApplied, null);
});

/* All vault content is untrusted. Only sanitized Markdown is inserted as HTML. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = {
    files: [], assets: [], directories: [], directoryPermissions: {}, selected: new Set(), folder: '', document: null,
    dirty: false, history: [], tab: 'chat', chatBusy: false, indexBusy: false,
    saveBusy: false, importBusy: false, authenticated: false, source: null,
    action: null, actionBusy: false, fileRequest: 0, sourceRequest: 0, openFolders: new Set(),
    sso: false, loginUrl: '', logoutUrl: '', permissions: null, features: null,
  };
  let previewTimer;
  const basename = (path) => String(path).split('/').pop();
  const dirname = (path) => String(path).split('/').slice(0, -1).join('/');
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const pathQuery = (path) => new URLSearchParams({ path }).toString();
  const isFresh = (file) => Boolean(file.indexed && (!file.indexed_hash || file.indexed_hash === file.hash));
  const isStale = (file) => Boolean(file.indexed_hash && file.indexed_hash !== file.hash);
  const errorText = (error) => error instanceof Error ? error.message : String(error);
  const can = (permission) => !state.sso || state.permissions?.includes(permission);
  const canEdit = (path) => can('knowledge.write') && (path === '' ||
    (state.document?.path === path ? state.document.editable !== false :
      (state.files.find(file => file.path === path) || state.directoryPermissions[path])?.editable === true));

  function configureSession(session) {
    if (!session.sso) return;
    state.sso = true;
    state.loginUrl = session.login_url || state.loginUrl;
    state.logoutUrl = session.logout_url || state.logoutUrl;
    state.permissions = Array.isArray(session.permissions) ? session.permissions : [];
    $('login-form').hidden = true;
    document.querySelector('.login-note').textContent = '正在前往 Fantasy 统一登录…';
    $('logout').textContent = '退出登录';
    $('return-home').href = '/';
    const writable = can('knowledge.write');
    for (const id of ['new-file', 'new-folder', 'import-open', 'empty-new-file', 'move-file', 'delete-file', 'save-file']) $(id).hidden = !writable;
    for (const id of ['index-selected', 'index-all', 'index-document']) $(id).hidden = !can('knowledge.index');
    $('document-editor').readOnly = !writable;
    if (!writable) document.querySelector('.header-description').textContent = '只读访问 · 可浏览资料与知识问答';
    setFolder(state.folder);
    updateSelection();
  }

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  async function api(url, method = 'GET', body) {
    let response;
    try {
      response = await fetch(url, {
        method, credentials: 'same-origin', cache: 'no-store',
        headers: method === 'GET' ? {} : { 'Content-Type': 'application/json' },
        ...(method === 'GET' ? {} : { body: JSON.stringify(body ?? {}) }),
      });
    } catch (_) {
      throw new ApiError('无法连接服务器，请检查 Docker 服务是否已启动，然后重试。', 0);
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new ApiError('服务器返回了无法读取的响应，请检查服务日志。', response.status); }
    if (!response.ok) {
      if (response.status === 401 && url !== '/knowledge/api/login') {
        if (data.sso) configureSession(data);
        showLogin('登录已过期，请重新登录。');
      }
      const detail = typeof data.detail === 'string' ? data.detail :
        Array.isArray(data.detail) ? '输入格式不符合要求，请检查路径和内容长度。' : `请求失败（${response.status}）`;
      throw new ApiError(detail, response.status);
    }
    return data;
  }

  async function assetBlob(source) {
    const params = new URLSearchParams({ path: source.path, hash: source.hash, max_side: '1600' });
    if (source.crop) params.set('crop', JSON.stringify(source.crop));
    let response;
    try { response = await fetch(`/knowledge/api/asset?${params}`, { credentials: 'same-origin', cache: 'no-store' }); }
    catch (_) { throw new ApiError('无法连接服务器，请检查 Docker 服务是否已启动。', 0); }
    if (!response.ok) {
      if (response.status === 401) showLogin('登录已过期，请重新输入访问口令。');
      if (response.status === 409) throw new ApiError('这张图片已被修改，此引用对应旧版本。为避免混用版本，不显示新版图片，请刷新资料或重新提问。', 409);
      let data;
      try { data = await response.json(); } catch (_) { /* Non-JSON errors still have a useful status. */ }
      throw new ApiError(typeof data?.detail === 'string' ? data.detail : `无法读取图片（${response.status}）`, response.status);
    }
    const blob = await response.blob();
    if (!blob.type.startsWith('image/')) throw new ApiError('服务器没有返回有效的图片。', response.status);
    return blob;
  }

  function releaseImages(target) {
    target.querySelectorAll('img[data-vault-image]').forEach((img) => {
      if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
      img.removeAttribute('src');
    });
  }

  function notice(message, error = false) {
    $('notice-text').textContent = message;
    $('notice').classList.toggle('error', error);
    $('notice').hidden = false;
  }

  function showLogin(message = '') {
    state.authenticated = false;
    $('app-shell').inert = true;
    if (state.sso && state.loginUrl) {
      window.location.assign(state.loginUrl);
      return;
    }
    $('login-screen').hidden = false;
    $('login-error').textContent = message;
    $('login-error').hidden = !message;
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
    $('access-token').focus();
  }

  async function enterWorkspace() {
    state.authenticated = true;
    $('app-shell').inert = false;
    $('login-screen').hidden = true;
    $('access-token').value = '';
    const results = await Promise.allSettled([refreshFiles(), refreshStatus()]);
    const failed = results.filter((result) => result.status === 'rejected');
    if (failed.length) notice(failed.map((result) => errorText(result.reason)).join('\n'), true);
  }

  function switchTab(tab) {
    state.tab = tab;
    for (const name of ['chat', 'docs']) {
      $(`tab-${name}`).classList.toggle('active', name === tab);
      $(`tab-${name}`).setAttribute('aria-selected', String(name === tab));
      $(`${name}-view`).hidden = name !== tab;
    }
  }

  function closeSidebar() {
    $('sidebar').classList.remove('open');
    $('toggle-sidebar').setAttribute('aria-expanded', 'false');
  }

  function setFolder(path) {
    state.folder = path;
    $('selected-folder').textContent = path || '根目录';
    $('selected-folder').title = `新建内容所在目录：${path || '根目录'}`;
    $('folder-rename').hidden = !path || !canEdit(path);
    $('folder-delete').hidden = !path || !canEdit(path);
    for (const id of ['new-file', 'new-folder', 'import-open']) $(id).disabled = !canEdit(path);
    document.querySelectorAll('.tree-folder').forEach((node) => node.classList.toggle('selected', node.dataset.path === path));
  }

  async function refreshFiles() {
    const [data, assetData] = await Promise.all([api('/knowledge/api/files'), api('/knowledge/api/assets')]);
    state.files = Array.isArray(data.files) ? data.files : [];
    state.assets = Array.isArray(assetData.assets) ? assetData.assets.filter((asset) => asset.kind === 'image') : [];
    state.directories = Array.isArray(data.directories) ? data.directories.map((dir) => typeof dir === 'string' ? dir : dir.path) : [];
    state.directoryPermissions = Object.fromEntries((data.directories || []).filter(dir => typeof dir === 'object').map(dir => [dir.path, dir]));
    const paths = new Set(state.files.map((file) => file.path));
    state.selected = new Set([...state.selected].filter((path) => paths.has(path)));
    if (state.folder && !state.directories.includes(state.folder) && !state.files.some((file) => file.path.startsWith(state.folder + '/'))) setFolder('');
    $('file-count').textContent = state.files.length;
    renderTree();
    setFolder(state.folder);
    updateDocumentState();
    if (state.document) renderPreview();
    return data;
  }

  async function refreshStatus() {
    try {
      const data = await api('/knowledge/api/status');
      const models = data.models || {};
      state.features = data.features || {
        raw_qa: { available: Boolean(models.llm || models.vision) },
        vector_index: { available: Boolean(models.embedding) },
      };
      const connection = $('connection-state');
      connection.className = 'connection online';
      connection.querySelector('span').textContent = state.features.raw_qa.available ? (models.embedding ? '问答与向量检索可用' : '原文问答可用') : models.embedding ? '文档管理与向量检索可用' : '文档管理可用';
      connection.title = `向量模型：${models.embedding ? '已配置' : '未配置'}；重排序（可选）：${models.reranker ? '已配置' : '未配置'}；问答模型：${state.features.raw_qa.available ? '已配置' : '未配置'}；识图：${models.vision ? '已配置' : '未配置'}`;
      $('model-label').textContent = data.model ? `模型 · ${data.model.split('/').pop()}` : '问答模型待配置';
      $('model-label').title = connection.title;
      $('index-summary').textContent = `${data.indexed_files ?? data.indexed ?? 0} 份 · ${data.chunks ?? 0} 片段`;
      if (data.indexing && !state.indexBusy) {
        $('index-hint').textContent = '服务器正在执行入库任务，请稍后刷新状态。';
      } else updateIndexHint();
      setChatBusy(state.chatBusy);
      return data;
    } catch (error) {
      $('connection-state').className = 'connection offline';
      $('connection-state').querySelector('span').textContent = '连接异常';
      $('model-label').textContent = '状态暂不可用';
      throw error;
    }
  }

  function updateIndexHint() {
    if (state.features?.vector_index?.available === false) {
      $('index-hint').textContent = '文档管理可用；原文问答只需 LLM 或 Vision。配置 Embedding 后可进行向量入库，重排序为可选项。';
      return;
    }
    const stale = state.files.filter(isStale).length;
    const pending = state.files.filter((file) => !isFresh(file)).length;
    $('index-hint').textContent = !state.files.length ? '新建或导入 Markdown 文档后，可直接管理原文，也可入库用于向量检索。' : pending ? `${pending} 份文档待入库${stale ? `，其中 ${stale} 份原文已更新` : ''}。原文仍可读取；保存后按需更新向量库。` : '所有文档均已入库。保存原文后，请重新入库。';
  }

  function updateSelection() {
    $('selection-count').textContent = state.selected.size;
    const indexingAvailable = state.features?.vector_index?.available !== false;
    $('index-selected').disabled = !indexingAvailable || !can('knowledge.index') || state.indexBusy || !state.selected.size;
    $('index-all').disabled = !indexingAvailable || !can('knowledge.index') || state.indexBusy || !state.files.length;
    $('index-document').disabled = !indexingAvailable || !can('knowledge.index') || state.indexBusy || !state.document || state.dirty || state.saveBusy;
    $('logout').disabled = state.chatBusy || state.indexBusy || state.saveBusy || state.importBusy || state.actionBusy;
  }

  function renderTree() {
    const tree = $('file-tree');
    tree.replaceChildren();
    const query = $('file-search').value.trim().toLocaleLowerCase();
    const files = state.files.filter((file) => file.path.toLocaleLowerCase().includes(query));
    const root = { folders: new Map(), files: [] };
    function folderNode(path) {
      let node = root;
      let current = '';
      for (const part of path.split('/').filter(Boolean)) {
        current = current ? `${current}/${part}` : part;
        if (!node.folders.has(part)) node.folders.set(part, { path: current, folders: new Map(), files: [] });
        node = node.folders.get(part);
      }
      return node;
    }
    if (!query) state.directories.forEach(folderNode);
    for (const file of files) folderNode(dirname(file.path)).files.push(file);
    const rootButton = element('button', 'text-button', '⌂ 根目录');
    rootButton.title = '在根目录新建文档或目录';
    rootButton.addEventListener('click', () => setFolder(''));
    tree.append(rootButton);
    function appendNode(parent, node) {
      [...node.folders.entries()].sort(([a], [b]) => a.localeCompare(b, 'zh-CN')).forEach(([name, child]) => {
        const details = element('details', 'tree-folder');
        details.dataset.path = child.path;
        details.open = Boolean(query || state.openFolders.has(child.path) || (state.document && state.document.path.startsWith(child.path + '/')));
        details.classList.toggle('selected', state.folder === child.path);
        const summary = element('summary');
        summary.title = child.path;
        summary.append(element('span', 'folder-icon', '▱'), element('span', 'tree-folder-label', name));
        summary.addEventListener('click', () => setFolder(child.path));
        details.addEventListener('toggle', () => details.open ? state.openFolders.add(child.path) : state.openFolders.delete(child.path));
        const children = element('div', 'tree-children');
        appendNode(children, child);
        details.append(summary, children);
        parent.append(details);
      });
      node.files.sort((a, b) => a.path.localeCompare(b.path, 'zh-CN')).forEach((file) => {
        const row = element('div', 'tree-file');
        row.classList.toggle('active', state.document?.path === file.path);
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.disabled = !can('knowledge.index');
        checkbox.hidden = !can('knowledge.index');
        checkbox.checked = state.selected.has(file.path);
        checkbox.setAttribute('aria-label', `选择入库：${file.path}`);
        checkbox.addEventListener('change', () => {
          checkbox.checked ? state.selected.add(file.path) : state.selected.delete(file.path);
          updateSelection();
        });
        const button = element('button', 'file-open');
        button.title = `${file.path}\n${file.owner_name || ''}${file.editable ? ' · 可编辑' : ' · 共享只读'}`;
        button.append(element('span', 'file-icon', '▤'), element('span', 'file-name', basename(file.path).replace(/\.md$/i, '')));
        button.addEventListener('click', () => openDocument(file.path));
        const dot = element('span', `file-status-dot ${isFresh(file) ? 'indexed' : isStale(file) ? 'stale' : ''}`);
        dot.title = isFresh(file) ? '已入库' : isStale(file) ? '原文已修改，需重新入库' : '尚未入库';
        dot.setAttribute('aria-label', dot.title);
        row.append(checkbox, button, dot);
        parent.append(row);
      });
    }
    appendNode(tree, root);
    if (!files.length && (query || !state.directories.length)) tree.append(element('div', 'tree-empty', query ? '没有匹配的文件。' : '这里还是空的。\n新建或导入你的第一份设定。'));
    renderAssets(query);
    updateSelection();
    updateIndexHint();
  }

  function renderAssets(query) {
    const list = $('asset-list');
    list.replaceChildren();
    $('asset-count').textContent = state.assets.length;
    const assets = state.assets.filter((asset) => asset.path.toLocaleLowerCase().includes(query));
    for (const asset of assets.sort((a, b) => a.path.localeCompare(b.path, 'zh-CN'))) {
      const button = element('button', 'asset-open');
      button.title = asset.path;
      const label = element('span', 'asset-label');
      label.append(element('span', 'asset-name', asset.title || basename(asset.path)), element('span', 'asset-path', asset.path));
      button.append(element('span', 'file-icon', '▧'), label);
      button.addEventListener('click', () => showSource(asset));
      list.append(button);
    }
    if (!assets.length) list.append(element('div', 'tree-empty', query ? '没有匹配的图片。' : '素材库暂无可读取的图片。'));
  }

  function canDiscard() {
    return !state.dirty || window.confirm('这份文档有尚未保存的修改。离开会丢失这些修改，确定继续吗？');
  }

  function setDirty(dirty) {
    state.dirty = dirty;
    $('dirty-tab').hidden = !dirty;
    $('save-state').textContent = dirty ? '有未保存修改' : '已保存';
    $('save-state').classList.toggle('dirty', dirty);
    $('save-file').disabled = !state.document || !canEdit(state.document.path) || !dirty || state.saveBusy;
    updateSelection();
    updateDocumentState();
  }

  function updateDocumentState() {
    if (!state.document) return;
    const file = state.files.find((entry) => entry.path === state.document.path);
    if (file) state.document.editable = file.editable;
    const editable = canEdit(state.document.path);
    $('document-editor').readOnly = !editable || state.saveBusy;
    for (const id of ['save-file', 'move-file', 'delete-file']) $(id).hidden = !editable;
    const pill = $('document-state');
    if (!editable) pill.textContent = '共享只读 · 可引用和入库';
    else if (state.dirty) pill.textContent = '未保存';
    else if (file && isFresh(file)) pill.textContent = '已入库';
    else if (file && isStale(file)) pill.textContent = '已修改 · 待重新入库';
    else pill.textContent = '尚未入库';
    pill.classList.toggle('stale', state.dirty || !file || !isFresh(file));
  }

  function wikiMarkdown(input) {
    // Skip fenced and inline code while making Obsidian's internal links navigable.
    let fence = '';
    return String(input).split('\n').map((line) => {
      const match = line.match(/^\s{0,3}(`{3,}|~{3,})/);
      if (match) {
        if (!fence) fence = match[1][0];
        else if (match[1][0] === fence) fence = '';
        return line;
      }
      if (fence) return line;
      return line.split(/(`+[^`]*`+)/g).map((part, i) => i % 2 ? part : part.replace(/(!?)\[\[([^\]\n]+)\]\]/g, (_, embed, target) => {
        const [path, alias] = target.split('|');
        const label = (alias || path).replace(/[\\\[\]]/g, '\\$&');
        if (embed && /\.(?:png|jpe?g|webp|gif|bmp)(?:#.*)?$/i.test(path)) return `![${label}](#asset=${encodeURIComponent(path)})`;
        return `[${label}](#doc=${encodeURIComponent(path)})`;
      })).join('');
    }).join('\n');
  }

  function renderMarkdown(target, content, basePath = '') {
    releaseImages(target);
    if (!window.marked || !window.DOMPurify) {
      target.textContent = content;
      return;
    }
    const html = window.marked.parse(wikiMarkdown(content), { gfm: true, breaks: false });
    const fragment = window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe', 'object', 'embed', 'video', 'audio'],
      FORBID_ATTR: ['style', 'srcset'],
      SANITIZE_NAMED_PROPS: true,
      RETURN_DOM_FRAGMENT: true,
    });
    const pendingImages = [];
    fragment.querySelectorAll('img').forEach((img) => {
      const reference = img.getAttribute('src') || '';
      const asset = resolveAsset(reference, basePath);
      // Remove every supplied URL before inserting into the live DOM. Only
      // authenticated images from the known asset inventory may be displayed.
      img.removeAttribute('src');
      if (!asset) {
        img.replaceWith(element('span', 'image-placeholder', `[图片未收录：${img.alt || reference || '未知图片'}]`));
        return;
      }
      img.dataset.vaultImage = 'true';
      img.classList.add('vault-image');
      img.alt = img.alt || asset.title || basename(asset.path);
      img.tabIndex = 0;
      img.setAttribute('role', 'button');
      img.setAttribute('aria-label', `查看图片：${asset.path}`);
      img.title = `查看图片：${asset.path}`;
      img.addEventListener('click', () => showSource(asset));
      img.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); showSource(asset); }
      });
      pendingImages.push({ img, asset });
    });
    target.replaceChildren(fragment);
    for (const { img, asset } of pendingImages) {
      assetBlob(asset).then((blob) => {
        if (img.isConnected) img.src = URL.createObjectURL(blob);
      }).catch((error) => {
        if (img.isConnected) img.replaceWith(element('span', 'image-placeholder', `图片暂不可用：${errorText(error)}`));
      });
    }
    target.querySelectorAll('a').forEach((anchor) => {
      const href = anchor.getAttribute('href') || '';
      const asset = resolveAsset(href.startsWith('#doc=') ? href.slice(5) : href, basePath);
      if (asset) {
        anchor.addEventListener('click', (event) => { event.preventDefault(); showSource(asset); });
      } else if (href.startsWith('#doc=') || (!/^[a-z][a-z\d+.-]*:/i.test(href) && !href.startsWith('//') && /\.md(?:#.*)?$/i.test(href))) {
        anchor.addEventListener('click', (event) => {
          event.preventDefault();
          let reference;
          try { reference = decodeURIComponent(href.startsWith('#doc=') ? href.slice(5) : href); }
          catch (_) { reference = href; }
          navigateLink(reference, basePath);
        });
      } else if (/^https?:/i.test(href)) {
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
      }
    });
  }

  function resolveAsset(reference, basePath) {
    let target = String(reference).replace(/^#asset=/, '');
    try { target = decodeURIComponent(target); } catch (_) { return null; }
    if (/^[a-z][a-z\d+.-]*:/i.test(target) || target.startsWith('/') || target.startsWith('\\')) return null;
    function normalize(path) {
      const parts = [];
      for (const part of path.replace(/\\/g, '/').split('/')) {
        if (!part || part === '.') continue;
        if (part === '..') { if (!parts.length) return null; parts.pop(); }
        else parts.push(part);
      }
      return parts.join('/');
    }
    const candidates = [normalize(`${dirname(basePath)}/${target}`), normalize(target)];
    for (const candidate of candidates) {
      const found = state.assets.find((asset) => asset.path === candidate);
      if (found) return found;
    }
    const matches = state.assets.filter((asset) => asset.path.endsWith('/' + target));
    return matches.length === 1 ? matches[0] : null;
  }

  function renderPreview() {
    renderMarkdown($('document-preview'), $('document-editor').value, state.document?.path || '');
  }

  function renderLinks(container, paths, emptyMessage) {
    container.replaceChildren();
    if (!Array.isArray(paths) || !paths.length) { container.textContent = emptyMessage; return; }
    for (const path of paths) {
      const button = element('button', '', path);
      button.addEventListener('click', () => openDocument(path));
      container.append(button);
    }
  }

  function applyDocument(doc) {
    state.document = doc;
    $('document-empty').hidden = true;
    $('document-workspace').hidden = false;
    $('document-name').textContent = basename(doc.path).replace(/\.md$/i, '');
    $('document-path').textContent = doc.path;
    $('document-editor').value = doc.content || '';
    setFolder(dirname(doc.path));
    setDirty(false);
    renderPreview();
    renderLinks($('outgoing-links'), doc.links, '暂无已解析的文档链接');
    renderLinks($('backlinks'), doc.backlinks, '暂无其他文档引用此文件');
    renderTree();
    switchTab('docs');
    closeSidebar();
  }

  async function openDocument(path, force = false) {
    if (state.saveBusy) { notice('文档正在保存，请稍后再打开其他文件。'); return false; }
    if (!force && state.document?.path === path) { switchTab('docs'); closeSidebar(); return true; }
    if (!force && !canDiscard()) return false;
    const request = ++state.fileRequest;
    const editorSnapshot = $('document-editor').value;
    try {
      const doc = await api(`/knowledge/api/file?${pathQuery(path)}`);
      if (request !== state.fileRequest) return false;
      if (state.saveBusy) { notice('当前文档正在保存，保存完成后请重新选择文件。'); return false; }
      if ($('document-editor').value !== editorSnapshot && !canDiscard()) return false;
      applyDocument(doc);
      return true;
    } catch (error) { notice(errorText(error), true); return false; }
  }

  function normalizeRelative(value) {
    const path = String(value).trim().replace(/\\/g, '/');
    if (!path || path.startsWith('/') || /^[a-z]:/i.test(path) || path.split('/').some((part) => part === '..' || part === '.' || !part)) {
      throw new Error('请输入知识库内的相对路径，不要包含绝对路径、空层级或 ..。');
    }
    return path;
  }

  async function ensureDirectories(path) {
    if (!path) return;
    const known = new Set(state.directories);
    let current = '';
    for (const part of path.split('/')) {
      current = current ? `${current}/${part}` : part;
      if (known.has(current)) continue;
      try { await api('/knowledge/api/directory', 'POST', { path: current }); }
      catch (error) {
        // Another editor may have created the same directory concurrently.
        if (error.status !== 409) throw error;
        const fresh = await api('/knowledge/api/files');
        const directories = (fresh.directories || []).map((dir) => typeof dir === 'string' ? dir : dir.path);
        if (!directories.includes(current)) throw error;
      }
      known.add(current);
      if (!state.directories.includes(current)) state.directories.push(current);
    }
  }

  function navigateLink(reference, fromPath) {
    let target = reference.split('|')[0].split('#')[0];
    if (!target) return;
    if (!/\.md$/i.test(target)) target += '.md';
    function normalize(path) {
      const result = [];
      for (const part of path.split('/')) {
        if (!part || part === '.') continue;
        if (part === '..') result.pop(); else result.push(part);
      }
      return result.join('/');
    }
    const candidates = [normalize(`${dirname(fromPath)}/${target}`), normalize(target)];
    let path = candidates.find((candidate) => state.files.some((file) => file.path === candidate));
    if (!path) {
      const matches = state.files.filter((file) => file.path === target || file.path.endsWith('/' + target));
      if (matches.length === 1) path = matches[0].path;
      else if (matches.length > 1) { notice(`“${reference}”匹配多个文件，请在左侧选择具体路径。`, true); return; }
    }
    if (path) openDocument(path);
    else notice(`未找到链接指向的文档：${reference}`, true);
  }

  async function saveDocument() {
    if (!state.document || !canEdit(state.document.path) || !state.dirty || state.saveBusy) return;
    state.saveBusy = true;
    const doc = state.document;
    const content = $('document-editor').value;
    $('save-file').disabled = true;
    $('save-file').textContent = '保存中…';
    $('document-editor').readOnly = true;
    updateSelection();
    try {
      const updated = await api('/knowledge/api/file', 'PUT', { path: doc.path, content, expected_hash: doc.hash, create: false });
      state.document = { ...doc, ...updated, content };
      setDirty(false);
      notice('原始文档已保存。点击“此文档入库”更新知识库检索。');
      await refreshFiles();
      const fresh = await api(`/knowledge/api/file?${pathQuery(doc.path)}`);
      if (fresh.hash === state.document.hash) {
        state.document = { ...state.document, links: fresh.links, backlinks: fresh.backlinks };
        renderLinks($('outgoing-links'), fresh.links, '暂无已解析的文档链接');
        renderLinks($('backlinks'), fresh.backlinks, '暂无其他文档引用此文件');
      } else {
        notice('本次保存已完成，但文档随后又被其他人更新。当前编辑仍保留你的版本；继续保存时会检测冲突，请刷新后核对。', true);
      }
    } catch (error) {
      notice(error.status === 409 ? '保存冲突：服务器上的文档已被修改。你的编辑仍保留在这里，请先复制备份，再刷新页面核对新版后合并。' : errorText(error), true);
    } finally {
      state.saveBusy = false;
      $('document-editor').readOnly = !canEdit(state.document.path);
      $('save-file').textContent = '保存';
      setDirty(state.dirty);
    }
  }

  function resetDocument() {
    ++state.fileRequest;
    state.document = null;
    setDirty(false);
    $('document-editor').value = '';
    releaseImages($('document-preview'));
    $('document-preview').replaceChildren();
    $('document-empty').hidden = false;
    $('document-workspace').hidden = true;
  }

  function showAction({ title, description, label = '路径', value = '', submit = '确认', danger = false, input = true, handler }) {
    state.action = handler;
    $('action-title').textContent = title;
    $('action-description').textContent = description;
    $('action-label').textContent = label;
    $('action-label').hidden = !input;
    $('action-value').hidden = !input;
    $('action-value').required = input;
    $('action-value').value = value;
    $('action-error').hidden = true;
    $('action-submit').textContent = submit;
    $('action-submit').classList.toggle('danger-button', danger);
    $('action-dialog').showModal();
    if (input) { $('action-value').focus(); $('action-value').select(); }
  }

  function newFile() {
    if (!can('knowledge.write')) return;
    if (state.saveBusy) { notice('请等待当前文档保存完成。'); return; }
    if (!canDiscard()) return;
    showAction({ title: '新建 Markdown 文档', description: '路径相对于原始资料根目录，可使用“目录/文档.md”创建嵌套文档。', label: '文档路径', value: state.folder ? `${state.folder}/新文档.md` : '新文档.md', submit: '创建文档', handler: async (value) => {
      let path = normalizeRelative(value);
      if (!/\.md$/i.test(path)) path += '.md';
      await ensureDirectories(dirname(path));
      const created = await api('/knowledge/api/file', 'PUT', { path, content: `# ${basename(path).replace(/\.md$/i, '')}\n\n`, create: true });
      ++state.fileRequest;
      applyDocument({ ...created, backlinks: [] });
      await refreshFiles();
      notice('文档已创建。编辑保存后，可将它加入向量知识库。');
    }});
  }

  function newFolder() {
    if (!can('knowledge.write')) return;
    showAction({ title: '新建目录', description: '可创建多级目录，例如“世界设定/人物”。', label: '目录路径', value: state.folder ? `${state.folder}/新目录` : '新目录', submit: '创建目录', handler: async (value) => {
      const path = normalizeRelative(value);
      await ensureDirectories(path);
      state.openFolders.add(path);
      await refreshFiles();
      setFolder(path);
      notice(`目录已创建：${path}`);
    }});
  }

  function movePath(directory = false) {
    if (!can('knowledge.write')) return;
    if (state.saveBusy) { notice('请等待当前文档保存完成。'); return; }
    const path = directory ? state.folder : state.document?.path;
    if (!path || !canEdit(path)) return;
    if (state.dirty && (state.document.path === path || state.document.path.startsWith(path + '/'))) { notice('请先保存当前文档，再重命名或移动它所在的目录。', true); return; }
    const hash = directory ? undefined : state.document.hash;
    showAction({ title: directory ? '重命名 / 移动目录' : '重命名 / 移动文档', description: '填写新的完整相对路径。移动会使相关内容需要重新入库；其他文档中的链接请按需手动更新。', label: '新路径', value: path, submit: '保存路径', handler: async (value) => {
      const newPath = normalizeRelative(value);
      if (!directory && !/\.md$/i.test(newPath)) throw new Error('文档路径必须以 .md 结尾。');
      if (newPath === path) return;
      if (directory && newPath.startsWith(path + '/')) throw new Error('不能把目录移动到它自己的子目录中。');
      await ensureDirectories(dirname(newPath));
      await api('/knowledge/api/move', 'POST', { path, new_path: newPath, expected_hash: hash });
      const currentPath = state.document?.path;
      const mappedPath = currentPath === path ? newPath : currentPath?.startsWith(path + '/') ? newPath + currentPath.slice(path.length) : null;
      state.selected = new Set([...state.selected].map((item) => item === path ? newPath : item.startsWith(path + '/') ? newPath + item.slice(path.length) : item));
      await refreshFiles();
      if (directory) setFolder(newPath);
      if (mappedPath) await openDocument(mappedPath, true);
      notice(`已移动至：${newPath}。请重新入库相关文档。`);
      await refreshStatus();
    }});
  }

  function deletePath(directory = false) {
    if (!can('knowledge.write')) return;
    if (state.saveBusy) { notice('请等待当前文档保存完成。'); return; }
    const path = directory ? state.folder : state.document?.path;
    if (!path || !canEdit(path)) return;
    if (!directory && !canDiscard()) return;
    const hash = directory ? undefined : state.document.hash;
    showAction({ title: directory ? '删除空目录' : '移至回收站', description: directory ? `仅支持删除空目录。\n${path}` : `文档将移入服务器回收站，并从检索库中移除；如需恢复，可按操作手册在服务器上恢复文件。\n${path}`, submit: directory ? '删除空目录' : '移至回收站', danger: true, input: false, handler: async () => {
      await api('/knowledge/api/file', 'DELETE', { path, expected_hash: hash });
      if (!directory) resetDocument();
      else setFolder(dirname(path));
      await Promise.all([refreshFiles(), refreshStatus()]);
      notice(directory ? `空目录已删除：${path}` : `已移至回收站：${path}`);
    }});
  }

  async function indexPaths(paths) {
    if (!can('knowledge.index') || state.indexBusy) return;
    if (state.document && state.dirty && (paths === null || paths.includes(state.document.path))) {
      notice('选中的文档含有未保存修改，请先保存再入库。', true);
      return;
    }
    state.indexBusy = true;
    updateSelection();
    $('index-progress').hidden = false;
    $('index-progress').textContent = '正在分段并计算向量，请保持页面打开…';
    try {
      const data = await api('/knowledge/api/index', 'POST', { paths });
      const indexed = Array.isArray(data.indexed) ? data.indexed.length : data.indexed ?? (paths?.length ?? state.files.length);
      const skipped = Array.isArray(data.skipped) ? data.skipped : [];
      notice(`入库完成：${indexed} 份文档，${data.chunks ?? 0} 个片段。${skipped.length ? `\n以下文档在入库期间发生变化，已跳过，请重新入库：${skipped.join('、')}` : '现在可以在知识问答中使用。'}`, Boolean(skipped.length));
      await Promise.all([refreshFiles(), refreshStatus()]);
      const skippedSet = new Set(skipped);
      if (paths) paths.forEach((path) => { if (!skippedSet.has(path)) state.selected.delete(path); });
      else state.selected = new Set(skipped);
      renderTree();
    } catch (error) { notice(errorText(error), true); }
    finally {
      state.indexBusy = false;
      $('index-progress').hidden = true;
      updateSelection();
    }
  }

  function openImport() {
    if (!can('knowledge.write')) return;
    $('import-prefix').value = state.folder;
    $('import-progress').textContent = '';
    $('import-dialog').showModal();
  }

  async function importFiles(fileList) {
    if (!can('knowledge.write') || state.importBusy || !fileList?.length) return;
    const files = [...fileList].filter((file) => /\.md$/i.test(file.name));
    if (!files.length) { $('import-progress').textContent = '未找到 .md 文件。'; return; }
    let prefix;
    try { prefix = $('import-prefix').value.trim() ? normalizeRelative($('import-prefix').value) : ''; }
    catch (error) { $('import-progress').textContent = errorText(error); return; }
    state.importBusy = true;
    for (const id of ['choose-files', 'choose-folder', 'import-close', 'import-prefix']) $(id).disabled = true;
    $('import-dialog').querySelector('[data-close-dialog]').disabled = true;
    let done = 0;
    const failed = [];
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const relative = file.webkitRelativePath || file.name;
        $('import-progress').textContent = `正在导入 ${i + 1} / ${files.length}：${relative}`;
        try {
          if (file.size > 2_000_000) throw new Error('文件超过 2 MB，请先拆分');
          const path = normalizeRelative(prefix ? `${prefix}/${relative}` : relative);
          const content = await file.text();
          if (content.length > 1_000_000) throw new Error('内容超过 100 万字符，请先拆分');
          await ensureDirectories(dirname(path));
          await api('/knowledge/api/file', 'PUT', { path, content, create: true });
          done++;
        } catch (error) {
          failed.push(`${relative}：${error.status === 409 ? '同名文件已存在，已跳过' : errorText(error)}`);
          if (error.status === 401 || error.status === 0) {
            failed.push(`连接中断，剩余 ${files.length - i - 1} 份文件未导入。`);
            break;
          }
        }
      }
      const summary = `已导入 ${done} / ${files.length} 份 Markdown 文档。${done ? '请选中文档入库后再用于问答。' : ''}`;
      $('import-progress').textContent = summary + (failed.length ? '\n\n' + failed.slice(0, 25).join('\n') + (failed.length > 25 ? '\n（仅显示前 25 条失败记录）' : '') : '');
      notice(summary, Boolean(failed.length));
      if (state.authenticated) await Promise.all([refreshFiles(), refreshStatus()]);
    } catch (error) { notice(errorText(error), true); }
    finally {
      state.importBusy = false;
      for (const id of ['choose-files', 'choose-folder', 'import-close', 'import-prefix']) $(id).disabled = false;
      $('import-dialog').querySelector('[data-close-dialog]').disabled = false;
      $('import-files').value = '';
      $('import-folder').value = '';
    }
  }

  function appendMessage(role, content, data = {}) {
    $('chat-welcome').hidden = true;
    const article = element('article', `chat-message ${role}`);
    const header = element('div', 'message-head');
    header.append(element('span', 'message-avatar', role === 'user' ? '你' : '册'), element('span', '', role === 'user' ? '你' : '设定助手'));
    header.append(element('span', 'message-time', new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })));
    const body = element('div', `message-body ${role === 'assistant' ? 'markdown-body' : ''}`);
    if (role === 'assistant') renderMarkdown(body, content);
    else body.textContent = content;
    article.append(header, body);
    if (Array.isArray(data.warnings) && data.warnings.length) article.append(element('div', 'message-warnings', data.warnings.join('\n')));
    if (Array.isArray(data.read_paths) && data.read_paths.length) article.append(element('div', 'message-read-paths', `本次按需读取资料：${data.read_paths.join('、')}`));
    const sources = Array.isArray(data.sources) ? data.sources : [];
    if (sources.length) {
      addCitations(body, sources);
      const details = element('details', 'source-list');
      details.open = true;
      details.append(element('summary', '', `参考来源 · ${sources.length} 处`));
      const cards = element('div', 'source-cards');
      for (const source of sources) {
        const card = element('button', 'source-card');
        const head = element('span', 'source-card-head');
        head.append(element('span', 'source-id', source.id), element('span', 'source-card-title', source.title || basename(source.path)));
        card.append(head, element('span', 'source-card-path', source.path), element('span', 'source-card-lines', `${sourceRange(source)} · ${String(source.hash || '').slice(0, 12)}`));
        card.title = `${source.path}\n${sourceRange(source)}\nSHA-256: ${source.hash}`;
        card.addEventListener('click', () => showSource(source));
        cards.append(card);
      }
      details.append(cards);
      article.append(details);
    }
    $('chat-messages').append(article);
    scrollChat();
    return article;
  }

  function sourceRange(source) {
    return source.kind === 'image' ? (source.crop ? '图片局部' : '图片') : `第 ${source.start_line}–${source.end_line} 行`;
  }

  function addCitations(container, sources) {
    const byId = new Map(sources.map((source) => [source.id, source]));
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) if (!walker.currentNode.parentElement.closest('pre,code,a,button')) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const matches = [...node.textContent.matchAll(/\[(S\d+)\]/g)];
      if (!matches.length) continue;
      const fragment = document.createDocumentFragment();
      let offset = 0;
      for (const match of matches) {
        fragment.append(document.createTextNode(node.textContent.slice(offset, match.index)));
        const source = byId.get(match[1]);
        if (source) {
          const button = element('button', 'citation-button', match[1]);
          button.title = `${source.path} · ${sourceRange(source)}`;
          button.setAttribute('aria-label', `查看引用 ${match[1]}：${source.path}，${sourceRange(source)}`);
          button.addEventListener('click', () => showSource(source));
          fragment.append(button);
        } else fragment.append(document.createTextNode(match[0]));
        offset = match.index + match[0].length;
      }
      fragment.append(document.createTextNode(node.textContent.slice(offset)));
      node.replaceWith(fragment);
    }
  }

  function scrollChat() { $('chat-messages').scrollTop = $('chat-messages').scrollHeight; }

  function setChatBusy(busy) {
    state.chatBusy = busy;
    const chatAvailable = state.features?.raw_qa?.available !== false;
    $('send-chat').disabled = busy || !chatAvailable;
    $('send-chat').textContent = busy ? '正在思考…' : chatAvailable ? '发送 ↑' : '请配置问答模型';
    $('chat-input').disabled = busy;
    $('allow-read').disabled = busy;
    $('clear-chat').disabled = busy;
    updateSelection();
  }

  async function sendChat(event) {
    event.preventDefault();
    const message = $('chat-input').value.trim();
    if (!message || state.chatBusy) return;
    if (state.features?.raw_qa?.available === false) { notice('问答需要配置 LLM 或 Vision；文档编辑、素材管理与向量检索独立可用。', true); return; }
    if (message.length > 2000) { notice('单次提问请控制在 2000 字以内。', true); return; }
    setChatBusy(true);
    const userMessage = appendMessage('user', message);
    const thinking = element('div', 'thinking', '正在检索资料并核对出处');
    thinking.setAttribute('role', 'status');
    $('chat-messages').append(thinking);
    scrollChat();
    const timer = window.setTimeout(() => { thinking.textContent = '模型仍在处理，原文读取和长回答可能需要数分钟'; }, 45000);
    try {
      const data = await api('/knowledge/api/chat', 'POST', { message, history: state.history.slice(-16), allow_read: $('allow-read').checked });
      thinking.remove();
      appendMessage('assistant', data.answer || '未收到有效回答，请重新提问。', data);
      state.history.push({ role: 'user', content: message }, { role: 'assistant', content: String(data.answer || '').slice(0, 10000) });
      state.history = state.history.slice(-16);
      $('chat-input').value = '';
      $('chat-input').style.height = '';
    } catch (error) {
      thinking.remove();
      userMessage.append(element('div', 'message-warnings', `本次提问未完成：${errorText(error)}\n输入已保留，可稍后重试。`));
      notice(errorText(error), true);
    } finally {
      window.clearTimeout(timer);
      setChatBusy(false);
      if (state.authenticated) $('chat-input').focus();
    }
  }

  function showLines(content, startLine, endLine, excerpt = false) {
    const container = $('source-content');
    container.replaceChildren();
    const lines = String(content).split('\n');
    const start = Math.max(1, Number(startLine) || 1);
    const end = Math.max(start, Number(endLine) || start);
    const startIndex = excerpt ? 0 : Math.max(0, start - 26);
    const endIndex = excerpt ? lines.length : Math.min(lines.length, end + 25);
    if (!excerpt && (startIndex > 0 || endIndex < lines.length)) container.append(element('div', 'source-loading', `显示第 ${startIndex + 1}–${endIndex} 行上下文，高亮部分为引用范围。完整文档可在文档管理中打开。`));
    const fragment = document.createDocumentFragment();
    for (let i = startIndex; i < endIndex; i++) {
      const number = excerpt ? start + i : i + 1;
      const row = element('div', `source-line${number >= start && number <= end ? ' highlight' : ''}`);
      row.append(element('span', 'source-line-number', number), element('span', 'source-line-text', lines[i] || ' '));
      fragment.append(row);
    }
    container.append(fragment);
    const highlighted = container.querySelector('.highlight');
    if (highlighted) container.scrollTop = highlighted.offsetTop - container.offsetTop - 25;
  }

  async function showSource(source) {
    state.source = source;
    const request = ++state.sourceRequest;
    const isImage = source.kind === 'image';
    $('source-title').textContent = `${source.id ? `${source.id} · ` : ''}${source.title || basename(source.path)}`;
    $('source-path').textContent = source.path;
    $('source-meta').replaceChildren(element('span', '', isImage ? `${sourceRange(source)}${source.width && source.height ? ` · ${source.width} × ${source.height}` : ''}` : `引用范围：${sourceRange(source)}`), element('span', '', `SHA-256：${source.hash}`));
    if (isImage && source.crop) $('source-meta').append(element('span', '', `局部范围（0–1000）：${source.crop.join(', ')}`));
    $('source-warning').hidden = true;
    releaseImages($('source-content'));
    $('source-content').classList.toggle('source-image-content', isImage);
    $('source-content').replaceChildren(element('div', 'source-loading', isImage ? '正在核对图片版本并加载预览…' : '正在核对引用版本并读取原文…'));
    $('source-open-document').hidden = isImage;
    $('source-open-document').disabled = false;
    if (!$('source-dialog').open) $('source-dialog').showModal();
    if (isImage) {
      try {
        const blob = await assetBlob(source);
        if (request !== state.sourceRequest) return;
        const img = element('img', 'source-image');
        img.dataset.vaultImage = 'true';
        img.alt = source.title || basename(source.path);
        img.src = URL.createObjectURL(blob);
        $('source-content').replaceChildren(img);
        if (source.text) $('source-content').append(element('p', 'image-description', source.text));
      } catch (error) {
        if (request !== state.sourceRequest) return;
        $('source-warning').textContent = errorText(error);
        $('source-warning').hidden = false;
        $('source-content').replaceChildren(element('div', 'source-loading', '无法显示该版本的图片。'));
        if (source.text) $('source-content').append(element('p', 'image-description', `回答生成时记录的图片说明：${source.text}`));
      }
      return;
    }
    try {
      const params = new URLSearchParams({ path: source.path, hash: source.hash, start_line: source.start_line, end_line: source.end_line });
      const doc = await api(`/knowledge/api/source?${params}`);
      if (request !== state.sourceRequest) return;
      if (doc.hash !== source.hash) {
        $('source-warning').textContent = '当前原文与引用版本不一致。以下保留回答生成时的引用摘录，请重新入库并提问。';
        $('source-warning').hidden = false;
        showLines(source.text, source.start_line, source.end_line, true);
      } else {
        showLines(doc.content, source.start_line, source.end_line);
        if (doc.warning) { $('source-warning').textContent = doc.warning; $('source-warning').hidden = false; }
      }
    } catch (error) {
      if (request !== state.sourceRequest) return;
      $('source-warning').textContent = `${errorText(error)}${source.text ? '\n以下是回答生成时的引用摘录，尚未与当前文件核对。' : ''}`;
      $('source-warning').hidden = false;
      if (source.text) showLines(source.text, source.start_line, source.end_line, true);
      else $('source-content').replaceChildren(element('div', 'source-loading', '无法载入该引用原文。'));
      $('source-open-document').disabled = error.status === 404;
    }
  }

  function bind() {
    $('return-home').addEventListener('click', (event) => {
      event.preventDefault();
      if (state.saveBusy || state.importBusy || state.actionBusy) { notice('操作正在进行，请完成后再返回主页。'); return; }
      if (canDiscard()) window.location.assign($('return-home').href);
    });
    $('login-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (state.sso) { showLogin(); return; }
      $('login-submit').disabled = true;
      $('login-submit').textContent = '正在连接…';
      $('login-error').hidden = true;
      try { await api('/knowledge/api/login', 'POST', { token: $('access-token').value }); await enterWorkspace(); }
      catch (error) { $('login-error').textContent = errorText(error); $('login-error').hidden = false; }
      finally { $('login-submit').disabled = false; $('login-submit').textContent = '进入资料室 →'; }
    });
    $('logout').addEventListener('click', async () => {
      if (!canDiscard()) return;
      if (state.sso && state.logoutUrl) { window.location.assign(state.logoutUrl); return; }
      try {
        await api('/knowledge/api/logout', 'POST');
        resetDocument();
        state.history = [];
        releaseImages($('chat-messages'));
        document.querySelectorAll('.chat-message,.thinking').forEach((message) => message.remove());
        $('chat-welcome').hidden = false;
        state.files = [];
        state.assets = [];
        state.directories = [];
        state.selected.clear();
        $('chat-input').value = '';
        releaseImages($('source-content'));
        $('source-content').replaceChildren();
        renderTree();
        showLogin();
      } catch (error) { notice(errorText(error), true); }
    });
    document.querySelector('.app-header .brand').addEventListener('click', (event) => { event.preventDefault(); switchTab('chat'); });
    $('toggle-sidebar').addEventListener('click', () => {
      const open = $('sidebar').classList.toggle('open');
      $('toggle-sidebar').setAttribute('aria-expanded', String(open));
    });
    document.querySelector('.main-panel').addEventListener('click', closeSidebar);
    document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));
    $('dismiss-notice').addEventListener('click', () => { $('notice').hidden = true; });
    $('refresh-files').addEventListener('click', async () => {
      $('refresh-files').disabled = true;
      try { await refreshFiles(); }
      catch (error) { notice(errorText(error), true); }
      finally { $('refresh-files').disabled = false; }
    });
    $('refresh-status').addEventListener('click', async () => {
      $('refresh-status').disabled = true;
      try { await Promise.all([refreshFiles(), refreshStatus()]); notice('资料列表与模型状态已刷新。'); }
      catch (error) { notice(errorText(error), true); }
      finally { $('refresh-status').disabled = false; }
    });
    $('file-search').addEventListener('input', renderTree);
    $('new-file').addEventListener('click', newFile);
    $('empty-new-file').addEventListener('click', newFile);
    $('new-folder').addEventListener('click', newFolder);
    $('move-file').addEventListener('click', () => movePath(false));
    $('delete-file').addEventListener('click', () => deletePath(false));
    $('folder-rename').addEventListener('click', () => movePath(true));
    $('folder-delete').addEventListener('click', () => deletePath(true));
    $('document-editor').addEventListener('input', () => {
      if (!can('knowledge.write') || !state.document) return;
      setDirty($('document-editor').value !== state.document.content);
      window.clearTimeout(previewTimer);
      previewTimer = window.setTimeout(renderPreview, 160);
    });
    $('document-editor').addEventListener('keydown', (event) => {
      if (event.currentTarget.readOnly) return;
      if (event.key === 'Tab') {
        event.preventDefault();
        const editor = event.currentTarget;
        editor.setRangeText('  ', editor.selectionStart, editor.selectionEnd, 'end');
        editor.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
    document.querySelectorAll('[data-editor-mode]').forEach((button) => button.addEventListener('click', () => {
      $('editor-panes').className = `editor-panes mode-${button.dataset.editorMode}`;
      document.querySelectorAll('[data-editor-mode]').forEach((item) => item.classList.toggle('active', item === button));
      if (button.dataset.editorMode !== 'edit') renderPreview();
    }));
    $('save-file').addEventListener('click', saveDocument);
    $('index-selected').addEventListener('click', () => indexPaths([...state.selected]));
    $('index-all').addEventListener('click', () => indexPaths(null));
    $('index-document').addEventListener('click', () => state.document && indexPaths([state.document.path]));
    $('import-open').addEventListener('click', openImport);
    $('choose-files').addEventListener('click', () => $('import-files').click());
    $('choose-folder').addEventListener('click', () => $('import-folder').click());
    $('import-files').addEventListener('change', (event) => importFiles(event.target.files));
    $('import-folder').addEventListener('change', (event) => importFiles(event.target.files));
    $('import-dialog').addEventListener('cancel', (event) => { if (state.importBusy) event.preventDefault(); });
    $('action-dialog').addEventListener('cancel', (event) => { if (state.actionBusy) event.preventDefault(); });
    $('source-dialog').addEventListener('close', () => {
      state.sourceRequest++;
      state.source = null;
      releaseImages($('source-content'));
      $('source-content').replaceChildren();
    });
    document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => $(button.dataset.closeDialog).close()));
    $('action-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!state.action || state.actionBusy) return;
      state.actionBusy = true;
      updateSelection();
      $('action-submit').disabled = true;
      $('action-value').disabled = true;
      $('action-dialog').querySelectorAll('[data-close-dialog]').forEach((button) => { button.disabled = true; });
      $('action-error').hidden = true;
      const handler = state.action;
      try { await handler($('action-value').value); $('action-dialog').close(); }
      catch (error) {
        $('action-error').textContent = error.status === 409 ? `操作冲突：${errorText(error)}。请刷新文件列表，核对服务器上的最新版本。` : errorText(error);
        $('action-error').hidden = false;
      } finally {
        state.actionBusy = false;
        updateSelection();
        $('action-submit').disabled = false;
        $('action-value').disabled = false;
        $('action-dialog').querySelectorAll('[data-close-dialog]').forEach((button) => { button.disabled = false; });
      }
    });
    $('chat-form').addEventListener('submit', sendChat);
    $('chat-input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('chat-form').requestSubmit(); }
    });
    $('chat-input').addEventListener('input', () => {
      $('chat-input').style.height = 'auto';
      $('chat-input').style.height = `${Math.min($('chat-input').scrollHeight, 160)}px`;
    });
    $('clear-chat').addEventListener('click', () => {
      if (state.chatBusy) return;
      state.history = [];
      releaseImages($('chat-messages'));
      document.querySelectorAll('.chat-message,.thinking').forEach((message) => message.remove());
      $('chat-welcome').hidden = false;
      notice('当前会话已清空。原始文档与知识库索引不受影响。');
    });
    $('source-open-document').addEventListener('click', async () => {
      if (!state.source || state.source.kind === 'image') return;
      if (await openDocument(state.source.path)) $('source-dialog').close();
    });
    window.addEventListener('beforeunload', (event) => {
      if (state.dirty || state.importBusy || state.indexBusy || state.saveBusy) { event.preventDefault(); event.returnValue = ''; }
    });
    document.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && state.tab === 'docs' && state.authenticated) { event.preventDefault(); saveDocument(); }
    });
  }

  async function init() {
    bind();
    try {
      const session = await api('/knowledge/api/session');
      configureSession(session);
      if (session.authenticated) await enterWorkspace(); else showLogin();
    } catch (error) { showLogin(errorText(error)); }
  }
  init();
})();

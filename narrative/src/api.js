let csrf = '';
export function setCsrf(value) { csrf = value; }

export async function api(path, options = {}, refreshed = false) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  let response;
  let data;
  try {
    response = await fetch(path.startsWith('/api/') ? '/narrative' + path : path, {
      credentials: 'same-origin',
      ...options,
      signal: controller.signal,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.method && options.method !== 'GET' ? { 'X-CSRF-Token': csrf } : {}), ...options.headers },
    });
    try { data = await response.json(); } catch { data = null; }
    if (controller.signal.aborted) throw new Error('timeout');
  } catch (cause) {
    const error = new Error(controller.signal.aborted ? '连接服务器超过 45 秒，请检查网络后重试。当前编辑仍保留在本页面。' : '无法连接服务器，请检查网络或工作台服务后重试。当前编辑仍保留在本页面。');
    error.status = 0;
    error.cause = cause;
    throw error;
  } finally { clearTimeout(timeout); }
  if (response.status === 403 && data?.detail === '页面验证已失效，请刷新后重试。' && options.method && options.method !== 'GET' && !refreshed) {
    const config = await api('/api/config');
    if (config.csrf) { setCsrf(config.csrf); return api(path, options, true); }
  }
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : '';
    const error = new Error(detail || (response.status === 401 ? '登录已失效，请返回主工作台重新登录。' : response.status === 403 ? '当前账号没有此操作的权限。' : `请求失败（${response.status}），请稍后重试。`));
    error.status = response.status;
    throw error;
  }
  if (!data) throw new Error('服务器返回了无法识别的内容，请重新登录或稍后重试。');
  return data;
}

export const uid = (prefix) => `${prefix}_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}`;
export const cloneDocument = (d) => ({
  ...d,
  boards: d.boards.map(x => ({ ...x })),
  nodes: d.nodes.map(x => ({ ...x, position: { ...x.position }, dialogue: (x.dialogue || []).map(line => ({ ...line })), prerequisites: (x.prerequisites || []).map(item => ({ ...item })), substory: x.substory ? cloneDocument(x.substory) : null })),
  edges: d.edges.map(x => ({ ...x })),
  characters: d.characters.map(x => ({ ...x })),
  assets: [...d.assets],
});

export function download(content, name, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name.replace(/[<>:"/\\|?*\x00-\x1f]/g, '_');
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

export const assetSize = (asset) => Math.floor((asset.data.split(',')[1]?.replace(/=+$/, '').length || 0) * 3 / 4);

export function scopeAt(document, path) {
  let current = document;
  for (const id of path) { current = current?.nodes.find(n => n.id === id && n.type === 'substory')?.substory; if (!current) return null; }
  return current;
}

export function documentAssetBytes(document) {
  return (document?.assets || []).reduce((total, asset) => total + assetSize(asset), 0) + (document?.nodes || []).reduce((total, node) => total + (node.substory ? documentAssetBytes(node.substory) : 0), 0);
}

export function documentDepth(document) {
  return Math.max(0, ...(document?.nodes || []).filter(n => n.substory).map(n => 1 + documentDepth(n.substory)));
}

export function blankDocument(title) {
  return { schema_version: 3, title: title.trim(), description: '', boards: [{ id: uid('board'), name: '主线大纲' }], nodes: [], edges: [], characters: [], assets: [], start_node_id: '' };
}

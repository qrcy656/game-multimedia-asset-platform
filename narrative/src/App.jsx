import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, blankDocument, cloneDocument, documentAssetBytes, documentDepth, download, scopeAt, setCsrf, uid } from './api';
import { createOutlineText, validateOutline } from './outline';
import { nodeLabel, removeNodes, takeReturnTarget } from './storyModel';
import useStory from './useStory';
import { Field, Icon, IconButton, Modal, types } from './ui';
import Library from './Library';
import Inspector from './Inspector';
import ReadOnlyInspector from './ReadOnlyInspector';
import './admin-scope.css';
import StoryGraph from './StoryGraph';
import SubstoryImport from './SubstoryImport';
import './navigation-v3.css';

function NewProject({ onClose, onCreate, pending }) {
  const [title, setTitle] = useState('');
  const [template, setTemplate] = useState('blank');
  return <Modal title="新建剧情项目" onClose={onClose}><form className="new-project-form" onSubmit={e => { e.preventDefault(); if (title.trim()) onCreate(title.trim(), template); }}><p className="muted">从空白大纲开始，或参考示例梳理游戏主线、支线与人物关系。</p><Field label="故事名称"><input autoFocus aria-label="新项目名称" value={title} maxLength={120} required placeholder="例如：雪境探索 · 第一章" onChange={e => setTitle(e.target.value)}/></Field><div className="template-options"><button type="button" className={template === 'blank' ? 'selected' : ''} onClick={() => setTemplate('blank')}><Icon name="scene" size={28}/><strong>空白大纲</strong><span>从你的游戏设定开始</span>{template === 'blank' && <Icon name="check"/>}</button><button type="button" className={template === 'snow' ? 'selected' : ''} onClick={() => { setTemplate('snow'); if (!title) setTitle('雪境剧情大纲'); }}><Icon name="spark" size={28}/><strong>雪境剧情大纲 · 示例</strong><span>主线、支线与角色示例</span>{template === 'snow' && <Icon name="check"/>}</button></div><div className="modal-actions"><button type="button" className="subtle" disabled={pending} onClick={onClose}>取消</button><button className="primary" disabled={!title.trim() || pending} type="submit">{pending ? '正在创建…' : '创建故事'}<Icon name="chevron" size={16}/></button></div></form></Modal>;
}

function Welcome({ create, importProject }) {
  return <section className="welcome"><div className="welcome-grid"/><div className="welcome-content"><span className="welcome-label"><Icon name="book" size={15}/> FANTASY / NARRATIVE STUDIO</span><h1>剧情项目</h1><p>按章节组织看板，用节点记录场景与对白。<br/>连接前后情节，整理支线、人物动机和故事脉络。</p><div className="welcome-actions"><button className="primary" onClick={create}><Icon name="plus"/>创建第一个故事</button><button className="subtle" onClick={importProject}><Icon name="upload"/>导入 JSON 项目</button></div><div className="welcome-features"><span><Icon name="boards"/>无限画布</span><span><Icon name="branch"/>主线与支线</span><span><Icon name="people"/>角色设定</span><span><Icon name="download"/>大纲导出</span></div></div><div className="welcome-illustration" aria-hidden="true"><div className="demo-story one"><span>01 / 场景</span><strong>风雪中的来信</strong><p>你在门前发现一封没有署名的信。</p></div><div className="demo-story two"><span>02 / 支线</span><strong>调查旧营地</strong><p>整理人物线索　→<br/>衔接主线进展　→</p></div><div className="demo-story three"><span>03 / 结局</span><strong>结局节点</strong></div><svg viewBox="0 0 480 520"><path d="M220 130H290Q320 130 320 160V220M270 310V390Q270 420 240 420H200"/></svg></div></section>;
}

export default function App() {
  const [config, setConfig] = useState(null);
  const [projects, setProjects] = useState([]);
  const [projectScope, setProjectScope] = useState('all');
  const [loading, setLoading] = useState(true);
  const [fatal, setFatal] = useState('');
  const [busy, setBusy] = useState(false);
  const [boardId, setBoardId] = useState('');
  const [selection, setSelection] = useState(null);
  const [prerequisiteHistory, setPrerequisiteHistory] = useState([]);
  const [tab, setTab] = useState('boards');
  const [newProject, setNewProject] = useState(false);
  const [substoryImport, setSubstoryImport] = useState(false);
  const [scopePath, setScopePath] = useState([]);
  const scopePathRef = useRef([]);
  const scopeContextRef = useRef({ id: '', doc: null, root: null });
  const [notice, setNotice] = useState('');
  const [diagnostics, setDiagnostics] = useState(null);
  const [mobilePanel, setMobilePanel] = useState('');
  const instanceRef = useRef(null);
  const importInput = useRef(null);
  const exportMenu = useRef(null);
  const noticeTimer = useRef(null);
  const operationBusy = useRef(false);
  const beginOperation = () => { if (operationBusy.current) return false; operationBusy.current = true; setBusy(true); return true; };
  const finishOperation = () => { operationBusy.current = false; setBusy(false); };
  const toast = useCallback(text => { setNotice(text); clearTimeout(noticeTimer.current); noticeTimer.current = setTimeout(() => setNotice(''), 5500); }, []);
  const story = useStory(env => setProjects(old => old.map(p => p.id === env.id ? { ...p, title: env.document.title, description: env.document.description, version: env.version, updated_at: env.updated_at, node_count: env.document.nodes.length } : p)));
  const readOnly = story.envelope?.read_only === true;
  const rootDocument = story.document;
  const projectUrl = (id, scope = projectScope) => `/api/projects/${encodeURIComponent(id)}${scope === 'all' ? '?scope=all' : ''}`;
  const listingUrl = (scope = projectScope) => `/api/projects?scope=${scope}`;
  const creatorLabel = project => project?.owner_name || project?.owner_id || '其他账号';
  const doc = scopeAt(rootDocument, scopePath) || rootDocument;
  scopePathRef.current = scopePath;
  scopeContextRef.current = { id: `${story.envelope?.id || ''}/${scopePath.join('/')}`, doc, root: rootDocument };
  const scopedEdit = useCallback((recipe, key = '', record = true) => {
    const path = [...scopePathRef.current];
    story.edit(root => { const target = scopeAt(root, path); if (target) recipe(target); }, key ? `${path.join('/')}:${key}` : '', record);
  }, [story.edit]);

  const changeScope = useCallback(path => {
    const next = scopeAt(story.current.current.doc, path);
    if (!next) return;
    const previousPath = scopePathRef.current;
    const returnNode = path.length < previousPath.length && path.every((id, index) => id === previousPath[index]) ? next.nodes.find(n => n.id === previousPath[path.length]) : null;
    scopePathRef.current = path;
    setScopePath(path); setBoardId(returnNode?.board_id || next.boards[0]?.id || ''); setSelection(returnNode ? { kind: 'node', id: returnNode.id } : null); setMobilePanel(''); setPrerequisiteHistory([]);
    scopeContextRef.current = { id: `${story.current.current.id}/${path.join('/')}`, doc: next, root: story.current.current.doc };
    if (returnNode) setTimeout(() => instanceRef.current?.fitView({ nodes: [{ id: returnNode.id }], padding: 0.5, maxZoom: 0.95, duration: 300 }), 80);
  }, []);

  const adopt = useCallback(env => { story.load(env); scopeContextRef.current = { id: `${env?.id || ""}/`, doc: env?.document || null, root: env?.document || null }; scopePathRef.current = []; setScopePath([]); setBoardId(env?.document.boards[0]?.id || ''); setSelection(null); setMobilePanel(''); setPrerequisiteHistory([]); }, [story.load]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [cfg, listing] = await Promise.all([api('/api/config'), api('/api/projects')]);
        if (!alive) return;
        setCsrf(cfg.csrf); setConfig(cfg); setProjects(listing.projects);
        if (listing.projects.length) {
          const env = await api(`/api/projects/${encodeURIComponent(listing.projects[0].id)}`);
          if (alive) adopt(env);
        }
      } catch (e) { if (alive) setFatal(e.message); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; clearTimeout(noticeTimer.current); };
  }, [adopt]);

  useEffect(() => {
    if (!doc) return;
    if (!scopeAt(rootDocument, scopePath)) { changeScope([]); return; }
    if (!doc.boards.some(b => b.id === boardId)) setBoardId(doc.boards[0]?.id || '');
    if (selection && !(selection.kind === 'node' ? doc.nodes : doc.edges).some(x => x.id === selection.id)) setSelection(null);
  }, [doc, boardId, selection, rootDocument, scopePath, changeScope]);

  const beforeSwitch = async () => {
    const ok = await story.save();
    if (!ok) toast('当前修改还没有成功保存，请先处理上方的保存问题，或导出本地 JSON 留存。');
    return ok;
  };
  const switchProject = async (id) => {
    if (!id || id === story.envelope?.id || busy) return;
    if (!beginOperation()) return;
    try { if (await beforeSwitch()) adopt(await api(projectUrl(id))); }
    catch (e) { toast(e.message); }
    finally { finishOperation(); }
  };
  const refreshProjects = async () => { const result = await api(listingUrl()); setProjects(result.projects); };
  const changeProjectScope = async checked => {
    if (!beginOperation()) return;
    try {
      if (!(await beforeSwitch())) return;
      const nextScope = checked ? 'all' : 'mine';
      const listing = await api(listingUrl(nextScope));
      const retained = listing.projects.find(p => p.id === story.current.current.id);
      const selected = retained || listing.projects[0];
      const env = selected ? await api(projectUrl(selected.id, nextScope)) : null;
      setProjectScope(nextScope); setProjects(listing.projects); adopt(env);
      setSubstoryImport(false); setDiagnostics(null);
    } catch (e) { toast(e.message); }
    finally { finishOperation(); }
  };
  const createProject = async (title, template) => {
    if (!beginOperation()) return;
    try {
      if (!(await beforeSwitch())) return;
      const env = await api('/api/projects', { method: 'POST', body: JSON.stringify({ title, template }) });
      adopt(env); setNewProject(false); await refreshProjects(); toast(template === 'snow' ? '大纲示例已创建。按看板查看主线与支线，点击节点编辑情节，点击连接补充剧情前提。' : '故事已创建。添加第一个节点，开始创作。');
    } catch (e) { toast(e.message); }
    finally { finishOperation(); }
  };
  const copyProject = async () => {
    if (!beginOperation()) return;
    try {
      if (!(await beforeSwitch())) return;
      const env = await api(`/api/projects/${encodeURIComponent(story.current.current.id)}/copy`, { method: 'POST' });
      adopt(env); await refreshProjects(); toast('已复制为你自己的项目，原项目保持不变。');
    } catch (e) { toast(e.message); }
    finally { finishOperation(); }
  };
  const deleteProject = async () => {
    if (operationBusy.current || readOnly) return;
    if (!rootDocument || !window.confirm(`永久删除项目「${rootDocument.title}」？所有看板、节点、子剧情、角色及内嵌媒体都会从服务器中删除，无法撤销。请先导出需要保留的内容。`)) return;
    if (!beginOperation()) return;
    try {
      if (!(await beforeSwitch())) return;
      const id = story.current.current.id;
      await api(`/api/projects/${encodeURIComponent(id)}?version=${story.current.current.version}`, { method: 'DELETE' });
      const listing = await api(listingUrl()); setProjects(listing.projects);
      adopt(listing.projects.length ? await api(projectUrl(listing.projects[0].id)) : null);
      toast('项目及其完整素材已删除。');
    } catch (e) { toast(e.message); }
    finally { finishOperation(); }
  };
  const importProject = async file => {
    if (!file) return;
    if (!beginOperation()) return;
    try {
      if (file.size > 32 * 1024 * 1024) throw new Error('JSON 文件超过 32 MB，无法导入。');
      if (!(await beforeSwitch())) return;
      const data = JSON.parse(await file.text());
      const env = await api('/api/import', { method: 'POST', body: JSON.stringify({ document: data }) });
      adopt(env); await refreshProjects(); toast('项目已作为新项目导入，原有项目仍然保留。');
    } catch (e) { toast(e instanceof SyntaxError ? '这个文件不是有效的 JSON，请选择从剧情工作台导出的项目文件。' : e.message); }
    finally { finishOperation(); if (importInput.current) importInput.current.value = ''; }
  };
  const exportJson = () => { if (rootDocument) download(JSON.stringify(rootDocument, null, 2), `${rootDocument.title}.json`); if (exportMenu.current) exportMenu.current.open = false; };
  const exportText = () => {
    if (rootDocument) download(createOutlineText(rootDocument), `${rootDocument.title}-剧情大纲.txt`, 'text/plain;charset=utf-8');
    if (exportMenu.current) exportMenu.current.open = false;
  };
  const navigate = useCallback((board, nodeId) => {
    setBoardId(board); setSelection({ kind: 'node', id: nodeId }); setMobilePanel('inspector');
    setTimeout(() => instanceRef.current?.fitView({ nodes: [{ id: nodeId }], padding: 0.55, maxZoom: 0.95, duration: 350 }), 80);
  }, []);
  const navigatePrerequisite = useCallback((board, nodeId, originNodeId) => {
    const scope = scopeAt(story.current.current.doc, scopePathRef.current);
    const target = scope?.nodes.find(n => n.id === nodeId);
    if (!target) { toast('这处前置剧情已被删除。'); return; }
    if (scope.nodes.some(n => n.id === originNodeId)) setPrerequisiteHistory(old => [...old.slice(-19), originNodeId]);
    navigate(target.board_id, target.id);
  }, [navigate, toast]);
  const returnToDialogue = () => {
    const { history, node: origin } = takeReturnTarget(doc, prerequisiteHistory);
    setPrerequisiteHistory(history);
    if (origin) navigate(origin.board_id, origin.id);
  };
  const returnNode = [...prerequisiteHistory].reverse().map(id => doc?.nodes.find(n => n.id === id)).find(Boolean);
  const addNode = type => {
    if (readOnly) return;
    if (type === 'substory') { if (scopePath.length >= 3) { toast('已到第 3 层子剧情，不能继续嵌套。'); return; } setSubstoryImport(true); return; }
    const id = uid('node'); const rect = window.document.querySelector('.flow-wrap')?.getBoundingClientRect();
    const p = rect && instanceRef.current ? instanceRef.current.screenToFlowPosition({ x: rect.x + rect.width / 2 - 120, y: rect.y + rect.height / 2 - 100 }) : { x: 80, y: 80 };
    const offset = doc.nodes.filter(n => n.board_id === boardId).length % 5 * 24;
    scopedEdit(d => { d.nodes.push({ id, board_id: boardId, type, position: { x: Math.round(p.x + offset), y: Math.round(p.y + offset) }, title: type === 'dialogue' ? '' : `新的${types[type].label}`, body: '', speaker: '', color: types[type].color, asset_id: '', substory: null, dialogue: type === 'dialogue' ? [{ id: uid('line'), speaker: '', body: '' }] : [], prerequisites: [] }); if (!d.start_node_id && type !== 'note') d.start_node_id = id; });
    setSelection({ kind: 'node', id }); setMobilePanel('inspector');
  };
  const connect = connection => {
    if (readOnly) return;
    const id = uid('edge');
    scopedEdit(d => d.edges.push({ id, source: connection.source, target: connection.target, source_handle: connection.sourceHandle || 'right', target_handle: connection.targetHandle || 'left', label: '后续剧情', requirements: '' }));
    setSelection({ kind: 'edge', id }); setMobilePanel('inspector');
  };
  const removeNode = id => {
    if (readOnly) return;
    const node = doc.nodes.find(n => n.id === id);
    if (!node || !window.confirm(`删除节点「${nodeLabel(node, doc.characters)}」及所有与它相连的连接和解锁引用？${node.substory ? '该节点内的完整子剧情和素材也会一并删除。' : ''}可通过撤销恢复。`)) return;
    scopedEdit(d => removeNodes(d, [id])); setSelection(null);
  };
  const removeEdge = id => { if (readOnly) return; scopedEdit(d => { d.edges = d.edges.filter(e => e.id !== id); }); setSelection(null); };
  const openSubstory = useCallback(id => changeScope([...scopePathRef.current, id]), [changeScope]);
  const importSubstory = async ({ projectId, file, newTitle }) => {
    if (readOnly) return;
    if (!beginOperation()) return;
    const expectedProject = story.current.current.id;
    const expectedPath = [...scopePathRef.current];
    const expectedBoard = boardId;
    try {
      if (expectedPath.length >= 3) throw new Error('最多支持 3 层子剧情嵌套。');
      let incoming;
      if (typeof newTitle === 'string') {
        if (!newTitle.trim() || newTitle.trim().length > 120) throw new Error('子剧情名称需要填写，且不超过 120 个字。');
        incoming = (await api('/api/import/prepare', { method: 'POST', body: JSON.stringify({ document: blankDocument(newTitle) }) })).document;
      } else if (file) {
        if (file.size > 32 * 1024 * 1024) throw new Error('JSON 文件超过 32 MB，无法导入。');
        const raw = JSON.parse(await file.text());
        incoming = (await api('/api/import/prepare', { method: 'POST', body: JSON.stringify({ document: raw }) })).document;
      } else {
        if (projectId === expectedProject) throw new Error('不能把当前父项目导入为它自己的子剧情。');
        incoming = (await api(`/api/projects/${encodeURIComponent(projectId)}`)).document;
      }
      if (story.current.current.id !== expectedProject || scopePathRef.current.join('/') !== expectedPath.join('/')) throw new Error('当前项目或子剧情已经切换，本次导入已取消。请在目标看板重试。');
      if (expectedPath.length + 1 + documentDepth(incoming) > 3) throw new Error('来源项目已经包含多层子剧情，导入后会超过 3 层嵌套限制。');
      if (documentAssetBytes(story.current.current.doc) + documentAssetBytes(incoming) > 20 * 1024 * 1024) throw new Error('复制这份大纲后，父项目所有层级的媒体总量会超过 20 MB。请先精简素材。');
      const scope = scopeAt(story.current.current.doc, expectedPath);
      if (!scope?.boards.some(b => b.id === expectedBoard)) throw new Error('目标看板已不存在，请重新选择看板。');
      const id = uid('node');
      const position = { x: 80 + scope.nodes.filter(n => n.board_id === expectedBoard).length % 6 * 40, y: 80 + scope.nodes.filter(n => n.board_id === expectedBoard).length % 6 * 35 };
      scopedEdit(d => d.nodes.push({ id, board_id: expectedBoard, type: 'substory', position, title: incoming.title, body: incoming.description || '', speaker: '', color: types.substory.color, asset_id: '', substory: cloneDocument(incoming), dialogue: [], prerequisites: [] }));
      setSelection({ kind: 'node', id }); setSubstoryImport(false); setMobilePanel('inspector');
      if (await story.save()) toast(typeof newTitle === 'string' ? '空白子剧情已创建并保存。打开节点即可编写内部剧情。' : '已作为独立子剧情副本保存。打开节点可编辑内部内容，来源项目不受影响。');
      else toast('子剧情已导入当前编辑内容，但保存尚未成功。请先处理保存提示。');
    } catch (e) { toast(e instanceof SyntaxError ? '这个文件不是有效的 JSON。' : e.message); }
    finally { finishOperation(); }
  };
  const resolveConflict = async (mode) => {
    if (!beginOperation()) return;
    try {
      if (mode === 'reload') {
        if (!window.confirm('重新载入会放弃当前尚未保存的编辑，改用服务器最新内容。建议先导出本地 JSON。确定继续？')) return;
        adopt(await api(`/api/projects/${encodeURIComponent(story.current.current.id)}`));
      } else {
        const snapshot = { ...story.current.current.doc, title: `${story.current.current.doc.title.slice(0, 108)}（本地修改副本）` };
        adopt(await api('/api/import', { method: 'POST', body: JSON.stringify({ document: snapshot }) }));
      }
      await refreshProjects(); toast(mode === 'reload' ? '已载入服务器最新内容。' : '当前编辑已保存为一个新项目，服务器原项目保留。');
    } catch (e) { toast(e.message); }
    finally { finishOperation(); }
  };
  useEffect(() => {
    const key = e => {
      if (e.key.toLowerCase() === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (/INPUT|TEXTAREA/.test(e.target.tagName)) e.target.blur(); if (!busy && doc && !readOnly) story.save().then(ok => ok && toast('所有修改已保存。')); return; }
      if (newProject || substoryImport || diagnostics || busy || readOnly || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? story.redo() : story.undo(); }
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); story.redo(); }
      else if (e.key === 'Delete' && selection) { e.preventDefault(); selection.kind === 'node' ? removeNode(selection.id) : removeEdge(selection.id); }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  });
  const leave = async e => { e.preventDefault(); if (!beginOperation()) return; try { if (await beforeSwitch()) window.location.assign(config.home_url); } finally { finishOperation(); } };
  const saveLabels = { saved: '已保存至服务器', dirty: '有未保存的修改', saving: '正在保存…', error: '保存失败', conflict: '版本冲突' };
  const breadcrumbs = [{ label: rootDocument?.title || '父项目', path: [] }];
  let breadcrumbDoc = rootDocument;
  for (let index = 0; index < scopePath.length; index++) {
    const node = breadcrumbDoc?.nodes.find(n => n.id === scopePath[index]);
    if (!node?.substory) break;
    breadcrumbs.push({ label: node.title, path: scopePath.slice(0, index + 1) });
    breadcrumbDoc = node.substory;
  }

  if (loading) return <main className="loading-screen"><div className="studio-logo"><Icon name="book" size={26}/></div><h1>剧情设计</h1><p>正在加载剧情项目…</p><span className="spinner"/></main>;
  if (fatal) return <main className="loading-screen"><Icon name="info" size={36}/><h1>暂时无法打开工作台</h1><p>{fatal}</p><button className="primary" onClick={() => location.reload()}>重新尝试</button><a href={config?.home_url || '/'}>返回主工作台</a></main>;
  return <div className={`studio ${mobilePanel ? `show-${mobilePanel}` : ''}`}>
    <header className="app-header" inert={busy ? true : undefined}><a className="home-link" href={config.home_url} onClick={leave} title="返回主工作台"><Icon name="back"/></a><div className="studio-brand"><div className="studio-logo"><Icon name="book" size={22}/></div><span><strong>剧情设计</strong><small>NARRATIVE STUDIO</small></span></div><span className="header-divider"/>
    <div className="project-picker"><select aria-label="当前剧情项目" value={story.envelope?.id || ''} onChange={e => switchProject(e.target.value)} disabled={busy || !projects.length}>{!projects.length && <option value="">尚无项目</option>}{projects.map(p => <option key={p.id} value={p.id}>{p.id === story.envelope?.id ? rootDocument.title : p.title}{projectScope === 'all' ? ` · ${p.owner_id === config.user.id ? '本人' : creatorLabel(p)}` : ''}</option>)}</select><IconButton name="plus" title="新建剧情项目" disabled={busy} onClick={() => setNewProject(true)}/></div>
    {doc && <div className={`save-status status-${story.status}`} role="status"><span/>{readOnly ? '只读查看' : saveLabels[story.status]}</div>}
    <div className="header-actions"><span data-theme-slot=""/>{doc && <><div className="history-buttons"><IconButton name="undo" title="撤销（Ctrl+Z）" disabled={!story.canUndo || busy} onClick={story.undo}/><IconButton name="redo" title="重做（Ctrl+Shift+Z）" disabled={!story.canRedo || busy} onClick={story.redo}/></div><button className="save-button subtle" aria-label="保存" disabled={busy || readOnly || story.status === 'saving'} onClick={() => story.save().then(ok => ok && toast('所有修改已保存。'))}><Icon name="save" size={16}/><span>保存</span></button><button className="subtle substory-import-button" aria-label="添加子剧情" disabled={busy || readOnly || scopePath.length >= 3} onClick={() => setSubstoryImport(true)}><Icon name="substory" size={16}/><span>添加子剧情</span></button><button className="primary outline-check-button" disabled={busy} onClick={() => setDiagnostics(validateOutline(doc))}><Icon name="check" size={16}/>检查结构</button></>}
    <details ref={exportMenu} className="file-menu"><summary aria-label="项目菜单" title="导入、导出与项目管理">⋯</summary><div><button onClick={() => { exportMenu.current.open = false; importInput.current.click(); }}><Icon name="upload"/>导入 JSON 项目</button>{doc && <><button onClick={() => { exportMenu.current.open = false; copyProject(); }}>复制为我的项目</button><button onClick={exportJson}><Icon name="download"/>导出 JSON 项目</button><button onClick={exportText}><Icon name="note"/>导出剧情文稿（TXT）</button><button onClick={() => { exportMenu.current.open = false; setDiagnostics(validateOutline(doc)); }}><Icon name="check"/>检查剧情结构</button><hr/><button className="danger-text" disabled={readOnly} onClick={() => { exportMenu.current.open = false; deleteProject(); }}><Icon name="trash"/>删除当前项目</button></>}</div></details><span className="user-badge" title={config.user.name || config.user.username || '工作台用户'}>{(config.user.name || config.user.username || 'U').slice(0, 1)}</span></div></header>
    {<div className="project-access-bar" aria-label="项目查看范围"><label><input type="checkbox" checked={projectScope === 'all'} disabled={busy} onChange={e => changeProjectScope(e.target.checked)}/>查看其他账号内容</label><span role="status">{readOnly ? `只读查看 · 创建者：${creatorLabel(story.envelope)}` : projectScope === 'all' ? (config.user.admin ? '已显示全部项目 · 管理员可编辑' : '已显示全部项目 · 本人项目可编辑') : '当前仅显示本人项目'}</span></div>}
    <input ref={importInput} type="file" accept="application/json,.json" hidden onChange={e => importProject(e.target.files[0])}/>
    {story.error && <div className={`save-banner ${story.status === 'conflict' ? 'conflict' : ''}`} role="alert"><div><strong>{story.status === 'conflict' ? '此项目已在其他窗口修改，当前编辑尚未保存。' : '自动保存失败，当前编辑仍保留在本页面。'}</strong><span>{story.error.message}</span></div><button onClick={exportJson}>导出本地 JSON</button>{story.status === 'conflict' ? <><button onClick={() => resolveConflict('copy')} disabled={busy}>另存为新项目</button><button onClick={() => resolveConflict('reload')} disabled={busy}>载入服务器版本</button></> : <button onClick={() => story.save()} disabled={busy}>重试保存</button>}</div>}
    {doc && <nav className="scope-breadcrumbs" aria-label="子剧情路径" inert={busy ? true : undefined}><div>{breadcrumbs.map((crumb, index) => <React.Fragment key={crumb.path.join("/") || "root"}>{index > 0 && <Icon name="chevron" size={13}/>}<button disabled={index === breadcrumbs.length - 1} onClick={() => changeScope(crumb.path)} title={crumb.label}>{index === 0 && <Icon name="book" size={14}/>}<span>{crumb.label}</span></button></React.Fragment>)}</div><small>{scopePath.length ? `第 ${scopePath.length} 层子剧情 · ${readOnly ? "只读查看" : "随父项目保存"}` : (readOnly ? "其他账号项目 · 只读查看" : "可编辑项目 · 支持导入子剧情")}</small></nav>}
    {returnNode && <div className="prerequisite-navigation" inert={busy ? true : undefined}><Icon name="link" size={15}/><span>正在查看解锁前置剧情 · 来自 {nodeLabel(returnNode, doc.characters)}</span><button onClick={returnToDialogue}><Icon name="back" size={14}/>返回这段对话</button><IconButton name="close" title="关闭前置剧情返回提示" onClick={() => setPrerequisiteHistory([])}/></div>}
    {doc ? <><nav className="mobile-nav" inert={busy ? true : undefined}><button onClick={() => setMobilePanel(mobilePanel === 'library' ? '' : 'library')}><Icon name="boards"/>项目资源</button><button className={!mobilePanel ? 'active' : ''} onClick={() => setMobilePanel('')}>画布</button><button onClick={() => setMobilePanel(mobilePanel === 'inspector' ? '' : 'inspector')}><Icon name="scene"/>属性</button></nav><div className="editor-layout" inert={busy ? true : undefined}><Library readOnly={readOnly} document={doc} boardId={boardId} setBoardId={setBoardId} tab={tab} setTab={setTab} edit={scopedEdit} currentRef={scopeContextRef} toast={toast} navigate={navigate} setSelection={setSelection}/><StoryGraph readOnly={readOnly} key={scopePath.join("/") || "root"} document={doc} historyDocument={rootDocument} boardId={boardId} selection={selection} setSelection={s => { setSelection(s); if (s) setMobilePanel('inspector'); }} edit={scopedEdit} addHistory={story.addHistory} addNode={addNode} connect={connect} navigate={navigate} navigatePrerequisite={navigatePrerequisite} openSubstory={openSubstory} instanceRef={instanceRef}/>{readOnly ? <ReadOnlyInspector document={doc} selection={selection} setSelection={setSelection} navigate={navigate} navigatePrerequisite={navigatePrerequisite} openSubstory={openSubstory}/> : <Inspector document={doc} selection={selection} setSelection={setSelection} edit={scopedEdit} navigate={navigate} navigatePrerequisite={navigatePrerequisite} removeNode={removeNode} removeEdge={removeEdge} openSubstory={openSubstory} toast={toast}/>}</div></> : <Welcome create={() => setNewProject(true)} importProject={() => importInput.current.click()}/>}
    {newProject && <NewProject pending={busy} onClose={() => { if (!busy) setNewProject(false); }} onCreate={createProject}/>}
    {substoryImport && <SubstoryImport projects={projects} currentProjectId={story.envelope?.id} depth={scopePath.length} pending={busy} onClose={() => { if (!busy) setSubstoryImport(false); }} onImport={importSubstory}/>}
    {diagnostics && <Modal title="剧情结构检查" onClose={() => setDiagnostics(null)}><div className="diagnostics-modal"><p className="muted">草稿可以保留尚未连接的节点。以下提示帮助你检查情节连接与孤立片段。</p><h3>{diagnostics.errors.length ? `${diagnostics.errors.length} 个需要修复的问题` : '没有发现结构问题'}</h3>{diagnostics.errors.length > 0 && <ul className="error-list">{diagnostics.errors.map((s, i) => <li key={i}>{s}</li>)}</ul>}<h3>{diagnostics.warnings.length ? `${diagnostics.warnings.length} 条建议` : '没有额外提醒'}</h3><ul>{diagnostics.warnings.map((s, i) => <li key={i}>{s}</li>)}</ul></div></Modal>}
    {notice && <div className="toast" role="status"><Icon name="info" size={17}/><span>{notice}</span><IconButton name="close" title="关闭提示" onClick={() => setNotice('')}/></div>}
    {busy && <div className="busy-overlay" role="status"><span className="spinner"/><span>正在处理，请稍候…</span></div>}
  </div>;
}

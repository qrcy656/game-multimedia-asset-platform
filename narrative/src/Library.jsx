import React, { useRef, useState } from 'react';
import { assetSize, documentAssetBytes, uid } from './api';
import { Empty, Field, Icon, IconButton } from './ui';
import { clearCharacter, nodeLabel, nodeSearchText, removeNodes } from './storyModel';

const tabs = [['boards', '看板'], ['people', '角色'], ['media', '媒体']];
const nextName = (list, prefix) => { let i = 1; while (list.some(x => x.name === `${prefix} ${i}`)) i++; return `${prefix} ${i}`; };

export default function Library({ document, boardId, setBoardId, tab, setTab, edit: editProject, currentRef, toast, navigate, setSelection, readOnly = false }) {
  const edit = (...args) => { if (!readOnly) editProject(...args); };
  const assetInput = useRef(null);
  const [uploading, setUploading] = useState(false);
  const [search, setSearch] = useState('');
  const commitName = (event, item, collection, limit = 100) => {
    if (readOnly) return;
    const name = event.target.value.trim();
    if (!name || name.length > limit) {
      event.target.value = item.name; toast('名称不能为空，且不能超过 100 个字。'); return;
    }
    if (name !== item.name) edit(d => { d[collection].find(x => x.id === item.id).name = name; });
  };
  const importMedia = async (files) => {
    if (readOnly || !files.length) return;
    setUploading(true);
    const projectId = currentRef.current.id;
    try {
      const allowed = { 'image/png': 'image', 'image/jpeg': 'image', 'image/webp': 'image', 'image/gif': 'image', 'audio/mpeg': 'audio', 'audio/wav': 'audio', 'audio/x-wav': 'audio', 'audio/ogg': 'audio' };
      let total = documentAssetBytes(currentRef.current.root || currentRef.current.doc);
      const incoming = [];
      for (const file of files) {
        let mime = file.type;
        if (!mime && /\.wav$/i.test(file.name)) mime = 'audio/wav';
        if (!mime && /\.mp3$/i.test(file.name)) mime = 'audio/mpeg';
        if (!mime && /\.ogg$/i.test(file.name)) mime = 'audio/ogg';
        if (!allowed[mime]) throw new Error(`「${file.name}」不是支持的图片或音频格式。`);
        if (file.size > 5 * 1024 * 1024) throw new Error(`「${file.name}」超过单个文件 5 MB 的限制。`);
        total += file.size;
        if (total > 20 * 1024 * 1024) throw new Error('项目媒体总量最多 20 MB，请先删除不需要的素材。');
        const raw = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('无法读取文件，请重新选择。')); reader.readAsDataURL(file); });
        mime = mime === 'audio/x-wav' ? 'audio/wav' : mime;
        const data = `data:${mime};base64,${raw.split(',')[1]}`;
        incoming.push({ id: uid('asset'), name: file.name.slice(0, 200), type: allowed[file.type] || allowed[mime], mime, data });
      }
      if (currentRef.current.id !== projectId) throw new Error('项目或子剧情已经切换，本次素材导入已取消。请在目标项目中重新选择文件。');
      if (documentAssetBytes(currentRef.current.root || currentRef.current.doc) + incoming.reduce((s, a) => s + assetSize(a), 0) > 20 * 1024 * 1024) throw new Error('父项目各层级媒体总量已变化，导入后会超过 20 MB，请先删除不需要的素材。');
      if (currentRef.current.doc.assets.length + incoming.length > 100) throw new Error('一个项目最多保存 100 个媒体素材。');
      edit(d => { d.assets.push(...incoming); }); toast(`已导入 ${incoming.length} 个媒体文件，可在节点或角色中引用。`);
    } catch (e) { toast(e.message); }
    finally { setUploading(false); if (assetInput.current) assetInput.current.value = ''; }
  };
  const deleteBoard = (board) => {
    if (readOnly || document.boards.length === 1) return;
    const count = document.nodes.filter(n => n.board_id === board.id).length;
    if (!window.confirm(`删除看板「${board.name}」？这会一起删除其中 ${count} 个节点、相连的连接和引用这些节点的解锁前提。可通过撤销恢复。`)) return;
    const nextBoard = document.boards.find(b => b.id !== board.id).id;
    edit(d => { const ids = new Set(d.nodes.filter(n => n.board_id === board.id).map(n => n.id)); removeNodes(d, ids); d.boards = d.boards.filter(b => b.id !== board.id); });
    if (boardId === board.id) setBoardId(nextBoard); setSelection(null);
  };
  return <aside className="library"><div className="library-tabs" role="tablist" aria-label="项目资源">{tabs.map(([key, label]) => <button role="tab" aria-selected={tab === key} className={tab === key ? 'active' : ''} key={key} onClick={() => setTab(key)}><Icon name={key} size={19}/><span>{label}</span></button>)}</div><div className="library-body">
  {tab === 'boards' && <>
    <div className="section-heading"><h3>主线 / 支线看板</h3><IconButton name="plus" title="新增看板" disabled={readOnly} onClick={() => { const id = uid('board'); edit(d => d.boards.push({ id, name: nextName(d.boards, '新看板') })); setBoardId(id); setSelection(null); }}/></div>
    <div className="board-list">{document.boards.map((b, i) => <div className={`board-row ${boardId === b.id ? 'active' : ''}`} key={b.id}><button className="board-select" onClick={() => { setBoardId(b.id); setSelection(null); }}><span className="chapter-index">{String(i + 1).padStart(2, '0')}</span><span>{b.name}</span><small>{document.nodes.filter(n => n.board_id === b.id).length}</small></button><details className="board-menu"><summary aria-label={`编辑看板 ${b.name}`}>⋯</summary><div><Field label="看板名称"><input readOnly={readOnly} key={`${b.id}:${b.name}`} defaultValue={b.name} maxLength={100} onBlur={e => commitName(e, b, 'boards')} onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); }}/></Field><button className="text-button danger-text" disabled={readOnly || document.boards.length < 2} onClick={() => deleteBoard(b)}>删除看板与节点</button></div></details></div>)}</div>
    <div className="section-heading nodes-heading"><h3>看板节点</h3><span className="counter">{document.nodes.filter(n => n.board_id === boardId).length}</span></div><input className="node-search" aria-label="搜索看板节点" placeholder="搜索剧情、台词或角色…" value={search} onChange={e => setSearch(e.target.value)}/>
    <div className="node-outline">{document.nodes.filter(n => n.board_id === boardId && nodeSearchText(n, document.characters).toLowerCase().includes(search.toLowerCase())).map(n => <button key={n.id} onClick={() => navigate(boardId, n.id)}><span style={{ color: n.color }}><Icon name={n.type} size={15}/></span><span>{nodeLabel(n, document.characters)}</span>{document.start_node_id === n.id && <small>开篇</small>}</button>)}</div>
    <details className="project-settings"><summary>{readOnly ? "项目信息" : "项目设置"}</summary><Field label="项目名称"><input readOnly={readOnly} key={document.title} defaultValue={document.title} maxLength={120} onBlur={e => { const title = e.target.value.trim(); if (!title) { e.target.value = document.title; toast('项目名称不能为空。'); } else if (title !== document.title) edit(d => { d.title = title; }); }} onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); }}/></Field><Field label="简介"><textarea readOnly={readOnly} value={document.description} maxLength={3000} placeholder="故事的主题、目标与设定…" onChange={e => edit(d => { d.description = e.target.value; }, 'description')}/></Field></details>
  </>}
  {tab === 'people' && <>
    <div className="section-heading"><h3>故事角色</h3><IconButton name="plus" title="新增角色" disabled={readOnly} onClick={() => edit(d => d.characters.push({ id: uid('char'), name: nextName(d.characters, '角色'), description: '', color: '#80aaff', asset_id: '' }))}/></div><p className="muted compact">{readOnly ? "查看此项目的人物设定与头像。" : "保存人物设定，并在对话节点中指定说话角色。"}</p>
    {!document.characters.length && <Empty icon="people" title="故事里的他们">{readOnly ? "此项目还没有角色卡片。" : "点击 ＋ 建立角色卡片。"}</Empty>}
    {document.characters.map(c => { const asset = document.assets.find(a => a.id === c.asset_id); return <details className="resource-card character-card" key={c.id} open><summary>{asset ? <img src={asset.data} alt=""/> : <span className="avatar-letter" style={{ background: `${c.color}22`, color: c.color }}>{c.name.slice(0, 1)}</span>}<strong>{c.name}</strong></summary><div className="resource-body"><Field label="角色名称"><input readOnly={readOnly} key={`${c.id}:${c.name}`} defaultValue={c.name} maxLength={100} onBlur={e => commitName(e, c, 'characters')} onKeyDown={e => { if (e.key === 'Enter') e.target.blur(); }}/></Field><Field label="人物设定"><textarea readOnly={readOnly} value={c.description} maxLength={3000} placeholder="身份、性格、动机…" onChange={e => edit(d => { d.characters.find(x => x.id === c.id).description = e.target.value; }, `char:${c.id}:description`)}/></Field><div className="two-fields"><Field label="头像"><select disabled={readOnly} aria-label={`${c.name} 头像`} value={c.asset_id} onChange={e => edit(d => { d.characters.find(x => x.id === c.id).asset_id = e.target.value; })}><option value="">无头像</option>{document.assets.filter(a => a.type === 'image').map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></Field><Field label="颜色"><input disabled={readOnly} type="color" aria-label={`${c.name} 颜色`} value={c.color} onChange={e => edit(d => { d.characters.find(x => x.id === c.id).color = e.target.value; }, `char:${c.id}:color`)}/></Field></div><button className="text-button danger-text" disabled={readOnly} onClick={() => { const count = document.nodes.filter(n => n.speaker === c.id || n.dialogue?.some(line => line.speaker === c.id)).length; if (!window.confirm(`删除角色「${c.name}」？${count ? `引用此角色的 ${count} 个节点将保留全部台词，并改为未指定角色。` : ''}`)) return; edit(d => { d.characters = d.characters.filter(x => x.id !== c.id); clearCharacter(d, c.id); }); }}>删除角色</button></div></details>; })}
  </>}
  {tab === 'media' && <>
    <div className="section-heading"><h3>媒体素材</h3><IconButton name="upload" title="导入图片或音频" disabled={readOnly || uploading} onClick={() => assetInput.current.click()}/></div><input disabled={readOnly} ref={assetInput} type="file" accept="image/png,image/jpeg,image/webp,image/gif,audio/mpeg,audio/wav,audio/ogg,.wav,.mp3,.ogg" multiple hidden onChange={e => importMedia([...e.target.files])}/>
    <div className="media-quota"><span>{(documentAssetBytes(currentRef.current.root || document) / 1048576).toFixed(1)} / 20 MB</span><small>父项目总量 · 单个 ≤ 5 MB</small></div><p className="muted compact">图片：PNG / JPG / WebP / GIF<br/>音频：MP3 / WAV / OGG</p>
    <button className="upload-drop" disabled={readOnly || uploading} onClick={() => assetInput.current.click()}><Icon name="upload" size={22}/><span>{uploading ? '正在读取素材…' : '选择图片或音频'}</span><small>随项目保存、随导出打包</small></button>
    {document.assets.map(a => <article className="asset-card" key={a.id}>{a.type === 'image' ? <img src={a.data} alt={a.name} loading="lazy"/> : <div className="audio-preview"><span>♫</span><audio controls src={a.data} preload="none"/></div>}<div className="asset-meta"><span><strong title={a.name}>{a.name}</strong><small>{(assetSize(a) / 1024).toFixed(0)} KB</small></span><IconButton name="trash" title={`删除素材 ${a.name}`} disabled={readOnly} onClick={() => { const count = document.nodes.filter(n => n.asset_id === a.id).length + document.characters.filter(c => c.asset_id === a.id).length; if (!window.confirm(`删除素材「${a.name}」？${count ? `将同时解除 ${count} 处节点或角色引用。` : ''}素材将从项目及服务器保存内容中移除。`)) return; edit(d => { d.assets = d.assets.filter(x => x.id !== a.id); d.nodes.forEach(n => { if (n.asset_id === a.id) n.asset_id = ''; }); d.characters.forEach(c => { if (c.asset_id === a.id) c.asset_id = ''; }); }); }}/></div></article>)}
  </>}
  </div><footer className="library-footer"><span className="privacy-dot"/> {readOnly ? "其他账号的项目 · 只读查看" : "当前账号私有项目"}</footer></aside>;
}

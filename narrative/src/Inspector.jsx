import React, { useEffect, useState } from 'react';
import { Field, Icon, IconButton, types } from './ui';
import { uid } from './api';
import { nodeLabel } from './storyModel';
import './dialogue-v3.css';

function NodeSelect({ document, value, onChange, label = '目标节点', exclude = [] }) {
  return <select aria-label={label} value={value} onChange={e => onChange(e.target.value)}><option value="">选择目标节点…</option>{document.boards.map(b => <optgroup key={b.id} label={b.name}>{document.nodes.filter(n => n.board_id === b.id && n.type !== 'note' && !exclude.includes(n.id)).map(n => <option key={n.id} value={n.id}>{nodeLabel(n, document.characters)}</option>)}</optgroup>)}</select>;
}

function DialogueEditor({ node, document, edit }) {
  const lines = node.dialogue || [];
  const updateLine = (id, patch, key) => edit(d => {
    const line = d.nodes.find(n => n.id === node.id)?.dialogue.find(item => item.id === id);
    if (line) Object.assign(line, patch);
  }, key ? `node:${node.id}:dialogue:${id}:${key}` : '');
  const moveLine = (id, delta) => edit(d => {
    const rows = d.nodes.find(n => n.id === node.id).dialogue;
    const from = rows.findIndex(row => row.id === id); const to = from + delta;
    if (from < 0 || to < 0 || to >= rows.length) return;
    [rows[from], rows[to]] = [rows[to], rows[from]];
  });
  return <section className="dialogue-editor" aria-label="整段对话">
    <div className="section-heading"><h3>整段对话</h3><span className="counter">{lines.length} / 200 句</span></div>
    <div className="dialogue-lines">{lines.map((line, index) => <div className="dialogue-line" key={line.id}>
      <div className="dialogue-line-heading"><span>第 {index + 1} 句</span><div className="dialogue-line-actions">
        <button type="button" aria-label={`上移第 ${index + 1} 句`} title="上移台词" disabled={index === 0} onClick={() => moveLine(line.id, -1)}>↑</button>
        <button type="button" aria-label={`下移第 ${index + 1} 句`} title="下移台词" disabled={index === lines.length - 1} onClick={() => moveLine(line.id, 1)}>↓</button>
        <IconButton name="trash" title={`删除第 ${index + 1} 句`} disabled={lines.length < 2} onClick={() => edit(d => { const current = d.nodes.find(n => n.id === node.id); if (current.dialogue.length > 1) current.dialogue = current.dialogue.filter(row => row.id !== line.id); })}/>
      </div></div>
      <Field label="说话角色"><select aria-label={`第 ${index + 1} 句说话角色`} value={line.speaker} onChange={e => updateLine(line.id, { speaker: e.target.value })}><option value="">旁白 / 未指定</option>{document.characters.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></Field>
      <Field label="对话内容"><textarea aria-label={`第 ${index + 1} 句对话内容`} className="dialogue-line-body" value={line.body} maxLength={50000} placeholder="写下这句台词…" onChange={e => updateLine(line.id, { body: e.target.value }, 'body')}/></Field>
    </div>)}</div>
    <button className="subtle add-dialogue-line" disabled={lines.length >= 200} onClick={() => edit(d => { const current = d.nodes.find(n => n.id === node.id); if (current.dialogue.length < 200) current.dialogue.push({ id: uid('line'), speaker: '', body: '' }); })}><Icon name="plus" size={15}/>添加一句对话</button>
    <small className="muted">一段对话保存在同一个节点中，按顺序编辑各角色的台词。</small>
  </section>;
}

function Prerequisites({ node, document, edit, navigatePrerequisite }) {
  const [target, setTarget] = useState('');
  const prerequisites = node.prerequisites || [];
  const excluded = [node.id, ...prerequisites.map(item => item.node_id)];
  const candidates = document.nodes.filter(item => item.type !== 'note' && !excluded.includes(item.id));
  const targetAvailable = candidates.some(item => item.id === target);
  useEffect(() => { if (target && !targetAvailable) setTarget(''); }, [target, targetAvailable]);
  const change = (id, patch, key) => edit(d => {
    const current = d.nodes.find(n => n.id === node.id && n.type === 'dialogue');
    if (!current) return;
    const prerequisite = current.prerequisites.find(item => item.node_id === id);
    if (patch.node_id && (!d.nodes.some(n => n.id === patch.node_id && n.type !== 'note' && n.id !== current.id) || current.prerequisites.some(item => item !== prerequisite && item.node_id === patch.node_id))) return;
    if (prerequisite) Object.assign(prerequisite, patch);
  }, key ? `node:${node.id}:prerequisite:${id}:${key}` : '');
  const addPrerequisite = () => {
    if (!targetAvailable) return;
    edit(d => {
      const current = d.nodes.find(n => n.id === node.id && n.type === 'dialogue');
      const destination = d.nodes.find(n => n.id === target && n.type !== 'note' && n.id !== node.id);
      if (!current || !destination || current.prerequisites.length >= 50 || current.prerequisites.some(item => item.node_id === target)) return;
      current.prerequisites.push({ node_id: target, description: '' });
    });
    setTarget('');
  };
  return <section className="dialogue-prerequisites" aria-label="对话解锁前提">
    <div className="section-heading"><h3>解锁前提</h3><span className="counter">{prerequisites.length} / 50</span></div>
    <p className="muted compact">关联达成条件的剧情节点，点击即可回到那段情节查看和编辑。</p>
    {prerequisites.map((item, index) => {
      const destination = document.nodes.find(n => n.id === item.node_id);
      const board = document.boards.find(b => b.id === destination?.board_id);
      return <div className="prerequisite-card" key={item.node_id}>
        <div className="prerequisite-heading"><span>前提 {index + 1}</span><IconButton name="close" title={`移除前提 ${index + 1}`} onClick={() => edit(d => { const current = d.nodes.find(n => n.id === node.id); current.prerequisites = current.prerequisites.filter(p => p.node_id !== item.node_id); })}/></div>
        <Field label="前置剧情"><NodeSelect document={document} value={item.node_id} exclude={excluded.filter(id => id !== item.node_id)} label={`前提 ${index + 1} 的剧情节点`} onChange={value => { if (value) change(item.node_id, { node_id: value }); }}/></Field>
        <Field label="解锁说明"><textarea aria-label={`前提 ${index + 1} 解锁说明`} value={item.description} maxLength={4000} placeholder="例如：在这里得知信件的来源，才能谈及写信人。" onChange={e => change(item.node_id, { description: e.target.value }, 'description')}/></Field>
        <button className="subtle prerequisite-jump" disabled={!destination} onClick={() => navigatePrerequisite(destination.board_id, destination.id, node.id)}><Icon name="back" size={15}/><span>前往前置剧情</span></button>
        {board && <small className="muted prerequisite-location">{board.name} · {nodeLabel(destination, document.characters)}</small>}
      </div>;
    })}
    {candidates.length && prerequisites.length < 50 ? <div className="add-prerequisite"><NodeSelect document={document} value={target} exclude={excluded} label="添加解锁前提的节点" onChange={setTarget}/><button className="subtle" disabled={!targetAvailable} onClick={addPrerequisite}><Icon name="plus" size={15}/>添加解锁前提</button></div> : !prerequisites.length && <p className="muted compact">先建立一个场景、对话或分支节点，再将它设为前置剧情。</p>}
  </section>;
}

export default function Inspector({ document, selection, setSelection, edit, navigate, navigatePrerequisite, removeNode, removeEdge, openSubstory }) {
  const [target, setTarget] = useState('');
  useEffect(() => setTarget(''), [selection?.id]);
  const node = selection?.kind === 'node' ? document.nodes.find(n => n.id === selection.id) : null;
  const edge = selection?.kind === 'edge' ? document.edges.find(n => n.id === selection.id) : null;
  const targetAvailable = document.nodes.some(n => n.id === target && n.type !== 'note');
  useEffect(() => { if (target && !targetAvailable) setTarget(''); }, [target, targetAvailable]);
  const updateNode = (patch, key) => edit(d => Object.assign(d.nodes.find(n => n.id === node.id), patch), key ? `node:${node.id}:${key}` : '');
  const updateEdge = (patch, key) => edit(d => {
    const current = d.edges.find(n => n.id === edge.id);
    if (!current || (patch.target && !d.nodes.some(n => n.id === patch.target && n.type !== 'note'))) return;
    Object.assign(current, patch);
  }, key ? `edge:${edge.id}:${key}` : '');
  const outgoing = node ? document.edges.filter(e => e.source === node.id) : [];
  const source = edge && document.nodes.find(n => n.id === edge.source);
  const edgeTarget = edge && document.nodes.find(n => n.id === edge.target);
  const addConnection = () => {
    if (!node || node.type === 'note' || !targetAvailable) return;
    const id = uid('edge'); let created = false;
    edit(d => {
      if (!d.nodes.some(n => n.id === node.id && n.type !== 'note') || !d.nodes.some(n => n.id === target && n.type !== 'note')) return;
      d.edges.push({ id, source: node.id, target, source_handle: 'right', target_handle: 'left', label: '后续剧情', requirements: '' });
      created = true;
    });
    if (created) setSelection({ kind: 'edge', id });
  };
  const reorder = (id, delta) => edit(d => {
    const sourceEdges = d.edges.filter(e => e.source === node.id);
    const from = sourceEdges.findIndex(e => e.id === id); const to = from + delta;
    if (to < 0 || to >= sourceEdges.length) return;
    [sourceEdges[from], sourceEdges[to]] = [sourceEdges[to], sourceEdges[from]];
    let index = 0; d.edges = d.edges.map(e => e.source === node.id ? sourceEdges[index++] : e);
  });
  return <aside className="inspector"><header className="panel-heading"><span><Icon name={edge ? 'link' : node?.type || 'scene'}/>{edge ? '连接说明' : node ? '节点内容' : '剧情概览'}</span>{selection && <IconButton name="close" title="取消选择" onClick={() => setSelection(null)}/>}</header><div className="inspector-body">
  {node ? <>
    <div className="selected-kind" style={{ color: node.color }}><Icon name={node.type}/>{types[node.type].label}<span>{document.start_node_id === node.id ? '剧情开篇' : ''}</span></div>
    {node.substory && <div className="substory-summary"><strong>{node.substory.title}</strong><span>{node.substory.boards.length} 个看板 · {node.substory.nodes.length} 个顶层节点</span><p>这个子剧情独立保存在当前父项目中。内部内容会随父项目一起保存，可随时展开编辑。</p><button className="primary" onClick={() => openSubstory(node.id)}><Icon name="substory" size={15}/>打开子剧情<Icon name="chevron" size={14}/></button></div>}
    {node.type === 'dialogue' ? <><DialogueEditor key={`dialogue:${node.id}`} node={node} document={document} edit={edit}/><Prerequisites key={`prerequisites:${node.id}`} node={node} document={document} edit={edit} navigatePrerequisite={navigatePrerequisite || navigate}/></> : <>
      <Field label="节点标题"><input aria-label="节点标题" value={node.title} maxLength={200} placeholder="给这个情节起个名字" onChange={e => updateNode({ title: e.target.value }, 'title')}/></Field>
      <Field label={node.type === 'note' ? '笔记内容' : node.type === 'branch' ? '分支设计' : '剧情正文'} hint={node.type === 'branch' ? '记录分流缘由、各条路线的目标与汇合位置。' : '记录情节、人物动机、对白或设计备注。'}><textarea aria-label="剧情正文" className="body-editor" value={node.body} maxLength={50000} placeholder="这里发生了什么？" onChange={e => updateNode({ body: e.target.value }, 'body')}/></Field>
    </>}
    <details className={`node-support-tools ${node.type === 'dialogue' ? 'dialogue-tools' : ''}`} key={`tools:${node.id}`} open={node.type !== 'dialogue'}><summary>布局、媒体与连线</summary><div className="node-support-tools-body">
    <div className="two-fields"><Field label="所属看板"><select aria-label="节点所属看板" value={node.board_id} onChange={e => { const board = e.target.value; updateNode({ board_id: board }); navigate(board, node.id); }}>{document.boards.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select></Field><Field label="节点颜色"><input aria-label="节点颜色" type="color" value={node.color} onChange={e => updateNode({ color: e.target.value }, 'color')}/></Field></div>
    {node.type !== 'note' && <>
      <Field label="参考图片 / 音频"><select aria-label="节点媒体" value={node.asset_id} onChange={e => updateNode({ asset_id: e.target.value })}><option value="">不使用媒体</option>{document.assets.map(a => <option key={a.id} value={a.id}>{a.type === 'image' ? '图片' : '音频'} · {a.name}</option>)}</select></Field>
      {(() => { const asset = document.assets.find(a => a.id === node.asset_id); return asset?.type === 'audio' ? <audio className="inspector-audio" controls src={asset.data} preload="metadata"/> : asset?.type === 'image' ? <img className="inspector-image" src={asset.data} alt={asset.name}/> : null; })()}
      <div className="inline-actions"><button className="subtle" onClick={() => edit(d => { d.start_node_id = document.start_node_id === node.id ? '' : node.id; })}><Icon name="book" size={14}/>{document.start_node_id === node.id ? '取消开篇标记' : '标为剧情开篇'}</button><small className="muted">可选的梳理标记</small></div>
      <section className="connections-list"><div className="section-heading"><h3>{node.type === 'branch' ? '分支路线' : '后续剧情'}</h3><span className="counter">{outgoing.length}</span></div>{node.type === 'branch' && <p className="muted compact">将主线与支线逐条展开。上下排序用于组织路线的显示顺序。</p>}
      {outgoing.map((e, i) => { const n = document.nodes.find(x => x.id === e.target); return <div className="connection-row" key={e.id}><button className="connection-main" onClick={() => setSelection({ kind: 'edge', id: e.id })}><span className="connection-index">{i + 1}</span><span><strong>{e.label || '后续剧情'}</strong><small>{nodeLabel(n, document.characters)}{n?.board_id !== node.board_id ? ' · 跨看板' : ''}</small></span><Icon name="chevron" size={14}/></button><div className="reorder-buttons"><button aria-label={`上移连接 ${i + 1}`} disabled={i === 0} onClick={() => reorder(e.id, -1)}>↑</button><button aria-label={`下移连接 ${i + 1}`} disabled={i === outgoing.length - 1} onClick={() => reorder(e.id, 1)}>↓</button></div></div>; })}
      <div className="add-connection"><NodeSelect document={document} value={target} onChange={setTarget}/><button className="subtle" disabled={!targetAvailable} onClick={addConnection}>＋ 连接到此节点</button></div><small className="muted">拖动节点上、下、左、右的圆点可以连线。上方菜单可连接其他主线、支线或章节看板。</small></section>
    </>}
    </div></details>
    <button className="danger delete-wide" onClick={() => removeNode(node.id)}><Icon name="trash" size={16}/>删除这个节点</button>
  </> : edge ? <>
    <div className="edge-route"><button disabled={!source} onClick={() => navigate(source.board_id, source.id)}>{nodeLabel(source, document.characters)}</button><Icon name="chevron"/><button disabled={!edgeTarget} onClick={() => navigate(edgeTarget.board_id, edgeTarget.id)}>{nodeLabel(edgeTarget, document.characters)}</button></div>
    <Field label="连接标题" hint="例如：主线推进、调查旧营地、返回主线。"><input aria-label="连接标题" maxLength={500} value={edge.label} placeholder="这条路线怎样展开？" onChange={e => updateEdge({ label: e.target.value }, 'label')}/></Field>
    <Field label="目标节点"><NodeSelect document={document} value={edge.target} onChange={value => { if (value) updateEdge({ target: value }); }}/></Field>
    {source?.board_id !== edgeTarget?.board_id && <div className="info-box"><Icon name="boards" size={16}/>这条连接通向「{document.boards.find(b => b.id === edgeTarget?.board_id)?.name}」看板。</div>}
    <Field label="剧情前提 / 触发说明" hint="用文字说明该路线出现的背景、前置情节或设计意图。"><textarea aria-label="剧情前提 / 触发说明" className="body-editor" value={edge.requirements || ''} maxLength={4000} placeholder="例如：角色在废弃营地发现线索后，可引出这条调查支线。" onChange={e => updateEdge({ requirements: e.target.value }, 'requirements')}/></Field>
    <button className="danger delete-wide" onClick={() => removeEdge(edge.id)}><Icon name="trash" size={16}/>删除这条连接</button>
  </> : <>
    <div className="overview-mark"><Icon name="book" size={28}/><span>STORY OUTLINE</span></div><h2 className="overview-title">{document.title}</h2><p className="overview-description">{document.description || '整理游戏主线与支线，记录每个情节的来龙去脉。'}</p>
    <div className="overview-stats"><div><b>{document.nodes.filter(n => n.type !== 'note').length}</b><span>剧情节点</span></div><div><b>{document.edges.length}</b><span>剧情连接</span></div><div><b>{document.boards.length}</b><span>看板</span></div></div>
    <div className="inspector-help"><h3>梳理你的故事</h3><p><b>01</b> 按章节、主线或支线建立看板</p><p><b>02</b> 用节点记录场景、对白与转折</p><p><b>03</b> 连接前后情节，写下分支前提</p><p><b>04</b> 补充角色与参考素材，完善大纲</p></div>
    <p className="muted">点击节点或连接，在这里编辑内容。开篇标记用于定位故事的开头，也可以留空。</p>
    <div className="keyboard-help"><span><kbd>Ctrl</kbd> <kbd>S</kbd> 保存</span><span><kbd>Ctrl</kbd> <kbd>Z</kbd> 撤销</span><span><kbd>Delete</kbd> 删除所选项</span></div>
    <div className="source-note">画布由开源 React Flow 驱动。项目保存在服务器，并按工作台账号隔离。 <a href="/THIRD_PARTY_LICENSES.txt" target="_blank" rel="noreferrer">开源许可证</a></div>
  </>}
  </div></aside>;
}

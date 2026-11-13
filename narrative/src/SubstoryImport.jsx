import React, { useRef, useState } from 'react';
import { Field, Icon, Modal } from './ui';

export default function SubstoryImport({ projects, currentProjectId, depth, onClose, onImport, pending }) {
  const available = projects.filter(p => p.id !== currentProjectId);
  const [mode, setMode] = useState('new');
  const [projectId, setProjectId] = useState(available[0]?.id || '');
  const [newTitle, setNewTitle] = useState('');
  const [file, setFile] = useState(null);
  const fileInput = useRef(null);
  const source = available.find(p => p.id === projectId);
  const canImport = mode === 'new' ? !!newTitle.trim() : mode === 'project' ? !!projectId : !!file;
  const submit = e => {
    e.preventDefault();
    if (!canImport || pending) return;
    onImport(mode === 'new' ? { newTitle: newTitle.trim() } : mode === 'project' ? { projectId } : { file });
  };
  return <Modal title="添加子剧情" onClose={onClose}><form className="new-project-form" onSubmit={submit}>
    <p className="muted">把一段完整剧情放入当前看板，作为一个可展开编辑的节点。可以从空白开始，也可以导入已有大纲的独立副本。</p>
    <div className="import-source-tabs">
      <button type="button" className={mode === 'new' ? 'active' : ''} disabled={pending} onClick={() => setMode('new')}><Icon name="plus"/>新建项目</button>
      <button type="button" className={mode === 'project' ? 'active' : ''} disabled={pending} onClick={() => setMode('project')}><Icon name="book"/>我的已有项目</button>
      <button type="button" className={mode === 'file' ? 'active' : ''} disabled={pending} onClick={() => setMode('file')}><Icon name="upload"/>JSON 文件</button>
    </div>
    {mode === 'new' ? <Field label="子剧情名称" hint="在当前项目内新建空白子剧情，进入节点即可开始编写。"><input aria-label="新建子剧情名称" autoFocus disabled={pending} value={newTitle} maxLength={120} placeholder="例如：冰原狼的委托" onChange={e => setNewTitle(e.target.value)}/></Field> : mode === 'project' ? <>
      <Field label="来源项目" hint="这里只列出当前账号的项目。导入后修改双方都不会影响对方。"><select aria-label="子剧情来源项目" disabled={pending || !available.length} value={projectId} onChange={e => setProjectId(e.target.value)}>{!available.length && <option value="">暂无其他项目，请新建或导入 JSON 文件</option>}{available.map(p => <option key={p.id} value={p.id}>{p.title}</option>)}</select></Field>
      {source && <div className="source-project-preview"><strong>{source.title}</strong><span>{source.node_count} 个顶层节点</span><p>{source.description || '这个项目暂未填写简介。'}</p></div>}
    </> : <Field label="选择剧情项目 JSON" hint="可导入自己导出的文件，或另一位作者提供的项目文件。"><button type="button" className="upload-drop file-pick" disabled={pending} onClick={() => fileInput.current?.click()}><Icon name="upload" size={24}/><span>{file?.name || '选择 JSON 文件'}</span><small>最大 32 MB，包含的图片和音频会一起复制</small></button><input ref={fileInput} type="file" accept="application/json,.json" disabled={pending} onChange={e => setFile(e.target.files[0] || null)}/></Field>}
    <div className="info-box"><Icon name="boards" size={16}/><span>放入当前看板，作为第 {depth + 1} 层子剧情。最多支持 3 层嵌套；打开节点可编辑内部内容，通过面包屑返回上层。</span></div>
    <div className="modal-actions"><button type="button" className="subtle" disabled={pending} onClick={onClose}>取消</button><button className="primary" disabled={!canImport || pending} type="submit">{pending ? '正在处理…' : mode === 'new' ? '创建子剧情' : '导入子剧情'}<Icon name="chevron" size={16}/></button></div>
  </form></Modal>;
}

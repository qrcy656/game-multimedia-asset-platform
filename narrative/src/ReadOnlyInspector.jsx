import React from 'react';
import { Icon, IconButton, types } from './ui';
import { nodeLabel } from './storyModel';
import './read-only-panels.css';

function Content({ label, children, empty = '未填写' }) {
  return <section className="read-only-field"><h3>{label}</h3><div className="read-only-copy">{children || <span className="muted">{empty}</span>}</div></section>;
}

function Media({ asset }) {
  if (!asset) return null;
  return <section className="read-only-field"><h3>参考媒体 · {asset.name}</h3>{asset.type === 'audio' ? <audio className="inspector-audio" controls src={asset.data} preload="none"/> : <img className="inspector-image" src={asset.data} alt={asset.name}/>}</section>;
}

export default function ReadOnlyInspector({ document, selection, setSelection, navigate, navigatePrerequisite = navigate, openSubstory }) {
  const node = selection?.kind === 'node' ? document.nodes.find(n => n.id === selection.id) : null;
  const edge = selection?.kind === 'edge' ? document.edges.find(e => e.id === selection.id) : null;
  const outgoing = node ? document.edges.filter(e => e.source === node.id) : [];
  const source = edge && document.nodes.find(n => n.id === edge.source);
  const target = edge && document.nodes.find(n => n.id === edge.target);
  return <aside className="inspector read-only-inspector" aria-label="剧情内容只读查看">
    <header className="panel-heading"><span><Icon name={edge ? 'link' : node?.type || 'scene'}/>{edge ? '连接说明' : node ? '节点内容' : '剧情概览'} · 只读</span>{selection && <IconButton name="close" title="取消选择" onClick={() => setSelection(null)}/>}</header>
    <div className="inspector-body">
      {node ? <>
        <div className="selected-kind" style={{ color: node.color }}><Icon name={node.type}/>{types[node.type]?.label || node.type}<span>{document.start_node_id === node.id ? '剧情开篇' : ''}</span></div>
        <Content label="所属看板">{document.boards.find(b => b.id === node.board_id)?.name}</Content>
        {node.type === 'dialogue' ? <section className="read-only-field" aria-label="整段对话只读内容"><h3>整段对话 · {node.dialogue?.length || 0} 句</h3>{node.dialogue?.length ? node.dialogue.map((line, index) => <div className="read-only-dialogue" key={line.id}><strong>{index + 1}. {document.characters.find(c => c.id === line.speaker)?.name || '旁白 / 未指定'}</strong><p className="read-only-copy">{line.body || <span className="muted">未填写台词</span>}</p></div>) : <p className="muted">尚未添加台词。</p>}</section> : <>
          <Content label="节点标题">{node.title}</Content>
          <Content label={node.type === 'note' ? '笔记内容' : node.type === 'branch' ? '分支设计' : '剧情正文'}>{node.body}</Content>
        </>}
        {node.speaker && <Content label="说话角色">{document.characters.find(c => c.id === node.speaker)?.name || '未指定角色'}</Content>}
        {!!node.prerequisites?.length && <section className="read-only-field" aria-label="对话解锁前提只读内容"><h3>解锁前提</h3>{node.prerequisites.map((item, index) => {
          const destination = document.nodes.find(n => n.id === item.node_id);
          const board = document.boards.find(b => b.id === destination?.board_id);
          return <div className="read-only-prerequisite" key={item.node_id}><strong>前提 {index + 1}</strong><p className="read-only-copy">{item.description || '未填写解锁说明'}</p><button className="subtle prerequisite-jump" disabled={!destination} onClick={() => navigatePrerequisite(destination.board_id, destination.id, node.id)}><Icon name="back" size={15}/><span>前往前置剧情</span></button><small>{board?.name} · {nodeLabel(destination, document.characters)}</small></div>;
        })}</section>}
        {node.substory && <section className="substory-summary"><strong>{node.substory.title}</strong><span>{node.substory.boards.length} 个看板 · {node.substory.nodes.length} 个顶层节点</span><p className="read-only-copy">{node.substory.description || '子剧情随父项目保存，可展开查看内部内容。'}</p><button className="primary" onClick={() => openSubstory(node.id)}><Icon name="substory" size={15}/>打开子剧情<Icon name="chevron" size={14}/></button></section>}
        <Media asset={document.assets.find(a => a.id === node.asset_id)}/>
        <section className="read-only-field"><h3>{node.type === 'branch' ? '分支路线' : '后续剧情'} · {outgoing.length}</h3>{outgoing.map((item, index) => {
          const destination = document.nodes.find(n => n.id === item.target);
          return <button className="read-only-route" key={item.id} onClick={() => setSelection({ kind: 'edge', id: item.id })}><span>{index + 1}. {item.label || '后续剧情'}</span><small>{nodeLabel(destination, document.characters)}</small><Icon name="chevron" size={14}/></button>;
        })}{!outgoing.length && <p className="muted">没有后续连接。</p>}</section>
      </> : edge ? <>
        <div className="edge-route"><button disabled={!source} onClick={() => navigate(source.board_id, source.id)}>{nodeLabel(source, document.characters)}</button><Icon name="chevron"/><button disabled={!target} onClick={() => navigate(target.board_id, target.id)}>{nodeLabel(target, document.characters)}</button></div>
        <Content label="连接标题">{edge.label}</Content>
        <Content label="剧情前提 / 触发说明">{edge.requirements}</Content>
        <Content label="目标看板">{document.boards.find(b => b.id === target?.board_id)?.name}</Content>
      </> : <>
        <div className="overview-mark"><Icon name="book" size={28}/><span>STORY OUTLINE</span></div>
        <h2 className="overview-title">{document.title}</h2><p className="overview-description read-only-copy">{document.description || '此项目尚未填写简介。'}</p>
        <div className="overview-stats"><div><b>{document.nodes.filter(n => n.type !== 'note').length}</b><span>剧情节点</span></div><div><b>{document.edges.length}</b><span>剧情连接</span></div><div><b>{document.boards.length}</b><span>看板</span></div></div>
        <p className="muted">正在只读查看其他账号的项目。选择节点或连线可查看全文，打开子剧情或点击解锁前提可继续浏览。</p>
      </>}
    </div>
  </aside>;
}

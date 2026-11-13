import React, { memo, useMemo, useRef, useState, useEffect } from 'react';
import { ReactFlow, Background, Controls, Handle, MiniMap, ConnectionMode, MarkerType } from '@xyflow/react';
import { Icon, types } from './ui';
import { edgeHandles, nodeLabel, NODE_PORTS } from './storyModel.js';
import './graph-v3.css';

const portNames = { left: '左', right: '右', top: '上', bottom: '下' };

function NodePorts() {
  return NODE_PORTS.map(port => <Handle key={port} id={port} type="source" position={port} className="story-port" aria-label={`${portNames[port]}方连接点`} title={`${portNames[port]}方连接点：拖出或接收剧情连接`}/>);
}

const StoryNode = memo(function StoryNode({ data, selected }) {
  const { node, start, character, characters, asset, outCount, openSubstory, prerequisites, navigatePrerequisite } = data;
  return <article className={`story-node node-${node.type} ${selected ? 'is-selected' : ''}`} style={{ '--node-color': node.color || types[node.type].color }}>
    {node.type !== 'note' && <NodePorts/>}
    <div className="node-title"><span className="node-kind"><Icon name={node.type} size={15}/>{types[node.type].label}</span>{start && <span className="start-pill">开篇</span>}</div>
    {asset?.type === 'image' && <img className="node-image" src={asset.data} alt="" draggable={false}/>}
    {node.type === 'dialogue' ? <div className="node-dialogue nodrag nowheel" aria-label="完整对话片段">{node.dialogue?.length ? node.dialogue.map(line => <div className="node-dialogue-line" key={line.id}><span className="node-speaker">{characters.find(c => c.id === line.speaker)?.name || '未指定角色'}</span><p>{line.body || '待填写对话内容…'}</p></div>) : <p className="node-dialogue-empty">点击节点，在右侧添加这一段对话。</p>}</div> : <div className="node-content"><strong>{nodeLabel(node)}</strong>{character && <span className="node-speaker">{character.name}</span>}<p>{node.body || (node.type === 'branch' ? '记录主线与支线如何展开、转折或汇合' : node.type === 'note' ? '记录灵感、待办与叙事笔记…' : '点击节点，在右侧开始编写…')}</p></div>}
    {prerequisites.length > 0 && <div className="node-prerequisites"><small>解锁前提 · 点击定位</small>{prerequisites.map(({ node_id, description, target }) => <button key={node_id} type="button" className="node-prerequisite nodrag nopan" disabled={!target} title={target ? `定位到「${nodeLabel(target, characters)}」${description ? `：${description}` : ''}` : '前置节点不存在'} aria-label={`定位前置节点：${nodeLabel(target, characters)}`} onClick={event => { event.stopPropagation(); if (target) navigatePrerequisite(target.board_id, target.id, node.id); }}><Icon name="back" size={12}/><span>{description || nodeLabel(target, characters)}</span></button>)}</div>}
    {node.substory && <button className="node-substory-open nodrag nopan" onClick={e => { e.stopPropagation(); openSubstory(node.id); }}><Icon name="substory" size={14}/><span>打开子剧情 · {node.substory.nodes.length} 个节点</span><Icon name="chevron" size={13}/></button>}
    <div className="node-footer"><span>{node.type === 'note' ? '设计备注' : `${outCount} 条剧情连接`}</span>{asset?.type === 'audio' && <span>♫ 音频</span>}</div>
  </article>;
});
const PortalNode = memo(function PortalNode({ data }) { return <><Handle id={data.targetHandle} type="source" position={data.targetHandle} isConnectable={false}/><button className="portal-node nodrag" onClick={() => data.navigate(data.target.board_id, data.target.id)}><Icon name="boards"/><span><small>前往 {data.board}</small><strong>{nodeLabel(data.target, data.characters)}</strong></span><Icon name="chevron"/></button></>; });
const nodeTypes = { story: StoryNode, portal: PortalNode };

export default function StoryGraph({ readOnly = false, document, historyDocument, boardId, selection, setSelection, edit, addHistory, addNode, connect, navigate, navigatePrerequisite = navigate, openSubstory, instanceRef }) {
  const [theme, setTheme] = useState(() => window.FantasyTheme?.getTheme() || 'dark');
  useEffect(() => { const update = event => setTheme(event.detail.theme); window.addEventListener('fantasy-theme-change', update); return () => window.removeEventListener('fantasy-theme-change', update); }, []);
  const dragStart = useRef(null);
  const graph = useMemo(() => {
    const all = new Map(document.nodes.map(n => [n.id, n]));
    const currentNodes = document.nodes.filter(n => n.board_id === boardId);
    const nodes = currentNodes.map(n => ({ id: n.id, type: 'story', position: n.position, selected: selection?.kind === 'node' && selection.id === n.id, data: { node: n, start: document.start_node_id === n.id, character: document.characters.find(c => c.id === n.speaker), characters: document.characters, asset: document.assets.find(a => a.id === n.asset_id), outCount: document.edges.filter(e => e.source === n.id).length, prerequisites: (n.prerequisites || []).map(item => ({ ...item, target: all.get(item.node_id) })), navigatePrerequisite, openSubstory } }));
    const portals = new Map();
    const edges = document.edges.filter(e => all.get(e.source)?.board_id === boardId).map(e => {
      const target = all.get(e.target); const source = all.get(e.source);
      if (!target || !source) return null;
      let targetId = target.id;
      if (target.board_id !== boardId) {
        targetId = `portal:${e.id}`;
        const index = portals.get(source.id) || 0; portals.set(source.id, index + 1);
        nodes.push({ id: targetId, type: 'portal', position: { x: source.position.x + 340, y: source.position.y + index * 100 }, draggable: false, selectable: false, connectable: false, data: { target, targetHandle: edgeHandles(e).targetHandle, characters: document.characters, board: document.boards.find(b => b.id === target.board_id)?.name, navigate } });
      }
      return { id: e.id, source: source.id, target: targetId, ...edgeHandles(e), type: 'smoothstep', label: e.label || '后续剧情', selected: selection?.kind === 'edge' && selection.id === e.id, markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: selection?.id === e.id ? '#80aaff' : '#737f94' }, style: { stroke: selection?.id === e.id ? '#80aaff' : '#637186', strokeWidth: 1.6, ...(target.board_id !== boardId ? { strokeDasharray: '6 4' } : {}) }, labelStyle: { fill: theme === 'dark' ? '#bfcadb' : '#30475a', fontSize: 11 }, labelBgStyle: { fill: theme === 'dark' ? '#151c26' : '#f4f7fa', fillOpacity: 0.95 }, labelBgPadding: [7, 5], labelBgBorderRadius: 5 };
    }).filter(Boolean);
    return { nodes, edges };
  }, [document, boardId, selection, navigate, navigatePrerequisite, openSubstory, theme]);
  const chapter = document.boards.find(b => b.id === boardId);
  return <section className="canvas-area" aria-label="剧情节点画布"><div className="canvas-heading"><div><span className="eyebrow">STORY OUTLINE</span><h2>{chapter?.name || '选择看板'}<span>{document.nodes.filter(n => n.board_id === boardId).length} 个节点</span></h2></div><div className="node-palette">{Object.entries(types).map(([type, item]) => <button key={type} title={`添加${item.label}节点`} disabled={readOnly} onClick={() => addNode(type)} style={{ '--palette-color': item.color }}><Icon name={type} size={16}/><span>{item.label}</span><b>＋</b></button>)}</div></div>
    <div className="flow-wrap"><ReactFlow key={boardId} nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} onInit={i => { instanceRef.current = i; }} fitView fitViewOptions={{ padding: 0.25, maxZoom: 0.95 }} minZoom={0.2} maxZoom={1.6} colorMode={theme} deleteKeyCode={null} nodesDraggable={!readOnly} nodesConnectable={!readOnly} connectionMode={ConnectionMode.Loose} connectionRadius={30} onConnect={connect} onNodeClick={(_, node) => { if (node.type === 'story') setSelection({ kind: 'node', id: node.id }); }} onEdgeClick={(_, edge) => setSelection({ kind: 'edge', id: edge.id })} onPaneClick={() => setSelection(null)} onNodeDragStart={() => { if (readOnly) return; dragStart.current = historyDocument || document; }} onNodeDragStop={() => { if (dragStart.current) addHistory(dragStart.current); dragStart.current = null; }} onNodesChange={changes => {
      if (readOnly) return;
      const moves = changes.filter(x => x.type === 'position' && x.position && document.nodes.some(n => n.id === x.id));
      if (moves.length) edit(d => { for (const move of moves) { const n = d.nodes.find(x => x.id === move.id); if (n) n.position = move.position; } }, '', false);
    }} isValidConnection={connection => {
      const source = document.nodes.find(n => n.id === connection.source); const target = document.nodes.find(n => n.id === connection.target);
      return !readOnly && !!source && !!target && source.type !== 'note' && target.type !== 'note';
    }} ariaLabelConfig={{ 'controls.zoomIn.ariaLabel': '放大画布', 'controls.zoomOut.ariaLabel': '缩小画布', 'controls.fitView.ariaLabel': '适应全部节点', 'controls.interactive.ariaLabel': '切换交互', 'minimap.ariaLabel': '剧情缩略图' }}>
      <Background color={theme === 'dark' ? '#323b49' : '#c1ccd5'} gap={24} size={1}/><Controls showInteractive={false}/><MiniMap pannable zoomable nodeColor={n => n.data?.node?.color || '#637186'} maskColor={theme === 'dark' ? 'rgba(10,15,22,.72)' : 'rgba(235,241,246,.75)'}/>
    </ReactFlow>
    {graph.nodes.length === 0 && <div className="canvas-empty"><span className="empty-constellation"><Icon name="scene" size={34}/><i/><Icon name="branch" size={28}/></span><h2>当前看板没有节点</h2><p>{readOnly ? "这个看板还没有剧情节点。" : "添加场景、对白或剧情分支，再拖动节点四周的圆点建立连接。"}</p><button className="primary" disabled={readOnly} onClick={() => addNode('scene')}><Icon name="plus"/>添加第一个场景</button></div>}
    <div className="canvas-tip"><Icon name="info" size={14}/>{readOnly ? "只读查看 · 点击节点查看内容 · 滚轮缩放 · 拖动画布平移" : "拖动圆点连接节点 · 滚轮缩放 · 拖动画布平移"}</div></div></section>;
}

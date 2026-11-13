import { nodeLabel, NODE_PORTS } from './storyModel.js';

// Narrative planning only: links describe story flow; no gameplay execution.
export function validateOutline(doc, depth = 0) {
  const errors = [], warnings = [];
  if (depth > 3) return {errors:['子剧情最多嵌套三层。'],warnings};
  if (!doc || doc.schema_version !== 3 || !Array.isArray(doc.nodes) || !Array.isArray(doc.edges) || !Array.isArray(doc.boards)) return {errors:['不是有效的剧情设计 v3 文件。'],warnings};
  const nodes = new Map(doc.nodes.map(n => [n.id,n]));
  const boards = new Set(doc.boards.map(b => b.id));
  if (nodes.size !== doc.nodes.length) errors.push('存在重复的节点编号。');
  for (const node of doc.nodes) {
    const label = nodeLabel(node, doc.characters);
    if (!boards.has(node.board_id)) errors.push(`节点「${label}」所属看板不存在。`);
    if (node.type !== 'dialogue' && !node.title?.trim()) warnings.push('有节点尚未填写标题。');
    if (node.type === 'dialogue' && (!Array.isArray(node.dialogue) || node.dialogue.length === 0)) errors.push(`对话「${label}」需要至少一条说话角色与对话内容。`);
    if (node.type === 'dialogue' && !(node.dialogue || []).some(line => line.body?.trim())) warnings.push(`对话「${label}」尚未填写对话内容。`);
    if (node.type === 'branch' && new Set(doc.edges.filter(e => e.source === node.id).map(e => e.target)).size < 2) warnings.push(`分支「${label}」尚未连接两个不同走向。`);
    for (const prerequisite of node.prerequisites || []) {
      if (node.type !== 'dialogue') errors.push(`节点「${label}」不是对话，不能设置对话解锁前提。`);
      if (!nodes.has(prerequisite.node_id)) errors.push(`节点「${label}」的解锁前提指向了已删除节点。`);
      else if (prerequisite.node_id === node.id) errors.push(`节点「${label}」的解锁前提不能指向自己。`);
      else if (nodes.get(prerequisite.node_id)?.type === 'note') errors.push(`节点「${label}」的解锁前提不能指向设计便签。`);
    }
    if (node.type === 'substory') {
      if (!node.substory) errors.push(`子剧情「${node.title || '未命名'}」尚未导入内容。`);
      else {
        const nested=validateOutline(node.substory,depth+1);
        errors.push(...nested.errors.map(s=>`子剧情「${node.title}」：${s}`));
        warnings.push(...nested.warnings.map(s=>`子剧情「${node.title}」：${s}`));
      }
    }
  }
  for (const edge of doc.edges) {
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) errors.push('存在指向已删除节点的连线。');
    if (nodes.get(edge.source)?.type === 'note' || nodes.get(edge.target)?.type === 'note') errors.push('设计便签不参与剧情连线。');
    if (!NODE_PORTS.includes(edge.source_handle) || !NODE_PORTS.includes(edge.target_handle)) errors.push('剧情连线的连接方向无效，请选择上、下、左或右。');
  }
  if (doc.start_node_id && (!nodes.has(doc.start_node_id) || nodes.get(doc.start_node_id)?.type === 'note')) errors.push('开篇标记指向了不存在的剧情节点。');
  const connected = new Set(doc.edges.flatMap(e => [e.source,e.target]));
  const loose = doc.nodes.filter(n => n.type !== 'note' && !connected.has(n.id));
  if (loose.length && doc.nodes.filter(n => n.type !== 'note').length > 1) warnings.push(`${loose.length} 个剧情节点尚未连线：${loose.slice(0,5).map(n => nodeLabel(n, doc.characters)).join('、')}${loose.length > 5 ? '…' : ''}。可以保留为待整理的灵感。`);
  return {errors:[...new Set(errors)],warnings:[...new Set(warnings)]};
}

export function createOutlineText(doc, depth = 0) {
  if (depth > 3) return '子剧情超过三层，请先修复项目结构。';
  const types={scene:'场景',dialogue:'对话',branch:'剧情分支',note:'设计便签',substory:'子剧情'};
  const lines=[doc.title,'='.repeat(40),doc.description || '', ''];
  for (const board of doc.boards) {
    lines.push(`【${board.name}】`, '');
    const nodes=doc.nodes.filter(n => n.board_id === board.id);
    for (const [index,node] of nodes.entries()) {
      lines.push(`${index+1}. ${node.type === 'dialogue' ? '对话片段' : nodeLabel(node, doc.characters)}（${types[node.type] || node.type}${doc.start_node_id === node.id ? ' · 开篇' : ''}）`);
      if (node.type === 'dialogue') {
        for (const line of node.dialogue || []) {
          const speaker=doc.characters.find(character => character.id === line.speaker);
          lines.push(`${speaker?.name || '未指定角色'}：`, line.body || '（待填写对话内容）', '');
        }
      } else {
        const speaker=doc.characters.find(c => c.id === node.speaker);
        if (speaker) lines.push(`人物：${speaker.name}`);
        if (node.body) lines.push(node.body);
      }
      for (const prerequisite of node.prerequisites || []) {
        const target=doc.nodes.find(n => n.id === prerequisite.node_id);
        const targetBoard=doc.boards.find(b => b.id === target?.board_id);
        lines.push(`  解锁前提：${prerequisite.description || '完成此处剧情'} → ${nodeLabel(target, doc.characters)}${target && target.board_id !== board.id ? ` [${targetBoard?.name || '其他看板'}]` : ''}`);
      }
      const media=doc.assets.find(a => a.id === node.asset_id);
      if (media) lines.push(`参考素材：${media.name}`);
      if (node.type === 'substory' && node.substory) {
        lines.push('  ┌ 子剧情内容（独立副本）');
        lines.push(...createOutlineText(node.substory,depth+1).split('\n').map(line=>'  │ '+line));
        lines.push('  └ 子剧情内容结束');
      }
      for (const edge of doc.edges.filter(e => e.source === node.id)) {
        const target=doc.nodes.find(n => n.id === edge.target);
        const targetBoard=doc.boards.find(b => b.id === target?.board_id);
        lines.push(`  → ${edge.label || '后续剧情'}：${nodeLabel(target, doc.characters)}${target && target.board_id !== board.id ? ` [${targetBoard?.name || '其他看板'}]` : ''}`);
        if (edge.requirements?.trim()) lines.push(`    剧情前提：${edge.requirements}`);
      }
      lines.push('');
    }
  }
  if (doc.characters.length) {
    lines.push('【人物资料】','');
    for (const character of doc.characters) lines.push(character.name,character.description || '尚未填写说明','');
  }
  lines.push('由 Fantasy 剧情设计导出。此文稿用于剧情梳理；图片与音频请从完整 JSON 工程中查看。');
  return lines.join('\n');
}

export function substoryDepth(doc) {
  function depth(document,level) {
    if (level > 3) return level;
    const children=(document?.nodes || []).filter(n=>n.type==='substory' && n.substory);
    return children.length ? Math.max(...children.map(n=>depth(n.substory,level+1))) : level;
  }
  return depth(doc,0);
}

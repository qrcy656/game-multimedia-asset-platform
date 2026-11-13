export const NODE_PORTS = Object.freeze(['left', 'right', 'top', 'bottom']);

const compact = value => String(value || '').replace(/\s+/g, ' ').trim();
const short = (value, length = 38) => {
  const characters = Array.from(compact(value));
  return characters.length > length ? `${characters.slice(0, length).join('')}…` : characters.join('');
};

// Dialogue has no authored title. Lists and navigation use a summary of its text.
export function nodeLabel(node, characters = []) {
  if (!node) return '未连接节点';
  if (node.type !== 'dialogue') return node.title?.trim() || '未命名节点';
  const lines = node.dialogue || [];
  const first = lines.find(line => compact(line.body)) || lines.find(line => line.speaker);
  if (!first) return '待编写的对话';
  const speaker = characters.find(character => character.id === first.speaker)?.name;
  const body = short(first.body);
  return body ? `${speaker ? `${speaker}：` : ''}${body}` : `${speaker || '未指定角色'}的对话`;
}

export function nodeSearchText(node, characters = []) {
  const characterNames = new Map(characters.map(character => [character.id, character.name]));
  const contents = node.type === 'dialogue'
    ? (node.dialogue || []).flatMap(line => [characterNames.get(line.speaker), line.body])
    : [node.title, node.body, characterNames.get(node.speaker)];
  return [...contents, ...(node.prerequisites || []).map(item => item.description)].filter(Boolean).join('\n');
}

// These changes apply only to this document. Child documents own their own IDs.
export function removeNodes(document, ids) {
  const removed = new Set(typeof ids === 'string' ? [ids] : ids);
  document.nodes = document.nodes.filter(node => !removed.has(node.id));
  document.edges = document.edges.filter(edge => !removed.has(edge.source) && !removed.has(edge.target));
  for (const node of document.nodes) {
    node.prerequisites = (node.prerequisites || []).filter(item => !removed.has(item.node_id));
  }
  if (removed.has(document.start_node_id)) document.start_node_id = '';
  return document;
}

export function clearCharacter(document, characterId) {
  document.characters = document.characters.filter(character => character.id !== characterId);
  for (const node of document.nodes) {
    if (node.speaker === characterId) node.speaker = '';
    for (const line of node.dialogue || []) if (line.speaker === characterId) line.speaker = '';
  }
  return document;
}

export function edgeHandles(edge) {
  return { sourceHandle: edge.source_handle || 'right', targetHandle: edge.target_handle || 'left' };
}


export function takeReturnTarget(document, previousHistory) {
  const history = [...previousHistory];
  let node;
  while (history.length && !node) {
    const id = history.pop();
    node = document.nodes.find(item => item.id === id);
  }
  return { history, node };
}

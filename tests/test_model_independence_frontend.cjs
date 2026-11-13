const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const media = fs.readFileSync(path.join(__dirname, '../media/static/app.js'), 'utf8');
const vault = fs.readFileSync(path.join(__dirname, '../knowledge/static/app.js'), 'utf8');

test('media shows a local draft without inventing a configured provider', () => {
  const draft = {id: '__prompt_draft__', preview_only: true};
  const context = {state: {models: [], config: {draft_model: draft}}, $: () => ({value: ''})};
  vm.createContext(context);
  vm.runInContext(media.match(/^function currentModel\(\).*$/m)[0], context);
  assert.equal(vm.runInContext('currentModel()', context), draft);
  assert.equal(context.state.models.length, 0);
});

test('missing optional LLM only blocks assisted media submission', () => {
  const source = 'function updateReadiness()' + media.split('function updateReadiness()')[1].split('function fieldSpec(')[0];
  const widgets = {};
  const widget = id => widgets[id] ||= {replaceChildren() {}, append() {}};
  const context = {state: {config: {user: {}}, csrf: 'token'}, currentModel: () => ({ready: true}),
    modelReady: model => model.ready, promptMode: () => 'direct', translationConfigured: () => false,
    updateTranslationControls() {}, $: widget, el() {}, homeURL: () => '/'};
  vm.createContext(context);
  vm.runInContext(source + '\nupdateReadiness();', context);
  assert.equal(widgets.generate.disabled, false);
  context.promptMode = () => 'assisted';
  vm.runInContext('updateReadiness();', context);
  assert.equal(widgets.generate.disabled, true);
  context.translationConfigured = () => true;
  vm.runInContext('updateReadiness();', context);
  assert.equal(widgets.generate.disabled, false);
});

test('vault independently gates chat and vector indexing while keeping editing available', () => {
  const selection = 'function updateSelection()' + vault.split('function updateSelection()')[1].split('function renderTree()')[0];
  const chat = 'function setChatBusy(busy)' + vault.split('function setChatBusy(busy)')[1].split('async function sendChat(')[0];
  const widgets = {};
  const widget = id => widgets[id] ||= {disabled: false};
  const context = {state: {features: {raw_qa: {available: false}, vector_index: {available: false}},
      selected: new Set(['world.md']), files: ['world.md'], document: 'world.md'}, $: widget, can: () => true};
  vm.createContext(context);
  vm.runInContext(selection + chat + '\nsetChatBusy(false);', context);
  assert.equal(widgets['send-chat'].disabled, true);
  assert.equal(widgets['index-document'].disabled, true);
  assert.equal(widgets['chat-input'].disabled, false);
  assert.equal(widget('save-file').disabled, false);
  context.state.features.vector_index.available = true;
  vm.runInContext('updateSelection();', context);
  assert.equal(Boolean(widgets['index-document'].disabled), false);
  assert.equal(widgets['send-chat'].disabled, true);
  context.state.features.raw_qa.available = true;
  context.state.features.vector_index.available = false;
  vm.runInContext('setChatBusy(false);', context);
  assert.equal(widgets['send-chat'].disabled, false);
  assert.equal(widgets['index-document'].disabled, true);
});

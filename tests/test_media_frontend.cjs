const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../media/static/app.js'), 'utf8');
const conditional = 'function updateConditionalFields()' + source.split('function updateConditionalFields()')[1].split('function clearFile()')[0];
const translation = source.match(/^function translationAvailable\(\).*$/m)[0];

function harness(module, model, task = 'generate') {
  const widgets = new Map(), visibility = new Map();
  function widget(id) {
    if (!widgets.has(id)) widgets.set(id, { value: id === 'field-task' ? task : '', checked: false,
      dataset: {mode: ''}, classList: {toggle(name, value) {this[name] = value;}},
      closest() {return {querySelector: () => ({textContent: ''})};} });
    return widgets.get(id);
  }
  const context = {state: {config: {module, image_profiles: module === 'image'}, busy: false},
    currentModel: () => model, $: widget, clearFile: () => {}, updateReadiness: () => {},
    setFieldVisible: (key, visible) => visibility.set(key, visible)};
  vm.createContext(context);
  vm.runInContext(conditional + translation + '\nupdateConditionalFields();', context);
  return {widgets, visibility, context};
}

test('profile reference picker belongs only to image module', () => {
  for (const module of ['image', 'speech', 'music', 'model3d']) {
    const result = harness(module, {id: 'provider-uuid', input: module === 'image' ? 'optional' : null});
    assert.equal(result.widgets.get('profile-reference-section').classList.hidden, module !== 'image');
  }
});

test('API capability enables training for arbitrary profile IDs and disables translation', () => {
  const result = harness('music', {id: 'provider-uuid', training: true, fields: {duration: {max: 300}}}, 'train');
  assert.equal(result.widgets.get('reference-file').dataset.mode, 'zip');
  assert.equal(result.widgets.get('reference-file').required, true);
  assert.equal(result.widgets.get('field-duration').max, '240');
  assert.equal(result.visibility.get('epochs'), true);
  assert.equal(result.visibility.get('tags'), false);
  assert.equal(result.visibility.get('lyrics'), false);
  assert.equal(vm.runInContext('translationAvailable()', result.context), false);
});

test('ordinary music API still exposes original lyrics, style and steps', () => {
  const result = harness('music', {id: 'provider-uuid', training: false});
  assert.equal(result.visibility.get('epochs'), false);
  assert.equal(result.visibility.get('lyrics'), true);
  assert.equal(result.visibility.get('tags'), true);
  assert.equal(result.visibility.get('steps'), true);
  assert.equal(vm.runInContext('translationAvailable()', result.context), true);
});

test('animation readiness cannot turn green with only video API ready', () => {
  const animation = fs.readFileSync(path.join(__dirname, '../animation/static/app.js'), 'utf8');
  const render = 'function renderGenerationHealth(' + animation.split('function renderGenerationHealth(')[1].split('async function submitGeneration(')[0];
  const widgets = {};
  const context = {state: {generationRevision: 1}, $: id => widgets[id] ||= {},
    renderGenerationMessage: message => {context.message = message;}, generationUnavailableMessage: missing => missing.join(',')};
  vm.createContext(context);
  vm.runInContext(render + `renderGenerationHealth({generation_ready:true,providers:{video:{available:true},image:{available:false}},matting:{mode:'api',available:false,message:'请配置抠图API'}});`, context);
  assert.match(widgets['#health'].textContent, /尚未就绪/);
  assert.equal(context.message, 'image');
});

import { mountModelPreview } from './model-preview.js';

const $ = (id) => document.getElementById(id);
const state = { config: null, models: [], jobs: [], profiles: [], profile: null, reference: null, selected: null, csrf: '', page: 'studio', busy: false, compiling: false, formRevision: 0, loading: false, scope: true, fileURL: null, poll: null, disposers: [], dialogDisposers: [], resultKey: '', refreshPromise: null };
const ACTIVE = new Set(['queued', 'pending', 'waiting', 'running', 'processing', 'cancelling', 'cancel_requested']);
const DONE = new Set(['succeeded', 'completed', 'success', 'done']);
const CANCELLED = new Set(['cancelled', 'canceled']);
const statusLabels = { queued: '等待制作', pending: '等待制作', waiting: '等待制作', running: '正在制作', processing: '正在制作', cancelling: '正在取消', cancel_requested: '正在取消', succeeded: '已完成', completed: '已完成', success: '已完成', done: '已完成', failed: '制作失败', error: '制作失败', interrupted: '任务已中断', cancelled: '已取消', canceled: '已取消' };
const langs = [['Auto','自动识别'],['Chinese','中文'],['English','英语'],['Japanese','日语'],['Korean','韩语'],['German','德语'],['French','法语'],['Russian','俄语'],['Portuguese','葡萄牙语'],['Spanish','西班牙语'],['Italian','意大利语']];
const speakers = [['Vivian','Vivian · 明亮女声（中文）'],['Serena','Serena · 温柔女声（中文）'],['Uncle_Fu','Uncle Fu · 低沉男声（中文）'],['Dylan','Dylan · 青年男声（北京话）'],['Eric','Eric · 活泼男声（四川话）'],['Ryan','Ryan · 节奏鲜明男声（英语）'],['Aiden','Aiden · 清晰男声（英语）'],['Ono_Anna','Ono Anna · 活泼女声（日语）'],['Sohee','Sohee · 温暖女声（韩语）']];
const fields = {
  prompt: { label: '制作描述', type: 'textarea', required: true, maxlength: 4000, rows: 5, placeholder: '描述你想制作的内容、风格和细节。', help: '清楚描述主体、风格与氛围；避免互相矛盾的要求。' },
  negative_prompt: { label: '负面描述', type: 'textarea', maxlength: 4000, rows: 3, placeholder: '描述希望避免的元素或画面问题。', help: '按原文传入工作流，不经过翻译辅助。' },
  workflow: { label: 'ComfyUI 工作流 · API 格式 JSON', type: 'textarea', required: true, maxlength: 65536, rows: 9, placeholder: '{"1":{"class_type":"CLIPTextEncode","inputs":{"text":"…"}}}', help: '粘贴从 ComfyUI 导出的 API 格式工作流（节点 ID 为键的 JSON 对象），不是界面布局 JSON。所需模型和自定义节点须已安装在 ComfyUI 服务中。' },
  bindings: { label: '工作流参数绑定 · JSON', type: 'textarea', maxlength: 2048, rows: 6, placeholder: '{"prompt":{"node":"1","field":"text"},"negative_prompt":{"node":"2","field":"text"},"seed":{"node":"3","field":"seed"},"width":{"node":"4","field":"width"},"height":{"node":"4","field":"height"},"image":{"node":"5","field":"image"}}', help: '按实际工作流填写节点 ID 和输入字段。可绑定 prompt、negative_prompt、seed、width、height、image；参考图需绑定 image。只填写工作流实际使用的项目。' },
  text: { label: '朗读文本', type: 'textarea', required: true, maxlength: 3000, rows: 6, placeholder: '输入角色要说的台词，标点会影响停顿和语气。', help: '最多 3000 字。建议按完整句子分段制作，便于调整语气和节奏。' },
  instruct: { label: '声音与表演要求', type: 'textarea', maxlength: 2000, rows: 3, placeholder: '例如：年轻女性，音色清亮，语速舒缓，带一点神秘感。' },
  language: { label: '语言', type: 'select', options: langs, default: 'Auto' },
  speaker: { label: '基础音色', type: 'select', options: speakers, default: 'Vivian', help: '选定音色后，可用表演要求进一步调整情绪与语速。' },
  lyrics: { label: '歌词', type: 'textarea', rows: 7, maxlength: 4096, placeholder: '[Verse]\n在这里写主歌歌词\n\n[Chorus]\n在这里写副歌歌词', help: '可使用 [Verse]、[Chorus] 等段落标签组织歌曲。' },
  tags: { label: '音乐风格标签', type: 'textarea', rows: 3, required: true, maxlength: 2000, placeholder: '例如：cinematic, orchestral, warm strings, female vocal', help: '用逗号分隔流派、乐器、情绪和人声等风格标签。' },
  duration: { label: '制作时长 / 秒', type: 'number', min: 1, max: 180, step: 1, default: 30 },
  seed: { label: '随机种子 Seed', type: 'number', min: 0, max: 2147483647, step: 1, default: 42, help: '固定种子方便比较参数；换一个种子可探索新结果。' },
  steps: { label: '采样步数', type: 'number', min: 1, max: 100, step: 1, default: 32, help: '步数越高通常耗时越长，不一定总能改善结果。' },
  width: { label: '图片宽度 / 像素', type: 'number', min: 256, max: 2048, step: 16, default: 1024 },
  height: { label: '图片高度 / 像素', type: 'number', min: 256, max: 2048, step: 16, default: 1024 },
  resolution: { label: '模型生成分辨率', type: 'select', options: [['512','512 · 快速预览'],['1024','1024 · 标准'],['1024_cascade','1024 · 级联精细'],['1536_cascade','1536 · 级联高精度']], default: '1024_cascade', help: '高分辨率需要更多显存与时间。建议先用较低分辨率确认轮廓。' },
  texture_size: { label: '纹理分辨率', type: 'select', options: [['1024','1024'],['2048','2048'],['4096','4096']], default: '2048', numeric: true },
  decimation_target: { label: '目标面数', type: 'number', min: 10000, max: 1000000, step: 1000, default: 100000, help: '控制导出网格的复杂度，实际面数取决于生成结果。' },
  instrumental: { label: '制作纯音乐', type: 'checkbox', default: true, help: '关闭后可填写歌词，制作带人声的音乐。' },
  task: { label: '制作方式', type: 'select', options: [['generate','生成音乐'],['train','训练 LoRA 微调']], default: 'generate', help: '微调使用上传的音频数据集。任务完成后，可在资产库下载训练结果。' },
  epochs: { label: '训练轮数', type: 'number', min: 1, max: 1000, step: 1, default: 10 },
  rank: { label: 'LoRA 秩', type: 'select', options: ['8','16','32','64'], default: '16', numeric: true },
  learning_rate: { label: '学习率', type: 'number', min: .000001, max: .001, step: 'any', default: .0001, help: '较小的学习率适合保守调整；过高可能降低音质。' },
};
const modelFields = {
  trellis2: ['resolution', 'texture_size', 'decimation_target', 'steps', 'seed'],
  'qwen-image21': ['prompt', 'width', 'height', 'steps', 'seed'],
  comfyui: ['prompt', 'negative_prompt', 'width', 'height', 'seed', 'workflow', 'bindings'],
  'qwen-tts-design': ['text', 'instruct', 'language', 'seed'],
  'qwen-tts-custom': ['text', 'speaker', 'language', 'instruct', 'seed'],
  'stable-audio3-medium': ['prompt', 'duration', 'steps', 'seed'],
  'stable-audio3-small-sfx': ['prompt', 'duration', 'steps', 'seed'],
  'acestep-sft': ['prompt', 'instrumental', 'lyrics', 'duration', 'steps', 'seed'],
  'acestep-base': ['task', 'prompt', 'instrumental', 'lyrics', 'duration', 'steps', 'seed', 'epochs', 'rank', 'learning_rate'],
  yue2: ['prompt', 'seed'],
  heartmula: ['tags', 'lyrics', 'duration', 'seed'],
};
const moduleCopy = {
  model3d: { name: '3D 模型生成', eyebrow: 'FANTASY COMMUNITY / 3D STUDIO', title: '3D 模型制作', description: '上传清晰主体图，生成带纹理的 3D 资产，预览后导出继续制作。', icon: '◇', guide: '让主体完整、轮廓清楚。', help: '尽量使用背景干净、主体无遮挡的图片。单图无法准确表达的背面细节，需要在生成后检查；GLB 可下载到 Blender 等工具继续处理。' },
  image: { name: '图片生成', eyebrow: 'FANTASY COMMUNITY / IMAGE STUDIO', title: '图片制作', description: '用文字描述创作画面，也可以上传参考图继续编辑。', icon: '▧', guide: '从主体、构图和光线开始。', help: '描述画面主体、视角、材质、光线与风格。编辑图片时，说明具体需要改变的区域和希望保留的内容。尺寸需符合所选模型 API 的要求。' },
  speech: { name: '语音制作', eyebrow: 'FANTASY COMMUNITY / VOICE STUDIO', title: '语音制作', description: '设计专属角色声线，或用预设音色制作 NPC 台词与对白。', icon: '≋', guide: '先确定声线，再调整表演。', help: '声音设计模型根据文字描述塑造声线；预设音色模型提供可复用的角色声音。生成后试听，确认发音、停顿、情绪和语速。' },
  music: { name: '音乐制作', eyebrow: 'FANTASY COMMUNITY / MUSIC STUDIO', title: '音乐制作', description: '通过模型 API 创作纯音乐、带词歌曲、环境氛围与短音效，或训练专属音乐风格。', icon: '♫', guide: '从场景与情绪出发。', help: '配乐描述使用场景、节奏与乐器；氛围音效描述环境与空间感；短音效描述单次事件及声音变化。带词歌曲可按主歌、副歌分段；支持训练的 API 可上传音频数据集制作 LoRA。' },
};

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'checked') node.checked = Boolean(value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
}
function toast(message) { $('toast').textContent = message; $('toast').classList.add('visible'); clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').classList.remove('visible'), 5000); }
function showError(message) { $('service-error').textContent = message; $('service-error').classList.toggle('hidden', !message); }
function currentModel() { return state.models.find((model) => model.id === $('model-select').value) || state.config?.draft_model || null; }
function modelLabel(id) { return state.models.find((model) => model.id === id)?.label || id || '未知模型'; }
function jobTitle(job) { return job.title || job.params?.title || job.model_label || modelLabel(job.model_id); }
function prettyDate(value) { if (!value) return '时间待更新'; const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value); return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' }).format(date); }
function statusClass(status) { return DONE.has(status) ? 'succeeded' : CANCELLED.has(status) ? 'cancelled' : ['failed','error','interrupted'].includes(status) ? 'failed' : ACTIVE.has(status) ? 'running' : 'offline'; }
function badge(status) { return el('span', { class: `pill ${statusClass(status)}`, text: statusLabels[status] || status || '状态未知' }); }
function activeJobMessage(job) {
  const queued = ['queued', 'pending', 'waiting'].includes(job.status);
  const messages = queued ? [job.waiting?.message, job.message] : [job.message];
  const message = messages.find((value) => typeof value === 'string' && value.trim());
  return message?.trim() || (queued ? '任务已保存，等待可用资源。页面会自动更新进度。' : ['cancelling', 'cancel_requested'].includes(job.status) ? '正在停止任务并释放模型资源，请稍候。' : '模型正在服务器制作，请稍候。页面会自动更新结果。');
}
function own(job) { const user = state.config?.user || state.config?.current_user || {}; return Boolean(user.admin || user.is_admin || job.owner?.id && job.owner.id === user.id); }
function homeURL() { return state.config?.home_url || '/'; }
function safeArtifactURL(value) { try { const url = new URL(value, location.origin); if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return null; if (state.scope) url.searchParams.set('scope', 'all'); return url.href; } catch { return null; } }
function artifacts(job) { return (Array.isArray(job.artifacts) ? job.artifacts : []).filter((artifact) => artifact && safeArtifactURL(artifact.url)); }
function fileKind(artifact) { if (artifact.kind && artifact.kind !== 'file') return artifact.kind; const name = (artifact.name || artifact.url || '').split('?')[0].toLowerCase(); return /\.(png|jpe?g|webp|gif)$/.test(name) ? 'image' : /\.(wav|mp3|flac|ogg|m4a)$/.test(name) ? 'audio' : /\.glb$/.test(name) ? 'model' : 'file'; }
function downloadLink(artifact) { const url = new URL(safeArtifactURL(artifact.url)); url.searchParams.set('download', '1'); return el('a', { class: 'button', href: url.href, download: artifact.name || '', text: `↓ ${artifact.name || '下载文件'}` }); }
async function api(path, options = {}, refreshed = false) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', state.csrf);
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
  let data; try { data = await response.json(); } catch { data = {}; }
  if (response.status === 403 && data.detail === '页面验证已失效，请刷新后重试。' && options.method && options.method !== 'GET' && !refreshed) {
    const session = await api('/api/session');
    if (session.csrf) { state.csrf = session.csrf; return api(path, options, true); }
  }
  if (!response.ok) {
    if (response.status === 401) { $('auth-error').classList.remove('hidden'); $('loading').classList.add('hidden'); state.csrf = ''; $('generate').disabled = true; }
    const detail = data.detail || data.error;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item) => `${item.loc?.slice(-1)[0] || ''} ${item.msg || ''}`).join('；') : `请求未完成（${response.status}）`;
    const error = new Error(message); error.status = response.status; throw error;
  }
  return data;
}
function navigate(page) {
  if (!['studio', 'jobs', 'assets', 'profiles'].includes(page) || page === 'profiles' && !state.config?.image_profiles) page = 'studio';
  state.page = page;
  for (const section of document.querySelectorAll('.page')) section.classList.toggle('hidden', section.id !== page || !state.config);
  for (const button of document.querySelectorAll('nav [data-page]')) { button.classList.toggle('active', button.dataset.page === page); if (button.dataset.page === page) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); }
  $('page-title').textContent = ({ studio: '制作工作台', jobs: '任务队列', assets: '资产库', profiles: '图片档案' })[page];
  history.replaceState(null, '', `#${page}`);
}
function modelReady(model) { return Boolean(model?.ready) && model.enabled !== false; }
function updateReadiness() {
  const model = currentModel();
  const user = state.config?.user || state.config?.current_user || {};
  const assistantMissing = promptMode() === 'assisted' && !translationConfigured();
  $('generate').disabled = state.busy || state.compiling || !modelReady(model) || !state.csrf || assistantMissing;
  updateTranslationControls();
  $('model-description').textContent = model?.description || '';
  const notice = $('model-readiness'); notice.replaceChildren(); notice.className = `notice${modelReady(model) ? '' : ' warning'}`;
  notice.append(el('span', { text: modelReady(model) ? '模型已就绪，可以提交制作任务。' : (model?.ready && model.enabled === false ? '请在主页检测并选择模型 API 配置。' : model?.reason || '尚未配置可用 API，请前往主页的 AI 模型配置。') }));
  if (!modelReady(model)) notice.append(el('br'), el('a', { href: homeURL(), text: '前往主页配置模型 API ↗' }));
  if (assistantMissing) notice.append(el('p', { text: '辅助模式需要 LLM 配置；可切换直接模式使用已配置的生成 API。' }));
  if (!state.csrf && state.config) notice.append(el('p', { text: '尚未取得会话校验信息，请刷新页面或重新登录。' }));
}
function fieldSpec(key, model) {
  const spec = { ...(fields[key] || { label: key, type: 'text' }) };
  if (model.id === 'trellis2' && key === 'steps') Object.assign(spec, { max: 50, default: 12 });
  if (model.id === 'qwen-image21' && key === 'steps') Object.assign(spec, { min: 2, default: 40 });
  if (model.id === 'qwen-tts-design' && key === 'instruct') Object.assign(spec, { required: true, label: '声音设计描述', help: '必填。用年龄、音色、情绪、语速等特征描述希望设计的声音。' });
  if (model.id.startsWith('stable-audio3') && key === 'prompt') Object.assign(spec, { label: '声音描述', placeholder: model.id.includes('sfx') ? '例如：一扇厚重木门缓缓打开，金属合页轻微吱呀作响，最后一声轻响。' : '例如：夜晚的森林，微风穿过树叶，远处有细弱虫鸣，安静而有空间感。', help: '描述声音来源、环境与声音变化；英语描述通常更适合音效模型。' });
  if (model.id.includes('small-sfx') && key === 'duration') Object.assign(spec, { max: 30, default: 5 });
  if (model.id.startsWith('acestep') && key === 'prompt') Object.assign(spec, { label: '音乐描述', maxlength: 512, placeholder: '例如：舒缓的奇幻森林配乐，木管与竖琴为主，轻柔弦乐铺底，无人声。' });
  if (model.id.startsWith('acestep') && key === 'duration') Object.assign(spec, { min: 10, max: 300, default: 60 });
  if (model.id.startsWith('acestep') && key === 'steps') Object.assign(spec, { min: 8 });
  if (model.id === 'yue2' && key === 'prompt') Object.assign(spec, { label: '环境音乐描述', maxlength: 2000, placeholder: '例如：古老城堡的探索配乐，缓慢的弦乐与低沉铜管，神秘、深远、没有人声。', help: '描述场景、乐器、节奏与氛围。时长由模型生成方式决定。' });
  if (model.id === 'heartmula' && key === 'lyrics') Object.assign(spec, { required: true, maxlength: 12000 });
  if (model.id === 'heartmula' && key === 'duration') Object.assign(spec, { max: 240, default: 60 });
  const override = !Array.isArray(model.fields) && model.fields?.[key];
  if (override && typeof override === 'object') {
    // The provider-independent field definition owns the input type and choices.
    if (override.type && !override.options && !override.enum) { delete spec.options; delete spec.numeric; }
    for (const name of ['label','type','default','min','max','step','required','options','help','placeholder','maxlength','rows','numeric']) if (override[name] !== undefined) spec[name] = override[name];
    if (override.enum) spec.options = override.enum;
    if (override.max_length) spec.maxlength = override.max_length;
    if (override.maxLength) spec.maxlength = override.maxLength;
    if (spec.options) spec.type = 'select';
    if (['integer','float','number'].includes(spec.type)) spec.type = 'number';
    if (['boolean','bool'].includes(spec.type)) spec.type = 'checkbox';
    if (spec.type === 'string') spec.type = fields[key]?.type || 'text';
  }
  if (Array.isArray(spec.options) && fields[key]?.options) {
    const labels = new Map(fields[key].options.filter(Array.isArray).map(([value,label]) => [String(value),label]));
    spec.options = spec.options.map((option) => typeof option !== 'object' && labels.has(String(option)) ? [option,labels.get(String(option))] : option);
  }
  return spec;
}
function makeField(key, spec) {
  const label = el('label', { for: `field-${key}`, 'data-field': key, class: spec.type === 'checkbox' ? 'field-checkbox' : '' });
  const caption = el('span', { text: spec.label });
  const input = el(spec.type === 'textarea' ? 'textarea' : spec.type === 'select' ? 'select' : 'input', { id: `field-${key}`, name: key, 'data-kind': spec.type, required: spec.required, 'aria-describedby': spec.help ? `help-${key}` : null });
  if (spec.type === 'select') {
    const options = Array.isArray(spec.options) ? spec.options : Object.entries(spec.options || {});
    for (const item of options) { const [value, text] = Array.isArray(item) ? item : typeof item === 'object' ? [item.value, item.label || item.value] : [item, item]; input.append(el('option', { value, text })); }
  } else if (spec.type === 'textarea') input.rows = spec.rows || 4;
  else input.type = spec.type || 'text';
  for (const attr of ['min','max','step','maxlength','placeholder']) if (spec[attr] !== undefined) input.setAttribute(attr, String(spec[attr]));
  if (spec.type === 'checkbox') input.checked = spec.default === true;
  else if (spec.default !== undefined) input.value = String(spec.default);
  if (spec.numeric) input.dataset.numeric = 'true';
  if (spec.type === 'checkbox') label.append(input, caption); else label.append(caption, input);
  if (spec.help) label.append(el('span', { class: 'field-help', id: `help-${key}`, text: spec.help }));
  return label;
}
function renderFields(values = {}) {
  invalidateTranslation();
  const model = currentModel(); const target = $('model-fields'); target.replaceChildren(); if (!model) { updateReadiness(); return; }
  let keys = modelFields[model.id] || [];
  if (model.fields) { const supported = (Array.isArray(model.fields) ? model.fields.map((item) => typeof item === 'string' ? item : item.name) : Object.keys(model.fields)).filter((key) => !['title','input','prompt_mode'].includes(key)); keys = [...keys.filter((key) => supported.includes(key)), ...supported.filter((key) => !keys.includes(key))]; }
  let pair;
  for (const key of keys) {
    const spec = fieldSpec(key, model); const field = makeField(key, spec);
    if (['number','select'].includes(spec.type) && !['speaker','task','resolution'].includes(key)) { if (!pair || pair.children.length === 2) { pair = el('div', { class: 'two' }); target.append(pair); } pair.append(field); }
    else { pair = null; target.append(field); }
    if (values[key] !== undefined) { const input = field.querySelector('input,select,textarea'); if (input.type === 'checkbox') input.checked = Boolean(values[key]); else input.value = typeof values[key] === 'object' ? JSON.stringify(values[key], null, 2) : values[key]; }
  }
  $('field-task')?.addEventListener('change', updateConditionalFields);
  $('field-instrumental')?.addEventListener('change', updateConditionalFields);
  updateConditionalFields(); updateReadiness();
}
function setFieldVisible(key, visible) { const label = document.querySelector(`[data-field="${key}"]`); if (label) { label.classList.toggle('hidden', !visible); label.querySelector('input,textarea,select').disabled = !visible; } }
function updateConditionalFields() {
  const model = currentModel(); if (!model) return;
  const training = model.training && $('field-task')?.value === 'train';
  for (const key of ['epochs','rank','learning_rate']) setFieldVisible(key, training);
  if (state.config?.module === 'music') { for (const key of ['instrumental','steps','seed','tags']) setFieldVisible(key, !training); setFieldVisible('lyrics', !training && !$('field-instrumental')?.checked); }
  const duration = $('field-duration'); if (model.training && duration) { duration.max = training ? '240' : String(model.fields.duration.max); duration.closest('label').querySelector('span').textContent = training ? '训练样本裁剪上限 / 秒' : '制作时长 / 秒'; }
  const uploadMode = training ? 'zip' : Boolean(model.input) ? 'image' : null;
  if ($('reference-file').dataset.mode !== uploadMode) clearFile();
  $('reference-file').dataset.mode = uploadMode || '';
  $('upload-section').classList.toggle('hidden', !uploadMode);
  $('profile-reference-section').classList.toggle('hidden', state.config?.module !== 'image' || !state.config?.image_profiles);
  $('reference-file').disabled = !uploadMode;
  $('reference-file').required = model.input === 'required' || training;
  $('reference-file').accept = training ? '.zip,application/zip,application/x-zip-compressed' : 'image/png,image/jpeg,image/webp';
  $('upload-label').textContent = training ? '音频训练数据集 ZIP（必填）' : model.input === 'required' ? '主体参考图片（必填）' : '参考图片（选填）';
  $('upload-help').textContent = training ? '上传包含 WAV、MP3 或 FLAC 音频的 ZIP，最大 256 MB。音乐描述作为所有样本的共同风格说明，训练结果保存到本次任务。' : model.input === 'required' ? 'PNG、JPG、WebP，最大 16 MB / 3600 万像素。建议主体完整、背景简洁；图片决定模型的主要外观。' : '临时上传：PNG、JPG、WebP，最多 200 MiB / 2500 万像素。也可使用上方档案图片；两者都不选则从文字生成图片。';
  if (model.protocol === 'comfyui') $('upload-help').textContent = 'PNG、JPG、WebP，可上传或从图片档案选择一张参考图。请在参数绑定中填写 image 对应的节点与字段，图片会上传给 ComfyUI 并传入该节点。';
  $('generate').textContent = state.busy ? '正在提交…' : training ? '提交微调训练任务 →' : '提交制作任务 →';
  updateReadiness();
}
function clearFile() { if (state.fileURL) URL.revokeObjectURL(state.fileURL); state.fileURL = null; $('reference-file').value = ''; $('reference-image').removeAttribute('src'); $('upload-preview').classList.add('hidden'); }
function previewFile() {
  if (state.fileURL) URL.revokeObjectURL(state.fileURL); state.fileURL = null;
  const file = $('reference-file').files[0]; if (!file) { clearFile(); return; }
  state.reference = null; renderReferenceImages();
  $('reference-name').textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
  $('upload-preview').classList.remove('hidden'); $('reference-image').classList.toggle('hidden', $('reference-file').dataset.mode !== 'image');
  if ($('reference-file').dataset.mode === 'image') { state.fileURL = URL.createObjectURL(file); $('reference-image').src = state.fileURL; }
}
function translationAvailable() { return ['image','speech','music'].includes(state.config?.module) && !(currentModel()?.training && $('field-task')?.value === 'train'); }
function translationConfigured() { return state.config?.features?.prompt_assistant?.available !== false; }
function promptMode() { return translationAvailable() && $('prompt-mode').value === 'assisted' ? 'assisted' : 'direct'; }
function invalidateTranslation() { state.formRevision += 1; $('translation-preview').replaceChildren(); $('translation-preview').classList.add('hidden'); }
function updateTranslationControls() {
  const supported = ['image','speech','music'].includes(state.config?.module);
  const available = translationAvailable(); const assisted = promptMode() === 'assisted';
  const configured = translationConfigured();
  $('translation-section').classList.toggle('hidden', !supported);
  $('prompt-mode').disabled = !available || state.busy || state.compiling;
  const assistedOption = $('prompt-mode').querySelector('option[value="assisted"]');
  if (assistedOption) assistedOption.disabled = !configured;
  $('preview-translation').classList.toggle('hidden', !assisted);
  $('preview-translation').disabled = state.busy || state.compiling || !state.csrf || !configured || !currentModel();
  $('preview-translation').textContent = state.compiling ? '正在翻译…' : '预览翻译';
  const scope = state.config?.module === 'speech' ? '只翻译声音与表演要求，台词保持原文。' : state.config?.module === 'music' ? '只翻译音乐、音效描述或风格标签，歌词保持原文。' : '只翻译制作描述，负面描述和工作流保持原文。';
  $('translation-help').textContent = !available && supported ? '微调训练直接使用原始描述，不启用翻译辅助。' : !configured ? '未配置 LLM：直接模式、图片档案和已有资产可继续使用。配置 LLM 后可启用辅助翻译。' : assisted ? `由所选 LLM API 将描述转换为适合制作模型的英文提示。${scope}可先预览；提交时会重新翻译，最终参数保存在任务记录中。` : '直接将填写内容交给制作模型，保留原文，不调用翻译模型。';
}
function collectParams() {
  const params = {}; const title = $('task-title').value.trim(); if (title) params.title = title;
  for (const input of $('model-fields').querySelectorAll('input,select,textarea')) {
    if (input.disabled) continue;
    if (input.type === 'checkbox') params[input.name] = input.checked;
    else if (input.type === 'number' || input.dataset.numeric) { if (input.value !== '') params[input.name] = Number(input.value); }
    else if (input.value.trim() !== '') params[input.name] = input.value;
  }
  return params;
}
function taskFormData(includeFile = true) {
  const model = currentModel(); if (!model) throw new Error('请先选择制作模型。');
  const params = collectParams();
  if (model.protocol === 'comfyui') {
    for (const key of ['workflow','bindings']) {
      if (!params[key]) continue;
      let value; try { value = JSON.parse(params[key]); } catch { throw new Error(`${fields[key].label} 格式有误，请填写有效 JSON。`); }
      if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(`${fields[key].label} 必须为 JSON 对象。`);
    }
  }
  const form = new FormData(); form.append('model_id', model.id); form.append('params', JSON.stringify(params)); form.append('prompt_mode', promptMode());
  const file = !$('reference-file').disabled && $('reference-file').files[0];
  if (includeFile && file) {
    const max = $('reference-file').dataset.mode === 'zip' ? 256 : state.config.module === 'image' ? 200 : 16;
    if (!file.size || file.size > max * 1024 * 1024) throw new Error(!file.size ? '上传文件不能为空。' : `上传文件超出 ${max} MiB，请缩小文件后重试。`);
    form.append('file', file);
  } else if (!file && state.config.module === 'image' && state.reference) { form.append('profile_id', state.reference.profileId); form.append('image_id', state.reference.imageId); }
  return form;
}
function inputRecord(record) {
  return { prompt_mode: record.prompt_mode || 'direct', original_params: record.original_params || record.params || {}, params: record.params || {}, ...(record.translation ? { translation: record.translation } : {}) };
}
async function previewTranslation() {
  if (state.busy || state.compiling || promptMode() !== 'assisted') return;
  for (const input of $('model-fields').querySelectorAll('input,select,textarea')) if (!input.disabled && !input.reportValidity()) return;
  $('form-error').classList.add('hidden');
  try {
    const form = taskFormData(false); const revision = state.formRevision;
    state.compiling = true; updateReadiness();
    const result = await api('/api/compile', { method: 'POST', body: form });
    if (revision !== state.formRevision) { toast('输入已更新，请重新预览翻译。'); return; }
    const key = result.translation?.field || (state.config.module === 'speech' ? 'instruct' : 'prompt');
    const preview = $('translation-preview');
    preview.replaceChildren(el('p', { class: 'hint', text: '翻译预览 · 输入框保留原文' }), el('strong', { text: '原始描述' }), el('pre', { text: result.original_params?.[key] || '' }), el('strong', { text: '翻译后描述' }), el('pre', { text: result.params?.[key] || '' }), el('details', {}, [el('summary', { text: '查看完整参数对照' }), el('pre', { text: JSON.stringify(inputRecord(result), null, 2) })]));
    preview.classList.remove('hidden');
  } catch (error) { $('form-error').textContent = error.message; $('form-error').classList.remove('hidden'); }
  finally { state.compiling = false; updateReadiness(); }
}
async function submitTask(event) {
  event.preventDefault(); if (state.busy || state.compiling || !$('task-form').reportValidity()) return;
  $('form-error').classList.add('hidden'); const model = currentModel();
  if (!modelReady(model)) { toast('请先在主页配置并检测模型 API。'); return; }
  try {
    const form = taskFormData();
    state.busy = true; updateReadiness(); updateConditionalFields();
    const result = await api('/api/jobs', { method: 'POST', body: form }); const job = result.job || result;
    if (!job.id) throw new Error('任务响应缺少编号，请刷新任务队列确认是否已创建。');
    state.selected = job.id; state.jobs.unshift(job); state.resultKey = ''; renderJobs(); renderAssets(); renderSelected(); toast('任务已加入制作队列，可在任务队列查看进度。');
    await refreshJobs();
  } catch (error) { $('form-error').textContent = error.message; $('form-error').classList.remove('hidden'); }
  finally { state.busy = false; updateReadiness(); updateConditionalFields(); }
}
function emptyState(title, description, button = true) { const node = el('div', { class: 'empty' }, [el('span', { class: 'empty-symbol', text: '◇', 'aria-hidden': 'true' }), el('h3', { text: title }), el('p', { text: description })]); if (button) node.append(el('button', { class: 'primary', text: '开始制作 →', onclick: () => navigate('studio') })); return node; }
function matches(job, search) { return `${jobTitle(job)} ${job.model_id} ${modelLabel(job.model_id)} ${job.id} ${artifacts(job).map((item) => item.name).join(' ')}`.toLowerCase().includes(search.toLowerCase()); }
function renderJobs() {
  const filter = $('job-filter').value; const query = $('job-search').value.trim();
  const jobs = state.jobs.filter((job) => matches(job, query) && (filter === 'all' || filter === 'active' && ACTIVE.has(job.status) || filter === 'succeeded' && DONE.has(job.status) || filter === 'failed' && ['failed','error','interrupted'].includes(job.status) || filter === 'cancelled' && CANCELLED.has(job.status)));
  $('job-count').textContent = state.jobs.filter((job) => ACTIVE.has(job.status)).length || '';
  $('job-total').textContent = `${jobs.length} 项任务`; const target = $('job-list'); target.replaceChildren();
  if (!jobs.length) { target.append(emptyState(state.jobs.length ? '没有匹配的任务' : '还没有制作任务', state.jobs.length ? '调整搜索词或状态筛选后重试。' : '提交第一个任务，进度与历史记录会显示在这里。', !state.jobs.length)); return; }
  for (const job of jobs) {
    const primary = el('div', {}, [el('span', { class: 'job-title', text: jobTitle(job) }), el('small', { text: `#${job.id}` })]);
    const model = el('div', { text: job.model_label || modelLabel(job.model_id) }, [el('small', { text: prettyDate(job.created_at) }), job.owner?.name ? el('small', { class: 'owner', text: `创建者：${job.owner.name}` }) : null]);
    const status = el('div', {}, [badge(job.status)]); if (Number.isFinite(job.progress)) status.append(el('small', { text: `${Math.round(job.progress <= 1 ? job.progress * 100 : job.progress)}%` }));
    if (ACTIVE.has(job.status)) status.append(el('small', { text: activeJobMessage(job) }));
    if (job.error) status.append(el('small', { text: String(job.error).slice(0, 100), title: job.error }));
    const actions = el('div', { class: 'job-actions' }, [el('button', { text: '查看', onclick: () => selectJob(job.id) }), el('button', { text: '复用参数', onclick: () => reuseJob(job) })]);
    if (own(job)) {
      if (ACTIVE.has(job.status) && !['cancelling','cancel_requested'].includes(job.status)) actions.append(el('button', { class: 'danger', text: '取消', onclick: (event) => jobAction(job, 'cancel', event.currentTarget) }));
      else if (['failed','error','interrupted'].includes(job.status) || CANCELLED.has(job.status)) actions.append(el('button', { text: '重试', onclick: (event) => jobAction(job, 'retry', event.currentTarget) }));
      if (!ACTIVE.has(job.status)) actions.append(el('button', { class: 'danger', text: '删除整套资产', onclick: (event) => deleteJob(job, event.currentTarget) }));
    }
    target.append(el('article', { class: 'job-row' }, [primary, model, status, actions]));
  }
}
function renderAssets() {
  const jobs = state.jobs.filter((job) => DONE.has(job.status) && artifacts(job).length);
  const filtered = jobs.filter((job) => matches(job, $('asset-search').value.trim()));
  $('asset-count').textContent = jobs.length || ''; $('asset-total').textContent = `${filtered.length} 组资产`; const target = $('asset-list'); target.replaceChildren();
  if (!filtered.length) { target.append(emptyState(jobs.length ? '没有匹配的资产' : '资产库等待第一份成果', jobs.length ? '试试任务名称、模型名称或文件名。' : '制作完成后，结果文件与任务参数会自动出现在这里。', !jobs.length)); return; }
  for (const job of filtered) {
    const files = artifacts(job); const main = files.find((item) => ['image','audio','model'].includes(fileKind(item))) || files[0]; const kind = fileKind(main);
    const cover = el('button', { class: 'card-cover', 'aria-label': `预览：${jobTitle(job)}`, onclick: () => openAsset(job) });
    if (kind === 'image') cover.append(el('img', { src: safeArtifactURL(main.url), alt: jobTitle(job), loading: 'lazy' })); else cover.append(el('span', { class: 'card-symbol', text: kind === 'audio' ? '♫' : kind === 'model' ? '◇' : '▤', 'aria-hidden': 'true' }));
    cover.append(el('span', { class: 'pill', text: `${files.length} 个文件` }));
    const body = el('div', { class: 'card-body' }, [el('h3', { text: jobTitle(job) }), el('p', { text: job.model_label || modelLabel(job.model_id) }), el('p', { text: prettyDate(job.created_at) }), job.owner?.name ? el('p', { class: 'owner', text: `创建者：${job.owner.name}` }) : null, el('div', { class: 'form-actions' }, [el('button', { text: '预览与下载', onclick: () => openAsset(job) }), el('button', { text: '复用参数', onclick: () => reuseJob(job) })])]);
    if (own(job)) body.querySelector('.form-actions').append(el('button', { class: 'danger', text: '删除整套资产', onclick: (event) => deleteJob(job, event.currentTarget) }));
    target.append(el('article', { class: 'card' }, [cover, body]));
  }
}
function releasePreviews(dialog = false) { const key = dialog ? 'dialogDisposers' : 'disposers'; for (const dispose of state[key]) dispose(); state[key] = []; }
function showArtifact(container, artifact, dialog = false) {
  container.replaceChildren(); container.className = 'result-stage'; const kind = fileKind(artifact); const url = safeArtifactURL(artifact.url); if (!url) return;
  if (kind === 'image') { container.classList.add('has-image'); const img = el('img', { src: url, alt: artifact.name || '生成图片' }); img.addEventListener('error', () => { container.replaceChildren(emptyState('图片预览未能加载', '可尝试刷新，或使用下方链接下载原文件。', false)); }); container.append(img); }
  else if (kind === 'audio') {
    const art = el('div', { class: 'audio-art', 'aria-hidden': 'true' }); for (let i = 0; i < 25; i++) art.append(el('i'));
    const audio = el('audio', { controls: true, preload: 'metadata', src: url, 'aria-label': artifact.name || '生成音频' });
    container.append(el('div', { class: 'audio-preview' }, [art, el('h3', { text: artifact.name || '音频制作成果' }), audio]));
    state[dialog ? 'dialogDisposers' : 'disposers'].push(() => { audio.pause(); audio.removeAttribute('src'); audio.load(); });
  } else if (kind === 'model' && /\.glb(?:\?|$)/i.test(artifact.url)) {
    const root = el('div', { class: 'model-viewer' }); container.append(root); const controller = new AbortController(); let dispose;
    state[dialog ? 'dialogDisposers' : 'disposers'].push(() => { controller.abort(); dispose?.(); });
    mountModelPreview(root, url, controller.signal).then((cleanup) => { dispose = cleanup; if (controller.signal.aborted) dispose?.(); }).catch((error) => { if (controller.signal.aborted) return; container.replaceChildren(emptyState('3D 预览暂不可用', `可下载 GLB 到 Blender 查看。${error.message ? `（${error.message}）` : ''}`, false)); });
  } else container.append(emptyState('文件已生成', '下载文件后，可在相应制作工具中继续使用。', false));
}
function renderSelected() {
  const job = state.jobs.find((item) => item.id === state.selected);
  if (!job) { if (state.resultKey) { releasePreviews(); state.resultKey = ''; $('studio-status').textContent = '等待任务'; $('studio-status').className = 'pill'; $('result-stage').className = 'result-stage'; $('result-stage').replaceChildren(emptyState('等待制作结果', '选择模型并提交任务，制作成果会显示在这里。', false)); $('studio-detail').replaceChildren(); $('studio-downloads').replaceChildren(); $('task-details').classList.add('hidden'); } return; }
  const key = JSON.stringify([job.id, job.status, job.updated_at, job.error, job.progress, job.message, job.waiting, job.artifacts]); if (key === state.resultKey) return; state.resultKey = key;
  $('studio-status').textContent = statusLabels[job.status] || job.status; $('studio-status').className = `pill ${statusClass(job.status)}`;
  const detail = $('studio-detail'); detail.replaceChildren(el('strong', { text: jobTitle(job) }), el('p', { text: `${modelLabel(job.model_id)} · ${prettyDate(job.created_at)} · #${job.id}` }));
  detail.append(el('p', { text: job.prompt_mode === 'assisted' ? '描述处理：LLM 翻译辅助。原始输入与实际制作参数可在下方查看。' : '描述处理：直接使用原文。' }));
  $('task-details').classList.remove('hidden'); $('task-json').textContent = JSON.stringify({ id: job.id, model_id: job.model_id, ...inputRecord(job), created_at: job.created_at }, null, 2);
  releasePreviews(); const stage = $('result-stage'); stage.replaceChildren(); stage.className = 'result-stage'; const downloads = $('studio-downloads'); downloads.replaceChildren(); const files = artifacts(job);
  if (DONE.has(job.status) && files.length) {
    const primary = files.find((item) => ['image','audio','model'].includes(fileKind(item))) || files[0]; showArtifact(stage, primary);
    downloads.append(...files.map(downloadLink), el('button', { text: '查看全部详情', onclick: () => openAsset(job) }));
  } else if (ACTIVE.has(job.status)) {
    const block = emptyState(statusLabels[job.status] || '正在制作', activeJobMessage(job), false);
    block.append(el('div', { class: 'progress-line', 'aria-hidden': 'true' }, [el('span')])); stage.append(block);
    if (own(job) && !['cancelling','cancel_requested'].includes(job.status)) downloads.append(el('button', { class: 'danger', text: '取消任务', onclick: (event) => jobAction(job, 'cancel', event.currentTarget) }));
  } else { stage.append(emptyState(CANCELLED.has(job.status) ? '本次任务已取消' : DONE.has(job.status) ? '任务完成，未返回可预览文件' : '制作暂未完成', job.error || '可调整参数后再次提交。', false)); if (job.error) detail.append(el('p', { class: 'form-error', text: job.error })); if (own(job) && !DONE.has(job.status)) downloads.append(el('button', { text: '重试此任务', onclick: (event) => jobAction(job, 'retry', event.currentTarget) })); }
  if (own(job)) downloads.append(el('button', { text: '复用参数', onclick: () => reuseJob(job) }));
  if (own(job) && !ACTIVE.has(job.status)) downloads.append(el('button', { class: 'danger', text: '删除整套资产', onclick: (event) => deleteJob(job, event.currentTarget) }));
}
function selectJob(id) { state.selected = id; state.resultKey = ''; navigate('studio'); renderSelected(); $('result-stage').scrollIntoView({ behavior: 'smooth', block: 'center' }); }
function reuseJob(job) {
  const available = state.models.find(model => model.id === job.model_id) || currentModel();
  if (!available) { toast('请先选择本次制作使用的模型。'); return; }
  $('model-select').value = available.id; $('prompt-mode').value = job.prompt_mode === 'assisted' ? 'assisted' : 'direct'; clearFile(); state.reference = null; renderReferenceImages(); renderFields(job.original_params || job.params || {}); $('task-title').value = job.title || job.params?.title || ''; $('asset-dialog').close(); navigate('studio'); $('task-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
  toast(!$('reference-file').disabled ? '已复用参数，请重新选择本次所需的参考文件。' : '已复用参数，调整后即可提交新任务。');
}
async function jobAction(job, action, button) {
  button.disabled = true;
  try { const result = await api(`/api/jobs/${encodeURIComponent(job.id)}/${action}${state.scope ? '?scope=all' : '?scope=mine'}`, { method: 'POST' }); const changed = result.job || result; if (action === 'retry' && changed.id) state.selected = changed.id; state.resultKey = ''; toast(action === 'cancel' ? '取消请求已提交。' : '重试任务已加入队列。'); await refreshJobs(); }
  catch (error) { toast(error.message); }
  finally { if (button.isConnected) button.disabled = false; }
}
async function deleteJob(job, button) {
  if (!window.confirm(`确定删除“${jobTitle(job)}”的整套资产？任务输入副本、全部生成文件和中间文件都会从服务器硬盘永久删除，无法恢复。独立图片档案不受影响。`)) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${encodeURIComponent(job.id)}${state.scope ? '?scope=all' : '?scope=mine'}`, { method: 'DELETE' });
    if (state.selected === job.id) state.selected = null;
    $('asset-dialog').close(); releasePreviews(); releasePreviews(true); state.resultKey = 'deleted';
    await refreshJobs(); toast('整套任务资产已从服务器硬盘删除。');
  } catch (error) { toast(error.message); }
  finally { if (button.isConnected) button.disabled = false; }
}
function openAsset(job) {
  releasePreviews(true); const dialog = $('asset-dialog'); const target = $('asset-detail'); target.replaceChildren(); $('dialog-title').textContent = jobTitle(job);
  const files = artifacts(job); const stage = el('div', { class: 'result-stage' }); target.append(stage); const primary = files.find((item) => ['image','audio','model'].includes(fileKind(item))) || files[0]; if (primary) showArtifact(stage, primary, true);
  target.append(el('p', { class: 'hint', text: `${modelLabel(job.model_id)} · ${prettyDate(job.created_at)} · #${job.id}` }));
  const list = el('div', { class: 'artifact-list' }); for (const artifact of files) { const row = el('div', { class: 'artifact-row' }, [el('span', { text: artifact.name || '结果文件' }), el('div', { class: 'form-actions' }, [ ['image','audio','model'].includes(fileKind(artifact)) ? el('button', { text: '预览', onclick: () => { releasePreviews(true); showArtifact(stage, artifact, true); } }) : null, downloadLink(artifact)])]); list.append(row); } target.append(list);
  target.append(el('details', {}, [el('summary', { text: '原始输入与实际制作参数' }), el('p', { class: 'hint', text: 'original_params 为原始输入；params 为交给制作模型的实际参数。' }), el('pre', { text: JSON.stringify(inputRecord(job), null, 2) })]));
  target.append(el('div', { class: 'form-actions' }, [el('button', { text: '复用参数制作新任务', onclick: () => reuseJob(job) })])); if (!dialog.open) dialog.showModal();
  if (own(job) && !ACTIVE.has(job.status)) target.append(el('button', { class: 'danger', text: '删除整套资产', onclick: (event) => deleteJob(job, event.currentTarget) }));
}
function renderReferenceImages() {
  const profile = state.profiles.find((item) => item.id === $('profile-reference-select').value);
  const target = $('profile-reference-images'); target.replaceChildren();
  if (state.reference && !state.profiles.some((item) => item.id === state.reference.profileId && item.images.some((image) => image.id === state.reference.imageId))) state.reference = null;
  for (const image of profile?.images || []) {
    const selected = state.reference?.profileId === profile.id && state.reference?.imageId === image.id;
    target.append(el('button', { type: 'button', class: `reference-choice${selected ? ' selected' : ''}`, 'aria-pressed': selected ? 'true' : 'false', onclick: () => { clearFile(); state.reference = selected ? null : { profileId: profile.id, imageId: image.id }; renderReferenceImages(); } }, [el('img', { src: image.url, alt: image.label, loading: 'lazy' }), el('span', { text: image.label })]));
  }
  const selectedProfile = state.profiles.find((item) => item.id === state.reference?.profileId);
  const selectedImage = selectedProfile?.images.find((item) => item.id === state.reference?.imageId);
  $('profile-reference-status').textContent = selectedImage ? `本次参考图：${selectedProfile.name} / ${selectedImage.label}。再次点击可取消选择。` : profile && !profile.images.length ? '此档案还没有图片，请在图片档案中新增。' : '点击一张图片作为本次参考图，或在下方临时上传。未选图时使用文字生成。';
}
function renderProfiles() {
  const list = $('profile-list'); list.replaceChildren();
  if (!state.profiles.length) list.append(el('p', { class: 'hint', text: '还没有图片档案。在左侧导入第一组图片。' }));
  for (const profile of state.profiles) list.append(el('button', { type: 'button', class: `profile-list-item${profile.id === state.profile ? ' selected' : ''}`, onclick: () => { state.profile = profile.id; renderProfiles(); } }, [profile.images[0] ? el('img', { src: profile.images[0].url, alt: '', loading: 'lazy' }) : null, el('span', {}, [el('strong', { text: profile.name }), el('small', { text: `${profile.images.length} 张图片 · ${profile.owner.name}` })])]));
  const selected = $('profile-reference-select').value;
  $('profile-reference-select').replaceChildren(el('option', { value: '', text: '不使用档案图片' }), ...state.profiles.map((profile) => el('option', { value: profile.id, text: `${profile.name}（${profile.images.length} 张）` })));
  if (state.profiles.some((profile) => profile.id === selected)) $('profile-reference-select').value = selected;
  renderReferenceImages();
  const profile = state.profiles.find((item) => item.id === state.profile); const target = $('profile-detail'); target.replaceChildren();
  target.classList.toggle('hidden', !profile); $('profile-add-form').classList.toggle('hidden', !profile?.editable);
  if (!profile) return;
  target.append(el('div', { class: 'section-heading' }, [el('div', {}, [el('h3', { text: profile.name }), el('p', { class: 'hint', text: profile.description || '每张图片都可以使用自由标签标注用途。' }), el('p', { class: 'hint', text: `${profile.owner.name} · ${profile.images.length} 张图片` })])]));
  if (profile.editable) {
    const name = el('input', { value: profile.name, required: true, maxlength: 100, 'aria-label': '档案名称' });
    const description = el('textarea', { maxlength: 2000, rows: 2, 'aria-label': '档案说明', text: profile.description });
    const edit = el('form', { class: 'profile-edit', onsubmit: async (event) => { event.preventDefault(); await profileAction(async () => api(`/api/profiles/${profile.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.value, description: description.value }) }), '档案信息已保存。'); } }, [el('label', {}, [el('span', { text: '档案名称' }), name]), el('label', {}, [el('span', { text: '档案说明' }), description]), el('button', { type: 'submit', text: '保存档案信息' })]);
    target.append(edit);
  }
  const gallery = el('div', { class: 'profile-gallery' });
  for (const image of profile.images) {
    const card = el('article', { class: 'profile-image' }, [el('a', { href: image.url, target: '_blank', rel: 'noopener', title: '查看原图' }, [el('img', { src: image.url, alt: image.label, loading: 'lazy' })]), el('strong', { text: image.label }), el('p', { class: 'hint', text: `${image.width} × ${image.height} · ${image.original_name}` })]);
    card.append(el('button', { type: 'button', text: '用这张图制作', onclick: () => { clearFile(); $('profile-reference-select').value = profile.id; state.reference = { profileId: profile.id, imageId: image.id }; renderReferenceImages(); navigate('studio'); } }));
    if (profile.editable) {
      const label = el('input', { value: image.label, required: true, maxlength: 80, 'aria-label': `${image.label} 的标签` });
      card.append(el('form', { onsubmit: async (event) => { event.preventDefault(); await profileAction(() => api(`/api/profiles/${profile.id}/images/${image.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: label.value }) }), '图片标签已保存。'); } }, [label, el('button', { type: 'submit', text: '保存标签' })]));
      card.append(el('button', { type: 'button', class: 'danger', text: '删除这张图片', onclick: async () => { if (!window.confirm(`确定从“${profile.name}”中永久删除“${image.label}”？已创建任务中的参考图副本仍保留。`)) return; await profileAction(() => api(`/api/profiles/${profile.id}/images/${image.id}`, { method: 'DELETE' }), '图片已从档案及硬盘删除。'); } }));
    }
    gallery.append(card);
  }
  if (!profile.images.length) gallery.append(el('p', { class: 'hint', text: '档案暂时没有图片，可以在下方添加。' }));
  target.append(gallery);
  if (profile.editable) target.append(el('button', { type: 'button', class: 'danger', text: '删除整个档案', onclick: async () => { if (!window.confirm(`确定永久删除“${profile.name}”及其中的 ${profile.images.length} 张图片？已创建的任务不受影响。`)) return; await profileAction(() => api(`/api/profiles/${profile.id}`, { method: 'DELETE' }), '档案及其中的图片已删除。'); } }));
}
async function refreshProfiles() {
  const result = await api('/api/profiles'); state.profiles = result.items || [];
  if (!state.profiles.some((profile) => profile.id === state.profile)) state.profile = state.profiles[0]?.id || null;
  renderProfiles();
}
async function profileAction(action, message) {
  try { await action(); await refreshProfiles(); toast(message); }
  catch (error) { toast(error.message); }
}
function importLabels(prefix) {
  const input = $(`profile-${prefix}-files`); const target = $(`profile-${prefix}-labels`); target.replaceChildren();
  for (const [index, file] of Array.from(input.files).entries()) target.append(el('label', {}, [el('span', { text: `${index + 1}. ${file.name} 的标签` }), el('input', { name: 'label', required: true, maxlength: 80, value: file.name.replace(/\.[^.]+$/, '').slice(0, 80) })]));
}
async function submitProfile(event, prefix) {
  event.preventDefault(); const formNode = event.currentTarget; if (!formNode.reportValidity()) return;
  const files = Array.from($(`profile-${prefix}-files`).files);
  if (!files.length || files.length > 16 || files.some((file) => !file.size || file.size > 200 * 1024 * 1024) || files.reduce((size, file) => size + file.size, 0) > 256 * 1024 * 1024) { toast('每批选择 1 至 16 张非空图片，单图最多 200 MiB，总计最多 256 MiB。'); return; }
  const form = new FormData(); for (const file of files) form.append('files', file);
  form.append('labels', JSON.stringify(Array.from($(`profile-${prefix}-labels`).querySelectorAll('input')).map((input) => input.value.trim())));
  if (prefix === 'create') { form.append('name', $('profile-name').value.trim()); form.append('description', $('profile-description').value.trim()); }
  const button = formNode.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    const result = await api(prefix === 'create' ? '/api/profiles' : `/api/profiles/${state.profile}/images`, { method: 'POST', body: form });
    state.profile = result.id; formNode.reset(); $(`profile-${prefix}-labels`).replaceChildren(); await refreshProfiles(); toast(prefix === 'create' ? '图片档案已建立。' : '图片已加入档案。');
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}
async function refreshJobs() {
  if (state.refreshPromise) return state.refreshPromise;
  state.refreshPromise = (async () => {
    const data = await api(`/api/jobs${state.scope ? '?scope=all' : '?scope=mine'}`); state.jobs = (Array.isArray(data) ? data : data.jobs || []).sort((a,b) => new Date(b.created_at) - new Date(a.created_at));
    if (!state.selected && state.jobs.length) state.selected = state.jobs[0].id;
    renderJobs(); renderAssets(); renderSelected(); $('connection').textContent = '已连接'; $('connection').className = 'pill';
  })();
  try { await state.refreshPromise; } finally { state.refreshPromise = null; }
}
async function refreshConfig(initial = false) {
  const config = await api('/api/config'); const previous = $('model-select').value; state.config = config; state.models = config.models || []; state.csrf = config.csrf || state.csrf;
  const user = config.user || config.current_user || {}; const copy = moduleCopy[config.module] || moduleCopy.image;
  document.title = `Fantasy · ${config.title || copy.name}`; $('brand-title').textContent = config.title || copy.name; $('module-eyebrow').textContent = copy.eyebrow; $('intro-title').textContent = copy.title; $('intro-description').textContent = config.description || copy.description; $('intro-icon').textContent = copy.icon; $('guide-title').textContent = copy.guide; $('guide-text').textContent = copy.help;
  $('current-user').textContent = user.name ? `${user.name}${user.admin || user.is_admin ? ' · 管理员' : ''}` : '';
  for (const link of document.querySelectorAll('.home-link,#home')) link.href = homeURL();
  $('logout').href = config.logout_url || new URL('/logout', homeURL()).href;
  $('profiles-nav').classList.toggle('hidden', !config.image_profiles);
  for (const label of document.querySelectorAll('.scope-toggle')) label.classList.remove('hidden');
  $('model-select').replaceChildren(...state.models.map((model) => el('option', { value: model.id, text: `${model.label || model.id}${modelReady(model) ? '' : ' · 未就绪'}` })));
  if (state.models.some((model) => model.id === previous)) $('model-select').value = previous;
  else $('model-select').value = state.models.find(modelReady)?.id || state.models[0]?.id || '';
  if (initial || previous !== $('model-select').value) renderFields(); else updateReadiness();
  if (!state.models.length) { $('model-select').append(el('option', { value: '', text: '未配置生成 API · 可编辑制作描述' })); $('model-readiness').textContent = '制作描述、图片档案和已有资产可用。已配置 LLM 时可预览翻译；生成作品前请配置生成 API。'; $('generate').disabled = true; }
  $('auth-error').classList.add('hidden'); $('loading').classList.add('hidden'); navigate(state.page);
}
async function refreshAll(initial = false) {
  if (state.loading) return; state.loading = true; $('refresh').disabled = true;
  try {
    await refreshConfig(initial);
    if (!state.csrf) { const session = await api('/api/session'); state.csrf = session.csrf || ''; updateReadiness(); }
    await refreshJobs(); showError('');
    if (state.config.image_profiles) await refreshProfiles();
  } catch (error) { $('loading').classList.add('hidden'); $('connection').textContent = '连接中断'; $('connection').className = 'pill offline'; showError(error.status === 401 ? '会话已过期，请返回主页面登录后刷新。' : `暂时无法读取工作台：${error.message}。请稍后刷新。`); }
  finally { state.loading = false; $('refresh').disabled = false; schedulePoll(); }
}
function schedulePoll() { clearTimeout(state.poll); state.poll = setTimeout(async () => { if (document.hidden || !state.config) { schedulePoll(); return; } try { await refreshJobs(); if (Date.now() - (schedulePoll.configAt || 0) > 30000) { await refreshConfig(); schedulePoll.configAt = Date.now(); } showError(''); } catch (error) { $('connection').textContent = '连接中断'; $('connection').className = 'pill offline'; if (error.status === 401) showError('会话已过期，请返回主页面重新登录。'); } finally { schedulePoll(); } }, 5000); }
for (const button of document.querySelectorAll('nav [data-page]')) button.addEventListener('click', () => navigate(button.dataset.page));
document.querySelector('.brand').addEventListener('click', () => navigate('studio'));
$('model-select').addEventListener('change', () => { const selected=currentModel(), module=state.config?.module; if(selected?.kind&&!selected.preview_only&&module){ window.FantasyModels?.set(module,selected.kind,selected.id); if(module==='image'&&['image','workflow'].includes(selected.kind))window.FantasyModels?.set(module,selected.kind==='image'?'workflow':'image',null); } clearFile(); renderFields(); $('form-error').classList.add('hidden'); });
window.addEventListener('storage', async event => {
  if (!state.config || event.key !== window.FantasyModels?.storageKey?.()) return;
  const draft = collectParams();
  try {
    await refreshConfig();
    const selected = state.models.find(model => model.default);
    if (selected) $('model-select').value = selected.id;
    else { $('model-select').append(el('option', {value: '', text: '未选择生成 API · 可编辑制作描述'})); $('model-select').value = ''; }
    invalidateTranslation(); renderFields(draft); updateReadiness();
  } catch (error) { toast(error.message); }
});
$('task-form').addEventListener('submit', submitTask); $('refresh').addEventListener('click', () => refreshAll());
$('task-form').addEventListener('input', invalidateTranslation);
$('prompt-mode').addEventListener('change', () => { invalidateTranslation(); updateTranslationControls(); });
$('preview-translation').addEventListener('click', previewTranslation);
$('reference-file').addEventListener('change', previewFile); $('clear-file').addEventListener('click', clearFile);
$('profile-reference-select').addEventListener('change', () => { state.reference = null; renderReferenceImages(); });
for (const prefix of ['create', 'add']) { $(`profile-${prefix}-files`).addEventListener('change', () => importLabels(prefix)); $(`profile-${prefix}-form`).addEventListener('submit', (event) => submitProfile(event, prefix)); }
$('job-search').addEventListener('input', renderJobs); $('job-filter').addEventListener('change', renderJobs); $('asset-search').addEventListener('input', renderAssets);
for (const id of ['jobs-all','assets-all']) $(id).addEventListener('change', async (event) => { state.scope = event.target.checked; state.selected = null; $('jobs-all').checked = state.scope; $('assets-all').checked = state.scope; try { await refreshJobs(); } catch (error) { toast(error.message); } });
$('dialog-close').addEventListener('click', () => $('asset-dialog').close()); $('asset-dialog').addEventListener('close', () => releasePreviews(true));
window.addEventListener('hashchange', () => navigate(location.hash.slice(1))); document.addEventListener('visibilitychange', () => { if (!document.hidden && state.config) refreshAll(); });
window.addEventListener('pagehide', () => { clearTimeout(state.poll); releasePreviews(); releasePreviews(true); if (state.fileURL) URL.revokeObjectURL(state.fileURL); });
state.page = location.hash.slice(1) || 'studio'; refreshAll(true);

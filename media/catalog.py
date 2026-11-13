"""Provider-independent fields; no model names, runtimes or GPU policy."""
import copy
import math

from providers import registry, resolve
from .comfy_workflow import validate_workflow
from .training import validate_dataset


def text(label, maximum=4000, required=False, default=''):
    return dict(type='textarea', label=label, maxLength=maximum, required=required, default=default)


def number(label, default, low, high, step=1):
    return dict(type='number', label=label, default=default, min=low, max=high, step=step)


COMMON = {'title': text('任务名称', 100), 'seed': number('随机种子', 42, 0, 2147483647)}
FIELDS = {
    'image': {'prompt': text('图片描述 / 编辑指令', required=True),
              'width': number('宽度', 1024, 256, 2048, 16), 'height': number('高度', 1024, 256, 2048, 16),
              'steps': number('采样步数', 32, 1, 100)},
    'workflow': {'prompt': text('图片描述'), 'negative_prompt': text('负面描述'),
                 'width': number('宽度', 1024, 64, 4096, 8), 'height': number('高度', 1024, 64, 4096, 8),
                 'workflow': text('ComfyUI API 工作流 JSON', 65536, True), 'bindings': text('参数绑定 JSON', 2048, default='{}')},
    'speech': {'text': text('朗读文本', 3000, True), 'instruct': text('声音与表演要求', 2000),
               'speaker': text('音色 ID', 100, default='alloy'), 'language': text('语言', 100, default='Chinese')},
    'music': {'prompt': text('音乐 / 音效描述'), 'lyrics': text('歌词', 12000),
              'tags': text('音乐风格标签', 2000), 'steps': number('采样步数', 50, 1, 200),
              'duration': number('时长（秒）', 30, 1, 300, 0.1),
              'instrumental': {'type': 'checkbox', 'label': '纯音乐', 'default': True}},
    'model3d': {'prompt': text('三维资产描述'), 'resolution': text('分辨率', 100, default='1024'),
                'steps': number('采样步数', 12, 1, 50),
                'texture_size': number('纹理大小', 2048, 512, 4096, 512),
                'decimation_target': number('目标面数', 100000, 10000, 1000000)},
}
TRAIN_FIELDS = {
    'task': dict(type='select', label='制作方式', default='generate', options=['generate', 'train']),
    'epochs': number('训练轮数', 10, 1, 1000),
    'rank': dict(type='select', label='LoRA 秩', default=16, options=[8, 16, 32, 64], numeric=True),
    'learning_rate': number('学习率', 0.0001, 0.000001, 0.001, 0.000001),
}

DRAFT_MODEL_ID = '__prompt_draft__'


def prompt_draft(module):
    """A local parameter form, never an API provider or a generation target."""
    if module not in {'image', 'speech', 'music', 'model3d'}:
        raise ValueError('Unsupported draft module')
    return {'id': DRAFT_MODEL_ID, 'module': module, 'kind': module, 'protocol': 'draft',
            'label': '制作描述草稿', 'name': '制作描述草稿', 'ready': False, 'enabled': False,
            'preview_only': True, 'fields': {**copy.deepcopy(COMMON), **copy.deepcopy(FIELDS[module])},
            'input': 'required' if module == 'model3d' else 'optional' if module == 'image' else None,
            'description': '可先编辑制作描述；已配置 LLM 时可预览辅助翻译。生成作品时选择对应生成 API。'}


def model_catalog():
    selected = {(r['module'], r['role']): r['profile_id'] for r in registry().list_bindings()}
    result = []
    for profile in registry().list_profiles():
        kind = profile['kind']
        if kind not in FIELDS or not profile['verified']:
            continue
        module = 'image' if kind == 'workflow' else kind
        training = kind == 'music' and profile['protocol'] == 'rest' and 'music_train' in profile.get('capabilities', [])
        fields = {**copy.deepcopy(COMMON), **copy.deepcopy(FIELDS[kind])}
        if training:
            fields.update(copy.deepcopy(TRAIN_FIELDS))
        result.append({'id': profile['id'], 'module': module, 'kind': kind,
            'protocol': profile['protocol'], 'label': profile['name'], 'name': profile['name'],
            'model': profile['model'], 'fields': fields, 'training': training,
            'input': 'required' if kind == 'model3d' else 'optional' if module == 'image' else None,
            'ready': True, 'enabled': True, 'default': selected.get((module, kind)) == profile['id'],
            'description': '通过已验证的模型 API 制作，作品保存在当前社区版。'})
    return sorted(result, key=lambda item: (not item['default'], item['label']))


def validate(module, model_id, params, input_path=None, *, preview=False):
    models = {row['id']: row for row in model_catalog()}
    model = prompt_draft(module) if preview and model_id == DRAFT_MODEL_ID else models.get(model_id)
    if model is None or model['module'] != module:
        raise ValueError('模型配置不存在、未通过检测，或不适用于此模块。')
    profile = None if model.get('preview_only') else resolve(module, model['kind'], model_id)
    if not isinstance(params, dict) or set(params) - set(model['fields']):
        raise ValueError('任务参数包含未知字段。')
    result = {}
    for key, field in model['fields'].items():
        value = params.get(key, field.get('default'))
        if field['type'] == 'textarea':
            if not isinstance(value, str) or '\0' in value or len(value) > field['maxLength']:
                raise ValueError(field['label'] + '格式无效或过长。')
            if field.get('required') and not value.strip():
                raise ValueError('请填写' + field['label'])
        elif field['type'] == 'checkbox':
            if type(value) is not bool:
                raise ValueError(field['label'] + '必须为开关值。')
        elif field['type'] == 'select':
            choices = {str(option): option for option in field['options']}
            if str(value) not in choices:
                raise ValueError(field['label'] + '选项无效。')
            value = choices[str(value)]
        else:
            if type(value) not in (int, float) or not math.isfinite(value) or not field['min'] <= value <= field['max']:
                raise ValueError(field['label'] + '超出允许范围。')
            if field['step'] >= 1:
                if value != int(value) or int(value) % field['step']:
                    raise ValueError(field['label'] + '步长无效。')
                value = int(value)
        result[key] = value
    if model['input'] == 'required' and not input_path:
        raise ValueError('请上传参考图片。')
    training = result.get('task') == 'train'
    if module == 'music' and not (result['prompt'].strip() or not training and result['tags'].strip()):
        raise ValueError('请填写音乐描述' + ('。' if training else '或音乐风格标签。'))
    if training:
        if not model['training']:
            raise ValueError('此 API 未验证音乐训练能力。')
        if result['duration'] > 240:
            raise ValueError('训练样本裁剪上限为 240 秒。')
        if not input_path and not preview:
            raise ValueError('请上传音频 ZIP 训练数据集。')
        if input_path:
            validate_dataset(input_path)
    elif input_path and not model['input']:
        raise ValueError('该 API 类型不接受参考图。')
    elif input_path and str(input_path).lower().endswith('.zip'):
        raise ValueError('图片和 3D 生成只接受 PNG、JPEG 或 WebP 参考图。')
    if model['kind'] == 'workflow':
        validate_workflow(result, has_image=None if preview and not input_path else bool(input_path), allowed_nodes=profile['options'].get('allowed_nodes', []))
    return model, profile, result

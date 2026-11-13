"""Shared API access. All endpoints and credentials come from the registry."""
from functools import lru_cache
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import hashlib
import json
import os
from pathlib import Path


_model_context = ContextVar('fantasy_model_context', default=None)


def parse_model_selection(raw):
    """Validate untrusted browser selectors. Never accepts credentials or actors."""
    from portal.registry import MODULE_ROLES, RegistryError
    if raw in (None, ''):
        return {}
    if isinstance(raw, str):
        if len(raw.encode('utf-8')) > 8192:
            raise RegistryError('模型选择超过大小限制')
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise RegistryError('模型选择格式无效') from None
    if not isinstance(raw, dict) or len(json.dumps(raw).encode()) > 8192:
        raise RegistryError('模型选择必须是大小合理的对象')
    result = {}
    for module, choices in raw.items():
        if module not in MODULE_ROLES or not isinstance(choices, dict):
            raise RegistryError('模型选择包含未知模块')
        result[module] = {}
        for role, identifier in choices.items():
            if role not in MODULE_ROLES[module] or (identifier is not None and
                    (not isinstance(identifier, str) or not 1 <= len(identifier) <= 64 or
                     not all(ch.isalnum() or ch in '_-' for ch in identifier))):
                raise RegistryError('模型用途或配置 ID 无效')
            result[module][role] = identifier
    return result


def current_model_context():
    return _model_context.get()


@contextmanager
def model_context(user, selections=None):
    """The caller supplies a server-authenticated actor, never request JSON."""
    actor = None if user is None else {'id': str(user['id']), 'admin': bool(user.get('admin', False))}
    token = _model_context.set({'user': actor, 'selections': parse_model_selection(selections)})
    try:
        yield
    finally:
        _model_context.reset(token)


def capture_model_context():
    context = current_model_context()
    if context is None:
        return None
    from portal.registry import MODULE_ROLES
    result = json.loads(json.dumps(context))
    defaults = {(row['module'], row['role']): row['profile_id'] for row in registry().list_bindings(effective=False)}
    result['selections'] = {module: {role: context['selections'].get(module, {}).get(role, defaults.get((module, role)))
                                   for role in roles} for module, roles in MODULE_ROLES.items()}
    return result


@contextmanager
def restore_model_context(snapshot, *, owner_id):
    """Restore a server-stored queue snapshot, checking its immutable owner."""
    from portal.registry import RegistryError
    if snapshot is None:
        snapshot = {'user': {'id': str(owner_id), 'admin': False}, 'selections': {}}
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('user'), dict)
            or snapshot['user'].get('id') != str(owner_id)):
        raise RegistryError('任务的模型配置身份与所属账号不一致')
    # Jobs need to use models, not administer global profiles.
    with model_context({'id': str(owner_id), 'admin': False}, snapshot.get('selections')):
        yield


def data_root():
    return Path(os.environ.get('COMMUNITY_DATA_DIR', '/data')).resolve()


@lru_cache(maxsize=8)
def _registry(path):
    from portal.registry import Registry
    return Registry(path)


def registry():
    return _registry(str(data_root()))


def fingerprint(profile):
    """Changing a key is safe; changing embedding space requires reindexing."""
    return hashlib.sha256(json.dumps({k: profile.get(k) for k in
        ('id', 'kind', 'protocol', 'base_url', 'model', 'options')},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def invoke(module, role, payload, profile_id=None, *, expected_fingerprint=None):
    return await registry().invoke(module, role, payload, profile_id=profile_id,
                                   expected_fingerprint=expected_fingerprint)


def invoke_sync(module, role, payload, profile_id=None, *, expected_fingerprint=None):
    return asyncio.run(invoke(module, role, payload, profile_id, expected_fingerprint=expected_fingerprint))


def resolve(module, role, profile_id=None):
    return registry().resolve(module, role, profile_id=profile_id)


def configured(module, role):
    try:
        return resolve(module, role)
    except (ValueError, KeyError, RuntimeError):
        return None


def feature_capabilities(module, source=None):
    """Describe independent features without turning missing AI into a module lock."""
    from portal.registry import MODULE_ROLES
    source = source or registry()
    roles = {}
    for role in MODULE_ROLES.get(module, ()):
        try:
            source.resolve(module, role)
            roles[role] = True
        except (ValueError, KeyError, RuntimeError):
            roles[role] = False

    def feature(label, *required, alternatives=()):
        missing = [role for role in required if not roles.get(role)]
        if alternatives and not any(roles.get(role) for role in alternatives):
            missing.append('|'.join(alternatives))
        return {'label': label, 'available': not missing, 'missing_roles': missing}

    features = {'management': feature('资料、档案与已有内容管理')}
    if module == 'knowledge':
        features.update(
            raw_qa=feature('原文问答', alternatives=('llm', 'vision')),
            image_qa=feature('图片理解', 'vision'),
            vector_index=feature('向量入库', 'embedding'),
            vector_search=feature('向量检索', 'embedding'),
            rag_qa=feature('向量检索问答', 'embedding', alternatives=('llm', 'vision')),
            rerank=feature('检索结果重排序', 'embedding', 'rerank'))
    elif module == 'animation':
        features.update(prompt_preview=feature('直接提示词预览'),
            visual_analysis=feature('角色与动作识图分析', 'vision'),
            assisted_prompt=feature('识图辅助指令预览', 'vision'),
            direct_generation=feature('直接模式动画生成', 'video', 'image'),
            assisted_generation=feature('辅助模式动画生成', 'vision', 'video', 'image'))
    elif module in {'image', 'speech', 'music', 'model3d'}:
        features['generation'] = feature('直接生成', *(() if module == 'image' else (module,)),
                                          alternatives=('image', 'workflow') if module == 'image' else ())
        # Media pages may explicitly choose any verified compatible profile;
        # a default binding is convenient, but it is not a prerequisite.
        candidates = {profile['kind'] for profile in source.list_profiles() if profile.get('verified')} if hasattr(source, 'list_profiles') else set()
        compatible = {'image', 'workflow'} if module == 'image' else {module}
        if candidates & compatible:
            features['generation'].update(available=True, missing_roles=[])
        if module == 'image':
            features['workflow'] = feature('ComfyUI 工作流', 'workflow')
            if 'workflow' in candidates:
                features['workflow'].update(available=True, missing_roles=[])
        if module != 'model3d':
            features['prompt_assistant'] = feature('LLM 辅助翻译', 'llm')
    return {'roles': roles, 'features': features, 'local_available': True}

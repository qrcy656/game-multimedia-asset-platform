"""Public creation records contain artwork parameters, never personal API setup."""
import copy


def public_metadata(value, *, owner_id=None, viewer_id=None):
    own = bool(owner_id and owner_id == viewer_id)
    def visit(item, context=''):
        if isinstance(item, list):
            return [visit(child, context) for child in item]
        if not isinstance(item, dict):
            return item
        provider = context in {'provider', 'video_provider', 'matting_provider', 'background_model'}
        private = provider and item.get('scope') != 'global' and not own
        connection = item.get('provider') if isinstance(item.get('provider'), dict) else {}
        translation_private = context == 'llm_translation' and not own and connection.get('scope') != 'global'
        result = {}
        for key, child in item.items():
            if key in {'api_key', 'model_context'}:
                continue
            if private and key in {'id', 'name', 'model', 'protocol', 'base_url', 'fingerprint', 'options', 'profile_id'}:
                continue
            if not own and (context == 'h3_request' and key == 'model' or
                            context == 'versions' and key in {'h3_model', 'video_model', 'translator_model'} or
                            translation_private and key == 'model'):
                continue
            result[key] = visit(child, key)
        return result
    return visit(copy.deepcopy(value))


def public_document(value, filename, **viewer):
    context = {'h3_request.json': 'h3_request', 'versions.json': 'versions'}.get(filename)
    if context:
        return public_metadata({context: value}, **viewer)[context]
    return public_metadata(value, **viewer)

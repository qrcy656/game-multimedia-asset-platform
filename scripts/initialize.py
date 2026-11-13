"""Prepare an installation using Python inside Docker, on Windows or Linux."""
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile


def initialize(root, uid, gid):
    root = Path(root).resolve()
    if any(value < 0 or value >= 2**32 - 1 for value in (uid, gid)):
        raise ValueError('EFFECTS_UID/GID is outside the supported range')
    env = root / '.env'
    if env.is_symlink() or (env.exists() and not env.is_file()):
        raise ValueError('.env must be a regular file')
    original_info = env.stat() if env.exists() else None
    original = env.read_text(encoding='utf-8-sig') if original_info else None
    env_owner = (original_info.st_uid, original_info.st_gid) if original_info else (uid, gid)
    values = {}
    for line in (original or '').splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        if key in values:
            raise ValueError('Duplicate installation setting: ' + key)
        values[key] = value
    required = ('POSTGRES_PASSWORD', 'COMMUNITY_IDENTITY_SECRET', 'ANIMATION_API_KEY')
    if original is not None and any(not values.get(key, '').strip() for key in required):
        raise ValueError('Existing .env requires its original installation secrets; restore them before starting')
    defaults = {'PORT': '18082', 'APP_ORIGIN': 'http://localhost:18082',
                'EFFECTS_UID': str(uid), 'EFFECTS_GID': str(gid),
                'COMPOSE_PROFILES': 'gitlab', 'COMMUNITY_REPOSITORY_DEFAULT': 'managed',
                'COMMUNITY_GITLAB_ROOT_PASSWORD': secrets.token_urlsafe(24)}
    if original is None:
        defaults.update({key: secrets.token_hex(32) for key in required})
    content = original or ''
    for key, value in defaults.items():
        if key not in values:
            content += ('\n' if content and not content.endswith('\n') else '') + key + '=' + value + '\n'
            values[key] = value
    uid = int(os.environ.get('EFFECTS_UID', values['EFFECTS_UID']))
    gid = int(os.environ.get('EFFECTS_GID', values['EFFECTS_GID']))
    if any(value < 0 or value >= 2**32 - 1 for value in (uid, gid)):
        raise ValueError('Invalid effects filesystem identity')
    for path in (root / 'data', root / 'data/effects'):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError('Data paths must be real directories')
    (root / 'data').mkdir(exist_ok=True)
    effects = root / 'data/effects'
    try:
        effects.mkdir(mode=0o755)
    except FileExistsError:
        pass
    else:
        effects.chmod(0o755)
        if hasattr(os, 'chown'):
            os.chown(effects, uid, gid)
    if content != original:
        fd, temporary = tempfile.mkstemp(prefix='.env.init-', dir=root)
        try:
            if hasattr(os, 'fchown'):
                os.fchown(fd, *env_owner)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
                output.write(content)
            os.replace(temporary, env)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print('Installation configuration and data directories are ready; existing credentials were preserved.')


if __name__ == '__main__':
    initialize('/workspace', int(sys.argv[1]), int(sys.argv[2]))

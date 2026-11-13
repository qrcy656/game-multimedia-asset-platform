"""Explicit Docker-console recovery: disable GitLab and restore an existing hash.

No password is changed or printed. The caller names the immutable local UUID and
must explicitly request leaving GitLab-managed authentication.
"""
import argparse
from pathlib import Path
import sqlite3


def restore(directory, account_id, *, disable_gitlab=False):
    if not disable_gitlab:
        raise ValueError('Pass --disable-gitlab to explicitly switch authentication to local mode')
    root = Path(directory).resolve()
    for name in ('accounts.sqlite3', 'repository.sqlite3'):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Existing installation databases are required')
    with sqlite3.connect(root / 'accounts.sqlite3', timeout=15) as db:
        db.execute('ATTACH DATABASE ? AS repository', (str(root / 'repository.sqlite3'),))
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT password_hash FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not row or not row[0].startswith('pbkdf2_sha256$'):
            raise ValueError('This account has no preserved local password; use GitLab account recovery')
        db.execute("UPDATE repository.repository_config SET mode='disabled',oauth_enabled=0,revision=revision+1 WHERE singleton=1")
        db.execute('UPDATE repository.repository_installation SET managed_defaults=0 WHERE singleton=1')
        db.execute('DELETE FROM repository.repository_oauth_states')
        db.execute('DELETE FROM repository.repository_oauth_grants')
        db.execute("UPDATE accounts SET auth_provider='local',active=1,admin=1 WHERE id=?", (account_id,))
        db.execute('DELETE FROM sessions WHERE user_id=?', (account_id,))
    print('GitLab authentication disabled; the selected account keeps its UUID, assets and previous local password.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default='/data')
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--disable-gitlab', action='store_true')
    options = parser.parse_args()
    restore(options.data_dir, options.account_id, disable_gitlab=options.disable_gitlab)

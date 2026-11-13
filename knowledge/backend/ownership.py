"""Source ownership is independent of each user's selected embedding space."""
from pathlib import PurePosixPath

from fastapi import HTTPException


class Ownership:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS source_owners (
                path TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_name TEXT NOT NULL)''')

    def metadata(self, path, user):
        with self.store._connect() as db:
            row = db.execute('SELECT owner_id,owner_name FROM source_owners WHERE path=?', (path,)).fetchone()
        owner = dict(row) if row else {'owner_id': None, 'owner_name': '管理员管理的已有资料'}
        return {**owner, 'editable': bool(user and (user.get('role') == 'admin' or owner['owner_id'] == user['id']))}

    def require_edit(self, path, user, *, subtree=False):
        name, target = self.store._path(path, exists=True)
        paths = [name]
        if subtree and target.is_dir():
            paths += [p.relative_to(self.store.vault).as_posix() for p in target.rglob('*')]
        for item in paths:
            if not self.metadata(item, user)['editable']:
                raise HTTPException(403, '只能修改自己的资料；他人资料可查看、引用和检索。')
        return name

    def require_parent(self, path, user):
        name, _ = self.store._path(path)
        parent = str(PurePosixPath(name).parent)
        if parent != '.':
            self.require_edit(parent, user)
        return name

    def created(self, path, user):
        with self.store._connect() as db:
            db.execute('INSERT INTO source_owners(path,owner_id,owner_name) VALUES(?,?,?)',
                       (path, user['id'], user.get('name', user['id'])))

    def moved(self, old, new):
        with self.store._connect() as db:
            rows = list(db.execute('SELECT * FROM source_owners WHERE path=? OR substr(path,1,?)=?',
                                   (old, len(old) + 1, old + '/')))
            for row in rows:
                db.execute('UPDATE source_owners SET path=? WHERE path=?', (new + row['path'][len(old):], row['path']))

    def deleted(self, path):
        with self.store._connect() as db:
            db.execute('DELETE FROM source_owners WHERE path=? OR substr(path,1,?)=?', (path, len(path) + 1, path + '/'))

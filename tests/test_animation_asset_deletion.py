"""Delete the full local asset, including API payloads encoded inside JSON."""
from types import SimpleNamespace

from animation.deletion import FileRemoval
from animation.store import Store


def test_asset_deletion_removes_video_frames_and_base64_receipts(tmp_path):
    source = tmp_path / 'jobs' / 'job1'
    asset = tmp_path / 'assets' / 'asset1'
    (source / 'matting' / '000000').mkdir(parents=True)
    (asset / 'frames').mkdir(parents=True)
    for path in (source / 'generation.mp4', source / 'matting' / '000000' / 'response.json',
                 source / 'matting' / '000000' / 'matte.png', asset / 'frames' / '0000.png', asset / 'metadata.json'):
        path.write_bytes(b'owned asset bytes')
    unrelated = tmp_path / 'jobs' / 'other'
    unrelated.mkdir()
    (unrelated / 'generation.mp4').write_bytes(b'keep')
    job = {'id': 'job1', 'status': 'completed', 'stage': 'completed', 'checkpoint': {'h3_terminal': 'completed'}}
    class Connection:
        def execute(self, sql, params=()):
            return SimpleNamespace(fetchone=lambda: job, fetchall=lambda: [])
    store = Store.__new__(Store)
    store.settings = SimpleNamespace(data_root=tmp_path)
    files, directories, retained = store._deletion_files(Connection(), 'assets', {'id': 'asset1', 'job_id': 'job1'})
    assert not retained
    assert source / 'matting' / '000000' / 'response.json' in files
    removal = FileRemoval(tmp_path, files, directories)
    removal.prepare()
    removal.purge()
    assert not source.exists() and not asset.exists()
    assert (unrelated / 'generation.mp4').read_bytes() == b'keep'

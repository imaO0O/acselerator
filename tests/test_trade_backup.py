import sqlite3
import zipfile

from kbagent import backup


def test_backup_preserves_trade_catalog_and_wal_commits(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, '_version', lambda root: 'test')
    folder = tmp_path / 'data' / 'trade'
    folder.mkdir(parents=True)
    path = folder / 'catalog.sqlite'
    db = sqlite3.connect(path)
    db.execute('pragma journal_mode=wal')
    db.execute('create table products(article text)')
    db.execute("insert into products values ('DEMO-01')")
    db.commit()
    archive = backup.create(tmp_path)
    manifest = backup.verify(archive)
    assert 'data/trade/catalog.sqlite' in manifest['files']
    assert not any(name.endswith(('-wal', '-shm')) for name in manifest['files'])
    with zipfile.ZipFile(archive) as z:
        restored = tmp_path / 'restored.sqlite'
        restored.write_bytes(z.read('data/trade/catalog.sqlite'))
    with sqlite3.connect(restored) as copy:
        assert copy.execute('select article from products').fetchone()[0] == 'DEMO-01'
    db.close()

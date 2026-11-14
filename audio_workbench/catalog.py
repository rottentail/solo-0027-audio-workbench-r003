"""Transactional metadata and immutable, content-addressed local files."""
import os
from pathlib import Path
import sqlite3
import tempfile
from contextlib import contextmanager


class Catalog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'blobs').mkdir(exist_ok=True)
        self.db = self.root / 'catalog.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
                    sample_rate INTEGER NOT NULL, channels INTEGER NOT NULL,
                    frames INTEGER NOT NULL, size INTEGER NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, head_revision INTEGER NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    project_id TEXT NOT NULL REFERENCES projects(id), number INTEGER NOT NULL,
                    document TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(project_id, number));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def atomic_write(path, data):
        path = Path(path)
        descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def identifier(value, name='id'):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError(f'{name} must be a lowercase hexadecimal UUID')
    return value


def display_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('name must contain 1 to 200 characters')
    return value.strip()

"""Append-only arrangement revisions with optimistic editing."""
import json
import time
import uuid
from .arrangement import normalize_document, compile_document
from .catalog import identifier, display_name


class ConflictError(ValueError):
    """An editor tried to overwrite a newer project revision."""


def revision_number(value):
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise ValueError('revision must be a positive integer')
    return value


class ProjectLibrary:
    def __init__(self, catalog, assets):
        self.catalog = catalog
        self.assets = assets

    def create(self, name, document):
        name = display_name(name)
        project_id, now = uuid.uuid4().hex, time.time()
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            normalized = normalize_document(document, self.assets.resolve)
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?)', (project_id, name, 1, now, now))
            db.execute('INSERT INTO revisions VALUES (?,?,?,?)',
                       (project_id, 1, json.dumps(normalized, allow_nan=False), now))
        return self.get(project_id)

    def update(self, project_id, expected_revision, document):
        identifier(project_id, 'project id')
        revision_number(expected_revision)
        now = time.time()
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT head_revision FROM projects WHERE id=?', (project_id,)).fetchone()
            if row is None:
                raise LookupError('project not found')
            if row['head_revision'] != expected_revision:
                raise ConflictError('project has changed; reload before editing')
            normalized = normalize_document(document, self.assets.resolve)
            next_revision = expected_revision + 1
            db.execute('INSERT INTO revisions VALUES (?,?,?,?)',
                       (project_id, next_revision, json.dumps(normalized, allow_nan=False), now))
            db.execute('UPDATE projects SET head_revision=?,updated=? WHERE id=?',
                       (next_revision, now, project_id))
        return self.get(project_id, next_revision)

    def get(self, project_id, revision=None):
        identifier(project_id, 'project id')
        if revision is not None:
            revision_number(revision)
        with self.catalog.connect() as db:
            row = db.execute('''SELECT p.id,p.name,r.number AS revision,r.document,
                                r.created,p.updated FROM projects p JOIN revisions r
                                ON r.project_id=p.id AND r.number=COALESCE(?,p.head_revision)
                                WHERE p.id=?''', (revision, project_id)).fetchone()
        if row is None:
            raise LookupError('project or revision not found')
        result = dict(row)
        result['document'] = json.loads(result['document'])
        return result

    def list(self):
        with self.catalog.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM projects ORDER BY updated DESC,id')]

    def freeze(self, project_id, revision=None):
        project = self.get(project_id, revision)
        # A historical revision may still use archived immutable material.
        manifest = compile_document(project['document'], self.assets.get)
        return {**manifest, 'project_id': project['id'], 'revision': project['revision'],
                'project_name': project['name']}

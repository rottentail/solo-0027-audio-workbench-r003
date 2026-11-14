"""Durable rendering of immutable project revisions by a bounded worker pool."""
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid

from . import arrangement, processing, rendering, waveio


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{32}', value) is None:
        raise ValueError('invalid render identifier')
    return value


class RenderQueue:
    def __init__(self, catalog, projects, assets, workers=2):
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError('workers must be an integer between 1 and 8')
        self.catalog, self.projects, self.assets = catalog, projects, assets
        self.root = catalog.root / 'renders'
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self._lifecycle = threading.RLock()
        self._cancel_events = {}
        self.threads = []
        with catalog.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS renders (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, manifest TEXT NOT NULL,
                project_id TEXT NOT NULL, revision INTEGER NOT NULL,
                progress REAL NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, started REAL, finished REAL,
                error TEXT, sha256 TEXT, size INTEGER, properties TEXT)''')
            db.execute("UPDATE renders SET status='failed', error='interrupted by service restart', "
                       "updated=?, finished=? WHERE status='running'", (time.time(), time.time()))
            published = {row['id'] for row in db.execute("SELECT id FROM renders WHERE status='succeeded'")}
        # A process can stop between filesystem publication and SQLite commit.
        # Only committed successful results are authoritative after restart.
        for path in self.root.iterdir():
            if re.fullmatch(r'[a-f0-9]{32}\.(tmp|wav)', path.name):
                if path.suffix == '.tmp' or path.stem not in published:
                    path.unlink(missing_ok=True)
        for index in range(workers):
            thread = threading.Thread(target=self._worker, name=f'pcm-render-{index}', daemon=True)
            self.threads.append(thread)
            thread.start()

    def submit(self, project_id, revision=None):
        with self._lifecycle:
            if self.stop_event.is_set():
                raise RuntimeError('render queue is closed')
            manifest = self.projects.freeze(project_id, revision)
            arrangement.validate_manifest(manifest)
            payload = json.dumps(manifest, allow_nan=False, separators=(',', ':'))
            render_id, now = uuid.uuid4().hex, time.time()
            with self.catalog.connect() as db:
                db.execute('INSERT INTO renders (id,status,manifest,project_id,revision,created,updated) '
                           'VALUES (?,?,?,?,?,?,?)',
                           (render_id, 'queued', payload, manifest['project_id'], manifest['revision'], now, now))
            self.wake.set()
        return self.get(render_id)

    def get(self, render_id):
        render_id = _identifier(render_id)
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM renders WHERE id=?', (render_id,)).fetchone()
        if row is None:
            raise LookupError('render not found')
        result = dict(row)
        result['manifest'] = json.loads(result['manifest'])
        result['cancel_requested'] = bool(result['cancel_requested'])
        result['properties'] = json.loads(result['properties']) if result['properties'] else None
        return result

    def list(self, project_id=None):
        with self.catalog.connect() as db:
            if project_id is None:
                rows = db.execute('SELECT id FROM renders ORDER BY created DESC,id DESC LIMIT 100').fetchall()
            else:
                if not isinstance(project_id, str) or not project_id or len(project_id) > 128:
                    raise ValueError('invalid project identifier')
                rows = db.execute('SELECT id FROM renders WHERE project_id=? '
                                  'ORDER BY created DESC,id DESC LIMIT 100', (project_id,)).fetchall()
        summaries = []
        for row in rows:
            record = self.get(row['id'])
            record.pop('manifest')
            summaries.append(record)
        return summaries

    def cancel(self, render_id):
        render_id = _identifier(render_id)
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM renders WHERE id=?', (render_id,)).fetchone()
            if row is None:
                raise LookupError('render not found')
            now = time.time()
            if row['status'] == 'queued':
                db.execute("UPDATE renders SET status='cancelled',cancel_requested=1,updated=?,finished=? WHERE id=?",
                           (now, now, render_id))
            elif row['status'] == 'running':
                db.execute('UPDATE renders SET cancel_requested=1,updated=? WHERE id=?', (now, render_id))
            else:
                raise ValueError('render is already finished')
        with self._lifecycle:
            event = self._cancel_events.get(render_id)
            if event is not None:
                event.set()
        self.wake.set()
        return self.get(render_id)

    def result(self, render_id):
        record = self.get(render_id)
        if record['status'] != 'succeeded':
            raise ValueError('render result is not available')
        try:
            data = (self.root / f'{record["id"]}.wav').read_bytes()
        except FileNotFoundError as exc:
            raise ValueError('render result is missing') from exc
        if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise ValueError('render result integrity check failed')
        return {'audio': base64.b64encode(data).decode('ascii'), 'sha256': record['sha256'],
                'properties': record['properties'], 'project_id': record['project_id'], 'revision': record['revision']}

    def _claim(self):
        with self._lifecycle:
            if self.stop_event.is_set():
                return None
            with self.catalog.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT id FROM renders WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
                if row is None:
                    return None
                now = time.time()
                db.execute("UPDATE renders SET status='running',started=?,updated=? WHERE id=?", (now, now, row['id']))
            self._cancel_events[row['id']] = threading.Event()
            return row['id']

    def _worker(self):
        while not self.stop_event.is_set():
            render_id = self._claim()
            if render_id is None:
                self.wake.wait(.2)
                self.wake.clear()
            else:
                self._run(render_id)

    def _run(self, render_id):
        temp = self.root / f'{render_id}.tmp'
        target = self.root / f'{render_id}.wav'
        event = self._cancel_events[render_id]
        last_progress = [0.0, 0.0]

        def cancelled():
            return event.is_set() or self.stop_event.is_set()

        def progress(value):
            now = time.monotonic()
            value = min(1.0, max(last_progress[1], float(value)))
            if now - last_progress[0] >= .1 or value == 1:
                with self.catalog.connect() as db:
                    db.execute("UPDATE renders SET progress=?,updated=? WHERE id=? AND status='running'",
                               (value, time.time(), render_id))
                last_progress[:] = [now, value]

        error = None
        try:
            record = self.get(render_id)
            if record['cancel_requested']:
                event.set()
            if cancelled():
                raise rendering.RenderCancelled('render interrupted')
            audio = rendering.render_manifest(record['manifest'], self.assets.content, cancelled, progress)
            if cancelled():
                raise rendering.RenderCancelled('render interrupted')
            properties = processing.inspect(audio)
            data = waveio.encode(audio)
            digest = hashlib.sha256(data).hexdigest()
            with temp.open('wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with self.catalog.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status,cancel_requested FROM renders WHERE id=?', (render_id,)).fetchone()
                if row['status'] != 'running':
                    return
                if row['cancel_requested'] or cancelled():
                    raise rendering.RenderCancelled('render interrupted')
                os.replace(temp, target)
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                now = time.time()
                db.execute("UPDATE renders SET status='succeeded',progress=1,size=?,sha256=?,properties=?,updated=?,finished=? WHERE id=?",
                           (len(data), digest, json.dumps(properties), now, now, render_id))
        except Exception as exc:
            error = str(exc)[:2000] or type(exc).__name__
            with self.catalog.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status,cancel_requested FROM renders WHERE id=?', (render_id,)).fetchone()
                if row is not None and row['status'] == 'running':
                    now = time.time()
                    status = 'cancelled' if row['cancel_requested'] else 'failed'
                    if self.stop_event.is_set() and status == 'failed':
                        error = 'interrupted by service shutdown'
                    db.execute('UPDATE renders SET status=?,error=?,updated=?,finished=? WHERE id=?',
                               (status, None if status == 'cancelled' else error, now, now, render_id))
        finally:
            temp.unlink(missing_ok=True)
            # A failed DB commit after rename must not expose an uncommitted result.
            with self.catalog.connect() as db:
                row = db.execute('SELECT status FROM renders WHERE id=?', (render_id,)).fetchone()
            if row is None or row['status'] != 'succeeded':
                target.unlink(missing_ok=True)
            with self._lifecycle:
                self._cancel_events.pop(render_id, None)

    def health(self):
        with self.catalog.connect() as db:
            counts = {row['status']: row['count'] for row in db.execute('SELECT status,COUNT(*) AS count FROM renders GROUP BY status')}
        return {'status': 'closed' if self.stop_event.is_set() else 'ready',
                'workers': len(self.threads), 'live_workers': sum(thread.is_alive() for thread in self.threads),
                'queued': counts.get('queued', 0), 'running': counts.get('running', 0)}

    def close(self):
        with self._lifecycle:
            self.stop_event.set()
            self.wake.set()
        for thread in self.threads:
            thread.join()

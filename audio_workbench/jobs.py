"""Persistent media jobs and a bounded pool of FFmpeg workers."""
import base64
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager

from .media import PRESETS, command


def decode_source(source):
    if not isinstance(source, str):
        raise ValueError('source must be base64 text')
    try:
        data = base64.b64decode(source, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('invalid base64 source') from exc
    if not data or len(data) > 5 * 1024 * 1024:
        raise ValueError('source must contain at most 5 MiB')
    return data


def interval(body):
    start, duration = body.get('start', 0), body.get('duration')
    for name, value, maximum in [('start', start, 3600), ('duration', duration, 60)]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > maximum:
            raise ValueError(f'{name} must be a finite number between 0 and {maximum}')
    if duration == 0:
        raise ValueError('duration must be positive')
    return {'start': start, 'duration': duration}


def validate_request(body):
    if not isinstance(body, dict) or body.get('preset') not in (*PRESETS, 'timeline'):
        raise ValueError('preset must be mp4, audio or timeline')
    if body['preset'] == 'timeline':
        segments = body.get('segments')
        if not isinstance(segments, list) or not 1 <= len(segments) <= 8 or any(not isinstance(s, dict) for s in segments):
            raise ValueError('segments must contain 1 to 8 objects')
        sources = [decode_source(segment.get('source')) for segment in segments]
        spans = [interval(segment) for segment in segments]
        if any(span['duration'] < 1 / 30 for span in spans):
            raise ValueError('each timeline segment must be at least one output frame')
        if sum(len(data) for data in sources) > 5 * 1024 * 1024 or sum(s['duration'] for s in spans) > 60:
            raise ValueError('timeline exceeds source size or duration limit')
        return sources, {'preset': 'timeline', 'segments': spans}
    return [decode_source(body.get('source'))], {'preset': body['preset'], **interval(body)}


class JobManager:
    def __init__(self, root, workers=2):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'jobs.sqlite3'
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, options TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, error TEXT,
                size INTEGER, sha256 TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0)''')
            interrupted = [row['id'] for row in db.execute("SELECT id FROM jobs WHERE status='running'")]
            db.execute("UPDATE jobs SET status='failed', error='interrupted by service restart', updated=? WHERE status='running'", (time.time(),))
        for job_id in interrupted:
            for filename in ('render.tmp', 'stderr.tmp', 'result'):
                (self.root / job_id / filename).unlink(missing_ok=True)
        for _ in range(workers):
            thread = threading.Thread(target=self.worker, daemon=True)
            thread.start()
            self.threads.append(thread)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, body):
        sources, options = validate_request(body)
        job_id = uuid.uuid4().hex
        folder = self.root / job_id
        folder.mkdir()
        try:
            for index, data in enumerate(sources):
                name = f'source-{index}' if options['preset'] == 'timeline' else 'source'
                (folder / name).write_bytes(data)
            now = time.time()
            with self.connect() as db:
                db.execute('INSERT INTO jobs (id,status,options,created,updated) VALUES (?, ?, ?, ?, ?)',
                           (job_id, 'queued', json.dumps(options), now, now))
        except Exception:
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
            raise
        self.wake.set()
        return self.get(job_id)

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise LookupError('job not found')
        result = dict(row)
        result['options'] = json.loads(result['options'])
        result['cancel_requested'] = bool(result['cancel_requested'])
        return result

    def cancel(self, job_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise LookupError('job not found')
            if row['status'] == 'queued':
                db.execute("UPDATE jobs SET status='cancelled', updated=? WHERE id=?", (time.time(), job_id))
            elif row['status'] == 'running':
                db.execute('UPDATE jobs SET cancel_requested=1, updated=? WHERE id=?', (time.time(), job_id))
            else:
                raise ValueError('job is already finished')
        self.wake.set()
        return self.get(job_id)

    def result(self, job_id):
        job = self.get(job_id)
        if job['status'] != 'succeeded':
            raise ValueError('job result is not available')
        data = (self.root / job_id / 'result').read_bytes()
        if hashlib.sha256(data).hexdigest() != job['sha256']:
            raise ValueError('result integrity check failed')
        return {'audio_video': base64.b64encode(data).decode('ascii'), 'sha256': job['sha256']}

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created, id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (time.time(), row['id']))
            return row['id']

    def worker(self):
        while not self.stop_event.is_set():
            job_id = self.claim()
            if job_id:
                self.run(job_id)
            else:
                self.wake.wait(.2)
                self.wake.clear()

    def run(self, job_id):
        folder = self.root / job_id
        temp = folder / 'render.tmp'
        job = self.get(job_id)
        options = job['options']
        error = None
        process = None
        try:
            cmd = command(folder / 'source', temp, options)
            with (folder / 'stderr.tmp').open('wb') as stderr:
                process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        process.wait(timeout=.1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if self.stop_event.wait(.1) or self.get(job_id)['cancel_requested'] or time.monotonic() > deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        error = 'processing interrupted or timed out'
                        break
            if process.returncode != 0 and error is None:
                error = (folder / 'stderr.tmp').read_text(errors='replace')[-2000:] or 'media processing failed'
            if error is None and (not temp.exists() or temp.stat().st_size == 0):
                error = 'empty media result'
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                cancelled = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
                if cancelled:
                    db.execute("UPDATE jobs SET status='cancelled', updated=? WHERE id=?", (time.time(), job_id))
                elif error:
                    db.execute("UPDATE jobs SET status='failed', error=?, updated=? WHERE id=?", (error, time.time(), job_id))
                else:
                    data = temp.read_bytes()
                    os.replace(temp, folder / 'result')
                    db.execute("UPDATE jobs SET status='succeeded', size=?, sha256=?, updated=? WHERE id=?",
                               (len(data), hashlib.sha256(data).hexdigest(), time.time(), job_id))
        except Exception as exc:
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='failed', error=?, updated=? WHERE id=? AND status='running'",
                           (str(exc)[:2000], time.time(), job_id))
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                process.wait()
            temp.unlink(missing_ok=True)
            (folder / 'stderr.tmp').unlink(missing_ok=True)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        for thread in self.threads:
            thread.join()

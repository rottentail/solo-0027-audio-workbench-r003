import array
import base64
import hashlib
import io
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from audio_workbench import rendering, waveio
from audio_workbench.assets import AssetLibrary
from audio_workbench.catalog import Catalog
from audio_workbench.projects import ProjectLibrary
from audio_workbench.render_queue import RenderQueue


class RenderQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Catalog(self.temp.name)
        self.assets = AssetLibrary(self.catalog)
        self.projects = ProjectLibrary(self.catalog, self.assets)
        self.wav = waveio.encode(waveio.Audio(8000, 1, array.array('h', [1200, -2400, 3600, -4800])))
        self.asset = self.assets.import_audio('voice', self.wav)
        self.document = {'schema_version': 1, 'format': {'sample_rate': 8000, 'channels': 1},
                         'clips': [{'id': 'voice', 'asset_id': self.asset['id']}]}
        self.project = self.projects.create('First mix', self.document)

    def queue(self):
        queue = RenderQueue(self.catalog, self.projects, self.assets, workers=1)
        self.addCleanup(queue.close)
        return queue

    def finished(self, queue, render_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = queue.get(render_id)
            if record['status'] not in ('queued', 'running'):
                return record
            threading.Event().wait(.005)
        self.fail(f'render did not finish: {queue.get(render_id)}')

    def block_render(self):
        started = threading.Event()

        def render(manifest, read_asset, cancelled, progress):
            started.set()
            deadline = time.monotonic() + 5
            while not cancelled() and time.monotonic() < deadline:
                threading.Event().wait(.005)
            if cancelled():
                raise rendering.RenderCancelled('cancelled during test render')
            raise RuntimeError('test renderer timed out')

        return started, render

    def test_actual_result_matches_pcm_and_survives_reopen(self):
        queue = self.queue()
        record = queue.submit(self.project['id'])
        done = self.finished(queue, record['id'])
        self.assertEqual('succeeded', done['status'])
        result = queue.result(record['id'])
        data = base64.b64decode(result['audio'])
        self.assertEqual(self.wav, data)
        self.assertEqual(hashlib.sha256(data).hexdigest(), result['sha256'])
        self.assertEqual(4, result['properties']['frames'])
        self.assertEqual(1, result['revision'])
        queue.close()
        reopened = self.queue()
        self.assertEqual(result, reopened.result(record['id']))
        self.assertEqual(record['id'], reopened.list(self.project['id'])[0]['id'])

    def test_queued_snapshot_does_not_follow_edits_or_archive(self):
        queue = self.queue()
        started, blocker = self.block_render()
        with patch('audio_workbench.render_queue.rendering.render_manifest', blocker):
            first = queue.submit(self.project['id'])
            self.assertTrue(started.wait(2))
            pending = queue.submit(self.project['id'])
            changed = {**self.document, 'master_gain': .5}
            self.projects.update(self.project['id'], 1, changed)
            self.assets.archive(self.asset['id'])
            self.assertEqual(1, pending['revision'])
            self.assertEqual(1, pending['manifest']['master_gain'])
            queue.close()
        self.assertEqual('failed', queue.get(first['id'])['status'])
        self.assertEqual('queued', queue.get(pending['id'])['status'])
        reopened = self.queue()
        self.assertEqual('succeeded', self.finished(reopened, pending['id'])['status'])
        self.assertEqual(self.wav, base64.b64decode(reopened.result(pending['id'])['audio']))
        latest = reopened.submit(self.project['id'])
        self.assertEqual('succeeded', self.finished(reopened, latest['id'])['status'])
        samples = waveio.decode(base64.b64decode(reopened.result(latest['id'])['audio'])).samples
        self.assertEqual([600, -1200, 1800, -2400], list(samples))

    def test_running_and_queued_cancellation_never_publish(self):
        queue = self.queue()
        started, blocker = self.block_render()
        with patch('audio_workbench.render_queue.rendering.render_manifest', blocker):
            running = queue.submit(self.project['id'])
            self.assertTrue(started.wait(2))
            queued = queue.submit(self.project['id'])
            self.assertEqual('cancelled', queue.cancel(queued['id'])['status'])
            queue.cancel(running['id'])
            self.assertEqual('cancelled', self.finished(queue, running['id'])['status'])
        for record in (running, queued):
            self.assertFalse((queue.root / f'{record["id"]}.wav').exists())
            with self.assertRaises(ValueError):
                queue.result(record['id'])
            with self.assertRaises(ValueError):
                queue.cancel(record['id'])

    def test_cancel_after_renderer_returns_prevents_publication(self):
        queue = self.queue()
        original = rendering.render_manifest

        def cancel_after_render(manifest, read_asset, cancelled, progress):
            audio = original(manifest, read_asset, cancelled, progress)
            with self.catalog.connect() as db:
                db.execute("UPDATE renders SET cancel_requested=1 WHERE status='running'")
            return audio

        with patch('audio_workbench.render_queue.rendering.render_manifest', cancel_after_render):
            record = queue.submit(self.project['id'])
            self.assertEqual('cancelled', self.finished(queue, record['id'])['status'])
        self.assertFalse((queue.root / f'{record["id"]}.wav').exists())

    def test_failed_input_is_isolated_and_result_integrity_checked(self):
        queue = self.queue()
        blob = self.catalog.root / 'blobs' / (self.asset['sha256'] + '.wav')
        blob.write_bytes(b'invalid')
        bad = queue.submit(self.project['id'])
        self.assertEqual('failed', self.finished(queue, bad['id'])['status'])
        self.assertIn('integrity', queue.get(bad['id'])['error'])
        blob.write_bytes(self.wav)
        good = queue.submit(self.project['id'])
        self.assertEqual('succeeded', self.finished(queue, good['id'])['status'])
        (queue.root / f'{good["id"]}.wav').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            queue.result(good['id'])
        self.assertEqual(1, queue.health()['live_workers'])

    def test_long_timeline_result_is_retrievable_beyond_legacy_input_limit(self):
        document = {**self.document, 'clips': [{'id': 'voice', 'asset_id': self.asset['id'], 'at': 31}]}
        project = self.projects.create('Long tail', document)
        queue = self.queue()
        record = queue.submit(project['id'])
        self.assertEqual('succeeded', self.finished(queue, record['id'])['status'])
        result = queue.result(record['id'])
        self.assertEqual(31 * 8000 + 4, result['properties']['frames'])
        with wave.open(io.BytesIO(base64.b64decode(result['audio'])), 'rb') as reader:
            reader.setpos(31 * 8000)
            self.assertEqual(self.wav[44:], reader.readframes(4))

    def test_invalid_persisted_manifest_fails_without_losing_worker(self):
        queue = self.queue()
        record = queue.submit(self.project['id'])
        self.assertEqual('succeeded', self.finished(queue, record['id'])['status'])
        queue.close()
        with self.catalog.connect() as db:
            db.execute("UPDATE renders SET status='queued',manifest=? WHERE id=?", ('{', record['id']))
        reopened = self.queue()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.catalog.connect() as db:
                row = db.execute('SELECT status,error FROM renders WHERE id=?', (record['id'],)).fetchone()
            if row['status'] == 'failed':
                break
            threading.Event().wait(.005)
        self.assertEqual('failed', row['status'])
        good = reopened.submit(self.project['id'])
        self.assertEqual('succeeded', self.finished(reopened, good['id'])['status'])
        self.assertEqual(1, reopened.health()['live_workers'])

    def test_restart_marks_interrupted_running_failed_and_removes_orphans(self):
        queue = self.queue()
        started, blocker = self.block_render()
        with patch('audio_workbench.render_queue.rendering.render_manifest', blocker):
            record = queue.submit(self.project['id'])
            self.assertTrue(started.wait(2))
            queue.close()
        with self.catalog.connect() as db:
            db.execute("UPDATE renders SET status='running' WHERE id=?", (record['id'],))
        (queue.root / f'{record["id"]}.tmp').write_bytes(b'partial')
        (queue.root / f'{record["id"]}.wav').write_bytes(b'uncommitted')
        reopened = self.queue()
        recovered = reopened.get(record['id'])
        self.assertEqual('failed', recovered['status'])
        self.assertIn('restart', recovered['error'])
        self.assertFalse((queue.root / f'{record["id"]}.tmp').exists())
        self.assertFalse((queue.root / f'{record["id"]}.wav').exists())

    def test_validation_and_closed_queue(self):
        queue = self.queue()
        with self.assertRaises(ValueError):
            queue.get('../outside')
        with self.assertRaises(LookupError):
            queue.get('0' * 32)
        queue.close()
        with self.assertRaises(RuntimeError):
            queue.submit(self.project['id'])
        self.assertEqual('closed', queue.health()['status'])


if __name__ == '__main__':
    unittest.main()

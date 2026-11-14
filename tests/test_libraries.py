import array
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
from unittest.mock import patch
import unittest
from audio_workbench.catalog import Catalog
from audio_workbench.assets import AssetLibrary
from audio_workbench.projects import ProjectLibrary, ConflictError
from audio_workbench.waveio import Audio, encode


class LibrariesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Catalog(self.temp.name)
        self.assets = AssetLibrary(self.catalog)
        self.projects = ProjectLibrary(self.catalog, self.assets)
        self.wav = encode(Audio(8000, 1, array.array('h', [100, -200, 300, -400] * 100)))
        self.asset = self.assets.import_audio('voice', self.wav)
        self.document = {'schema_version': 1, 'format': {'sample_rate': 16000, 'channels': 2},
                         'clips': [{'id': 'voice-1', 'asset_id': self.asset['id']}]}

    def test_immutable_history_and_archival(self):
        project = self.projects.create('episode', self.document)
        self.document['master_gain'] = .25
        updated = self.projects.update(project['id'], 1, self.document)
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.projects.get(project['id'], 1)['document']['master_gain'], 1)
        self.assets.archive(self.asset['id'])
        self.assertEqual(self.assets.list(), [])
        self.assertEqual(self.assets.content(self.asset['id']), self.wav)
        self.assertEqual(self.projects.freeze(project['id'], 1)['revision'], 1)
        with self.assertRaises(ValueError):
            self.projects.update(project['id'], 2, self.document)

    def test_concurrent_revision_has_single_winner(self):
        project = self.projects.create('episode', self.document)
        def edit(gain):
            try:
                return self.projects.update(project['id'], 1, dict(self.document, master_gain=gain))['revision']
            except ConflictError:
                return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(edit, [.25, .5]))
        self.assertCountEqual(results, [2, 'conflict'])
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 2)

    def test_shared_blob_has_independent_archival_and_integrity(self):
        other = self.assets.import_audio('duplicate', self.wav)
        self.assets.archive(self.asset['id'])
        self.assertFalse(self.assets.resolve(other['id'])['archived'])
        path = self.catalog.root / 'blobs' / (other['sha256'] + '.wav')
        path.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.assets.content(other['id'])

    def test_bad_identifiers_and_revisions(self):
        for value in ('../escape', '', None, 'a' * 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.assets.get(value)
        for value in (True, 0, 1.5, 10**100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.projects.get('a' * 32, value)

    def test_archive_serializes_after_accepted_revision(self):
        resolved, release, archived = threading.Event(), threading.Event(), threading.Event()
        original = self.assets.resolve
        def blocked(asset_id):
            result = original(asset_id)
            resolved.set()
            if not release.wait(3):
                raise RuntimeError('test release timed out')
            return result
        def archive():
            self.assets.archive(self.asset['id'])
            archived.set()
        with ThreadPoolExecutor(2) as pool, patch.object(self.assets, 'resolve', blocked):
            created = pool.submit(self.projects.create, 'episode', self.document)
            self.assertTrue(resolved.wait(3))
            archiving = pool.submit(archive)
            try:
                self.assertFalse(archived.wait(.1))
            finally:
                release.set()
            project = created.result(timeout=3)
            archiving.result(timeout=3)
        self.assertTrue(self.assets.get(self.asset['id'])['archived'])
        self.assertEqual(self.projects.freeze(project['id'])['revision'], 1)

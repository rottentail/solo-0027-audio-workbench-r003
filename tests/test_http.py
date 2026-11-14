import array
import base64
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from audio_workbench.jobs import JobManager
from audio_workbench.server import AudioServer, Handler
from audio_workbench.waveio import Audio, decode, encode
from audio_workbench.workbench import Workbench


class HTTPWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workbench = Workbench(Path(self.temp.name) / 'workbench', 1)
        self.addCleanup(self.workbench.close)
        self.jobs = JobManager(self.temp.name, 1)
        self.addCleanup(self.jobs.close)
        self.server = AudioServer(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        self.server.workbench, self.server.jobs = self.workbench, self.jobs
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.wav = encode(Audio(8000, 1, array.array('h', [1000, -2000] * 80)))
        self.encoded = base64.b64encode(self.wav).decode()

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(3)

    def call(self, path, body=None):
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}' + path,
                                         None if body is None else json.dumps(body).encode(),
                                         {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_edit_archive_render_and_legacy(self):
        status, asset = self.call('/assets', {'name': 'voice', 'audio': self.encoded})
        self.assertEqual(status, 201)
        doc = {'schema_version': 1, 'format': {'sample_rate': 8000, 'channels': 1},
               'clips': [{'id': 'take', 'asset_id': asset['id'], 'at': .01}]}
        status, project = self.call('/projects', {'name': 'episode', 'document': doc})
        self.assertEqual(status, 201)
        doc['master_gain'] = .5
        status, updated = self.call('/projects/' + project['id'] + '/revisions',
                                    {'expected_revision': 1, 'document': doc})
        self.assertEqual((status, updated['revision']), (201, 2))
        self.assertEqual(self.call('/projects/' + project['id'] + '/revisions',
                                   {'expected_revision': 1, 'document': doc})[0], 409)
        self.assertEqual(self.call('/assets/' + asset['id'] + '/archive', {})[0], 200)
        self.assertEqual(self.call('/assets')[1]['assets'], [])
        status, render = self.call('/projects/' + project['id'] + '/render', {'revision': 1})
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            finished = self.call('/renders/' + render['id'])[1]
            if finished['status'] in ('succeeded', 'failed'):
                break
            time.sleep(.01)
        self.assertEqual(finished['status'], 'succeeded', finished)
        status, result = self.call('/renders/' + render['id'] + '/result')
        self.assertEqual((status, result['revision']), (200, 1))
        audio = decode(base64.b64decode(result['audio']))
        self.assertEqual(audio.frames, 240)
        self.assertEqual(list(audio.samples[:80]), [0] * 80)
        self.assertEqual(list(audio.samples[80:]), [1000, -2000] * 80)
        self.assertEqual(self.call('/inspect', {'audio': self.encoded})[1]['frames'], 160)
        self.assertEqual(self.call('/clip', {'audio': self.encoded, 'start': 0, 'end': .01})[0], 200)
        self.assertEqual(self.call('/health')[0], 200)

    def test_validation_and_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'already open'):
            Workbench(Path(self.temp.name) / 'workbench', 1)
        self.assertEqual(self.call('/assets', {'name': 'x', 'audio': 'bad!'})[0], 400)
        self.assertEqual(self.call('/projects', [1, 2])[0], 400)
        self.assertEqual(self.call('/not-found')[0], 404)
        self.assertEqual(self.call('/renders/../../x')[0], 404)


class ProcessLifecycleTest(unittest.TestCase):
    def test_sigterm_releases_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            command = [sys.executable, '-m', 'audio_workbench.server', '--port', str(port), '--data-dir', root]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                ready = False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(process.communicate()[1].decode())
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=.2):
                            ready = True
                            break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(.02)
                self.assertTrue(ready, 'service never became ready')
                # This acquires the same lock that the service holds until all queues close.
                with self.assertRaises(RuntimeError):
                    Workbench(Path(root) / 'workbench', 1)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode())
                with Workbench(Path(root) / 'workbench', 1):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

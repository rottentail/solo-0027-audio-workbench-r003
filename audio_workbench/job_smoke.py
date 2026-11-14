"""Exercise media jobs through HTTP with a generated audiovisual source."""
import base64
import json
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import imageio_ffmpeg
from .jobs import JobManager
from .server import Handler


def main():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'sample.mp4'
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg, '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=8000', '-t', '1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-y', str(source)],
                       check=True, timeout=30)
        other = Path(tmp) / 'other.mp4'
        subprocess.run([ffmpeg, '-loglevel', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=240x160:rate=24',
                        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=44100', '-t', '1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-y', str(other)],
                       check=True, timeout=30)
        silent = Path(tmp) / 'silent.mp4'
        subprocess.run([ffmpeg, '-loglevel', 'error', '-i', str(source), '-an', '-c:v', 'copy', '-y', str(silent)],
                       check=True, timeout=30)
        manager = JobManager(Path(tmp) / 'data', workers=2)
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            server.jobs = manager
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def call(path, body=None):
                data = None if body is None else json.dumps(body).encode()
                request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}', data,
                                                 {'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.load(exc)
            def finish(job_id):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    job = call('/jobs/' + job_id)[1]
                    if job['status'] in ('succeeded', 'failed', 'cancelled'):
                        return job
                    time.sleep(.05)
                raise RuntimeError('job timed out')
            try:
                encoded = base64.b64encode(source.read_bytes()).decode()
                ids = []
                for preset in ('mp4', 'audio'):
                    status, job = call('/jobs', {'source': encoded, 'preset': preset, 'start': .1, 'duration': .5})
                    assert status == 202
                    ids.append(job['id'])
                for job_id in ids:
                    job = finish(job_id)
                    assert job['status'] == 'succeeded', job
                    status, result = call('/jobs/' + job_id + '/result')
                    assert status == 200 and result['sha256'] == job['sha256']
                    rendered = Path(tmp) / (job_id + '.media')
                    rendered.write_bytes(base64.b64decode(result['audio_video']))
                    subprocess.run([ffmpeg, '-v', 'error', '-i', str(rendered), '-f', 'null', '-'],
                                   check=True, timeout=30)
                second = base64.b64encode(other.read_bytes()).decode()
                status, timeline = call('/jobs', {'preset': 'timeline', 'segments': [
                    {'source': encoded, 'start': .1, 'duration': .4},
                    {'source': second, 'start': .2, 'duration': .5}]})
                assert status == 202
                completed = finish(timeline['id'])
                assert completed['status'] == 'succeeded', completed
                timeline_path = Path(tmp) / 'timeline.mp4'
                timeline_path.write_bytes(base64.b64decode(call('/jobs/' + timeline['id'] + '/result')[1]['audio_video']))
                frames = imageio_ffmpeg.read_frames(str(timeline_path))
                metadata = next(frames)
                count = sum(1 for _ in frames)
                assert metadata['size'] == (640, 360) and metadata['fps'] == 30 and 26 <= count <= 28, (metadata, count)
                audio_path = Path(tmp) / 'timeline.wav'
                subprocess.run([ffmpeg, '-loglevel', 'error', '-i', str(timeline_path), '-vn', '-c:a', 'pcm_s16le', '-y', str(audio_path)], check=True, timeout=30)
                with wave.open(str(audio_path)) as audio:
                    assert audio.getframerate() == 48000 and audio.getnchannels() == 2
                    assert abs(audio.getnframes() / audio.getframerate() - count / 30) < .06
                assert call('/jobs', {'preset': 'timeline', 'segments': []})[0] == 400
                status, missing_audio = call('/jobs', {'preset': 'timeline', 'segments': [
                    {'source': base64.b64encode(silent.read_bytes()).decode(), 'start': 0, 'duration': .5}]})
                assert status == 202 and finish(missing_audio['id'])['status'] == 'failed'
                status, job = call('/jobs', {'source': base64.b64encode(b'invalid').decode(), 'preset': 'audio', 'duration': 1})
                assert status == 202 and finish(job['id'])['status'] == 'failed'
                assert call('/jobs', {'source': encoded, 'preset': 'audio', 'duration': -1})[0] == 400
            finally:
                server.shutdown()
                thread.join()
                manager.close()
        manager = JobManager(Path(tmp) / 'data', workers=1)
        try:
            assert manager.get(ids[0])['status'] == 'succeeded'
            assert manager.result(ids[0])['sha256']
        finally:
            manager.close()
        manager = JobManager(Path(tmp) / 'data', workers=0)
        try:
            queued = manager.submit({'source': encoded, 'preset': 'audio', 'duration': 1})
            assert manager.cancel(queued['id'])['status'] == 'cancelled'
            interrupted = manager.submit({'source': encoded, 'preset': 'audio', 'duration': 1})
            assert manager.claim() == interrupted['id']
        finally:
            manager.close()
        manager = JobManager(Path(tmp) / 'data', workers=0)
        try:
            assert manager.get(interrupted['id'])['status'] == 'failed'
            assert manager.get(interrupted['id'])['error'] == 'interrupted by service restart'
        finally:
            manager.close()
    print('media jobs: ok')


if __name__ == '__main__':
    main()

"""Bounded production interface checks using generated PCM audio."""
import argparse
import array
import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from .server import Handler
from .waveio import Audio, decode, encode


def run(which):
    audio = Audio(8000, 1, array.array('h', [10000, -10000] * 4000))
    encoded = base64.b64encode(encode(audio)).decode('ascii')
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def call(path, body):
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/{path}',
                                         json.dumps(body).encode(), {'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)
        try:
            if which == 'inspect':
                status, result = call(which, {'audio': encoded})
                assert status == 200 and result['frames'] == 8000 and result['peak'] > 0
                assert call(which, {'audio': 'bad!'})[0] == 400
            elif which == 'clip':
                status, result = call(which, {'audio': encoded, 'start': .25, 'end': .5, 'gain': 2})
                rendered = decode(base64.b64decode(result['audio']))
                assert status == 200 and rendered.frames == 2000 and rendered.samples[0] == 20000
                assert call(which, {'audio': encoded, 'start': .5, 'end': .25})[0] == 400
            else:
                status, result = call(which, {'tracks': [{'audio': encoded}, {'audio': encoded, 'offset': .5, 'gain': 4}]})
                rendered = decode(base64.b64decode(result['audio']))
                assert status == 200 and rendered.frames == 12000 and rendered.samples[4000] == 32767
                assert call(which, {'tracks': []})[0] == 400
                status, result = call(which, {'tracks': [{'audio': encoded}],
                                              'format': {'sample_rate': 16000, 'channels': 2}})
                rendered = decode(base64.b64decode(result['audio']))
                assert status == 200 and rendered.frames == 16000 and rendered.channels == 2
                assert rendered.samples[0] == 10000 and rendered.samples[1] == 10000 and rendered.samples[2] == 0
                assert call(which, {'tracks': [{'audio': encoded}], 'format': {'sample_rate': 7999, 'channels': 1}})[0] == 400
        finally:
            server.shutdown()
            thread.join()
    print(f'{which}: ok')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('workflow', choices=['inspect', 'clip', 'mix'])
    run(parser.parse_args().workflow)

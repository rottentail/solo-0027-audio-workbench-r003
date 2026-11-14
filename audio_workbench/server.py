import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from .service import execute
from .projects import ConflictError

MAX_BODY = 8 * 1024 * 1024


def audio_bytes(value):
    if not isinstance(value, str):
        raise ValueError('audio must be base64 text')
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('invalid base64 audio') from exc


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, result):
        payload = json.dumps(result, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def route(self, method, body=None):
        url = urlsplit(self.path)
        parts = url.path.strip('/').split('/')
        query = parse_qs(url.query)
        workbench = getattr(self.server, 'workbench', None)
        if method == 'GET':
            if parts == ['health']:
                return 200, {'status': 'ok', 'renders': workbench.renders.health()}
            if parts == ['assets']:
                return 200, {'assets': workbench.assets.list(query.get('archived') == ['true'])}
            if len(parts) == 2 and parts[0] == 'assets':
                return 200, workbench.assets.get(parts[1])
            if len(parts) == 3 and parts[0] == 'assets' and parts[2] == 'content':
                return 200, {'audio': base64.b64encode(workbench.assets.content(parts[1])).decode('ascii')}
            if parts == ['projects']:
                return 200, {'projects': workbench.projects.list()}
            if len(parts) == 2 and parts[0] == 'projects':
                revision = int(query['revision'][0]) if 'revision' in query else None
                return 200, workbench.projects.get(parts[1], revision)
            if parts == ['renders']:
                return 200, {'renders': workbench.renders.list(query.get('project_id', [None])[0])}
            if len(parts) == 2 and parts[0] == 'renders':
                return 200, workbench.renders.get(parts[1])
            if len(parts) == 3 and parts[0] == 'renders' and parts[2] == 'result':
                return 200, workbench.renders.result(parts[1])
            if len(parts) == 2 and parts[0] == 'jobs':
                return 200, self.server.jobs.get(parts[1])
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'result':
                return 200, self.server.jobs.result(parts[1])
        else:
            if parts == ['assets']:
                return 201, workbench.assets.import_audio(body.get('name'), audio_bytes(body.get('audio')))
            if len(parts) == 3 and parts[0] == 'assets' and parts[2] == 'archive':
                return 200, workbench.assets.archive(parts[1])
            if parts == ['projects']:
                return 201, workbench.projects.create(body.get('name'), body.get('document'))
            if len(parts) == 3 and parts[0] == 'projects' and parts[2] == 'revisions':
                return 201, workbench.projects.update(parts[1], body.get('expected_revision'), body.get('document'))
            if len(parts) == 3 and parts[0] == 'projects' and parts[2] == 'render':
                return 202, workbench.renders.submit(parts[1], body.get('revision'))
            if len(parts) == 3 and parts[0] == 'renders' and parts[2] == 'cancel':
                return 200, workbench.renders.cancel(parts[1])
            if parts == ['jobs']:
                return 202, self.server.jobs.submit(body)
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'cancel':
                return 200, self.server.jobs.cancel(parts[1])
            return 200, execute(url.path, body)
        raise LookupError('unknown endpoint')

    def handle_request(self, method):
        try:
            body = None
            if method == 'POST':
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('transfer encoding is not supported')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    raise ValueError('invalid request size')
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError('incomplete request')
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise ValueError('request must be a JSON object')
            status, result = self.route(method, body)
        except ConflictError as exc:
            status, result = 409, {'error': str(exc)}
        except LookupError as exc:
            status, result = 404, {'error': str(exc)}
        except (ValueError, UnicodeDecodeError, OverflowError, RecursionError) as exc:
            status, result = 400, {'error': str(exc)[:500]}
        self.respond(status, result)

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')


class AudioServer(ThreadingHTTPServer):
    # On shutdown, finish accepted requests before closing their data services.
    daemon_threads = False
    block_on_close = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


def main():
    parser = argparse.ArgumentParser(description='Audio arrangements and media processing HTTP service')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default=os.environ.get('AUDIO_WORKBENCH_DATA', './media-data'))
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    from .jobs import JobManager
    from .workbench import Workbench
    previous_handlers = {}
    with Workbench(Path(args.data_dir) / 'workbench', args.workers) as workbench:
        jobs = JobManager(args.data_dir, args.workers)
        try:
            with AudioServer((args.host, args.port), Handler) as server:
                server.jobs, server.workbench = jobs, workbench
                def stop(signum, frame):
                    # shutdown must run outside serve_forever's own thread.
                    threading.Thread(target=server.shutdown, daemon=True).start()
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[signum] = signal.signal(signum, stop)
                try:
                    server.serve_forever(poll_interval=.1)
                finally:
                    for signum, handler in previous_handlers.items():
                        signal.signal(signum, handler)
        finally:
            jobs.close()


if __name__ == '__main__':
    main()

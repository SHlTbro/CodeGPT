"""A real owned HTTP response must not become a false network-outage claim."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from config import dump_toml
from setup import SetupApplication


@pytest.mark.parametrize('http_status', [401, 403])
def test_http_auth_rejection_is_not_reported_as_unreachable(tmp_path, http_status):
    received = []
    class Rejected(BaseHTTPRequestHandler):
        def do_POST(self):
            self.do_GET()
        def do_GET(self):
            received.append(self.command)
            self.send_response(http_status)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Rejected)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = tmp_path / 'config.toml'
    config.write_text(dump_toml({'projects': [], 'server': {'host': '127.0.0.1', 'port': server.server_port, 'path': '/mcp'},
        'tunnel': {'mode': 'off', 'public_url': ''}}), encoding='utf-8')
    try:
        app = SetupApplication(config, tmp_path / 'state', setup_port=0)
        app.last_snapshot = app.store.read()
        app.runtime._sample_network('local', f'http://127.0.0.1:{server.server_port}/mcp')
        status = app.status()['local']
        assert received, 'Actual HTTP probe did not reach the owned endpoint'
        assert status['reachability'] in {'unknown', 'reachable'}
        assert status['mcp'] != 'ready'
        assert status['auth'] != 'authorized'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()

import gzip
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from src.utils.http import create_session, make_request


class EncodingTests(unittest.TestCase):
    def test_session_preserves_installed_decoder_capabilities(self):
        # A clean Actions install has no Brotli decoder. Never promise `br`
        # just because a browser would support it.
        headers = requests.utils.default_headers()
        headers['Accept-Encoding'] = 'gzip, deflate'
        with patch('requests.sessions.default_headers', return_value=headers):
            with create_session() as session:
                self.assertEqual(session.headers['Accept-Encoding'], 'gzip, deflate')

    def test_gzip_json_response_decodes(self):
        payload = {'data': {'calendarMonths': [{'days': [{'available': True}]}]}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = gzip.compress(json.dumps(payload).encode())
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Encoding', 'gzip')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with create_session() as session:
                response = make_request(session, 'GET', f'http://127.0.0.1:{server.server_port}/')
                self.assertEqual(response.json(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()

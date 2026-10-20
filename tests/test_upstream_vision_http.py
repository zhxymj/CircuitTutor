"""Real HTTP boundary for the optional local vision worker; no model execution."""
import http.client
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from core import upstream_vision  # Ensure package attribute exists in standalone runs.
from test_upstream_vision import PNG_DATA_URL, worker_result


class WorkerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class VisionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=2)

    def test_local_vision_module_is_served_by_real_server(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        conn.request('GET', '/static/upstream_vision.js')
        response = conn.getresponse()
        content = response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn(b'CircuitUpstreamVision', content)

    def post(self, payload, *, header_only=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        body = json.dumps(payload).encode('utf-8')
        if header_only:
            # The size limit rejects Content-Length before reading a body. Do
            # not race the response by flooding the socket after rejection.
            conn.putrequest('POST', '/api/image/analyze')
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Content-Length', str(len(body)))
            conn.putheader('Connection', 'close')
            conn.endheaders()
        else:
            conn.request('POST', '/api/image/analyze', body,
                         {'Content-Type': 'application/json', 'Connection': 'close'})
        response = conn.getresponse()
        result = response.status, json.loads(response.read().decode('utf-8'))
        conn.close()
        return result

    @contextlib.contextmanager
    def with_worker(self, fn):
        module = types.ModuleType('core.upstream_vision')
        module.UpstreamVisionError = WorkerError
        module.analyze_image = fn
        with patch.dict(sys.modules, {'core.upstream_vision': module}), \
                patch('core.upstream_vision', module):
            yield

    def test_explicit_backend_returns_detection_only_manual_correction(self):
        received = []
        def analyze(image_data_url, *, source_name='', is_cancelled=None):
            received.append((image_data_url, source_name))
            return {'status': 'needs_manual_correction', 'circuit': None,
                    'accepted': False, 'electrical_validated': False,
                    'detections': [{'class_name': 'resistor', 'box': [1, 2, 3, 4]}],
                    'topology': {'status': 'not_inferred'}, 'issues': []}
        image = 'data:image/png;base64,aGVsbG8='
        with self.with_worker(analyze):
            code, body = self.post({'backend': 'circuitvision', 'image': image,
                                    'source_name': 'board.png'})
        self.assertEqual(code, 200)
        self.assertEqual(received, [(image, 'board.png')])
        self.assertEqual(body['status'], 'needs_manual_correction')
        self.assertIsNone(body['circuit'])
        self.assertFalse(body['accepted'])
        self.assertFalse(body['electrical_validated'])

    def test_missing_or_unknown_backend_and_wrong_image_type_return_400(self):
        with self.with_worker(lambda *a, **k: None):
            for payload in ({'image': 'data:image/png;base64,aGVsbG8='},
                            {'backend': 'other', 'image': 'data:image/png;base64,aGVsbG8='},
                            {'backend': 'circuitvision', 'image': {}}):
                with self.subTest(payload=payload):
                    code, body = self.post(payload)
                    self.assertEqual(code, 400)
                    self.assertIn('error', body)

    def test_only_new_route_accepts_more_than_one_mib_and_caps_at_ten_mib(self):
        seen = []
        with self.with_worker(lambda image_data_url, **k: seen.append(len(image_data_url)) or
                             {'status': 'needs_manual_correction', 'circuit': None,
                              'accepted': False, 'electrical_validated': False}):
            code, _ = self.post({'backend': 'circuitvision',
                                 'image': 'data:image/png;base64,' + 'A' * (1100 * 1024)})
            self.assertEqual(code, 200)
            self.assertEqual(len(seen), 1)
            code, body = self.post({'backend': 'circuitvision',
                                    'image': 'data:image/png;base64,' + 'A' * (11 * 1024 * 1024)}, header_only=True)
            self.assertEqual(code, 400)
            self.assertIn('error', body)
            self.assertEqual(len(seen), 1)

    def test_worker_error_codes_map_to_non_success_http(self):
        mapping = {'invalid_request': 400, 'runtime_unavailable': 503,
                   'busy': 409, 'timeout': 504, 'inference_failed': 502}
        for error_code, expected in mapping.items():
            with self.subTest(error_code=error_code), self.with_worker(
                    lambda *a, **k: (_ for _ in ()).throw(WorkerError(error_code))):
                code, body = self.post({'backend': 'circuitvision',
                                        'image': 'data:image/png;base64,aGVsbG8='})
                self.assertEqual(code, expected)
                self.assertEqual(body['status'], 'incomplete')
                self.assertFalse(body['accepted'])

    def test_disconnect_reaps_controlled_worker_and_allows_retry(self):
        """Use the real bridge/HTTP path with a lightweight Python worker."""
        started = threading.Event()
        processes = []
        original = upstream_vision.subprocess.Popen
        def launch(command, **kwargs):
            if 'tools.circuitvision_worker' not in command:
                return original(command, **kwargs)
            code = ('import time; time.sleep(60)' if not processes else
                    'print(' + repr(json.dumps(worker_result())) + ')')
            process = original([sys.executable, '-B', '-c', code], **kwargs)
            processes.append(process)
            started.set()
            return process
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(upstream_vision, '_runtime_paths', return_value=types.SimpleNamespace(
                    python=Path(sys.executable), worktree=Path(temp) / 'CircuitVision')), \
                patch.object(upstream_vision, '_runtime_ready', return_value=True), \
                patch.object(upstream_vision.subprocess, 'Popen', side_effect=launch), \
                patch.object(upstream_vision, 'WORKER_TIMEOUT_SECONDS', 5):
            try:
                payload = {'backend': 'circuitvision', 'image': PNG_DATA_URL}
                conn.request('POST', '/api/image/analyze', json.dumps(payload), {'Content-Type': 'application/json'})
                self.assertTrue(started.wait(3), 'Controlled worker did not start')
                self.assertEqual(self.post(payload)[0], 409)
                cancelled_at = time.monotonic()
                conn.close()
                deadline = time.monotonic() + 2
                while processes[0].poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                print('vision_cancellation_observation=' + json.dumps({
                    'owned_python_pid': processes[0].pid,
                    'seconds_after_disconnect': round(time.monotonic() - cancelled_at, 3),
                    'worker_returncode': processes[0].poll(),
                }), flush=True)
                self.assertIsNotNone(processes[0].poll(), 'Cancelled request left its vision worker running')
                status, body = self.post(payload)
                self.assertEqual(status, 200)
                self.assertEqual(body['status'], 'needs_manual_correction')
                self.assertFalse(body['accepted'])
            finally:
                conn.close()
                if processes:
                    processes[0].wait(timeout=10)


if __name__ == '__main__':
    unittest.main()

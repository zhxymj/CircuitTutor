"""Real offline TSX engine and HTTP contracts; no frozen evidence rewrites."""
import http.client
import importlib
import json
from pathlib import Path
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RC = (ROOT / 'tools/upstream_examples/tscircuit_rc.tsx').read_text(encoding='utf-8')
CHIP = 'export default () => (<board width="24mm" height="16mm"><chip name="U1" pinLabels={{1:"VCC",2:"GND"}} footprint="soic2" /></board>)'
BAD = 'export default () => (<board><resistor name="R1" resistance={ /></board>)'


class TscircuitEngineTests(unittest.TestCase):
    def engine(self):
        return importlib.import_module('core.tscircuit_engine')

    def test_real_rc_generates_json_svg_and_unaccepted_candidate(self):
        result = self.engine().generate_tscircuit(RC, name='rc.tsx')
        self.assertEqual(result['status'], 'needs_review')
        self.assertTrue(result['schematic_svg'].startswith('<svg'))
        self.assertTrue(any(x['type'] == 'schematic_trace' for x in result['upstream_circuit_json']))
        self.assertEqual([p['id'] for p in result['circuit']['parts'][:3]], ['R1', 'C1', 'R2'])
        self.assertFalse(result['readiness']['accepted'])
        self.assertFalse(result['readiness']['electrical_validated'])
        self.assertIn('RATINGS_UNKNOWN', {issue['code'] for issue in result['issues']})
        self.assertTrue(result['runtime']['offline'])
        self.assertEqual(result['runtime']['versions']['@tscircuit/eval'], '0.0.1619')

    def test_unknown_chip_rejects_whole_graph_and_preserves_preview(self):
        result = self.engine().generate_tscircuit(CHIP)
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(result['circuit'])
        self.assertEqual(result['issues'][0]['code'], 'UNSUPPORTED_COMPONENT')
        self.assertTrue(result['upstream_circuit_json'])
        self.assertTrue(result['schematic_svg'].startswith('<svg'))

    def test_bad_code_preserves_diagnostic_without_candidate(self):
        result = self.engine().generate_tscircuit(BAD)
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(result['circuit'])
        self.assertTrue(result['diagnostics'])
        self.assertIn('main.tsx', json.dumps(result['diagnostics']))

    def test_invalid_request_and_missing_runtime(self):
        engine = self.engine()
        for code in ('', None, ' '):
            with self.assertRaises(ValueError): engine.generate_tscircuit(code)
        with patch.dict('os.environ', {'TSCIRCUIT_PREFIX': str(ROOT / 'missing-runtime')}):
            with self.assertRaises(engine.TscircuitError) as caught:
                engine.generate_tscircuit(RC)
        self.assertEqual(caught.exception.code, 'runtime_unavailable')

    def test_bundled_node_is_used_when_system_node_is_absent(self):
        engine = self.engine()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            prefix = Path(directory)
            (prefix / 'node_modules/@tscircuit/eval').mkdir(parents=True)
            (prefix / 'node_modules/@tscircuit/eval/package.json').write_text('{}')
            (prefix / 'node.exe').touch()
            with patch.dict('os.environ', {'TSCIRCUIT_PREFIX': str(prefix)}), patch.object(engine.shutil, 'which', return_value=None):
                self.assertEqual(Path(engine._runtime_paths()[0]), prefix / 'node.exe')

    def test_real_worker_timeout_reaps_owned_node_and_releases_lock(self):
        engine = self.engine()
        processes = []
        original = engine.subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(engine.subprocess, 'Popen', side_effect=launch), patch.object(engine, 'WORKER_TIMEOUT_SECONDS', 5):
            with self.assertRaises(engine.TscircuitError) as caught:
                engine.generate_tscircuit('export default () => { while(true) {} return <board/> }')
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertIsNotNone(processes[0].poll(), 'Owned Node process survived timeout')
        self.assertTrue(engine._LOCK.acquire(blocking=False))
        engine._LOCK.release()


class TscircuitHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.srv = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thread.join(timeout=2)

    def post(self, payload):
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=90)
        conn.request('POST', '/api/design/tscircuit', json.dumps(payload), {'Content-Type':'application/json'})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_real_http_success_rejection_and_invalid_request(self):
        for payload, expected in (({'code': RC}, 200), ({'code': CHIP}, 422), ({'code': BAD}, 422), ({'code':{}},400)):
            with self.subTest(expected=expected):
                status, body = self.post(payload)
                self.assertEqual(status, expected)
                self.assertFalse(body.get('readiness', {}).get('accepted', False))

    def test_http_runtime_errors(self):
        engine = importlib.import_module('core.tscircuit_engine')
        for code, expected in (('runtime_unavailable',503),('timeout',504),('busy',409)):
            with patch.object(engine, 'generate_tscircuit', side_effect=engine.TscircuitError(code, code)):
                self.assertEqual(self.post({'code':RC})[0], expected)

    def test_disconnect_reaps_worker_and_allows_immediate_retry(self):
        engine = importlib.import_module('core.tscircuit_engine')
        started = threading.Event()
        processes = []
        original = engine.subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            started.set()
            return process
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        with patch.object(engine.subprocess, 'Popen', side_effect=launch), patch.object(engine, 'WORKER_TIMEOUT_SECONDS', 5):
            try:
                conn.request('POST', '/api/design/tscircuit', json.dumps({
                    'code': 'export default () => { while(true) {} return <board/> }',
                }), {'Content-Type': 'application/json'})
                self.assertTrue(started.wait(3), 'Owned Node did not start')
                self.assertEqual(self.post({'code': RC})[0], 409)
                cancelled_at = time.monotonic()
                conn.close()
                deadline = time.monotonic() + 2
                while processes[0].poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                print('cancellation_observation=' + json.dumps({
                    'owned_node_pid': processes[0].pid,
                    'seconds_after_disconnect': round(time.monotonic() - cancelled_at, 3),
                    'node_returncode': processes[0].poll(),
                }), flush=True)
                self.assertIsNotNone(processes[0].poll(), 'Cancelled request left its owned Node running')
                status, body = self.post({'code': RC})
                self.assertEqual(status, 200)
                self.assertEqual(body['status'], 'needs_review')
            finally:
                conn.close()
                if processes:
                    processes[0].wait(timeout=10)


if __name__ == '__main__': unittest.main()

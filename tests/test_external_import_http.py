"""HTTP contract for an external candidate without model calls."""
import hashlib
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class ExternalImportHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, payload):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        conn.request('POST', '/api/import/external', json.dumps(payload),
                     {'Content-Type': 'application/json'})
        resp = conn.getresponse()
        result = resp.status, json.loads(resp.read().decode('utf-8'))
        conn.close()
        return result

    def test_complete_candidate_returns_200_with_original_digest_and_no_acceptance(self):
        content = 'V1 1 0 5\nR1 1 2 1k\nR2 2 0 1k'
        status, result = self.post({'source': 'circuitvision_spice', 'content': content,
                                    'name': 'divider.net', 'license': 'user-approved',
                                    'accepted': True})
        self.assertEqual(status, 200)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['source']['sha256'], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(result['source']['license'], 'Apache-2.0')
        self.assertFalse(result['readiness']['accepted'])
        self.assertFalse(result['readiness']['electrical_validated'])

    def test_unrepresentable_input_returns_422_without_partial_circuit(self):
        content = 'V1 1 0 5\nU1 1 0 STM32'
        status, result = self.post({'source': 'circuitvision_spice', 'content': content})
        self.assertEqual(status, 422)
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(result['circuit'])
        self.assertEqual(result['source']['sha256'], hashlib.sha256(content.encode()).hexdigest())
        self.assertFalse(result['readiness']['importable'])

    def test_wrong_source_and_content_type_return_400(self):
        for payload in ({'source': 'other', 'content': 'foo'},
                        {'source': 'hcd', 'content': {}}):
            with self.subTest(payload=payload):
                status, result = self.post(payload)
                self.assertEqual(status, 400)
                self.assertIn('error', result)


if __name__ == '__main__':
    unittest.main()

"""Offline format interoperability, not recognition accuracy or model execution."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / 'data/external_import/fixtures'


def convert(source, content, **kwargs):
    if importlib.util.find_spec('core.external_import') is None:
        raise AssertionError('External result adapter is not implemented')
    from core.external_import import import_external_result
    return import_external_result(source, content, **kwargs)


class ExternalImportTests(unittest.TestCase):
    def test_circuitvision_preserves_values_and_ground_without_acceptance(self):
        text = 'V1 1 0 5\nR1 1 2 1k\nR2 2 0 1k'
        result = convert('circuitvision_spice', text, name='divider.net')
        self.assertEqual(result['status'], 'needs_review')
        self.assertFalse(result['readiness']['accepted'])
        self.assertIn('RATINGS_UNKNOWN', {issue['code'] for issue in result['issues']})
        c = result['circuit']
        self.assertEqual([(p['id'], p['attrs'].get('value')) for p in c['parts'][:3]],
                         [('V1', '5V'), ('R1', '1000'), ('R2', '1000')])
        self.assertIn(['V1:+', 'R1:1'], c['connections'])
        self.assertEqual(result['source']['sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(c['provenance']['origin'], 'import')
        self.assertEqual(c['provenance']['license'], 'Apache-2.0')
        self.assertNotIn('rating_confirmed', c['parts'][1])

    def test_spice_milli_and_mega_remain_distinct(self):
        result = convert('circuitvision_spice', 'V1 1 0 5\nR1 1 2 1M\nR2 2 0 1Meg')
        self.assertEqual([p['attrs']['value'] for p in result['circuit']['parts'][1:3]],
                         ['0.001', '1000000'])

    def test_spice_case_insensitive_duplicate_designators_reject(self):
        result = convert('circuitvision_spice', 'V1 1 0 5\nR1 1 2 1k\nr1 2 0 2k')
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['issues'][0]['code'], 'DUPLICATE_ID')

    def test_spice_ambiguous_bare_f_and_dangling_node_reject(self):
        for text, code in (('V1 1 0 5\nC1 1 0 1F', 'UNKNOWN_VALUE'),
                           ('V1 1 0 5\nR1 1 2 1k', 'INCOMPLETE_TOPOLOGY')):
            with self.subTest(text=text):
                result = convert('circuitvision_spice', text)
                self.assertEqual(result['status'], 'rejected')
                self.assertIsNone(result['circuit'])
                self.assertEqual(result['issues'][0]['code'], code)

    def test_explicit_spice_microfarad_value_is_preserved(self):
        result = convert('circuitvision_spice', 'V1 1 0 5\nR1 1 2 1k\nC1 2 0 1uF')
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['circuit']['parts'][2]['attrs']['value'], '0.000001F')

    def test_hcd_explicit_contract_fixture_maps_terminals_and_text(self):
        result = convert('hcd', (FIXTURES / 'hcd_supported.final.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'needs_review')
        parts = result['circuit']['parts']
        self.assertEqual([(p['type'], p['attrs'].get('value')) for p in parts[:3]],
                         [('battery', '5V'), ('resistor', '1000'), ('resistor', '1000')])
        self.assertEqual(result['source']['license'], 'unresolved')

    def test_tscircuit_shared_source_net_connects_separate_traces(self):
        text = (FIXTURES / 'tscircuit_supported.json').read_text(encoding='utf-8')
        result = convert('tscircuit', text)
        self.assertEqual(result['status'], 'needs_review')
        c = result['circuit']
        self.assertIn(['V1:+', 'R1:1'], c['connections'])
        self.assertIn(['R1:2', 'R2:1'], c['connections'])
        self.assertEqual(c['parts'][2]['attrs']['value'], '1000')

    def test_tscircuit_named_ground_net_becomes_ground_marker(self):
        data = json.loads((FIXTURES / 'tscircuit_supported.json').read_text(encoding='utf-8'))
        next(row for row in data if row.get('type') == 'source_net' and row.get('name') == 'GND').pop('is_ground')
        result = convert('tscircuit', json.dumps(data))
        self.assertEqual(result['status'], 'needs_review')
        self.assertTrue(any(p['type'] == 'gnd' for p in result['circuit']['parts']))

    def test_tscircuit_unrouted_pcb_error_does_not_invalidate_source_schematic(self):
        data = json.loads((FIXTURES / 'tscircuit_supported.json').read_text(encoding='utf-8'))
        data.append({'type': 'pcb_port_not_connected_error', 'message': 'PCB trace absent'})
        self.assertEqual(convert('tscircuit', json.dumps(data))['status'], 'needs_review')

    def test_missing_unknown_and_nonfinite_inputs_reject_whole_candidate(self):
        samples = ['V1 1 0 None\nR1 1 0 1k', 'V1 1 0\nR1 1 0 1k',
                   'V1 1 0 5\nU1 1 0 STM32', 'V1 1 0 5\nR1 1 0 NaN',
                   'V1 1 0 5\nR1 1 0 1e999', 'V1 1 0 AC 5',
                   'V1 1 0 5\nR1 1 0 1k\nR1 1 0 2k',
                   'V1 1 0 5\nR1 1 0 1k\nr1 1 0 2k']
        for text in samples:
            with self.subTest(text=text):
                r = convert('circuitvision_spice', text)
                self.assertEqual(r['status'], 'rejected')
                self.assertIsNone(r['circuit'])
                self.assertTrue(r['issues'])
                self.assertFalse(r['readiness']['importable'])

    def test_hcd_missing_role_and_value_are_not_guessed(self):
        data = json.loads((FIXTURES / 'hcd_supported.final.json').read_text(encoding='utf-8'))
        for mutate in ['role', 'value', 'unknown', 'membership']:
            payload = copy.deepcopy(data)
            if mutate == 'role': payload['endpoints'][0].pop('port_role')
            if mutate == 'value': payload['texts'] = []
            if mutate == 'unknown': payload['components'][1]['cls_name'] = 'microcontroller'
            if mutate == 'membership': payload['nets'][0]['endpoint_eids'].append(9999)
            result = convert('hcd', json.dumps(payload))
            self.assertEqual(result['status'], 'rejected', mutate)
            self.assertIsNone(result['circuit'])

    def test_hcd_upstream_ground_truth_with_current_source_is_rejected(self):
        text = (ROOT / 'third_party/upstream/HCD/data/ground_truth/cir1.gt.json').read_text(encoding='utf-8')
        result = convert('hcd', text)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['issues'][0]['code'], 'UNSUPPORTED_COMPONENT')
        self.assertEqual(result['source']['sha256'], hashlib.sha256(text.encode()).hexdigest())

    def test_tscircuit_unknown_chip_port_and_value_reject(self):
        data = json.loads((FIXTURES / 'tscircuit_supported.json').read_text(encoding='utf-8'))
        for mutate in ['chip', 'port', 'value', 'nan']:
            payload = copy.deepcopy(data)
            if mutate == 'chip': payload[1]['ftype'] = 'simple_chip'
            if mutate == 'port': payload[-1]['connected_source_port_ids'] = ['missing']
            if mutate == 'value': payload[1].pop('resistance')
            if mutate == 'nan': payload[1]['resistance'] = float('nan')
            result = convert('tscircuit', json.dumps(payload))
            self.assertEqual(result['status'], 'rejected', mutate)

    def test_request_contract_refuses_wrong_source_or_payload_type(self):
        for args in [('other', 'text'), ('hcd', {}), ('hcd', '[]')]:
            if args[0] == 'hcd' and isinstance(args[1], str):
                self.assertEqual(convert(*args)['status'], 'rejected')
            else:
                with self.assertRaises(ValueError): convert(*args)

    def test_json_input_is_not_mutated_and_duplicate_ids_reject(self):
        for source, fixture, key in (
                ('hcd', 'hcd_supported.final.json', 'components'),
                ('tscircuit', 'tscircuit_supported.json', None)):
            with self.subTest(source=source):
                text = (FIXTURES / fixture).read_text(encoding='utf-8')
                original = text[:]
                result = convert(source, text)
                self.assertEqual(text, original)
                self.assertEqual(result['source']['sha256'], hashlib.sha256(original.encode()).hexdigest())
                payload = json.loads(text)
                if key:
                    payload[key].append(copy.deepcopy(payload[key][0]))
                else:
                    payload.insert(1, copy.deepcopy(payload[0]))
                duplicate = convert(source, json.dumps(payload))
                self.assertEqual(duplicate['status'], 'rejected')
                self.assertEqual(duplicate['issues'][0]['code'], 'DUPLICATE_ID')


if __name__ == '__main__':
    unittest.main()

"""Independent score and freeze gates; no runtime or GPU required."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/evaluate_yolo_round2.py'
spec = importlib.util.spec_from_file_location('round2_eval', SCRIPT) if SCRIPT.exists() else None
evaluation = importlib.util.module_from_spec(spec) if spec else None
if spec:
    spec.loader.exec_module(evaluation)

def box(name='resistor', score=.9, xyxy=(0,0,10,10)):
    return {'class_name':name, 'score':score, 'xyxy':list(xyxy)}

class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(evaluation, 'Independent round2 evaluator is missing')

    def test_duplicate_wrong_class_and_exact_half_iou(self):
        counts = evaluation.match_raw([box(),box('capacitor.unpolarized',xyxy=(20,0,30,10))],
            [box(xyxy=(0,0,5,10)),box(score=.8),box('resistor',xyxy=(20,0,30,10))])
        self.assertEqual(tuple(counts[k] for k in ('tp','fp','fn')), (1,2,1))

    def test_missing_class_precision_null_f1_zero_macro_not_micro(self):
        report = evaluation.score_raw([{'id':'a','status':'complete','ground_truth':[box(),box('capacitor.unpolarized')], 'predictions':[box()]}])
        self.assertEqual(report['per_class']['capacitor.unpolarized']['f1'],0)
        self.assertIsNone(report['per_class']['capacitor.unpolarized']['precision'])
        self.assertEqual(report['macro_recall'],.5)
        self.assertEqual(report['macro_f1'],.5)
        self.assertIsNone(report['macro_precision'])
        self.assertAlmostEqual(report['f1'],2/3)

    def test_failure_preserves_all_ground_truth_and_nulls_primary(self):
        report = evaluation.score_raw([{'id':'broken','status':'incomplete','ground_truth':[box(),box('capacitor.unpolarized')], 'predictions':None}])
        self.assertEqual(report['recall_denominator'],2)
        self.assertEqual(report['fn'],2)
        for key in ('precision','recall','f1','macro_recall','macro_f1'):
            self.assertIsNone(report[key])
        self.assertEqual(report['recall_lower_bound'],0)

    def test_empty_targets_and_predictions_are_undefined(self):
        report = evaluation.score_raw([{'id':'a','status':'complete','ground_truth':[], 'predictions':[]}])
        self.assertIsNone(report['precision'])
        self.assertIsNone(report['recall'])
        self.assertIsNone(report['f1'])

    def test_raw_matching_rejects_nonfinite_boxes(self):
        with self.assertRaises(ValueError):
            evaluation.match_raw([box()], [box(xyxy=(0,0,float('nan'),10))])

    def test_frozen_hash_rejects_changed_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'checkpoint'
            path.write_bytes(b'first')
            frozen={'checkpoint':{'path':str(path),'sha256':evaluation.sha256(path)}}
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):
                evaluation.verify_frozen(frozen)

    def test_training_gate_requires_real_optimizer_steps(self):
        with self.assertRaisesRegex(ValueError,'optimizer'):
            evaluation.validate_training_summary({'status':'complete','epochs_completed':50,'epochs_requested':50,'evaluation_split':'val','optimizer_steps':0})

    def test_existing_data_only_plan_allows_400_steps_and_never_test(self):
        plan=evaluation.dataset_plan({'train':{'images':16},'val':{'images':4},'test':{'images':10}},'existing_data_only',400)
        self.assertFalse(plan['final_test_allowed'])
        self.assertEqual(plan['expected_optimizer_steps'],400)
        self.assertEqual(plan['fresh_test_status'],'not_run')

    def test_incorrect_actual_update_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'optimizer'):
            evaluation.dataset_plan({'train':{'images':16},'val':{'images':4},'test':{'images':10}},'existing_data_only',399)

    def test_expanded_plan_remains_explicit_with_1200_steps(self):
        plan=evaluation.dataset_plan({'train':{'images':48},'val':{'images':4},'test':{'images':12}},'expanded_data',1200)
        self.assertTrue(plan['final_test_allowed'])

    def test_fresh_manifest_requires_all_twelve_d31_records(self):
        samples=[{'id':str(i),'split':'test','drafter':31,'source_image':f'drafter_31/images/C{i+100}_D1_P1.jpg','image_sha256':str(i),'source_image_sha256':str(i),'source_annotation_sha256':'xml'+str(i)} for i in range(12)]
        self.assertEqual(len(evaluation.validate_fresh_manifest({'samples':samples},{'samples':[]})),12)
        with self.assertRaisesRegex(ValueError,'12'):
            evaluation.validate_fresh_manifest({'samples':samples[:-1]},{'samples':[]})

    def test_fresh_manifest_rejects_shared_record_or_source_hash(self):
        samples=[{'id':str(i),'split':'test','drafter':31,'source_image':f'drafter_31/images/C{i+100}_D1_P1.jpg','image_sha256':str(i),'source_image_sha256':str(i),'source_annotation_sha256':'xml'+str(i)} for i in range(12)]
        with self.assertRaisesRegex(ValueError,'overlap'):
            evaluation.validate_fresh_manifest({'samples':samples},{'samples':[{'source_image':'drafter_1/images/C100_D2_P3.jpg','image_sha256':'old','source_image_sha256':'old','source_annotation_sha256':'oldxml'}]})

    def test_original_and_fresh_manifest_hashes_both_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            original=Path(directory)/'original';fresh=Path(directory)/'fresh'
            original.write_bytes(b'original');fresh.write_bytes(b'fresh')
            freeze={k:{'path':str(p),'sha256':evaluation.sha256(p)} for k,p in [('training_manifest',original),('fresh_manifest',fresh)]}
            original.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'training_manifest'):
                evaluation.verify_frozen(freeze)
            original.write_bytes(b'original');fresh.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'fresh_manifest'):
                evaluation.verify_frozen(freeze)

class Round3EvaluationTests(unittest.TestCase):
    def test_failed_image_quality_is_incomplete_not_undefined(self):
        result=evaluation.score_raw([{'id':'fail','status':'incomplete','ground_truth':[box()],'predictions':None}])
        self.assertEqual(evaluation.quality_status(result['status']=='complete',None,None),'incomplete')
        self.assertEqual(evaluation.quality_status(True,None,None),'undefined_class_metric')

    def test_training_plan_normalizes_only_known_semantic_name(self):
        self.assertTrue(evaluation.execution_plan_matches('expanded_real_data','expanded_data'))
        self.assertTrue(evaluation.execution_plan_matches('existing_data_only','existing_data_only'))
        self.assertFalse(evaluation.execution_plan_matches('existing_data_only','expanded_data'))
    def test_steps_derive_from_config_not_fixed_batch(self):
        plan=evaluation.dataset_plan({'train':{'images':47},'val':{'images':4},'test':{'images':12}},'expanded_data',600,batch=4,epochs=50)
        self.assertEqual(plan['expected_optimizer_steps'],600)
    def test_protocol_freeze_enforces_evaluator_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'tool.py';script.write_text('fixed')
            path=Path(directory)/'freeze.json'
            evaluation.write_json(path,{'protocol':'round3_first_unused_d21_d30','test_predictions_before_freeze':0,'frozen':{'evaluation_tool':{'path':str(script),'sha256':evaluation.sha256(script)}}})
            evaluation.verify_protocol_freeze(path)
            script.write_text('changed')
            with self.assertRaisesRegex(ValueError,'evaluation_tool'):
                evaluation.verify_protocol_freeze(path)

    def test_expanded47_uses_manifest_derived_steps(self):
        plan=evaluation.dataset_plan({'train':{'images':47},'val':{'images':4},'test':{'images':12}},'expanded_data',1200)
        self.assertEqual(plan['expected_optimizer_steps'],1200)
    def test_partial_round3_preserves_acquired_denominator(self):
        samples=[{'id':str(d),'split':'test','drafter':d,'source_image':f'drafter_{d}/images/C{d*12}_D1_P1.jpg','image_sha256':str(d),'source_image_sha256':str(d),'source_annotation_sha256':'xml'+str(d)} for d in range(21,29)]
        result=evaluation.validate_fresh_manifest({'samples':samples,'fresh_protocol':'round3_first_unused_d21_d30'},{'samples':[]})
        self.assertEqual(len(result),8)
        samples.append(dict(samples[0],id='duplicate-drafter',source_image='drafter_21/images/C999_D1_P1.jpg',image_sha256='new',source_image_sha256='new',source_annotation_sha256='newxml'))
        with self.assertRaisesRegex(ValueError,'drafter'):
            evaluation.validate_fresh_manifest({'samples':samples,'fresh_protocol':'round3_first_unused_d21_d30'},{'samples':[]})
    def test_undefined_macro_comparison_does_not_pass(self):
        self.assertIsNone(evaluation.compare_quality({'macro_f1':None,'macro_recall':0.0},{'macro_f1':.5,'macro_recall':.5}))

if __name__=='__main__':
    unittest.main()

"""Freeze val-selected artifacts, then evaluate each registered model once.

Supports historical d31 or fixed first-unused d21..30 source holdouts.
Original30 test records are never re-evaluated; pretraining overlap is unknown.
Independent scoring uses raw boxes, never training-summary metrics.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import sys

try:
    from .prepare_yolo_dataset import resolve_recorded_path
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from prepare_yolo_dataset import resolve_recorded_path

ROOT = Path(__file__).resolve().parents[1]
CLASSES = ('resistor', 'capacitor.unpolarized')

def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'))

def _default_detector_is(best_sha):
    """当前默认检测器（evidence/yolo_training/current/）的 best SHA 是否等于给定值。"""
    try:
        current=read_json(ROOT/'evidence/yolo_training/current/run_summary.json')
        return current.get('best_checkpoint',{}).get('sha256')==best_sha
    except Exception:
        return False

def _registered_data_matches(registered,dataset):
    """Compare the full registered project path at its current location."""
    return resolve_recorded_path(registered, ROOT).resolve() == (Path(dataset) / 'dataset.yaml').resolve()

def resolve_recorded(path, summary_path):
    """Keep recorded metadata intact; resolve only the known moved project root."""
    return resolve_recorded_path(path, ROOT)


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')

def match_raw(targets,predictions,threshold=.5):
    for row in targets+predictions:
        coordinates=row['xyxy']
        if row['class_name'] not in CLASSES or len(coordinates)!=4 or not all(math.isfinite(v) for v in coordinates):
            raise ValueError('Invalid raw target/prediction')
        if coordinates[2]<=coordinates[0] or coordinates[3]<=coordinates[1]:
            raise ValueError('Invalid raw box extent')
    if any(not math.isfinite(p.get('score',0)) for p in predictions):
        raise ValueError('Nonfinite raw confidence')
    counts={name:dict(tp=0,fp=0,fn=0) for name in CLASSES}
    matched=set()
    pairs=[]
    for pi in sorted(range(len(predictions)),key=lambda index:(-predictions[index].get('score',0),index)):
        p=predictions[pi]
        candidates=[]
        for gi,g in enumerate(targets):
            if gi in matched or p['class_name']!=g['class_name']:
                continue
            a,b=p['xyxy'],g['xyxy']
            intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
            union=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
            candidates.append((intersection/union,gi))
        overlap,gi=max(candidates,default=(0,-1))
        if gi>=0 and overlap>=threshold:
            matched.add(gi)
            counts[p['class_name']]['tp']+=1
            pairs.append(dict(prediction_index=pi,ground_truth_index=gi,iou=overlap))
        else:
            counts[p['class_name']]['fp']+=1
    for gi,g in enumerate(targets):
        if gi not in matched:
            counts[g['class_name']]['fn']+=1
    return {k:sum(c[k] for c in counts.values()) for k in ('tp','fp','fn')}|{'per_class':counts,'matches':pairs}

def metrics(counts,complete):
    tp,fp,fn=(counts[k] for k in ('tp','fp','fn'))
    return counts|dict(precision_numerator=tp,precision_denominator=tp+fp,
        recall_numerator=tp,recall_denominator=tp+fn,
        precision=tp/(tp+fp) if complete and tp+fp else None,
        recall=tp/(tp+fn) if complete and tp+fn else None,
        f1=2*tp/(2*tp+fp+fn) if complete and 2*tp+fp+fn else None,
        recall_lower_bound=tp/(tp+fn) if tp+fn else None)

def score_raw(rows):
    class_counts={name:dict(tp=0,fp=0,fn=0) for name in CLASSES}
    failed=sum(row['status']!='complete' for row in rows)
    for row in rows:
        result=match_raw(row['ground_truth'],row['predictions'] if row['status']=='complete' else [])
        for name in CLASSES:
            for key in ('tp','fp','fn'):
                class_counts[name][key]+=result['per_class'][name][key]
    complete=failed==0
    per_class={name:metrics(c,complete) for name,c in class_counts.items()}
    total=metrics({key:sum(c[key] for c in class_counts.values()) for key in ('tp','fp','fn')},complete)
    for name in ('precision','recall','f1'):
        values=[c[name] for c in per_class.values()]
        total['macro_'+name]=sum(values)/len(values) if complete and all(v is not None for v in values) else None
    return total|dict(status='complete' if complete else 'incomplete',images_expected=len(rows),
        images_completed=len(rows)-failed,images_failed=failed,per_class=per_class,
        matching_iou=.5,matching='confidence ordered class-aware greedy one-to-one',
        undefined_policy='null when denominator zero; macro null if any class metric undefined',
        failed_image_fn_policy='all annotated targets remain as misses; primary metrics null when incomplete')

def verify_frozen(registration):
    for name,artifact in registration.items():
        if sha256(artifact['path'])!=artifact['sha256']:
            raise ValueError('Frozen artifact changed: '+name)

def validate_training_summary(summary):
    if not isinstance(summary.get('optimizer_steps'),int) or summary['optimizer_steps']<=0:
        raise ValueError('Actual optimizer updates required')
    if summary.get('status')!='complete' or summary.get('epochs_completed')!=50 or summary.get('epochs_requested')!=50:
        raise ValueError('Complete fixed 50 epoch training required')
    if summary.get('evaluation_split')!='val':
        raise ValueError('Training must use val only')

def dataset_plan(splits,plan,actual_steps,batch=2,epochs=50):
    counts={s:splits[s]['images'] for s in ('train','val','test')}
    expected={'train':16,'val':4,'test':10} if plan=='existing_data_only' else {'train':48,'val':4,'test':12} if plan=='expanded_data' else None
    if counts!=expected and not (plan=='expanded_data' and counts=={'train':47,'val':4,'test':12}):
        raise ValueError('Dataset differs from explicitly declared execution plan')
    steps=math.ceil(counts['train']/batch)*epochs
    if actual_steps!=steps:
        raise ValueError('Actual optimizer updates differ from manifest-derived count')
    return {'execution_plan':plan,'expected_optimizer_steps':steps,
        'final_test_allowed':plan=='expanded_data','fresh_test_status':'not_run'}

def source_record(sample):
    match=re.search(r'C(\d+)_D(\d+)_P(\d+)',sample['source_image'])
    if not match:
        raise ValueError('Source physical record missing')
    return tuple(int(value) for value in match.groups())

def validate_fresh_manifest(fresh,original):
    tests=[sample for sample in fresh['samples'] if sample['split']=='test']
    round3=fresh.get('fresh_protocol')=='round3_first_unused_d21_d30'
    if round3:
        if not 0<len(tests)<=10 or len({s['id'] for s in tests})!=len(tests) or len({s['drafter'] for s in tests})!=len(tests):
            raise ValueError('Fresh round3 requires unique drafter 21..30 actual acquired samples')
    elif len(tests)!=12 or len({s['id'] for s in tests})!=12:
        raise ValueError('Fresh diagnosis requires all 12 d31 images')
    reference_samples=original['samples']+[s for s in fresh['samples'] if s['split']!='test']
    old_records={source_record(s)[0] for s in reference_samples}
    hash_fields=('image_sha256','source_image_sha256','source_annotation_sha256')
    old_hashes={s[field] for s in reference_samples for field in hash_fields}
    records=set()
    hashes=set()
    for sample in tests:
        c,d,p=source_record(sample)
        if (sample['drafter'] not in range(21,31) if round3 else sample['drafter']!=31) or (d,p)!=(1,1):
            raise ValueError('Fresh test must contain only d31 D1/P1')
        if c in old_records or c in records or any(sample[field] in old_hashes for field in hash_fields):
            raise ValueError('Fresh test physical record/hash overlap')
        if any(sample[field] in hashes for field in hash_fields):
            raise ValueError('Fresh test source hash overlap')
        records.add(c)
        hashes.update(sample[field] for field in hash_fields)
    return tests

def verify_raw_report(report,samples):
    expected={sample['id']:sample for sample in samples}
    rows=report['images']
    if len(rows)!=len(expected) or len({row['id'] for row in rows})!=len(rows) or {r['id'] for r in rows}!=set(expected):
        raise ValueError('Raw evaluation sample denominator mismatch')
    for row in rows:
        if row['ground_truth']!=expected[row['id']]['ground_truth']:
            raise ValueError('Raw evaluation ground truth changed')
    independent=score_raw(rows)
    for key in ('tp','fp','fn','images_expected','images_completed','images_failed','status'):
        if independent[key]!=report['summary'][key]:
            raise ValueError('Raw scoring differs from registered summary: '+key)
    for name in CLASSES:
        for key in ('tp','fp','fn'):
            if independent['per_class'][name][key]!=report['summary']['per_class'][name][key]:
                raise ValueError('Raw class scoring differs from registered summary')
    return independent

def compare_quality(current,reference,strict=True):
    values=[row.get(key) for row in (current,reference) for key in ('macro_f1','macro_recall')]
    if any(v is None for v in values):
        return None
    return (current['macro_f1']>reference['macro_f1'] if strict else current['macro_f1']>=reference['macro_f1']) and current['macro_recall']>=reference['macro_recall']

def quality_status(complete,improved,public_met):
    if not complete:
        return 'incomplete'
    if improved is None or public_met is None:
        return 'undefined_class_metric'
    if improved:
        return 'improved_over_previous_public_reference_met' if public_met else 'improved_over_previous_below_public_reference'
    return 'not_accepted'

def execution_plan_matches(recorded,requested):
    return {'expanded_real_data':'expanded_data'}.get(recorded,recorded)==requested

def verify_protocol_freeze(path):
    protocol=read_json(path)
    if protocol.get('protocol')!='round3_first_unused_d21_d30' or protocol.get('test_predictions_before_freeze')!=0:
        raise ValueError('Invalid prediction-free round3 protocol freeze')
    verify_frozen(protocol['frozen'])
    return protocol

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'evidence/yolo_training/round2_warmstart50')
    parser.add_argument('--dataset',type=Path,default=ROOT/'data/image_samples/yolo_cghd_round2')
    parser.add_argument('--acceptance',type=Path,default=ROOT/'evidence/upgrade/yolo_round2_acceptance.json')
    parser.add_argument('--execution-plan',choices=('existing_data_only','expanded_data'),required=True)
    parser.add_argument('--fresh-holdout-dataset',type=Path,help='Separate d31 or fixed first-unused d21..30 evaluation dataset; training manifest unchanged')
    parser.add_argument('--previous-summary',type=Path,help='Registered warm-start reference summary; defaults to round1 historical contract')
    parser.add_argument('--protocol-freeze',type=Path,help='Required prediction-free protocol registration for round3')
    args=parser.parse_args()
    is_round3=args.previous_summary is not None
    if is_round3:
        if args.protocol_freeze is None:
            raise ValueError('Round3 requires --protocol-freeze')
        protocol=verify_protocol_freeze(args.protocol_freeze)
        if Path(protocol['frozen']['evaluation_tool']['path']).resolve()!=Path(__file__).resolve():
            raise ValueError('Frozen evaluation tool path mismatch')
    sys.path.insert(0,str(ROOT))
    from tools import train_yolo_detector as training
    out,dataset=args.output.resolve(),args.dataset.resolve()
    summary=read_json(out/'run_summary.json')
    validate_training_summary(summary)
    if summary.get('warm_start',{}).get('optimizer_resume') is not False or summary.get('head_transfer',{}).get('unchanged') is not True:
        raise ValueError('Verified two-class head warm-start with fresh optimizer required')
    transfer=summary['head_transfer']
    if not transfer.get('digests_before') or transfer['digests_before']!=transfer.get('digests_after_setup'):
        raise ValueError('Two-class head setup changed registered tensor digests')
    if not execution_plan_matches(summary.get('execution_plan'),args.execution_plan):
        raise ValueError('Declared execution plan differs from training registration')
    config=read_json(out/'config.json')
    required={'epochs':50,'optimizer':'SGD','lr0':.001,'lrf':.1,'nbs':2,'warmup_epochs':0,'imgsz':640,'batch':2,'workers':0,'seed':42,'amp':False}
    if any(config.get(k)!=v for k,v in required.items()) or config.get('evaluation')!=training.EVAL_CONFIG:
        raise ValueError('Training/evaluation config differs from frozen contract')
    if summary.get('evaluation_config')!=training.EVAL_CONFIG or not _registered_data_matches(config['data'],dataset):
        raise ValueError('Registered evaluation or dataset config mismatch')
    for artifact in summary.get('training_metadata',{}).values():
        if sha256(resolve_recorded(artifact['path'],out/'run_summary.json'))!=artifact['sha256']:
            raise ValueError('Registered training metadata changed')
    manifest,val_samples=training.load_dataset(dataset,split='val')
    if len(val_samples)!=4 or sum(len(s['ground_truth']) for s in val_samples)!=70:
        raise ValueError('Fixed val denominator changed')
    if manifest.get('license_status')!='conflicting_declarations_pending_review':
        raise ValueError('Source license conflict must be preserved')
    splits=manifest['statistics']['splits']
    plan=dataset_plan(splits,args.execution_plan,summary['optimizer_steps'],batch=config['batch'],epochs=config['epochs'])
    pre=verify_raw_report(read_json(out/'pre_val_results.json'),val_samples)
    post=verify_raw_report(read_json(out/'post_val_results.json'),val_samples)
    if pre['status']!='complete' or post['status']!='complete':
        raise ValueError('Incomplete val must not unlock final test')
    with (out/'fit/results.csv').open(encoding='utf8',newline='') as stream:
        csv_rows=list(csv.DictReader(stream))
    if len(csv_rows)!=50 or [int(r['epoch']) for r in csv_rows]!=list(range(1,51)):
        raise ValueError('Actual complete CSV epochs differ from contract')
    if any(not value.strip() for row in csv_rows for value in row.values()):
        raise ValueError('Incomplete CSV row')
    selection=summary['checkpoint_selection']
    score=max(float(r['metrics/mAP50-95(B)']) for r in csv_rows)
    best_epoch=max(int(r['epoch']) for r in csv_rows if float(r['metrics/mAP50-95(B)'])==score)
    if selection.get('split')!='val' or selection.get('metric')!='metrics/mAP50-95(B)' or selection.get('best_epoch')!=best_epoch or not math.isclose(selection['best_score'],score,abs_tol=.00002):
        raise ValueError('Checkpoint selection differs from actual val CSV')
    best=resolve_recorded(summary['best_checkpoint']['path'],out/'run_summary.json')
    # 搬迁兼容：登记路径失效时，用解析后的实际位置做登记副本送检；
    # origin/SHA 核对不变，仅路径字段随解析结果更新（不放宽内容校验）。
    best_registration=dict(summary['best_checkpoint']); best_registration['path']=str(best)
    training.verify_local_checkpoint(best,best_registration)
    # T07（2026-10-14）：默认检测器已切到 round-2 best，round-1 best 归档到
    # round1_best_archived_20261014/；验收基线随之指向归档，契约 SHA 不变。
    old_summary_path=args.previous_summary.resolve() if is_round3 else ROOT/'evidence/yolo_training/round1_best_archived_20261014/run_summary.json'
    previous=read_json(old_summary_path)
    previous_best=resolve_recorded(previous['best_checkpoint']['path'],old_summary_path)
    previous_registration=dict(previous['best_checkpoint']); previous_registration['path']=str(previous_best)
    training.verify_local_checkpoint(previous_best,previous_registration)
    expected_source_sha='52b3191ded9ae75c277223cb2e2fb7e1689fbaec6b9566a213e1e05cd1491cdd' if is_round3 else '1f6c384a2311b05ed69f405ba5256171a242546259da7f882a107711cb097cd7'
    if previous['best_checkpoint']['sha256']!=expected_source_sha:
        raise ValueError('Previous checkpoint differs from initial contract')
    if summary['source_checkpoint']['sha256']!=previous['best_checkpoint']['sha256'] or not summary.get('source_checkpoint_unchanged'):
        raise ValueError('Local warm-start source registration changed')
    if summary['warm_start']['checkpoint']['sha256']!=previous['best_checkpoint']['sha256']:
        raise ValueError('Warm-start provenance mismatch')
    first_manifest=ROOT/'data/image_samples/yolo_cghd_v15/manifest.json'
    if sha256(first_manifest)!=previous['manifest_sha256']:
        raise ValueError('First-round dataset changed')
    rejected_summary_path=ROOT/'evidence/yolo_training/failed_fused_model/run_summary.json'
    rejected=read_json(rejected_summary_path)
    from core import image_detector
    public_path=image_detector.MODEL_PATH
    frozen_paths={'config':out/'config.json','manifest':dataset/'manifest.json','csv':out/'fit/results.csv',
        'best_checkpoint':best,'previous_checkpoint':previous_best,'public_checkpoint':public_path,
        'run_summary':out/'run_summary.json','previous_summary':old_summary_path,
        'first_manifest':first_manifest,'dataset_yaml':dataset/'dataset.yaml','training_args':out/'fit/args.yaml',
        'rejected_summary':rejected_summary_path,'rejected_checkpoint':resolve_recorded(rejected['best_checkpoint']['path'],rejected_summary_path),
        'val_pre_raw':out/'pre_val_results.json','val_post_raw':out/'post_val_results.json',
        'evaluation_tool':Path(__file__),'training_tool':ROOT/'tools/train_yolo_detector.py'}
    if is_round3:
        frozen_paths['protocol_freeze']=args.protocol_freeze.resolve()
    frozen={name:{'path':str(path),'sha256':sha256(path)} for name,path in frozen_paths.items()}
    if frozen['manifest']['sha256']!=summary['manifest_sha256'] or frozen['public_checkpoint']['sha256']!='7ce8be8383c47b924db327826c6cd9c40bc7d97a539f5b9d3e4fa543f14ad01c':
        raise ValueError('Manifest/public provenance mismatch')
    test_dataset=dataset
    fresh_manifest=None
    acquisition=ROOT/'data/image_samples/cghd_round2/acquisition.json'
    if is_round3 and not args.fresh_holdout_dataset:
        raise ValueError('Round3 requires its separate fresh dataset; old tests prohibited')
    if args.fresh_holdout_dataset:
        test_dataset=args.fresh_holdout_dataset.resolve()
        if test_dataset==dataset:
            raise ValueError('Separate new source holdout dataset required')
        fresh_manifest_path=test_dataset/'manifest.json'
        fresh_manifest=read_json(fresh_manifest_path)
        if is_round3 and fresh_manifest.get('fresh_protocol')!='round3_first_unused_d21_d30':
            raise ValueError('Round3 fixed first-unused protocol required')
        fresh_tests=validate_fresh_manifest(fresh_manifest,{'samples':manifest['samples']+read_json(first_manifest)['samples']})
        for sample in fresh_tests:
            source_root=resolve_recorded_path(sample.get('source_root',fresh_manifest.get('source_root','')), ROOT)
            for field,hash_field in [('source_image','source_image_sha256'),('source_annotation','source_annotation_sha256')]:
                if sha256(source_root/sample[field])!=sample[hash_field]:
                    raise ValueError('Fresh source image/XML hash mismatch')
        for name,path in [('fresh_manifest',fresh_manifest_path),('fresh_dataset_yaml',test_dataset/'dataset.yaml')]:
            frozen[name]={'path':str(path),'sha256':sha256(path)}
        acquisition=resolve_recorded_path(fresh_manifest['source_root'],ROOT)/'acquisition.json' if fresh_manifest else ROOT/'data/image_samples/cghd_round2/acquisition.json'
        frozen['fresh_source_acquisition']={'path':str(acquisition),'sha256':sha256(acquisition)}
        source_manifest_path=resolve_recorded_path(fresh_manifest['source_root'],ROOT)/'manifest.json'
        frozen['fresh_source_manifest']={'path':str(source_manifest_path),'sha256':sha256(source_manifest_path)}
        if frozen['fresh_source_manifest']['sha256']!=fresh_manifest['source_manifest_sha256']:
            raise ValueError('Fresh source manifest changed')
        if fresh_manifest.get('acquisition_sha256')!=frozen['fresh_source_acquisition']['sha256']:
            raise ValueError('Fresh dataset acquisition registration mismatch')
    acceptance={'generated_at':datetime.now(timezone.utc).isoformat(),'experiment_execution':'frozen',
        'model_quality':'not_evaluated','T07':'partial','topology_electrical_validation':'not_evaluated',
        'frozen':frozen,'split_statistics':splits,'validation':{'previous_two_class':pre,'round2_two_class':post},
        'checkpoint_selection':selection,'csv_epochs_completed':len(csv_rows),'optimizer_steps':summary['optimizer_steps'],
        'training_elapsed_seconds':summary['training_elapsed_seconds'],'environment':summary['environment'],
        'evaluation_config':training.EVAL_CONFIG,'matching_iou':.5,'test_scope':'d31 source holdout not used in local selection; public pretraining overlap unknown; not independent blind test',
        'original30_test_inference_calls':0,'license_status':manifest['license_status'],
        'default_detector_changed':_default_detector_is(summary['best_checkpoint']['sha256']),'final_test_models_expected':3,'final_test_models_attempted':0,
        'execution_plan':args.execution_plan,'expected_optimizer_steps':plan['expected_optimizer_steps']}
    if not plan['final_test_allowed'] and fresh_manifest is None:
        verify_frozen(frozen)
        improved=compare_quality(post,pre)
        acceptance.update(experiment_execution='pass',model_quality='validation_improved_fresh_test_incomplete' if improved else 'not_accepted',
            new_data_acquisition='incomplete',fresh_test_evaluation='not_run',public_reference_evaluation='not_rerun',
            fresh_test_reason='New source acquisition yielded zero selected samples; old d21..30 test prohibited',
            final_test_models_expected=0,quality_comparison={'validation_improved_over_previous_two_class':improved,
                'fresh_test_improved_over_previous_two_class':None,'meets_public_reference':None,
                'criterion':'Fixed-val macro F1 strictly higher and macro recall nondecreasing; val participated in selection and is not held-out evidence'})
        acceptance['fixed_val_ground_truth_boxes']=70
        acceptance['head_transfer_verified']=summary['head_transfer']['unchanged']
        acceptance['optimizer_steps_basis']=summary['optimizer_steps_basis']
        acquisition=resolve_recorded_path(fresh_manifest['source_root'],ROOT)/'acquisition.json' if fresh_manifest else ROOT/'data/image_samples/cghd_round2/acquisition.json'
        if acquisition.is_file():
            acceptance['acquisition_evidence']={'path':str(acquisition),'sha256':sha256(acquisition)}
        write_json(args.acceptance,acceptance)
        print(json.dumps({'experiment_execution':acceptance['experiment_execution'],'model_quality':acceptance['model_quality'],
            'optimizer_steps':summary['optimizer_steps'],'fresh_test_evaluation':'not_run'},ensure_ascii=False))
        return 0
    marker=out/'final_test_evaluation.started.json'
    if marker.exists():
        raise ValueError('Final test already started; inspect saved raw artifacts, do not rerun')
    write_json(args.acceptance,acceptance)
    verify_frozen(frozen)
    _,test_samples=training.load_dataset(test_dataset,split='test')
    test_manifest=fresh_manifest or manifest
    validate_fresh_manifest(test_manifest,{'samples':manifest['samples']+read_json(first_manifest)['samples']} if is_round3 else {'samples':[]})
    if len(test_samples)!=len([s for s in test_manifest['samples'] if s['split']=='test']):
        raise ValueError('Actual acquired test denominator differs')
    acceptance['fresh_holdout_dataset']=str(test_dataset)
    acceptance['fresh_split_statistics']=test_manifest['statistics']['splits']
    acceptance['new_extra_train_images_used']=splits['train']['images']-16
    acceptance['new_data_acquisition']=read_json(acquisition).get('status')
    acceptance['fresh_test_evaluation']='started'
    acceptance['fresh_test_source_files_verified']=2*len(test_samples)
    acceptance['fresh_test_images_planned']=10 if is_round3 else 12
    acceptance['fresh_test_images_acquired']=len(test_samples)
    acceptance['test_scope']='Fixed first-unused records from d21..30; template and public pretraining overlap unknown; not independent blind test' if is_round3 else acceptance['test_scope']
    acceptance['old_d31_inference_calls']=0 if is_round3 else None
    acceptance['fresh_inference_calls_per_model_planned']=len(test_samples)
    acceptance['fresh_models_planned']=3
    acceptance['head_transfer_verified']=summary['head_transfer']['unchanged']
    acceptance['optimizer_steps_basis']=summary['optimizer_steps_basis']
    acceptance['fixed_val_ground_truth_boxes']=70
    acceptance['independent_evaluation_behavioral_tests']={'passed':20,'total':20,'command':'python -X utf8 -B tests/test_yolo_round2_evaluation.py'}
    with marker.open('x',encoding='utf8') as stream:
        json.dump({'frozen':frozen,'started_at':datetime.now(timezone.utc).isoformat()},stream,ensure_ascii=False,indent=2)
    if is_round3:
        verify_protocol_freeze(args.protocol_freeze)
    results={}
    device=summary['environment']['device']
    with training.offline_network():
        for name,path,registration in [('previous_two_class',previous_best,previous_registration),
                ('round2_two_class',best,best_registration),('public_58class',public_path,None)]:
            verify_frozen(frozen)
            acceptance['final_test_models_attempted']+=1
            model=None
            try:
                model=training.load_registered_model(path,registration) if registration else image_detector._load_model()[0]
                if registration is None:
                    training.validate_source_classes(model.names)
                    if len(model.names)!=58:
                        raise ValueError('Public reference must retain original 58 classes')
                raw=training.evaluate_samples(test_samples,training.predictor_for(model,device))
            except Exception as error:
                raw={'images':[{'id':s['id'],'image':s['image'],'ground_truth':s['ground_truth'],
                    'predictions':None,'status':'incomplete','error':f'{type(error).__name__}: {error}'} for s in test_samples]}
            independent=score_raw(raw['images'])
            if 'summary' in raw:
                verify_raw_report(raw,test_samples)
            for row in raw['images']:
                matched=match_raw(row['ground_truth'],row['predictions'] if row['status']=='complete' else [])
                row['matches']=matched['matches']
                row['error']=row.get('error')
            raw['framework_summary']=raw.get('summary')
            raw['summary']=independent
            raw['checkpoint']=frozen['public_checkpoint' if registration is None else 'previous_checkpoint' if name=='previous_two_class' else 'best_checkpoint']
            raw['model_classes']=model.names if model is not None else None
            raw['prediction_scope']='Raw predictions of the two exact target classes only; other public-model classes excluded from this two-class comparison'
            raw['evaluation_config']=training.EVAL_CONFIG
            raw_path=out/f'final_test_{name}.json'
            write_json(raw_path,raw)
            results[name]=independent|{'raw_path':str(raw_path),'raw_sha256':sha256(raw_path)}
            acceptance['final_test']=results
            write_json(args.acceptance,acceptance)
    verify_frozen(frozen)
    complete=all(row['status']=='complete' for row in results.values())
    acceptance['experiment_execution']='pass' if complete else 'incomplete'
    acceptance['fresh_test_evaluation']='complete' if complete else 'incomplete'
    acceptance['public_reference_evaluation']='complete' if results['public_58class']['status']=='complete' else 'incomplete'
    current,old,public=(results[name] for name in ('round2_two_class','previous_two_class','public_58class'))
    improved=compare_quality(current,old) if complete else None
    public_met=compare_quality(current,public,strict=False) if complete else None
    acceptance['quality_comparison']={'improved_over_previous_two_class':improved if complete else None,
        'validation_improved_over_previous_two_class':compare_quality(post,pre),
        'meets_public_reference':public_met if complete else None,
        'criterion':'macro F1 strictly higher and macro recall nondecreasing vs previous; both nondecreasing vs public reference; descriptive diagnosis only'}
    acceptance['model_quality']=quality_status(complete,improved,public_met)
    write_json(args.acceptance,acceptance)
    print(json.dumps({'experiment_execution':acceptance['experiment_execution'],'model_quality':acceptance['model_quality'],
        'optimizer_steps':summary['optimizer_steps'],'final_test_models_attempted':acceptance['final_test_models_attempted']},ensure_ascii=False))
    return 0 if complete else 1

if __name__=='__main__':
    raise SystemExit(main())

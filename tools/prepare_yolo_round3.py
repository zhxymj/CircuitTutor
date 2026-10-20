"""Fixed, class-agnostic third-round source holdout; reuse verified round2 train/val."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import zipfile
import prepare_yolo_round2 as r2
base=r2.base
ROOT=r2.ROOT
RAW=ROOT/'data/image_samples/cghd_round3'
OUTPUT=ROOT/'data/image_samples/yolo_cghd_round3'
SPLITS={'train':list(range(1,17)),'val':list(range(17,21)),'test':list(range(21,31))}
SELECTION='For each drafter 21..30 choose first globally numbered C ascending absent from all original30 and round2 records, fixed D1/P1; no class or prediction selection. Retain zero-target images; no replacement.'
def select_members(names,used):
 result=[]
 for d in range(21,31):
  candidates=sorted((n for n in names if n.startswith(f'drafter_{d}/annotations/') and re.search(r'_D1_P1\.xml$',n) and r2.identity(n)[1] not in used),key=lambda n:r2.identity(n)[1])
  if not candidates: raise ValueError(f'No unused fixed record for drafter {d}')
  result.append(candidates[0])
 return result

def acquire():
 original=json.loads((r2.ORIGINAL/'manifest.json').read_text(encoding='utf8'))
 prior=json.loads((r2.RAW/'manifest.json').read_text(encoding='utf8'))
 priorlog=json.loads((r2.RAW/'acquisition.json').read_text(encoding='utf8'))
 item=json.loads((r2.ORIGINAL/'zenodo_record.json').read_text(encoding='utf8'))['files'][0]
 if item['key']!='cghd-zenodo-15.zip' or item['checksum']!=original['archive_md5']: raise ValueError('Archive identity mismatch')
 logpath=RAW/'acquisition.json'
 if logpath.exists(): raise ValueError('Acquisition already attempted; preserve evidence; explicit recovery needed')
 used={r2.identity(s['image'])[1] for s in original['samples']+prior['samples']}
 budget=r2.NetworkBudget(200*1024*1024,priorlog['network_requested_bytes'])
 log=dict(status='incomplete',selection=SELECTION,source=original['source'],archive_url=item['links']['self'],archive_bytes=item['size'],archive_md5=item['checksum'],archive_verified=False,network_budget_bytes=budget.maximum,prior_network_requested_bytes=budget.requested_bytes,requests=[],selected=[],failures=[],original_manifest_sha256=r2.sha((r2.ORIGINAL/'manifest.json').read_bytes()),round2_manifest_sha256=r2.sha((r2.RAW/'manifest.json').read_bytes()),round2_acquisition_sha256=r2.sha((r2.RAW/'acquisition.json').read_bytes()))
 r2.write_json(logpath,log)
 remote=r2.BudgetRangeFile(item['links']['self'],item['size'],budget,log['requests'])
 try:
  with zipfile.ZipFile(remote) as archive:
   names=archive.namelist(); lookup={n.lower():n for n in names}
   selected=select_members(names,used)
   log['fixed_members_before_reading_annotations']=selected
   r2.write_json(logpath,log)
   for annotation in selected:
    try:
     d,c,drawing,photo=r2.identity(annotation)
     stem=annotation.replace('/annotations/','/images/')[:-4]
     image=next((lookup[(stem+ext).lower()] for ext in ('.jpg','.jpeg','.png') if (stem+ext).lower() in lookup),None)
     if not image: raise ValueError('Missing source image')
     xml=archive.read(annotation); labels,counts,ignored=base.convert_annotation(xml.decode('utf8'))
     raw=archive.read(image)
     for name,blob in ((annotation,xml),(image,raw)):
      path=RAW/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(blob)
     base.image_geometry(RAW/image,base.annotation_dimensions(xml.decode('utf8')))
     files=[dict(path=n,bytes=len(b),sha256=r2.sha(b),crc32=archive.getinfo(n).CRC) for n,b in ((annotation,xml),(image,raw))]
     sample=dict(id=f'cghd_drafter_{d}_C{c}_D1_P1',drafter=d,split='test',source_split='round3_acquired',source_root=RAW.as_posix(),source_C=c,source_D=drawing,source_photo=photo,image=image,annotation=annotation,files=files)
     log['selected'].append(sample)
     print(f'Acquired {sample["id"]}: {counts}',flush=True)
    except Exception as error:
     log['failures'].append(dict(annotation=annotation,error=f'{type(error).__name__}: {error}'))
    finally:
     log['network_requested_bytes']=budget.requested_bytes; r2.write_json(logpath,log)
  log['status']='complete' if len(log['selected'])==10 else 'incomplete'
 except Exception as error:
  log['error']=f'{type(error).__name__}: {error}'
 finally:
  log['network_requested_bytes']=budget.requested_bytes; r2.write_json(logpath,log)
 manifest={k:prior[k] for k in ('source','creators','license_status','license_declarations','archive_md5','verification')}
 manifest.update(schema_version=1,archive_verified=False,selection=SELECTION,split_drafters=SPLITS,circuit_template_overlap='unknown',pretraining_overlap='unknown',samples=log['selected'],status=log['status'])
 r2.write_json(RAW/'manifest.json',manifest)
 return manifest

def export_dataset():
 source=json.loads((RAW/'manifest.json').read_text(encoding='utf8'))
 oldpath=r2.OUTPUT/'manifest.json'; old=json.loads(oldpath.read_text(encoding='utf8'))
 exported=[]
 for s in old['samples']:
  if s['split']=='test': continue
  for key,hashkey in (('image','image_sha256'),('label','label_sha256')):
   path=r2.OUTPUT/s[key]; base.verify_source_file(path,s[hashkey]); target=OUTPUT/s[key]
   target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(path,target)
  sourcepath=base.resolve_recorded_path(s['source_root'],ROOT)
  for key,hashkey in (('source_image','source_image_sha256'),('source_annotation','source_annotation_sha256')):
   base.verify_source_file(sourcepath/s[key],s[hashkey])
  exported.append(dict(s))
 for s in source['samples']:
  hashes={f['path']:f['sha256'] for f in s['files']}
  for key in ('image','annotation'): base.verify_source_file(RAW/s[key],hashes[s[key]])
  xml=(RAW/s['annotation']).read_text(encoding='utf8'); labels,counts,ignored=base.convert_annotation(xml)
  geo=base.image_geometry(RAW/s['image'],base.annotation_dimensions(xml))
  suffix=Path(s['image']).suffix.lower() if geo['effective_orientation']==1 else '.png'
  ir=Path('images/test')/(s['id']+suffix); lr=Path('labels/test')/(s['id']+'.txt')
  mode=base.export_image(RAW/s['image'],OUTPUT/ir,geo)
  (OUTPUT/lr).parent.mkdir(parents=True,exist_ok=True); (OUTPUT/lr).write_text('\n'.join(labels)+('\n' if labels else ''),encoding='utf8')
  e={k:s[k] for k in ('id','drafter','split','source_root','source_C','source_D','source_photo')}
  e.update(source_split='holdout',source_acquisition_split='round3_acquired',source_image=s['image'],source_annotation=s['annotation'],source_image_sha256=hashes[s['image']],source_annotation_sha256=hashes[s['annotation']],image=ir.as_posix(),label=lr.as_posix(),image_sha256=r2.sha((OUTPUT/ir).read_bytes()),label_sha256=r2.sha((OUTPUT/lr).read_bytes()),image_export_mode=mode,image_geometry=geo,class_counts=counts,ignored_class_counts=ignored,zero_target_labels=not labels)
  exported.append(e)
 for e in exported:
  e.update(source=source['source'],license_status=source['license_status'],license_declarations=source['license_declarations'])
 manifest={k:v for k,v in old.items() if k not in ('samples','statistics','underfilled_drafters')}
 manifest.update(source_root=RAW.as_posix(),source_manifest_sha256=r2.sha((RAW/'manifest.json').read_bytes()),round2_dataset_manifest_sha256=r2.sha(oldpath.read_bytes()),acquisition_sha256=r2.sha((RAW/'acquisition.json').read_bytes()),acquisition_status=source['status'],fresh_protocol='round3_first_unused_d21_d30',split_drafters=SPLITS,split_policy=SELECTION,expected_counts=dict(Counter(s['split'] for s in exported)),planned_counts=dict(train=47,val=4,test=10),source_files_sha256_verified=2*len(exported),samples=exported,statistics={'total':base.summarize(exported),'splits':{sp:base.summarize([s for s in exported if s['split']==sp]) for sp in base.SPLITS}})
 r2.write_json(OUTPUT/'manifest.json',manifest)
 (OUTPUT/'dataset.yaml').write_text(f'path: {json.dumps(OUTPUT.as_posix(),ensure_ascii=False)}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: resistor\n  1: capacitor.unpolarized\n',encoding='utf8')
 (OUTPUT/'README.md').write_text('# CGHD 第三轮来源留出\n\n'+SELECTION+'\n\n训练与验证原样复用 round2 的 47/4 图，新测试仅使用实际取得的21..30新记录；不包含旧测试。模板与公开预训练交叉均 unknown，不能称独立盲测。逐样本源/许可/SHA及实际分母见manifest，累计网络预算/HTTP206/CRC见 ../cghd_round3/acquisition.json。\n',encoding='utf8')
 return manifest
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--export-only',action='store_true');a=p.parse_args()
 if not a.export_only: acquire()
 print(json.dumps(export_dataset()['statistics'],ensure_ascii=False,indent=2))


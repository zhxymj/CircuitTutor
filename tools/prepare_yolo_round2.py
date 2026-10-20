"""Acquire the fixed CGHD v15 round-two subset and export verified YOLO data.

Only existing stdlib/Pillow dependencies; never train or touch first-round data.
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib

sys.dont_write_bytecode = True
import prepare_yolo_dataset as base

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / 'data/image_samples/cghd_v15'
RAW = ROOT / 'data/image_samples/cghd_round2'
OUTPUT = ROOT / 'data/image_samples/yolo_cghd_round2'
EXPECTED = {'train': 48, 'val': 4, 'test': 12}
SPLIT_DRAFTERS = {'train': list(range(1, 17)), 'val': list(range(17, 21)), 'test': [31]}
spec = importlib.util.spec_from_file_location('cghd_range', ORIGINAL.parent / 'acquire_cghd_subset.py')
range_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(range_module)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


class NetworkBudget:
    def __init__(self, maximum, prior=0):
        self.maximum = maximum
        self.requested_bytes = prior

    def reserve(self, size):
        if size < 0 or self.requested_bytes + size > self.maximum:
            raise ValueError('Network budget exceeded; full archive refused')
        self.requested_bytes += size


class BudgetRangeFile(range_module.RangeFile):
    """Reuse original seek/cache contract, count every attempted HTTP range."""
    def __init__(self, url, size, budget, requests):
        super().__init__(url, size)
        self.budget, self.requests = budget, requests

    def read(self, count=-1):
        end = self.size if count < 0 else min(self.size, self.pos + count)
        if end <= self.pos:
            return b''
        if end - self.pos > 12_000_000:
            raise ValueError('Refusing range above 12 MB')
        if self.cache_start <= self.pos and end <= self.cache_start + len(self.cache):
            raw = self.cache[self.pos-self.cache_start:end-self.cache_start]
            self.pos = end
            return raw
        fetched_end = min(self.size, max(end, self.pos + 131072))
        start = self.pos
        for attempt in range(3):
            self.budget.reserve(fetched_end - start)
            evidence = {'start': start, 'end_exclusive': fetched_end, 'attempt': attempt + 1,
                        'requested_bytes': fetched_end - start, 'status': 'attempted'}
            self.requests.append(evidence)
            request = urllib.request.Request(self.url, headers={
                'Range': f'bytes={start}-{fetched_end-1}', 'User-Agent': 'ZhihuiCircuitRound2/1.0'})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    evidence['http_status'] = response.status
                    evidence['content_range'] = response.headers.get('Content-Range')
                    if response.status != 206:
                        raise ValueError('HTTP 206 required; full archive refused')
                    if evidence['content_range'] != f'bytes {start}-{fetched_end-1}/{self.size}':
                        raise ValueError('Content-Range mismatch')
                    raw = response.read(fetched_end-start+1)
                    if len(raw) != fetched_end-start:
                        raise OSError('Truncated or oversized HTTP range')
                    evidence['received_bytes'] = len(raw)
                    evidence['status'] = 'verified'
                break
            except OSError as error:
                evidence['status'], evidence['error'] = 'failed', str(error)
                if attempt == 2:
                    raise
                time.sleep(2)
        self.cache_start, self.cache = start, raw
        self.pos = end
        return raw[:end-start]


def read_member(archive, name):
    # zipfile checks central-directory CRC when reading the complete member.
    return archive.read(name)


def identity(name):
    match = re.fullmatch(r'drafter_(\d+)/(?:images|annotations)/C(\d+)_D(\d+)_P(\d+)\.[^.]+', name)
    if not match:
        raise ValueError(f'Unsupported source identity: {name}')
    return tuple(map(int, match.groups()))


def quota_result(drafter, acquired):
    if drafter == 31:
        if acquired != 12:
            raise ValueError(f'Test drafter31 requires 12, acquired {acquired}')
        return None
    if not 0 <= acquired <= 2:
        raise ValueError('Train addition count outside 0..2')
    return None if acquired == 2 else {'drafter': drafter, 'planned_additions': 2,
                                     'available_additions': acquired, 'shortfall': 2-acquired}


def validate_isolation(samples):
    ids = set()
    owners = {'circuit': {}, 'drawing': {}, 'hash': {}}
    for sample in samples:
        if sample['id'] in ids:
            raise ValueError('Duplicate sample id')
        ids.add(sample['id'])
        split = sample['split']
        if sample['drafter'] not in SPLIT_DRAFTERS.get(split, []):
            raise ValueError('Drafter outside fixed split')
        keys = {'circuit': sample['source_C'], 'drawing': (sample['source_C'], sample['source_D']),
                'hash': sample['source_image_sha256']}
        for kind, key in keys.items():
            if key in owners[kind] and owners[kind][key] != split:
                raise ValueError(f'Cross-split {kind} leakage')
            owners[kind][key] = split


def acquire(source=ORIGINAL, raw_root=RAW, maximum=200*1024*1024):
    source, raw_root = Path(source).resolve(), Path(raw_root).resolve()
    original_raw = (source / 'manifest.json').read_bytes()
    original = json.loads(original_raw)
    record_raw = (source / 'zenodo_record.json').read_bytes()
    record = json.loads(record_raw)
    item = record['files'][0]
    if item['key'] != 'cghd-zenodo-15.zip' or item['checksum'] != original['archive_md5']:
        raise ValueError('Original archive metadata mismatch')
    log_path = raw_root / 'acquisition.json'
    prior_log = json.loads(log_path.read_text(encoding='utf8')) if log_path.exists() else {}
    budget = NetworkBudget(maximum, prior_log.get('network_requested_bytes', 0))
    log = {'status': 'incomplete', 'source': original['source'], 'archive_url': item['links']['self'],
           'archive_bytes': item['size'], 'archive_md5': item['checksum'], 'archive_verified': False,
           'network_budget_bytes': maximum, 'prior_network_requested_bytes': budget.requested_bytes,
           'source_manifest_sha256': sha(original_raw), 'zenodo_record_sha256': sha(record_raw),
           'selection': 'For each train drafter 1..16, at most first two unused global C D1/P1 with unpolarized capacitor; retain and disclose shortfalls; val original; test all drafter31 D1/P1. No predictions consulted.',
           'candidates': [], 'selected': [], 'requests': []}
    log['prior_attempts'] = prior_log.get('prior_attempts', [])
    if prior_log:
        log['prior_attempts'].append({k: v for k, v in prior_log.items() if k != 'prior_attempts'})
    samples = []
    for sample in original['samples']:
        if sample['drafter'] > 20:
            continue
        d, c, drawing, photo = identity(sample['image'])
        samples.append({**sample, 'source_root': source.as_posix(), 'source_split': sample['split'],
                        'split': 'train' if d <= 16 else 'val',
                        'source_C': c, 'source_D': drawing, 'source_photo': photo})
    used = {s['drafter']: s['source_C'] for s in samples}
    prior_candidates = {e['annotation']: e for e in prior_log.get('candidates', [])}
    underfilled = []
    remote = BudgetRangeFile(item['links']['self'], item['size'], budget, log['requests'])
    try:
        with zipfile.ZipFile(remote) as archive:
            names = archive.namelist()
            lookup = {n.lower(): n for n in names}
            log['archive_entries'] = len(names)
            def image_for(annotation):
                stem = annotation.replace('/annotations/', '/images/')[:-4]
                return next((lookup[(stem+ext).lower()] for ext in ('.jpg', '.jpeg', '.png')
                             if (stem+ext).lower() in lookup), None)
            for drafter in [31] + list(range(1, 17)):
                candidates = sorted((n for n in names if n.startswith(f'drafter_{drafter}/annotations/')
                                     and re.search(r'_D1_P1\.xml$', n)), key=lambda n: identity(n)[1])
                selected = 0
                for annotation in candidates:
                    d, c, drawing, photo = identity(annotation)
                    if c == used.get(d):
                        continue
                    image = image_for(annotation)
                    if image is None:
                        raise ValueError(f'Missing image pair: {annotation}')
                    prior = prior_candidates.get(annotation)
                    if prior and not prior['selected'] and prior['annotation_crc32'] == archive.getinfo(annotation).CRC:
                        log['candidates'].append({**prior, 'selection_evidence_reused': True})
                        continue
                    existing_xml = raw_root / annotation
                    if prior and existing_xml.exists():
                        base.verify_source_file(existing_xml, prior['annotation_sha256'])
                        xml_raw = existing_xml.read_bytes()
                        if zlib.crc32(xml_raw) != archive.getinfo(annotation).CRC:
                            raise ValueError(f'Reused XML CRC mismatch: {annotation}')
                    else:
                        xml_raw = read_member(archive, annotation)
                    xml_text = xml_raw.decode('utf8')
                    labels, counts, ignored = base.convert_annotation(xml_text)
                    evidence = {'drafter': d, 'source_C': c, 'source_D': drawing, 'source_photo': photo,
                                'annotation': annotation, 'annotation_sha256': sha(xml_raw),
                                'annotation_bytes': len(xml_raw), 'annotation_crc32': archive.getinfo(annotation).CRC,
                                'class_counts': counts, 'selected': d == 31 or counts['capacitor.unpolarized'] > 0}
                    log['candidates'].append(evidence)
                    if not evidence['selected']:
                        continue
                    image_path = raw_root / image
                    existing = next((e for e in prior_log.get('selected', []) if e['image'] == image), None)
                    if existing and image_path.exists():
                        base.verify_source_file(image_path, existing['files'][1]['sha256'])
                        image_raw = image_path.read_bytes()
                        if zlib.crc32(image_raw) != archive.getinfo(image).CRC:
                            raise ValueError(f'Reused image CRC mismatch: {image}')
                    else:
                        image_raw = read_member(archive, image)
                        image_path.parent.mkdir(parents=True, exist_ok=True)
                        image_path.write_bytes(image_raw)
                    annotation_path = raw_root / annotation
                    annotation_path.parent.mkdir(parents=True, exist_ok=True)
                    annotation_path.write_bytes(xml_raw)
                    base.image_geometry(image_path, base.annotation_dimensions(xml_text))
                    files = [{'path': n, 'bytes': len(b), 'sha256': sha(b), 'crc32': archive.getinfo(n).CRC}
                             for n, b in ((annotation, xml_raw), (image, image_raw))]
                    s = {'id': f'cghd_drafter_{d}_C{c}_D{drawing}_P{photo}', 'drafter': d,
                         'split': 'test' if d == 31 else 'train', 'source_split': 'round2_acquired',
                         'source_root': raw_root.as_posix(), 'source_C': c, 'source_D': drawing,
                         'source_photo': photo, 'image': image, 'annotation': annotation, 'files': files}
                    samples.append(s)
                    log['selected'].append(s)
                    selected += 1
                    log['network_requested_bytes'] = budget.requested_bytes
                    write_json(log_path, log)
                    print(f'Acquired {s["id"]}: R={counts["resistor"]} C={counts["capacitor.unpolarized"]}', flush=True)
                    if d != 31 and selected == 2:
                        break
                shortfall = quota_result(drafter, selected)
                if shortfall:
                    underfilled.append(shortfall)
        actual = dict(Counter(s['split'] for s in samples))
        if actual.get('val') != 4 or actual.get('test') != 12:
            raise ValueError(f'Incomplete validation/test counts: {actual}')
        for s in samples:
            s['source_image_sha256'] = next(f['sha256'] for f in s['files'] if f['path'] == s['image'])
        validate_isolation(samples)
        manifest = {'schema_version': 1, 'source': original['source'], 'creators': original['creators'],
                    'license_status': 'conflicting_declarations_pending_review',
                    'license_declarations': [{'license': original['license'], 'evidence': 'cghd_v15/manifest.json'},
                                             {'license': 'CC-BY-SA-3.0', 'evidence': 'cghd_v15/README.md:326'}],
                    'archive_md5': original['archive_md5'], 'archive_verified': False,
                    'verification': 'HTTP206 and exact Content-Range; complete acquired-member CRC; per-source SHA256. Full archive MD5 not checked.',
                    'original_source_manifest_sha256': sha(original_raw), 'selection': log['selection'],
                    'split_drafters': SPLIT_DRAFTERS, 'expected_counts': actual,
                    'planned_counts': EXPECTED, 'planned_train_additions': 32,
                    'actual_train_additions': actual['train']-16,
                    'underfilled_drafters': underfilled,
                    'circuit_template_overlap': 'unknown', 'pretraining_overlap': 'unknown', 'samples': samples}
        write_json(raw_root / 'manifest.json', manifest)
        log['status'] = 'complete'
        log['actual_counts'] = actual
        log['underfilled_drafters'] = underfilled
        return manifest
    except Exception as error:
        log['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        log['network_requested_bytes'] = budget.requested_bytes
        write_json(log_path, log)


def export_dataset(raw_root=RAW, output=OUTPUT):
    raw_root, output = Path(raw_root).resolve(), Path(output).resolve()
    manifest_path = raw_root / 'manifest.json'
    source = json.loads(manifest_path.read_text(encoding='utf8'))
    if source['expected_counts'].get('val') != 4 or source['expected_counts'].get('test') != 12 or source['split_drafters'] != SPLIT_DRAFTERS:
        raise ValueError('Source fixed split contract mismatch')
    prepared = []
    for s in source['samples']:
        path = base.resolve_recorded_path(s['source_root'], ROOT)
        hashes = {f['path']: f['sha256'] for f in s['files']}
        ih = base.verify_source_file(path / s['image'], hashes[s['image']])
        ah = base.verify_source_file(path / s['annotation'], hashes[s['annotation']])
        if identity(s['image']) != (s['drafter'], s['source_C'], s['source_D'], s['source_photo']):
            raise ValueError('Source identity mismatch')
        xml_text = (path / s['annotation']).read_text(encoding='utf8')
        labels, counts, ignored = base.convert_annotation(xml_text)
        geometry = base.image_geometry(path / s['image'], base.annotation_dimensions(xml_text))
        prepared.append((s, path / s['image'], labels, counts, ignored, geometry, ih, ah))
    validate_isolation([{**s, 'source_image_sha256': ih} for s, _, _, _, _, _, ih, _ in prepared])
    if dict(Counter(s['split'] for s, *_ in prepared)) != source['expected_counts']:
        raise ValueError('Incomplete export counts')
    exported = []
    for s, image, labels, counts, ignored, geometry, ih, ah in prepared:
        suffix = image.suffix.lower() if geometry['effective_orientation'] == 1 else '.png'
        ir = Path('images') / s['split'] / (s['id'] + suffix)
        lr = Path('labels') / s['split'] / (s['id'] + '.txt')
        mode = base.export_image(image, output / ir, geometry)
        (output / lr).parent.mkdir(parents=True, exist_ok=True)
        (output / lr).write_text('\n'.join(labels) + ('\n' if labels else ''), encoding='utf8')
        exported.append({k: s[k] for k in ('id', 'drafter', 'split', 'source_split', 'source_root',
                                         'source_C', 'source_D', 'source_photo')})
        exported[-1]['source_acquisition_split'] = s['source_split']
        exported[-1]['source_split'] = 'holdout' if s['split'] == 'test' else 'development'
        exported[-1].update(source_image=s['image'], source_annotation=s['annotation'],
                            source_image_sha256=ih, source_annotation_sha256=ah,
                            image=ir.as_posix(), label=lr.as_posix(), image_sha256=sha((output/ir).read_bytes()),
                            label_sha256=sha((output/lr).read_bytes()), image_export_mode=mode,
                            image_geometry=geometry, class_counts=counts, ignored_class_counts=ignored,
                            zero_target_labels=not labels)
    result = {'schema_version': 1, 'source': source['source'], 'creators': source['creators'],
              'classes': base.CLASSES, 'source_root': raw_root.as_posix(),
              'source_manifest_sha256': sha(manifest_path.read_bytes()),
              'source_archive_md5': source['archive_md5'], 'source_archive_verified': False,
              'source_verification': source['verification'], 'source_files_sha256_verified': 2*len(exported),
              'license_status': source['license_status'], 'license_declarations': source['license_declarations'],
              'split_drafters': SPLIT_DRAFTERS, 'expected_counts': source['expected_counts'],
              'planned_counts': EXPECTED, 'underfilled_drafters': source['underfilled_drafters'],
              'actual_train_additions': source['actual_train_additions'],
              'acquisition_sha256': sha((raw_root/'acquisition.json').read_bytes()),
              'split_policy': source['selection'], 'circuit_template_overlap': 'unknown', 'pretraining_overlap': 'unknown',
              'bbox_convention': 'XML extents in EXIF-oriented pixels; center and xmax-xmin/ymax-ymin, no +/-1 or box rotation.',
              'statistics': {'total': base.summarize(exported), 'splits': {
                  split: base.summarize([s for s in exported if s['split']==split]) for split in base.SPLITS}},
              'samples': exported}
    write_json(output / 'manifest.json', result)
    (output / 'dataset.yaml').write_text(f'path: {json.dumps(output.as_posix(), ensure_ascii=False)}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: resistor\n  1: capacitor.unpolarized\n', encoding='utf8')
    rows = [f"| {sp} | {st['images']} | {st['drafters']} | {st['boxes_by_class']['resistor']} | {st['boxes_by_class']['capacitor.unpolarized']} | {st['zero_target_images']} |"
            for sp, st in result['statistics']['splits'].items()]
    (output / 'README.md').write_text('# CGHD 第二轮二类训练数据\n\n来源：https://zenodo.org/records/15333233。作者：'+', '.join(source['creators'])+'。\n\n仅resistor与capacitor.unpolarized；选样只看标注，按全球C升序。train保留原16并每人补2图；val原4图；test为drafter31的12个D1/P1。原drafter21–30未参与本轮。\n\n| split | 图 | 绘图者 | R框 | C框 | 零目标图 |\n|---|---:|---:|---:|---:|---:|\n'+'\n'.join(rows)+'\n\n每样本记录源根目录、全球C、绘制D、照片P、原图/XML/导出图/标签SHA256。CRC和HTTP206请求证据见同级cghd_round2/acquisition.json；原数据仅引用，未改写。转正图使用原EXIF helper，XML框不二次旋转。\n\n许可 conflicting_declarations_pending_review：Zenodo/原manifest CC-BY-4.0，上游README326行 CC-BY-SA-3.0，两者保留。全球C是记录流水号；不同人复用电路模板未知，公开预训练重叠未知；test仅本地留出，不能称独立盲测。\n\n复现：`python -B tools/prepare_yolo_round2.py`；只重新导出使用 `--export-only`。使用原有stdlib/Pillow，无新增依赖。无需转置图片可能硬链接，不要编辑训练图片以免改变源图。\n', encoding='utf8')
    readme_path = output / 'README.md'
    text = readme_path.read_text(encoding='utf8').replace('train保留原16并每人补2图', 'train保留原16并每人至多补2图')
    text += '\n计划训练新增32图；实际新增'+str(source['actual_train_additions'])+'图。缺额绘图者及可用数：`'+json.dumps(source['underfilled_drafters'], ensure_ascii=False)+'`。不足不补D2或重复照片；expected_counts记录实际数量。此数据集的新训练图本轮使用0，已完成的50epochs仍只用原16train/4val。\n'
    readme_path.write_text(text, encoding='utf8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--source', type=Path, default=ORIGINAL)
    parser.add_argument('--raw', type=Path, default=RAW)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    if not args.export_only:
        acquire(args.source, args.raw)
    result = export_dataset(args.raw, args.output)
    print(json.dumps(result['statistics'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

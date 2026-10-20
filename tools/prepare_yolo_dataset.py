"""Export the verified 30-image CGHD subset as a two-class YOLO dataset.

Uses the standard library and the existing Pillow installation. Does not download
data, install packages, train a model, or alter the source files.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
CLASSES = {"resistor": 0, "capacitor.unpolarized": 1}
SPLITS = ("train", "val", "test")


def resolve_recorded_path(path, project_root=ROOT):
    """Resolve this project's recorded location after its directory was moved."""
    recorded = Path(path)
    parts = str(path).replace("\\", "/").split("/")
    marker = ["智绘电路-企业命题演示包", "智绘电路"]
    for index in range(len(parts) - 1):
        if parts[index:index + 2] == marker:
            suffix = parts[index + 2:]
            if ".." in suffix:
                raise ValueError("Recorded project path must not traverse its root")
            return Path(project_root).joinpath(*suffix)
    return recorded


def verify_source_file(path, expected_hash):
    actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if actual != expected_hash:
        raise ValueError(f"Source SHA256 mismatch: {path}")
    return actual


def assign_splits(samples):
    if len(samples) != 30:
        raise ValueError("Exactly 30 source samples required")
    if len({s["id"] for s in samples}) != 30:
        raise ValueError("Duplicate sample id")
    if len({s["drafter"] for s in samples}) != 30:
        raise ValueError("Each drafter must occur exactly once; drafter leakage detected")
    development = sorted((s for s in samples if s["split"] == "development"), key=lambda s: s["drafter"])
    holdout = sorted((s for s in samples if s["split"] == "holdout"), key=lambda s: s["drafter"])
    if len(development) != 20 or len(holdout) != 10:
        raise ValueError("Source must contain 20 development and 10 holdout drafters")
    result = []
    for index, sample in enumerate(development + holdout):
        split = "train" if index < 16 else "val" if index < 20 else "test"
        result.append({**sample, "source_split": sample["split"], "split": split})
    return result


def annotation_dimensions(xml_text):
    root = ET.fromstring(xml_text)
    try:
        dimensions = tuple(int(root.findtext("size/" + key)) for key in ("width", "height"))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid XML dimensions") from error
    if min(dimensions) <= 0:
        raise ValueError("Invalid XML dimensions")
    return dimensions


def convert_annotation(xml_text):
    width, height = annotation_dimensions(xml_text)
    root = ET.fromstring(xml_text)
    counts = dict.fromkeys(CLASSES, 0)
    ignored = Counter()
    labels = []
    for obj in root.findall("object"):
        name = obj.findtext("name")
        try:
            xmin, ymin, xmax, ymax = [float(obj.findtext("bndbox/" + key))
                                     for key in ("xmin", "ymin", "xmax", "ymax")]
        except (ValueError, TypeError) as error:
            raise ValueError(f"Invalid bbox for {name}") from error
        if (not all(math.isfinite(v) for v in (xmin, ymin, xmax, ymax)) or
                not (0 <= xmin < xmax <= width and 0 <= ymin < ymax <= height)):
            raise ValueError(f"Invalid bbox for {name}: {(xmin, ymin, xmax, ymax)}, image={(width, height)}")
        if name not in CLASSES:
            ignored[name] += 1
            continue
        counts[name] += 1
        # Same coordinate extents used by tests/evaluate_image_public.js. XML
        # coordinates already refer to EXIF-oriented pixels; never rotate boxes.
        values = ((xmin + xmax) / (2 * width), (ymin + ymax) / (2 * height),
                  (xmax - xmin) / width, (ymax - ymin) / height)
        labels.append(str(CLASSES[name]) + " " + " ".join(f"{v:.10f}" for v in values))
    return labels, counts, dict(sorted(ignored.items()))


def image_geometry(image_path, dimensions):
    with Image.open(image_path) as image:
        raw_width, raw_height = image.size
        orientation = image.getexif().get(274, 1)
        # EXIF 0 means undefined, and Pillow applies no transform. Preserve that
        # raw value as evidence instead of inferring a rotation from the boxes.
        if orientation not in range(9):
            raise ValueError(f"Unsupported EXIF orientation: {orientation} in {image_path}")
        oriented = ImageOps.exif_transpose(image)
        oriented.load()
        if oriented.size != dimensions:
            raise ValueError(f"EXIF-oriented dimensions {oriented.size} do not match XML dimensions {dimensions}: {image_path}")
        width, height = oriented.size
        return {"raw_width": raw_width, "raw_height": raw_height,
                "exif_orientation": orientation, "effective_orientation": orientation or 1,
                "width": width, "height": height,
                "annotation_width": dimensions[0], "annotation_height": dimensions[1]}


def export_image(source, destination, geometry):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if geometry["effective_orientation"] != 1:
        with Image.open(source) as image:
            oriented = ImageOps.exif_transpose(image).convert("RGB")
            # PNG is lossless relative to decoded source JPEG pixels. Clearing
            # metadata prevents a training loader from applying EXIF twice.
            oriented.info.clear()
            oriented.save(destination, format="PNG")
        return "exif_transposed_png"
    if destination.exists():
        verify_source_file(destination, hashlib.sha256(source.read_bytes()).hexdigest())
        return "hardlink" if os.path.samefile(source, destination) else "byte_identical_copy"
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copyfile(source, destination)
        return "byte_identical_copy"


def summarize(samples):
    class_boxes = {name: sum(s["class_counts"][name] for s in samples) for name in CLASSES}
    class_images = {name: sum(s["class_counts"][name] > 0 for s in samples) for name in CLASSES}
    ignored = Counter()
    for sample in samples:
        ignored.update(sample["ignored_class_counts"])
    return {"images": len(samples), "drafters": len({s["drafter"] for s in samples}),
            "zero_target_images": sum(s["zero_target_labels"] for s in samples),
            "boxes_by_class": class_boxes, "images_with_class": class_images,
            "target_boxes": sum(class_boxes.values()), "ignored_boxes": sum(ignored.values()),
            "ignored_boxes_by_class": dict(sorted(ignored.items())),
            "annotation_boxes_total": sum(class_boxes.values()) + sum(ignored.values())}


def write_readme(output, manifest):
    rows = []
    export_modes = Counter(s["image_export_mode"] for s in manifest["samples"])
    for split in SPLITS:
        stats = manifest["statistics"]["splits"][split]
        rows.append(f"| {split} | {stats['images']} | {stats['images_with_class']['resistor']} / {stats['boxes_by_class']['resistor']} | "
                    f"{stats['images_with_class']['capacitor.unpolarized']} / {stats['boxes_by_class']['capacitor.unpolarized']} | "
                    f"{stats['zero_target_images']} | {stats['ignored_boxes']} |")
    total = manifest["statistics"]["total"]
    text = f"""# CGHD v15 二类 YOLO 数据集

来源：{manifest['source']}。作者：{', '.join(manifest['creators'])}。
仅检测 `0=resistor`、`1=capacitor.unpolarized`；其他类别共 {total['ignored_boxes']} 个框（{len(total['ignored_boxes_by_class'])} 类）被忽略。这是二类训练与评测数据，不能据此报告全类电路组件识别能力。

## 固定划分和分母

沿用原始20个 development 绘图者与10个 holdout 绘图者；development按绘图者数值升序，前16位train、后4位val，holdout原样test。未参考模型预测或分数。每个绘图者只有1张照片，绘图者在三个划分间不重合。test仅作训练完成后的留出评估，不能用于选模型、调超参数或提前停止。

| 划分 | 图片/绘图者 | 电阻含类图 / 框 | 无极性电容含类图 / 框 | 零目标图片 | 忽略框 |
| --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

合计 {total['images']} 图、{total['target_boxes']} 个目标框（电阻170、无极性电容75）、{total['annotation_boxes_total']} 个原始标注框。零目标图保留，并生成空label文件。逐例原始框数、各类忽略数、绘图者和源/新划分见 `manifest.json`。

## 标注与图像方向

直接解析Pascal VOC XML。使用原有公开评测的连续坐标范围：中心为`(xmin+xmax)/2`与`(ymin+ymax)/2`，宽高为`xmax-xmin`与`ymax-ymin`，按XML宽高归一化；不虚构减1或加1的像素偏移。所有类别的非法、非有限、负面积与越界框均拒绝，不裁剪、不猜方向。

Pillow读取JPEG原始宽高和EXIF，核对EXIF转正像素的尺寸与XML一致。绘图者3为方向8，原688×1600转正为1600×688，保存无EXIF的PNG；XML框已对应转正像素，无须旋转框。绘图者19、21的EXIF为0（未定义），按Pillow无变换处理，记录原始0与有效1。其余方向1。无需转置的JPG优先硬链接原图，链接受限时使用逐字节相同复制；不要直接编辑训练图片硬链接，以免修改原图。此次实际输出：{export_modes['hardlink']}张硬链接、{export_modes['byte_identical_copy']}张字节相同复制、{export_modes['exif_transposed_png']}张转正PNG，逐例方式见manifest。

## 来源与许可声明

`license_status=conflicting_declarations_pending_review`。现有声明存在冲突：原始导入 `data/image_samples/cghd_v15/manifest.json:license` 声称CC-BY-4.0；原始上游README `data/image_samples/cghd_v15/README.md:325-326` 的Licence节明确声明CC-BY-SA-3.0。现有 `evaluation/image_public_report.md:5,14` 也声称CC-BY-4.0。以上均作为证据保留，没有将其中一种当作已解决的最终许可。作者署名、来源链接和两种声明随本地研究训练数据保留。

源压缩包标称MD5 `{manifest['source_archive_md5']}`；原导入没有验证完整压缩包，`source_archive_verified=false`。此次逐一复验清单中全部60个原图/XML SHA256；输出图与label也各记录SHA256，不能把成员校验描述成完整包校验。

## 复现与验证

从项目根目录执行（使用已有Pillow，不安装依赖）：

```powershell
python -B tools/prepare_yolo_dataset.py
python -B tests/test_yolo_dataset.py
```

`dataset.yaml`由导出器写入当前输出目录的绝对path，便于在Windows路径下直接训练；移动数据目录后重新运行导出命令更新path。目录仅含必要图片、labels、配置、manifest和本说明。导出器完整验证源数据后再写训练文件，不修改原始数据或既有评测。
"""
    (output / "README.md").write_text(text, encoding="utf-8")


def export_dataset(source_root, output_root):
    source_root, output_root = Path(source_root).resolve(), Path(output_root).resolve()
    source_manifest_path = source_root / "manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    assigned = assign_splits(source_manifest["samples"])
    # Validate all sources first: a bad final sample must not yield a partial
    # apparently-ready training dataset.
    prepared = []
    for sample in assigned:
        hashes = {entry["path"]: entry["sha256"] for entry in sample["files"]}
        source_image = source_root / sample["image"]
        annotation = source_root / sample["annotation"]
        image_hash = verify_source_file(source_image, hashes[sample["image"]])
        annotation_hash = verify_source_file(annotation, hashes[sample["annotation"]])
        xml_text = annotation.read_text(encoding="utf-8")
        labels, counts, ignored = convert_annotation(xml_text)
        geometry = image_geometry(source_image, annotation_dimensions(xml_text))
        prepared.append((sample, source_image, labels, counts, ignored, geometry, image_hash, annotation_hash))
    output_root.mkdir(parents=True, exist_ok=True)
    exported = []
    for sample, source_image, labels, counts, ignored, geometry, image_hash, annotation_hash in prepared:
        suffix = source_image.suffix.lower() if geometry["effective_orientation"] == 1 else ".png"
        image_relative = Path("images") / sample["split"] / (sample["id"] + suffix)
        label_relative = Path("labels") / sample["split"] / (sample["id"] + ".txt")
        mode = export_image(source_image, output_root / image_relative, geometry)
        label_path = output_root / label_relative
        label_path.parent.mkdir(parents=True, exist_ok=True)
        label_path.write_text("\n".join(labels) + ("\n" if labels else ""), encoding="utf-8")
        exported.append({"id": sample["id"], "drafter": sample["drafter"],
                         "source_split": sample["source_split"], "split": sample["split"],
                         "source_image": sample["image"], "source_annotation": sample["annotation"],
                         "source_image_sha256": image_hash, "source_annotation_sha256": annotation_hash,
                         "image": image_relative.as_posix(), "label": label_relative.as_posix(),
                         "image_sha256": hashlib.sha256((output_root / image_relative).read_bytes()).hexdigest(),
                         "label_sha256": hashlib.sha256(label_path.read_bytes()).hexdigest(),
                         "image_export_mode": mode, "image_geometry": geometry,
                         "class_counts": counts, "ignored_class_counts": ignored,
                         "zero_target_labels": not labels})
    manifest = {"schema_version": 1, "source": source_manifest["source"],
                "creators": source_manifest["creators"], "classes": CLASSES,
                "source_root": source_root.as_posix(), "source_manifest_sha256": hashlib.sha256(source_manifest_path.read_bytes()).hexdigest(),
                "source_archive_md5": source_manifest["archive_md5"],
                "source_archive_verified": source_manifest["archive_verified"],
                "source_verification": source_manifest["verification"],
                "source_files_sha256_verified": 2 * len(exported),
                "license_status": "conflicting_declarations_pending_review",
                "license_declarations": [
                    {"license": source_manifest["license"], "evidence": "data/image_samples/cghd_v15/manifest.json:license"},
                    {"license": "CC-BY-SA-3.0", "evidence": "data/image_samples/cghd_v15/README.md:325-326"},
                    {"license": "CC-BY-4.0", "evidence": "evaluation/image_public_report.md:5,14"}],
                "split_policy": "Numerically sort the 20 source development drafters; first 16 train, remaining 4 val; preserve all 10 source holdout drafters as test. No model outcomes used.",
                "bbox_convention": "XML extents in EXIF-oriented pixels; normalize centers and xmax-xmin/ymax-ymin exactly as existing public evaluation; no +/-1 shift or box rotation.",
                "statistics": {"total": summarize(exported), "splits": {split: summarize([s for s in exported if s["split"] == split]) for split in SPLITS}},
                "samples": exported}
    (output_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # JSON double-quoted strings are valid YAML strings, including Windows paths.
    yaml = f"path: {json.dumps(output_root.as_posix(), ensure_ascii=False)}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: resistor\n  1: capacitor.unpolarized\n"
    (output_root / "dataset.yaml").write_text(yaml, encoding="utf-8")
    write_readme(output_root, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/image_samples/cghd_v15")
    parser.add_argument("--output", type=Path, default=ROOT / "data/image_samples/yolo_cghd_v15")
    args = parser.parse_args()
    manifest = export_dataset(args.source, args.output)
    print(json.dumps(manifest["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

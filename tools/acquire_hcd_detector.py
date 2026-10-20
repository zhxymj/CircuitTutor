"""Acquire only the YOLO checkpoint from the author's fixed HF revision.

Never imports the unlicensed HCD code. The model card declares Apache-2.0,
while YOLOv10's AGPL derivative obligations remain recorded separately.
This script performs acquisition only, not a license compatibility assertion.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "image_samples"))
from acquire_cghd_subset import RangeFile

REPOSITORY = "why0722/hcd-circuit-weights"
REVISION = "64cb8ab"
FILENAME = "weights_yolo_parseq.zip"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--parallel", action="store_true", help="Resume bounded 4 MiB official range chunks with eight workers; verify entire archive SHA256")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.drive.upper() != "F:":
        raise RuntimeError("Model acquisition output must be on the authorized F drive")
    output.mkdir(parents=True, exist_ok=True)
    api = f"https://huggingface.co/api/models/{REPOSITORY}/revision/{REVISION}?blobs=true"
    with urllib.request.urlopen(api, timeout=40) as response:
        metadata = json.load(response)
    revision = metadata["sha"]
    if metadata.get("cardData", {}).get("license") != "apache-2.0":
        raise RuntimeError("Author license declaration changed; acquisition stopped")
    item = next(i for i in metadata["siblings"] if i["rfilename"] == FILENAME)
    size = item["lfs"]["size"]
    url = f"https://huggingface.co/{REPOSITORY}/resolve/{revision}/{FILENAME}?download=true"
    audit = {"repository": REPOSITORY, "revision": revision, "url": url,
             "archive_size": size, "archive_sha256_expected": item["lfs"]["sha256"],
             "archive_sha256_verified": False, "declared_license": "Apache-2.0",
             "upstream_runtime_license": "AGPL-3.0; https://github.com/THU-MIG/yolov10/blob/main/LICENSE",
             "rights_status": "Author Apache declaration and upstream YOLO derivative obligations unresolved; research evaluation only, no bundled redistribution"}
    (output / "acquisition.json").write_text(json.dumps(audit, indent=2), encoding="utf8")
    source = RangeFile(url, size)
    if args.parallel and not args.list:
        chunks = output / "official-range-chunks"
        chunks.mkdir(exist_ok=True)
        chunk_size = 4 * 1024 * 1024
        def download(index):
            start, end = index * chunk_size, min(size, (index + 1) * chunk_size)
            destination = chunks / f"{index:04d}.part"
            if destination.exists() and destination.stat().st_size == end - start:
                return index
            request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end - 1}"})
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end - 1}/{size}":
                            raise OSError("Official HF range response mismatch")
                        raw = response.read(end - start + 1)
                    if len(raw) != end - start:
                        raise OSError("Truncated official range chunk")
                    destination.write_bytes(raw)
                    print(f"Official range chunk {index + 1}/{(size + chunk_size - 1) // chunk_size}", flush=True)
                    return index
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(2)
        count = (size + chunk_size - 1) // chunk_size
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(download, range(count)))
        full = output / FILENAME
        checksum = hashlib.sha256()
        with full.open("wb") as target:
            for index in range(count):
                raw = (chunks / f"{index:04d}.part").read_bytes()
                checksum.update(raw)
                target.write(raw)
        if checksum.hexdigest() != audit["archive_sha256_expected"]:
            raise RuntimeError("Official archive SHA256 mismatch; model not extracted")
        audit["archive_sha256_verified"] = True
        source = full
    with zipfile.ZipFile(source) as archive:
        audit["members"] = [{"name": i.filename, "size": i.file_size, "compressed_size": i.compress_size,
                             "crc32": f"{i.CRC:08x}"} for i in archive.infolist()]
        print(json.dumps(audit["members"], indent=2), flush=True)
        if not args.list:
            member = next(i for i in archive.infolist() if i.filename.endswith("yolo/best.pt"))
            destination = output / "best.pt"
            partial = output / "best.pt.partial"
            checksum = hashlib.sha256()
            written = 0
            with archive.open(member) as source, partial.open("wb") as target:
                while True:
                    chunk = source.read(131072)
                    if not chunk:
                        break
                    target.write(chunk)
                    checksum.update(chunk)
                    written += len(chunk)
                    if written % (4 * 1024 * 1024) < len(chunk):
                        print(f"Checkpoint extracted {written}/{member.file_size} bytes", flush=True)
            if written != member.file_size:
                raise RuntimeError("Incomplete checkpoint")
            partial.replace(destination)
            audit["checkpoint"] = {"member": member.filename, "bytes": written,
                                   "sha256": checksum.hexdigest(), "zip_member_crc_verified": True}
    (output / "acquisition.json").write_text(json.dumps(audit, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()

"""Local-file CLI. stdout is one JSON object; errors go to stderr/nonzero."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.image_detector import detect_image

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--image-size", type=int, default=1280)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--model", choices=("upstream", "local"), default=None,
                        help="显式选择上游公开权重或本地研究权重；默认 upstream")
    parser.add_argument("--model-root", default=None,
                        help="本地权重目录；仅在 --model local 时使用")
    args = parser.parse_args()
    if args.model:
        os.environ["CIRCUIT_VISION_MODEL"] = args.model
    if args.model_root:
        os.environ["CIRCUIT_VISION_LOCAL_MODEL_ROOT"] = str(Path(args.model_root).expanduser().resolve())
    try:
        result = detect_image(args.input, args.confidence, args.image_size, device=args.device)
        sys.stdout.reconfigure(encoding="utf8")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(f"DETECTOR_UNAVAILABLE: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)

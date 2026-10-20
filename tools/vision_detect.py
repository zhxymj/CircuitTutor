"""Local-file CLI. stdout is one JSON object; errors go to stderr/nonzero."""
import argparse
import json
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
    args = parser.parse_args()
    try:
        result = detect_image(args.input, args.confidence, args.image_size, device=args.device)
        sys.stdout.reconfigure(encoding="utf8")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(f"DETECTOR_UNAVAILABLE: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)

"""Download a large public artifact in bounded ranges; verify separately before use.

Existing destination bytes must be an uninterrupted prefix of the same artifact.
Range parts stay alongside the destination for inspection/retry. No installer runs.
"""
import argparse
import concurrent.futures
import pathlib
import shutil
import subprocess


def download(url, destination, size, workers=6):
    target = pathlib.Path(destination)
    start = target.stat().st_size if target.exists() else 0
    if start > size:
        raise ValueError("destination is larger than the expected artifact")
    if start == size:
        return
    chunk = (size - start + workers - 1) // workers
    ranges = [(offset, min(offset + chunk - 1, size - 1))
              for offset in range(start, size, chunk)]

    def fetch(bounds):
        lo, hi = bounds
        path = target.with_name(target.name + f".range-{lo}-{hi}")
        expected = hi - lo + 1
        if not path.exists() or path.stat().st_size != expected:
            subprocess.run(["curl.exe", "-L", "--fail", "--silent", "--show-error",
                            "--connect-timeout", "20", "--max-time", "900",
                            "--max-filesize", str(expected), "-r", f"{lo}-{hi}",
                            "-o", str(path), url], check=True)
        if path.stat().st_size != expected:
            raise ValueError(f"range {lo}-{hi} has unexpected length")
        print(f"range {lo}-{hi} complete", flush=True)
        return path

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        parts = list(executor.map(fetch, ranges))
    if (target.stat().st_size if target.exists() else 0) != start:
        raise ValueError("destination changed during download")
    with target.open("ab") as output:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, output)
    if target.stat().st_size != size:
        raise ValueError("assembled artifact has unexpected length")
    print(f"download complete: {size} bytes; signature/hash verification required")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("destination")
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.size <= 0 or not 1 <= args.workers <= 8:
        parser.error("size must be positive and workers must be between 1 and 8")
    download(args.url, args.destination, args.size, args.workers)

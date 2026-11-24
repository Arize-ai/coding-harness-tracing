"""Compare built dists with PyPI's files for the same version, for publish.yml.

    python scripts/compare_pypi.py <tag> <dist dir> <upload dir>

Copies files PyPI doesn't have into <upload dir> and writes ``upload`` and ``version`` to
$GITHUB_OUTPUT when set. Exits 1 if a built wheel's SHA-256 differs from PyPI's. Only the
wheel is compared: the sdist's tar and gzip headers carry build-time timestamps.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = "coding-harness-tracing"


def released_files(version: str) -> dict[str, str]:
    """Return PyPI's filename -> SHA-256 for ``version``, or {} if it isn't published."""
    url = f"https://pypi.org/pypi/{PROJECT}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return {f["filename"]: f["digests"]["sha256"] for f in json.load(r)["urls"]}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return {}


def compare(dist: Path, upload: Path, released: dict[str, str]) -> list[str]:
    """Copy files missing from ``released`` into ``upload``; return wheels whose hash differs."""
    upload.mkdir()
    mismatched = []
    for f in sorted(dist.iterdir()):
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        theirs = released.get(f.name)
        print(f"{f.name}\n  built: {digest}\n  PyPI:  {theirs or 'not uploaded'}")
        if theirs is None:
            shutil.copy2(f, upload)
        elif f.suffix == ".whl" and theirs != digest:
            mismatched.append(f.name)
    return mismatched


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    tag, dist, upload = argv[0], Path(argv[1]), Path(argv[2])
    version = tag.removeprefix("v")
    mismatched = compare(dist, upload, released_files(version))
    if mismatched:
        print(f"::error::{', '.join(mismatched)} differs from the file on PyPI")
        return 1

    todo = sorted(p.name for p in upload.iterdir())
    print(f"to upload: {', '.join(todo) or 'nothing, all files are on PyPI'}")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"upload={'true' if todo else 'false'}\nversion={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

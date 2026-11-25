"""scripts/compare_pypi.py, with PyPI mocked."""

from __future__ import annotations

import hashlib
import importlib.util

from tests._pyproject import REPO_ROOT

_spec = importlib.util.spec_from_file_location("compare_pypi", REPO_ROOT / "scripts" / "compare_pypi.py")
compare_pypi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare_pypi)

WHEEL = "coding_harness_tracing-1.0-py3-none-any.whl"
SDIST = "coding_harness_tracing-1.0.tar.gz"


def test_wheel_hash_mismatch_fails_and_uploads_nothing(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / WHEEL).write_bytes(b"rebuilt wheel")
    (dist / SDIST).write_bytes(b"rebuilt sdist")
    published = {WHEEL: hashlib.sha256(b"published wheel").hexdigest(), SDIST: "0" * 64}
    monkeypatch.setattr(compare_pypi, "released_files", lambda version: published)
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    assert compare_pypi.main(["v1.0", str(dist), str(tmp_path / "upload")]) == 1
    assert list((tmp_path / "upload").iterdir()) == []
    assert not out.exists()

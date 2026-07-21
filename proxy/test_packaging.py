"""The daemon must boot from ONLY the files the Dockerfile copies.

PR #98 moved the bundle schema into verify/ and the daemon began importing it, but the image
copied proxy/ alone -- `python -m proxy.main` died with ModuleNotFoundError at boot. Every test
passed, because tests run from the repo root where verify/ is importable: the one place the
failure cannot appear. This reproduces the image's filesystem instead of trusting it.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _copied_paths() -> list[str]:
    """Paths the Dockerfile COPYs into the image (source side of each COPY)."""
    out = []
    for line in (REPO / "Dockerfile").read_text().splitlines():
        m = re.match(r"\s*COPY\s+(.+)$", line)
        if not m:
            continue
        parts = m.group(1).split()
        out.extend(parts[:-1])  # last token is the destination
    return out


def test_daemon_imports_with_only_dockerfile_copied_files(tmp_path):
    for src in _copied_paths():
        s = REPO / src
        d = tmp_path / src
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(s, d) if s.is_dir() else shutil.copy2(s, d)

    # -E drops PYTHONPATH so the repo root cannot leak in. Not -I/-P (they strip cwd, which the
    # container supplies via WORKDIR /app) and not -s (it hides site-packages like aiohttp, which
    # the image pip-installs).
    r = subprocess.run(
        [sys.executable, "-E", "-c", "import proxy.ingress, proxy.evidence, proxy.main"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        "the daemon cannot import from the files the Dockerfile ships:\n" + r.stderr
    )


def test_schema_is_importable_without_the_verifier(tmp_path):
    """verify.bundle must not drag in facts: the attested daemon takes the schema, not the verifier."""
    shutil.copytree(REPO / "verify", tmp_path / "verify")
    (tmp_path / "verify" / "facts.py").unlink()  # simulate the image, which never ships it
    r = subprocess.run(
        [sys.executable, "-E", "-c", "import verify.bundle; print(verify.bundle.SCHEMA_VERSION)"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert r.returncode == 0, "verify.bundle must import without facts.py present:\n" + r.stderr

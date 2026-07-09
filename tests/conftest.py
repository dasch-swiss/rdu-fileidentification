"""Shared pytest fixtures and helpers.

Tests are split in two groups:

* plain unit tests — pure logic, no external binaries, fast. They run everywhere.
* tests marked ``@pytest.mark.docker`` — drive the real fileidentification Docker
  image and CLI (pygfried / ffmpeg / imagemagick / soffice) against the files in
  ``testdata/``. Run only the fast ones with ``pytest -m "not docker"``.
"""

import shutil
from pathlib import Path

import pytest

from fileidentification.definitions.models import SfInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA = REPO_ROOT / "testdata"


@pytest.fixture
def testdata_dir() -> Path:
    """Absolute path to the repo's testdata directory (read-only — never mutate it)."""
    return TESTDATA


@pytest.fixture
def sample_files(tmp_path: Path) -> Path:
    """A fresh, writable copy of a small subset of testdata for destructive tests.

    Returns the directory holding the copies. Each test gets its own tmp dir, so
    files may be renamed/removed/converted without touching the originals.
    """
    dst = tmp_path / "sample"
    dst.mkdir()
    for name in ("SampleJPGImage.jpg", "file-sample_100kB.pdf", "corrupt.mp4"):
        src = TESTDATA / name
        if src.is_file():
            shutil.copy2(src, dst / name)
    return dst


def make_sfinfo(
    filename: str | Path = "file.jpg",
    *,
    puid: str = "fmt/43",
    mime: str = "image/jpeg",
    filesize: int = 100,
    errors: str = "",
    md5: str = "0" * 32,
    warning: str = "",
) -> SfInfo:
    """Build an SfInfo without touching the filesystem.

    ``md5`` is supplied so ``model_post_init`` does not try to hash a real file.
    """
    return SfInfo(
        filename=Path(filename),
        filesize=filesize,
        modified="2024-01-01T00:00:00+00:00",
        errors=errors,
        md5=md5,
        matches=[{"id": puid, "mime": mime, "warning": warning}],
    )

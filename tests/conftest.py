"""Shared pytest fixtures and helpers.

Tests are split in two groups:

* plain unit tests — pure logic, no external binaries, fast. They run everywhere.
* tests marked ``@pytest.mark.docker`` — drive the real fileidentification Docker
  image and CLI (pygfried / ffmpeg / imagemagick / soffice) against the files in
  ``testdata/``. Run only the fast ones with ``pytest -m "not docker"``.
"""

from pathlib import Path

from fileidentification.definitions.models import SfInfo


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

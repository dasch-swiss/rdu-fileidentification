"""End-to-end tests driving the real FileHandler against files from testdata/.

These need the external tooling (pygfried, ffmpeg/ffprobe, imagemagick) on PATH and
are marked ``e2e``. Run only the fast suite with ``pytest -m "not e2e"``.

Each test copies the file(s) it needs into a tmp directory so the originals in
testdata/ are never modified.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from fileidentification.filehandling import FileHandler

pytestmark = pytest.mark.e2e

TESTDATA = Path(__file__).resolve().parent.parent / "testdata"


def _which(*bins: str) -> bool:
    return all(shutil.which(b) for b in bins)


def _stage(tmp_path: Path, *names: str) -> Path:
    """Copy named testdata files into a fresh working dir and return it."""
    work = tmp_path / "work"
    work.mkdir()
    for name in names:
        shutil.copy2(TESTDATA / name, work / name)
    return work


def _read_log(work: Path) -> dict[str, Any]:
    log = work / "__fileidentification" / "_log.json"
    assert log.is_file(), "expected _log.json to be written"
    data: dict[str, Any] = json.loads(log.read_text())
    return data


@pytest.mark.skipif(not _which("sf"), reason="pygfried/siegfried not available")
def test_identify_only_writes_policies_and_log(tmp_path: Path) -> None:
    work = _stage(tmp_path, "SampleJPGImage.jpg")
    fh = FileHandler()
    fh.run(
        root_folder=work,
        assert_integrity=False,
        apply=False,
        remove_tmp=False,
        mode_quiet=True,
        mode_verbose=False,
    )
    assert (work / "__fileidentification" / "_policies.json").is_file()
    log = _read_log(work)
    puids = {f["processed_as"] for f in log["files"]}
    assert "fmt/43" in puids


@pytest.mark.skipif(not _which("ffprobe"), reason="ffmpeg/ffprobe not available")
def test_assert_integrity_removes_corrupt_file(tmp_path: Path) -> None:
    work = _stage(tmp_path, "corrupt.mp4")
    fh = FileHandler()
    fh.run(
        root_folder=work,
        assert_integrity=True,
        apply=False,
        remove_tmp=False,
        mode_quiet=True,
        mode_verbose=False,
    )
    # the corrupt file is moved out of the working dir into the _REMOVED bucket
    assert not (work / "corrupt.mp4").exists()
    removed = list((work / "__fileidentification").rglob("_REMOVED/**/corrupt.mp4"))
    assert removed, "corrupt.mp4 should have been quarantined in _REMOVED"


@pytest.mark.skipif(not _which("magick"), reason="imagemagick not available")
def test_assert_integrity_quarantines_truncated_jpeg(tmp_path: Path) -> None:
    """A JPEG with a 'premature end of data segment' is unrecoverable.

    imagemagick flags it as an error during the integrity check, so it must be
    moved out of the working dir into _REMOVED rather than kept or converted.
    """
    name = "47fdDI7XARj-dD5pt3RUc2e.jpg"
    work = _stage(tmp_path, name)
    fh = FileHandler()
    fh.run(
        root_folder=work,
        assert_integrity=True,
        apply=False,
        remove_tmp=False,
        mode_quiet=True,
        mode_verbose=True,  # magick reports the truncation as a warning -> error
    )

    assert not (work / name).exists()
    assert list((work / "__fileidentification").rglob(f"_REMOVED/**/{name}")), (
        "the truncated jpeg should have been quarantined in _REMOVED"
    )
    log = _read_log(work)
    quarantined = next(f for f in log["files"] if f["filename"] == name)
    assert quarantined["status"].get("removed") is True
    assert any("premature end of data" in w["msg"] for w in quarantined.get("warnings", []))


@pytest.mark.skipif(not _which("magick"), reason="imagemagick not available")
def test_convert_jpeg_to_tiff(tmp_path: Path) -> None:
    work = _stage(tmp_path, "SampleJPGImage.jpg")
    fh = FileHandler()
    fh.run(
        root_folder=work,
        assert_integrity=False,
        apply=True,
        remove_tmp=True,
        mode_quiet=True,
        mode_verbose=False,
    )
    # default policy converts fmt/43 (jpeg) -> tif and moves it next to the original
    tiffs = list(work.glob("*.tif"))
    assert tiffs, "expected a converted .tif next to the original"
    log = _read_log(work)
    assert any(f.get("derived_from") for f in log["files"]), "log should record a derived file"


@pytest.mark.skipif(not _which("ffmpeg", "ffprobe"), reason="ffmpeg/ffprobe not available")
def test_non_intra_slice_mp4_is_reencoded(tmp_path: Path) -> None:
    """A non-intra slice in an IDR NAL unit is a fixable defect.

    `-i` (verbose, without `-a`) must flag the file for re-encoding, silently
    re-encode it, replace the original with the fixed file, and quarantine the
    original in _REMOVED.
    """
    work = _stage(tmp_path, "non-intra slice.mp4")
    fh = FileHandler()
    fh.run(
        root_folder=work,
        assert_integrity=True,
        apply=False,
        remove_tmp=True,
        mode_quiet=True,
        mode_verbose=True,  # the re-encode warning only surfaces in verbose ffmpeg output
    )

    log = _read_log(work)
    flagged = [f for f in log["files"] if any("reencoding" in m["msg"] for m in f.get("processing_logs", []))]
    assert flagged, "the file should have been flagged for re-encoding"

    # the original is quarantined and a re-encoded replacement takes its place on disk
    original = next(f for f in log["files"] if f["status"].get("removed"))
    replacement = next(f for f in log["files"] if f["status"].get("added"))
    assert original["filename"] == "non-intra slice.mp4"
    assert replacement.get("derived_from"), "the replacement must record what it was derived from"
    assert replacement["processed_as"] == "fmt/199"  # re-encoded back to a valid mp4
    assert (work / "non-intra slice.mp4").is_file(), "the fixed file replaces the original in place"
    assert list((work / "__fileidentification").rglob("_REMOVED/**/non-intra slice.mp4")), (
        "the defective original should be quarantined in _REMOVED"
    )

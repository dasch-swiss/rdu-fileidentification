"""End-to-end tests that drive the shipped Docker image and CLI (ENTRYPOINT identify.py), exercising the real
delivery artifact with its bundled ffmpeg / imagemagick / libreoffice.

Marked ``docker`` (run the fast suite with ``pytest -m "not docker"``). Skipped when Docker is unavailable; the
image is built once per session if missing (FIDR_NO_BUILD=1 requires a pre-built one, FIDR_IMAGE picks a tag).
Each test bind-mounts a tmp dir; the container runs as root, so ownership is reclaimed afterwards for cleanup.
"""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from fileidentification.definitions.settings import ErrMsgIM

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA = REPO_ROOT / "testdata"
IMAGE = os.environ.get("FIDR_IMAGE", "fileidentification")

# Corrupt image fixtures + the compiled ErrMsgIM patterns, used by the
# ImageMagick output-drift canary below. The fixture list is read from disk at
# collection time so dropping a file in testdata/corrupt/ auto-adds a case.
CORRUPT_DIR = TESTDATA / "corrupt"
CORRUPT_FIXTURES = (
    sorted(p.name for p in CORRUPT_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    if CORRUPT_DIR.is_dir()
    else []
)
_CORRUPT_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in ErrMsgIM]


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def _reclaim_ownership(image: str, path: Path) -> None:
    """Reclaim the bind-mounted tree for the current user via a throwaway chown container.

    On Linux the container writes files as root; without this the host user
    cannot delete them and pytest's tmp cleanup fails. Reuses the already-built
    image (no extra pull). No-op on platforms without POSIX uids.
    """
    if not hasattr(os, "getuid"):
        return
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "chown",
            "-v",
            f"{path}:{path}",
            image,
            "-R",
            f"{os.getuid()}:{os.getgid()}",
            str(path),
        ],
        capture_output=True,
        check=False,
    )


def run_cli(image: str, work: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI inside the container against the mounted work dir."""
    cmd = ["docker", "run", "--rm", "-v", f"{work}:{work}", image, *flags, str(work)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def run_identify(image: str, target: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the container's `identify` binary directly to test the shipped ImageMagick's raw output"""
    work = target.parent
    cmd = ["docker", "run", "--rm", "--entrypoint", "identify", "-v", f"{work}:{work}", image, *flags, str(target)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _read_log(work: Path) -> dict[str, Any]:
    log = work / "__fileidentification" / "_log.json"
    assert log.is_file(), "expected _log.json to be written"
    data: dict[str, Any] = json.loads(log.read_text())
    return data


def _puids(log: dict[str, Any]) -> set[str]:
    return {f["processed_as"] for f in log["files"]}


@pytest.fixture(scope="session")
def fidr_image() -> str:
    """Ensure the fileidentification image exists, building it once if needed."""
    if not _docker_ready():
        pytest.skip("docker is not available")
    present = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False).returncode == 0
    if not present:
        if os.environ.get("FIDR_NO_BUILD"):
            pytest.skip(f"docker image '{IMAGE}' not found and FIDR_NO_BUILD is set")
        build = subprocess.run(
            ["docker", "build", "-t", IMAGE, str(REPO_ROOT)], capture_output=True, text=True, check=False
        )
        if build.returncode != 0:
            pytest.skip(f"failed to build docker image '{IMAGE}':\n{build.stderr[-2000:]}")
    return IMAGE


@pytest.fixture
def stage(tmp_path: Path, fidr_image: str) -> Iterator[Callable[..., Path]]:
    """Return a helper that copies testdata files into a mounted work dir.

    Each name is copied by its basename into ``work``, so testdata files living
    in subfolders can be placed flat. Always returns the same ``work`` dir.
    """
    work = tmp_path / "work"

    def _stage(*names: str) -> Path:
        work.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy2(TESTDATA / name, work / Path(name).name)
        return work

    yield _stage

    _reclaim_ownership(fidr_image, work)


def test_identify_writes_policies_and_log(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr <dir>` identifies files and writes the policies + log artifacts."""
    work = stage("SampleJPGImage.jpg")
    proc = run_cli(fidr_image, work)
    assert proc.returncode == 0, proc.stderr
    assert (work / "__fileidentification" / "_policies.json").is_file()
    assert "fmt/43" in _puids(_read_log(work))


def test_assert_integrity_removes_corrupt_file(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -i <dir>` quarantines a corrupt mp4 into _REMOVED."""
    work = stage("corrupt.mp4")
    proc = run_cli(fidr_image, work, "-i")
    assert proc.returncode == 0, proc.stderr
    assert not (work / "corrupt.mp4").exists()
    assert list((work / "__fileidentification").rglob("_REMOVED/**/corrupt.mp4"))


def test_assert_integrity_quarantines_truncated_jpeg(stage: Callable[..., Path], fidr_image: str) -> None:
    """A JPEG with a 'premature end of data segment' is flagged corrupt and removed."""
    name = "47fdDI7XARj-dD5pt3RUc2e.jpg"
    work = stage(name)
    proc = run_cli(fidr_image, work, "-i", "-v")
    assert proc.returncode == 0, proc.stderr
    assert not (work / name).exists()
    assert list((work / "__fileidentification").rglob(f"_REMOVED/**/{name}"))
    quarantined = next(f for f in _read_log(work)["files"] if f["filename"] == name)
    assert quarantined["status"].get("removed") is True
    assert any("premature end of data" in w["msg"] for w in quarantined.get("processing_logs", []))


def test_convert_jpeg_to_tiff(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -a -r <dir>` converts a jpeg to tiff and moves it next to the original."""
    work = stage("SampleJPGImage.jpg")
    proc = run_cli(fidr_image, work, "-a", "-r")
    assert proc.returncode == 0, proc.stderr
    assert list(work.glob("*.tif")), "expected a converted .tif next to the original"
    assert any(f.get("derived_from") for f in _read_log(work)["files"])


def test_non_intra_slice_mp4_is_reencoded(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -i -v <dir>` silently re-encodes a fixable non-intra-slice mp4 in place."""
    name = "non-intra slice.mp4"
    work = stage(name)
    proc = run_cli(fidr_image, work, "-i", "-v")
    assert proc.returncode == 0, proc.stderr

    log = _read_log(work)
    flagged = [f for f in log["files"] if any("reencoding" in m["msg"] for m in f.get("processing_logs", []))]
    assert flagged, "the file should have been flagged for re-encoding"

    original = next(f for f in log["files"] if f["status"].get("removed"))
    replacement = next(f for f in log["files"] if f["status"].get("added"))
    assert original["filename"] == name
    assert replacement.get("derived_from")
    assert replacement["processed_as"] == "fmt/199"
    assert (work / name).is_file(), "the fixed file replaces the original in place"
    assert list((work / "__fileidentification").rglob(f"_REMOVED/**/{name}"))


def test_soffice_converts_doc_to_docx(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -a -r` runs the LibreOffice path: a legacy .doc is converted to docx.

    Exercises the soffice branch of the converter and proves libreoffice-nogui
    works inside the image (no ffmpeg/imagemagick test touches this binary).
    """
    work = stage("file-sample_100kB.docx")  # actually fmt/40 (legacy MS Word .doc)
    proc = run_cli(fidr_image, work, "-a", "-r")
    assert proc.returncode == 0, proc.stderr

    derived = [f for f in _read_log(work)["files"] if f.get("derived_from")]
    assert derived, "expected a soffice-converted file"
    assert derived[0]["processed_as"] == "fmt/412"  # OOXML Word document
    assert derived[0]["status"].get("added") is True
    # the original and the converted docx (md5-suffixed to avoid a name clash) coexist
    assert len(list(work.glob("*.docx"))) == 2


def test_extension_mismatch_is_autorenamed(stage: Callable[..., Path], fidr_image: str) -> None:
    """A file whose format has a single known extension is renamed to match it."""
    work = stage("nested folder/testavi")  # fmt/5 (AVI), no extension on disk
    proc = run_cli(fidr_image, work, "-i")
    assert proc.returncode == 0, proc.stderr
    assert (work / "testavi.avi").is_file(), "should have been renamed to .avi"
    assert not (work / "testavi").exists()
    renamed = next(f for f in _read_log(work)["files"] if f["processed_as"] == "fmt/5")
    assert any("did rename" in m["msg"] for m in renamed["processing_logs"])


def test_ffmpeg_converts_mkv_to_mp4(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -a -r` runs the ffmpeg path: an mkv without ffv1 video is re-encoded to mp4.

    Complements the magick (jpg->tiff) and soffice (doc->docx) conversion tests — this is
    the only e2e that drives a real ffmpeg *policy* conversion (fmt/569 -> fmt/199).
    """
    work = stage("test_hevc.mkv")  # fmt/569 with hevc video, not the ffv1 archival standard
    proc = run_cli(fidr_image, work, "-a", "-r")
    assert proc.returncode == 0, proc.stderr

    assert (work / "test_hevc.mp4").is_file(), "expected the ffmpeg-converted .mp4 next to the original"
    derived = [f for f in _read_log(work)["files"] if f.get("derived_from")]
    assert derived, "expected an ffmpeg-converted file"
    assert derived[0]["processed_as"] == "fmt/199"  # MPEG-4
    assert derived[0]["status"].get("added") is True
    assert (work / "test_hevc.mkv").is_file()  # original kept (remove_original not set)


def test_strict_mode_removes_unlisted_format(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -a -s -p <policies>`: a file whose PUID is absent from the policies is quarantined."""
    work = stage("SampleJPGImage.jpg")  # fmt/43
    policies = work / "empty_policies.json"
    policies.write_text(json.dumps({"policies": {}}))
    proc = run_cli(fidr_image, work, "-a", "-s", "-p", str(policies))
    assert proc.returncode == 0, proc.stderr

    assert not (work / "SampleJPGImage.jpg").exists()
    assert list((work / "__fileidentification").rglob("_REMOVED/**/SampleJPGImage.jpg"))
    flagged = next(f for f in _read_log(work)["files"] if f["filename"] == "SampleJPGImage.jpg")
    assert flagged["status"].get("removed") is True
    assert any("not in policies" in m["msg"] for m in flagged["processing_logs"])


def test_readable_file_with_warnings_is_kept(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -i -v`: a TIFF with a benign imagemagick warning is recorded but not quarantined."""
    name = "warn_wrong_data_type_tag.tiff"
    work = stage(name)
    proc = run_cli(fidr_image, work, "-i", "-v")
    assert proc.returncode == 0, proc.stderr

    assert (work / name).is_file(), "a readable-with-warnings file must not be removed"
    assert not list((work / "__fileidentification").rglob("_REMOVED/**/*"))
    rec = next(f for f in _read_log(work)["files"] if f["filename"] == name)
    assert not rec["status"].get("removed")
    assert any(log["name"] == "magick" for log in rec.get("processing_logs", [])), (
        "the imagemagick warning should be recorded on the file"
    )


def test_fidr_wrapper_script(stage: Callable[..., Path], fidr_image: str) -> None:
    """The fidr.sh wrapper resolves paths, mounts the dir and runs the image."""
    if IMAGE != "fileidentification":
        pytest.skip("fidr.sh hardcodes the 'fileidentification' image tag")
    if not shutil.which("bash"):
        pytest.skip("bash not available")

    work = stage("SampleJPGImage.jpg")
    # FIDR_NO_TTY drops `docker run -t` so the wrapper works headless (CI has no TTY)
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "fidr.sh"), str(work)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FIDR_NO_TTY": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "fmt/43" in _puids(_read_log(work))


def test_fidr_single_file_input(stage: Callable[..., Path], fidr_image: str) -> None:
    """fidr.sh accepts a single file: it mounts the parent and the tmp dir is the file stem."""
    if IMAGE != "fileidentification":
        pytest.skip("fidr.sh hardcodes the 'fileidentification' image tag")
    if not shutil.which("bash"):
        pytest.skip("bash not available")

    work = stage("SampleJPGImage.jpg")
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "fidr.sh"), str(work / "SampleJPGImage.jpg")],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FIDR_NO_TTY": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    # for a file input the tmp dir is named after the file stem, not __fileidentification
    log = json.loads((work / "SampleJPGImage" / "_log.json").read_text())
    assert "fmt/43" in _puids(log)


def test_rerun_reuses_log_and_does_not_duplicate(stage: Callable[..., Path], fidr_image: str) -> None:
    """A second `-i` run reloads _log.json and skips files already probed — no re-probe, no duplicated logs.

    Exercises the cross-invocation behaviour that in-process tests can only mock: status.probed persisted in
    _log.json makes assert_integrity skip the file on the rerun, so its recorded warning is not appended twice.
    """
    name = "warn_wrong_data_type_tag.tiff"  # readable, kept, with a benign imagemagick warning
    work = stage(name)

    first = run_cli(fidr_image, work, "-i", "-v")
    assert first.returncode == 0, first.stderr
    rec1 = next(f for f in _read_log(work)["files"] if f["filename"] == name)
    assert rec1["status"].get("probed") is True  # marked, and persisted to _log.json
    warns1 = [log for log in rec1["processing_logs"] if log["name"] == "magick"]
    assert warns1, "the imagemagick warning should be recorded on the first run"

    # second run: the container (root) reads the root-written _log.json and reloads instead of rescanning
    second = run_cli(fidr_image, work, "-i", "-v")
    assert second.returncode == 0, second.stderr
    rec2 = next(f for f in _read_log(work)["files"] if f["filename"] == name)
    warns2 = [log for log in rec2["processing_logs"] if log["name"] == "magick"]
    assert len(warns2) == len(warns1), "the rerun must not re-probe and duplicate the warning"


def test_apply_twice_does_not_reconvert(stage: Callable[..., Path], fidr_image: str) -> None:
    """A second `-a` run reloads _log.json and skips files already applied — no re-evaluation, no second conversion.

    Without status.applied, the rerun would re-flag the original as pending and convert it again; the flag makes
    apply_policies skip it, so exactly one converted file exists after both runs.
    """
    work = stage("SampleJPGImage.jpg")  # fmt/43, converted to tif by the default policy

    first = run_cli(fidr_image, work, "-a")  # convert, but no -r so nothing is moved out of the working dir
    assert first.returncode == 0, first.stderr
    files1 = _read_log(work)["files"]
    original = next(f for f in files1 if f["filename"] == "SampleJPGImage.jpg")
    assert original["status"].get("applied") is True  # marked, and persisted to _log.json
    assert len([f for f in files1 if f.get("derived_from")]) == 1  # converted exactly once

    second = run_cli(fidr_image, work, "-a")
    assert second.returncode == 0, second.stderr
    files2 = _read_log(work)["files"]
    assert len([f for f in files2 if f.get("derived_from")]) == 1  # still one -> the rerun did not re-convert


def test_inspect_mode_reports_corruption_without_removing(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr --inspect` reports a corrupt file but — unlike `-i` — never removes or modifies it (read-only)."""
    name = "corrupt.mp4"  # `-i` quarantines this; --inspect must only report it
    work = stage(name)
    proc = run_cli(fidr_image, work, "--inspect")
    assert proc.returncode == 0, proc.stderr

    assert (work / name).is_file()  # read-only: the corrupt file is NOT removed
    assert not list((work / "__fileidentification").rglob("_REMOVED/**/*")), "nothing quarantined"
    fid = work / "__fileidentification"
    reports = list(fid.glob("*_report.json"))
    assert len(reports) == 1, "a dated report should be written"
    assert not (fid / "_policies.json").exists(), "inspect removes the policies file so the report is standalone"
    assert (fid / "_log.json").is_file()  # the bare inventory is persisted so a later run skips the rescan
    # the corruption is still detected and recorded (error-level) in the report — just not acted on
    rec = next(f for f in json.loads(reports[0].read_text())["files"] if f["filename"] == name)
    assert any(log.get("level") == "error" for log in rec.get("processing_logs", []))


def _diagnose_not_quarantined(image: str, work: Path, name: str, rec: dict[str, Any] | None) -> str:
    """
    Only called on failure of test_corrupt_folder_is_quarantined.
    Runs the container's `identify` directly on the file
    and reports which ErrMsgIM patterns matched its stderr
    """
    removed = bool(rec and rec["status"].get("removed"))
    errlog = bool(rec and any(m.get("level") == "error" for m in rec.get("processing_logs", [])))
    lines = [f"{name}: removed={removed} error_logged={errlog} in_log={rec is not None}"]
    found = next(iter(work.rglob(name)), None)  # still in place, or moved under _REMOVED/
    if found is None:
        lines.append("    (file not found on disk)")
        return "\n".join(lines)
    res = run_identify(image, found, "-verbose", "-regard-warnings")
    matched = [p.pattern for p in _CORRUPT_PATTERNS if p.search(res.stderr)]
    lines.append(f"    identify stderr: {res.stderr.strip()!r}")
    lines.append(f"    ErrMsgIM matched: {matched or 'NONE — ImageMagick output changed, update ErrMsgIM'}")
    return "\n".join(lines)


def test_corrupt_folder_is_quarantined(stage: Callable[..., Path], fidr_image: str) -> None:
    """`fidr -i -v` on the corrupt fixtures quarantines every corrupt image."""

    good = "SampleJPGImage.jpg"  # negative control: a valid image must survive
    work = stage(good, *(f"corrupt/{name}" for name in CORRUPT_FIXTURES))
    proc = run_cli(fidr_image, work, "-i", "-v")
    assert proc.returncode == 0, proc.stderr

    by_name = {Path(f["filename"]).name: f for f in _read_log(work)["files"]}
    fid = work / "__fileidentification"

    problems = [
        _diagnose_not_quarantined(fidr_image, work, name, by_name.get(name))
        for name in CORRUPT_FIXTURES
        if not (
            (rec := by_name.get(name))
            and rec["status"].get("removed")
            and any(m.get("level") == "error" for m in rec.get("processing_logs", []))
            and list(fid.rglob(f"_REMOVED/**/{name}"))
        )
    ]
    assert not problems, "corrupt files not quarantined as expected:\n" + "\n".join(problems)

    # negative control: the valid image is untouched
    assert (work / good).is_file(), f"{good} should not be removed"
    assert by_name[good]["status"].get("removed") is False

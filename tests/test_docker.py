"""End-to-end tests that drive the shipped Docker image and CLI.

Instead of constructing a FileHandler in-process, these run the real
``fileidentification`` container the same way production does (the image's
ENTRYPOINT is ``identify.py``), so they exercise the actual delivery artifact
together with the bundled ffmpeg / imagemagick / libreoffice binaries.

They are marked ``docker`` — run the fast suite with ``pytest -m "not docker"``.

Behaviour:
* The whole module is skipped when Docker is unavailable.
* The ``fileidentification`` image is built once per session if it is missing
  (set ``FIDR_NO_BUILD=1`` to require a pre-built image, e.g. in CI where a
  separate step builds it; set ``FIDR_IMAGE`` to use a different tag).
* Each test copies the files it needs into a tmp dir that is bind-mounted into
  the container; the container writes its output back there. The container runs
  as root (as in production), so file ownership is reclaimed after each test to
  let pytest clean up.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA = REPO_ROOT / "testdata"
IMAGE = os.environ.get("FIDR_IMAGE", "fileidentification")


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
    assert any("premature end of data" in w["msg"] for w in quarantined.get("warnings", []))


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
    assert rec.get("warnings"), "the imagemagick warning should be recorded on the file"


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

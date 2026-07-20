"""Unit tests for the shell-command construction in wrappers.converter.

subprocess.run is monkeypatched so no real conversion tool is invoked; the tests
assert on the command list / string and the returned paths.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fileidentification.definitions.models import PolicyParams, SfInfo
from fileidentification.definitions.settings import PDFSETTINGS
from fileidentification.wrappers import converter
from tests.conftest import make_sfinfo


@pytest.fixture
def capture_cmd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the cmd_list passed to subprocess.run inside converter."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(stdout="out", stderr="err", returncode=0)

    monkeypatch.setattr("fileidentification.wrappers.converter.subprocess.run", fake_run)
    return calls


def _sfinfo(tmp_path: Path) -> SfInfo:
    s = make_sfinfo("clip.mp4", md5="abcdef0000")
    s.tdir = tmp_path
    s.path = tmp_path / "clip.mp4"
    return s


def test_ffmpeg_command(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo(tmp_path)
    args = PolicyParams(
        accepted=False,
        bin="ffmpeg",
        target_container="mkv",
        processing_args="-c:v ffv1",
        expected=["fmt/569"],
    )
    target, _cmd_str, logtext = converter.convert(s, args)
    cmd = capture_cmd[0]
    assert cmd[:4] == ["ffmpeg", "-y", "-i", str(s.path)]
    assert "-c:v" in cmd and "ffv1" in cmd  # processing_args were shlex-split
    assert cmd[-1] == str(target)
    assert target.name == "clip.mkv"
    assert logtext == "err"  # ffmpeg log comes from stderr


def test_magick_command(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo(tmp_path)
    args = PolicyParams(
        accepted=False,
        bin="magick",
        target_container="tif",
        processing_args="",
        expected=["fmt/353"],
    )
    target, _cmd_str, _logtext = converter.convert(s, args)
    cmd = capture_cmd[0]
    assert cmd[0] == "magick"
    assert cmd[-2:] == [str(s.path), str(target)]
    assert target.name == "clip.tif"


def test_soffice_pdf_uses_pdf_filter(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo(tmp_path)
    args = PolicyParams(
        accepted=False,
        bin="soffice",
        target_container="pdf",
        processing_args="--headless --convert-to",
        expected=["fmt/95"],
    )
    converter.convert(s, args)
    cmd = capture_cmd[0]
    # the pdf branch wraps the container in the PDF/A export filter
    assert f"pdf{PDFSETTINGS}" in cmd
    assert "--outdir" in cmd


def test_soffice_non_pdf_uses_plain_container(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo(tmp_path)
    args = PolicyParams(
        accepted=False,
        bin="soffice",
        target_container="docx",
        processing_args="--headless --convert-to",
        expected=["fmt/412"],
    )
    converter.convert(s, args)
    assert "docx" in capture_cmd[0]


def test_cmd_str_is_shell_quoted(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = make_sfinfo("my clip.mp4", md5="abcdef0000")
    s.tdir = tmp_path
    s.path = tmp_path / "my clip.mp4"
    args = PolicyParams(
        accepted=False,
        bin="ffmpeg",
        target_container="mkv",
        processing_args="-c:v ffv1",
        expected=["fmt/569"],
    )
    _target, cmd_str, _ = converter.convert(s, args)
    # a path with a space must be quoted so the string is copy-pasteable
    import shlex

    assert shlex.quote(str(s.path)) in cmd_str
    assert "'" in cmd_str  # the space forced shell quoting


def test_working_dir_is_created(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo(tmp_path)
    args = PolicyParams(
        accepted=False,
        bin="magick",
        target_container="tif",
        expected=["fmt/353"],
    )
    target, _, _ = converter.convert(s, args)
    assert target.parent.is_dir()
    # <name>_<pathhash[:6]>: basename prefix plus a 6-char hex hash of the file's relative path
    name = target.parent.name
    assert name.startswith("clip.mp4_")
    suffix = name.rsplit("_", 1)[1]
    assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)


def test_identical_files_at_different_paths_get_distinct_working_dirs(
    capture_cmd: list[list[str]], tmp_path: Path
) -> None:
    # two duplicates (same content md5, same basename) at different paths must not share a working dir,
    # otherwise one conversion would overwrite the other
    args = PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])

    a = make_sfinfo("sub_a/clip.mp4", md5="abcdef0000")
    a.tdir = tmp_path
    a.path = tmp_path / "sub_a" / "clip.mp4"
    b = make_sfinfo("sub_b/clip.mp4", md5="abcdef0000")
    b.tdir = tmp_path
    b.path = tmp_path / "sub_b" / "clip.mp4"

    target_a, _, _ = converter.convert(a, args)
    target_b, _, _ = converter.convert(b, args)

    assert target_a.parent != target_b.parent

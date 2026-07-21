"""Unit tests for wrappers.converter.convert.

convert() resolves the source / target / working-dir and runs the tool's command. The per-bin command
shape is owned by test_tools (MediaTool.build_command) and the working-dir math by test_workspace, so these
tests cover only convert()'s own wiring: source & target resolution, working-dir creation, the log stream it
returns, and shell-quoting of the printable command. Real per-bin conversions are covered by test_docker.
"""

import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fileidentification.definitions.models import PolicyParams, SfInfo
from fileidentification.wrappers import converter
from tests.conftest import make_sfinfo, make_ws


@pytest.fixture
def capture_cmd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the cmd_list passed to subprocess.run inside converter."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(stdout="out", stderr="err", returncode=0)

    monkeypatch.setattr("fileidentification.wrappers.converter.subprocess.run", fake_run)
    return calls


def _sfinfo(filename: str = "clip.mp4") -> SfInfo:
    return make_sfinfo(filename, md5="abcdef0000")


def test_wires_source_target_and_creates_working_dir(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo()
    ws = make_ws(tmp_path, tmp_path)
    args = PolicyParams(
        accepted=False, bin="ffmpeg", target_container="mkv", processing_args="-c:v ffv1", expected=["fmt/569"]
    )
    target, _cmd_str, logtext = converter.convert(s, args, ws)
    cmd = capture_cmd[0]
    assert str(ws.abs_path(s.filename)) in cmd  # source resolved from the workspace
    assert cmd[-1] == str(target)
    assert target.name == "clip.mkv"  # named from target_container
    assert target.parent.is_dir()  # the working dir was created
    assert logtext == "err"  # log comes from the tool's stream (ffmpeg: stderr)


def test_magick_places_source_and_target(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    # magick puts the source positionally (no -i), so guard that convert wires the other command shape too
    s = _sfinfo()
    ws = make_ws(tmp_path, tmp_path)
    args = PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
    target, _, _ = converter.convert(s, args, ws)
    assert capture_cmd[0][-2:] == [str(ws.abs_path(s.filename)), str(target)]
    assert target.name == "clip.tif"


def test_cmd_str_is_shell_quoted(capture_cmd: list[list[str]], tmp_path: Path) -> None:
    s = _sfinfo("my clip.mp4")
    ws = make_ws(tmp_path, tmp_path)
    args = PolicyParams(
        accepted=False, bin="ffmpeg", target_container="mkv", processing_args="-c:v ffv1", expected=["fmt/569"]
    )
    _target, cmd_str, _ = converter.convert(s, args, ws)
    # a path with a space must be quoted so the string is copy-pasteable
    assert shlex.quote(str(ws.abs_path(s.filename))) in cmd_str
    assert "'" in cmd_str  # the space forced shell quoting

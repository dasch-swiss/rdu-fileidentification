"""Unit tests for tasks.conversion.

_verify re-identifies a freshly converted file with pygfried and decides whether the conversion produced the
expected format; pygfried is monkeypatched so the tests are deterministic. _run_tool is the module-internal seam
that runs the tool command (its subprocess is faked); the per-bin command shape is owned by test_tools
(MediaTool.build_command) and the working-dir math by test_workspace. convert_file wires the two together.
Real per-bin conversions are covered by test_docker.
"""

import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fileidentification.definitions.models import PolicyParams
from fileidentification.definitions.settings import Bin, FPMsg, LogLevel
from fileidentification.tasks import conversion as conv_mod
from fileidentification.tasks.conversion import _add_media_info, _run_tool, _verify, convert_file
from fileidentification.wrappers import tools
from fileidentification.wrappers.tools import MediaTool, tool_for
from tests.conftest import fake_identify_payload, make_sfinfo, make_ws


def _tool(bin_: str) -> MediaTool:
    """Resolve a MediaTool for tests, narrowing away the None case (the bins used here are always known)."""
    tool = tool_for(bin_)
    assert tool is not None
    return tool


def _patch_identify(monkeypatch: pytest.MonkeyPatch, target: Path, puid: str) -> None:
    """Make pygfried.identify report `puid` for the converted target file."""

    def fake_identify(path: str, detailed: bool = False) -> dict[str, Any]:
        return fake_identify_payload(path, puid=puid, mime="image/tiff", md5="f" * 32)

    monkeypatch.setattr(conv_mod, "pygfried", SimpleNamespace(identify=fake_identify))


def test_missing_target_is_conversion_failure(tmp_path: Path) -> None:
    """No output file on disk -> conversion failed, original logs CONVFAILED."""
    origin = make_sfinfo("sub/orig.jpg")
    result = _verify(tmp_path / "never-created.tif", origin, expected=["fmt/353"], ws=make_ws(tmp_path, tmp_path))
    assert result is None
    assert any(FPMsg.CONVFAILED in log.msg and log.level == LogLevel.ERROR for log in origin.processing_logs)


def test_unexpected_format_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output exists but is the wrong format -> rejected, NOTEXPECTEDFMT logged."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/43")  # got jpeg, not the expected tiff

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    result = _verify(target, origin, expected=["fmt/353"], ws=make_ws(tmp_path, tmp_path))

    assert result is None
    assert origin.status.pending is True  # left pending: conversion did not succeed
    msgs = " ".join(log.msg for log in origin.processing_logs)
    assert FPMsg.NOTEXPECTEDFMT in msgs
    assert "fmt/353" in msgs and "fmt/43" in msgs  # expected vs. actual reported
    assert origin.processing_logs[-1].level == LogLevel.ERROR  # unexpected-format is error-level


def test_expected_format_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output matching any expected PUID -> wired up as a derived file."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    # tmp_dir = tmp_path, so the target's tmp-relative location is just "orig.tif"
    result = _verify(target, origin, expected=["fmt/152", "fmt/353"], ws=make_ws(tmp_path, tmp_path))

    assert result is not None
    assert result.processed_as == "fmt/353"
    assert result.derived_from is origin
    assert result.filename == Path("orig.tif")  # points at its physical location relative to tmp_dir
    assert result.dest == Path("sub")  # future home dir, next to the original
    assert origin.status.pending is False  # original is now resolved
    assert any("converted ->" in log.msg for log in origin.processing_logs)  # success logged on the origin


class TestAddMediaInfo:
    def test_ffmpeg_appends_json_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "ffmpeg_media_info", lambda path: [{"codec_type": "video"}])
        s = make_sfinfo("v.mp4", puid="fmt/199")
        _add_media_info(s, tool_for(Bin.FFMPEG), s.filename)
        assert s.media_info[0].name == "ffmpeg"
        assert '"codec_type": "video"' in s.media_info[0].msg

    def test_magick_appends_identify_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "imagemagick_media_info", lambda path: "TIFF 10x10")
        s = make_sfinfo("i.tif", puid="fmt/353")
        _add_media_info(s, tool_for(Bin.MAGICK), s.filename)
        assert s.media_info[0].name == "imagemagick"
        assert s.media_info[0].msg == "TIFF 10x10"

    def test_other_bin_is_noop(self) -> None:
        s = make_sfinfo("d.docx", puid="fmt/412")
        _add_media_info(s, tool_for(Bin.SOFFICE), s.filename)
        assert not s.media_info

    def test_probes_physical_path_not_filename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # invariant: the converted file's filename is already its relative home, so media info must be
        # probed at the physical working-dir path passed in, never at sfinfo.filename.
        seen: list[Path] = []

        def rec(path: Path) -> list[Any]:
            seen.append(path)
            return []

        monkeypatch.setattr(tools, "ffmpeg_media_info", rec)
        s = make_sfinfo("sub/out.mp4", puid="fmt/199")  # relative home, not where the file physically is
        physical = Path("/work/out.mp4_abc123/out.mp4")
        _add_media_info(s, tool_for(Bin.FFMPEG), physical)
        assert seen == [physical]


def test_convert_file_strips_abs_paths_from_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """convert_file removes root_folder/tdir prefixes from the tool log before attaching it."""
    root, tdir = tmp_path / "root", tmp_path / "tdir"
    ws = make_ws(root, tdir)
    origin = make_sfinfo("sub/orig.jpg", puid="fmt/43")
    origin.status.pending = True

    target = tdir / "orig.tif"  # the converted file sits under tmp_dir
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")
    logtext = f"{root}/sub/orig.jpg -> {tdir}/out"
    monkeypatch.setattr(conv_mod, "_run_tool", lambda s, a, tool, ws: (target, "the cmd", logtext))
    monkeypatch.setattr(conv_mod, "_add_media_info", lambda s, t, p: None)

    policies = {"fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])}
    result, cmds, bin_log = convert_file(origin, policies, ws)

    assert result is not None
    assert cmds == ["the cmd"]
    assert bin_log is None  # consumed by the successful target
    log = next(log for log in result.processing_logs if log.name == "magick")
    assert log.msg == "sub/orig.jpg -> out"  # both absolute prefixes stripped


def test_convert_file_returns_bin_log_on_failure_without_touching_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On failure the bin's log is returned (for the "errors" copy) and is not added to the origin sfinfo."""
    origin = make_sfinfo("sub/orig.jpg", puid="fmt/43")
    origin.status.pending = True
    ws = make_ws("/root", "/tmp/tdir")

    missing_target = tmp_path / "never-created.tif"  # never written -> conversion failed
    monkeypatch.setattr(
        conv_mod, "_run_tool", lambda s, a, tool, ws: (missing_target, "the cmd", "magick: some fatal error")
    )

    policies = {"fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])}
    result, _, bin_log = convert_file(origin, policies, ws)

    assert result is None
    # the failure reason is recorded on the origin, but the bin log is NOT (it only travels back to the caller)
    assert FPMsg.CONVFAILED in origin.processing_logs[-1].msg
    assert not any(log.name == "magick" for log in origin.processing_logs)
    assert bin_log is not None
    assert bin_log.name == "magick"
    assert bin_log.msg == "magick: some fatal error"


class TestRunTool:
    """_run_tool resolves source/target/working-dir and runs the tool command; the subprocess is faked here.

    Covers only _run_tool's own wiring: source & target resolution, working-dir creation, the log stream it
    returns, and shell-quoting of the printable command. Real per-bin conversions are covered by test_docker.
    """

    @pytest.fixture
    def capture_cmd(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """Capture the cmd_list passed to subprocess.run inside conversion."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(stdout="out", stderr="err", returncode=0)

        monkeypatch.setattr("fileidentification.tasks.conversion.subprocess.run", fake_run)
        return calls

    def test_wires_source_target_and_creates_working_dir(self, capture_cmd: list[list[str]], tmp_path: Path) -> None:
        s = make_sfinfo("clip.mp4", md5="abcdef0000")
        ws = make_ws(tmp_path, tmp_path)
        args = PolicyParams(
            accepted=False, bin="ffmpeg", target_container="mkv", processing_args="-c:v ffv1", expected=["fmt/569"]
        )
        target, _cmd_str, logtext = _run_tool(s, args, _tool(args.bin), ws)
        cmd = capture_cmd[0]
        assert str(ws.abs_path(s.filename)) in cmd  # source resolved from the workspace
        assert cmd[-1] == str(target)
        assert target.name == "clip.mkv"  # named from target_container
        assert target.parent.is_dir()  # the working dir was created
        assert logtext == "err"  # log comes from the tool's stream (ffmpeg: stderr)

    def test_magick_places_source_and_target(self, capture_cmd: list[list[str]], tmp_path: Path) -> None:
        # magick puts the source positionally (no -i), so guard that _run_tool wires the other command shape too
        s = make_sfinfo("clip.mp4", md5="abcdef0000")
        ws = make_ws(tmp_path, tmp_path)
        args = PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
        target, _, _ = _run_tool(s, args, _tool(args.bin), ws)
        assert capture_cmd[0][-2:] == [str(ws.abs_path(s.filename)), str(target)]
        assert target.name == "clip.tif"

    def test_cmd_str_is_shell_quoted(self, capture_cmd: list[list[str]], tmp_path: Path) -> None:
        s = make_sfinfo("my clip.mp4", md5="abcdef0000")
        ws = make_ws(tmp_path, tmp_path)
        args = PolicyParams(
            accepted=False, bin="ffmpeg", target_container="mkv", processing_args="-c:v ffv1", expected=["fmt/569"]
        )
        _target, cmd_str, _ = _run_tool(s, args, _tool(args.bin), ws)
        # a path with a space must be quoted so the string is copy-pasteable
        assert shlex.quote(str(ws.abs_path(s.filename))) in cmd_str
        assert "'" in cmd_str  # the space forced shell quoting

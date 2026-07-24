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
from typing import Any, Self

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


class _LockSpy:
    """A context manager that counts how many times it is entered (stands in for the serial lock)."""

    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _patch_identify(monkeypatch: pytest.MonkeyPatch, target: Path, puid: str) -> None:
    """Make pygfried.identify report `puid` for the converted target file."""

    def fake_identify(path: str, detailed: bool = False) -> dict[str, Any]:
        return fake_identify_payload(path, puid=puid, mime="image/tiff", md5="f" * 32)

    monkeypatch.setattr(conv_mod, "pygfried", SimpleNamespace(identify=fake_identify))


def test_missing_target_is_conversion_failure(tmp_path: Path) -> None:
    """No output file on disk -> (None, reason) with CONVFAILED; the origin's log is left untouched."""
    origin = make_sfinfo("sub/orig.jpg")
    result, reason = _verify(tmp_path / "never-created.tif", origin, expected=["fmt/353"], ws=make_ws(tmp_path, tmp_path))
    assert result is None
    assert reason is not None and FPMsg.CONVFAILED in reason.msg and reason.level == LogLevel.ERROR
    assert origin.processing_logs == []  # reason is returned for the caller to record, not written to the origin


def test_unexpected_format_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output exists but is the wrong format -> rejected, NOTEXPECTEDFMT logged."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/43")  # got jpeg, not the expected tiff

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    result, reason = _verify(target, origin, expected=["fmt/353"], ws=make_ws(tmp_path, tmp_path))

    assert result is None
    assert origin.status.pending is True  # left pending: conversion did not succeed
    assert reason is not None and reason.level == LogLevel.ERROR  # unexpected-format is error-level
    assert FPMsg.NOTEXPECTEDFMT in reason.msg
    assert "fmt/353" in reason.msg and "fmt/43" in reason.msg  # expected vs. actual reported
    assert origin.processing_logs == []  # reason is returned for the caller to record, not written to the origin


def test_expected_format_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output matching any expected PUID -> wired up as a derived file."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    # tmp_dir = tmp_path, so the target's tmp-relative location is just "orig.tif"
    result, reason = _verify(target, origin, expected=["fmt/152", "fmt/353"], ws=make_ws(tmp_path, tmp_path))

    assert result is not None
    assert reason is None  # success: no failure reason
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
    res = convert_file(origin, policies, ws)

    assert res.converted is not None
    assert res.cmd == "the cmd"
    assert res.error is None and res.bin_log is None  # success: log attached to the target, nothing left to record
    log = next(log for log in res.converted.processing_logs if log.name == "magick")
    assert log.msg == "sub/orig.jpg -> out"  # both absolute prefixes stripped


def test_convert_file_returns_bin_log_on_failure_without_touching_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On failure the reason and the bin's log both travel back to the caller; the origin sfinfo is untouched."""
    origin = make_sfinfo("sub/orig.jpg", puid="fmt/43")
    origin.status.pending = True
    ws = make_ws("/root", "/tmp/tdir")

    missing_target = tmp_path / "never-created.tif"  # never written -> conversion failed
    monkeypatch.setattr(
        conv_mod, "_run_tool", lambda s, a, tool, ws: (missing_target, "the cmd", "magick: some fatal error")
    )

    policies = {"fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])}
    res = convert_file(origin, policies, ws)

    assert res.converted is None
    assert res.error is not None and FPMsg.CONVFAILED in res.error.msg  # the failure summary travels back to the caller
    assert origin.processing_logs == []  # the origin is left untouched
    assert res.bin_log is not None
    assert res.bin_log.name == "magick"
    assert res.bin_log.msg == "magick: some fatal error"


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

    def test_serial_tool_takes_the_lock(self, capture_cmd: list[list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # a serial tool (soffice) runs its subprocess under the module lock; a non-serial one does not
        spy = _LockSpy()
        monkeypatch.setattr(conv_mod, "_serial_lock", spy)
        ws = make_ws(tmp_path, tmp_path)

        soffice = PolicyParams(accepted=False, bin="soffice", target_container="docx", expected=["fmt/412"])
        _run_tool(make_sfinfo("d.doc", md5="a" * 10), soffice, _tool("soffice"), ws)
        assert spy.entered == 1

        magick = PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
        _run_tool(make_sfinfo("i.jpg", md5="b" * 10), magick, _tool("magick"), ws)
        assert spy.entered == 1  # unchanged: the non-serial tool skipped the lock

"""Unit tests for conversion._verify.

_verify re-identifies a freshly converted file with pygfried and decides whether
the conversion produced the expected format. pygfried is monkeypatched so the
tests are deterministic and do not depend on a real conversion having run.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fileidentification.definitions.models import PolicyParams
from fileidentification.definitions.settings import Bin, FPMsg
from fileidentification.tasks import conversion as conv_mod
from fileidentification.tasks.conversion import _add_media_info, _verify, convert_file
from tests.conftest import fake_identify_payload, make_sfinfo


def _patch_identify(monkeypatch: pytest.MonkeyPatch, target: Path, puid: str) -> None:
    """Make pygfried.identify report `puid` for the converted target file."""

    def fake_identify(path: str, detailed: bool = False) -> dict[str, Any]:
        return fake_identify_payload(path, puid=puid, mime="image/tiff", md5="f" * 32)

    monkeypatch.setattr(conv_mod, "pygfried", SimpleNamespace(identify=fake_identify))


def test_missing_target_is_conversion_failure(tmp_path: Path) -> None:
    """No output file on disk -> conversion failed, original logs CONVFAILED."""
    origin = make_sfinfo("sub/orig.jpg")
    result = _verify(tmp_path / "never-created.tif", origin, expected=["fmt/353"])
    assert result is None
    assert any(FPMsg.CONVFAILED in log.msg for log in origin.processing_logs)


def test_unexpected_format_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output exists but is the wrong format -> rejected, NOTEXPECTEDFMT logged."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/43")  # got jpeg, not the expected tiff

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    result = _verify(target, origin, expected=["fmt/353"])

    assert result is None
    assert origin.status.pending is True  # left pending: conversion did not succeed
    msgs = " ".join(log.msg for log in origin.processing_logs)
    assert FPMsg.NOTEXPECTEDFMT in msgs
    assert "fmt/353" in msgs and "fmt/43" in msgs  # expected vs. actual reported


def test_expected_format_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output matching any expected PUID -> wired up as a derived file."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    result = _verify(target, origin, expected=["fmt/152", "fmt/353"])  # matches the second listed PUID

    assert result is not None
    assert result.processed_as == "fmt/353"
    assert result.derived_from is origin
    assert result.dest == Path("sub")  # placed next to the original
    assert origin.status.pending is False  # original is now resolved


class TestAddMediaInfo:
    def test_ffmpeg_appends_json_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(conv_mod, "ffmpeg_media_info", lambda path: [{"codec_type": "video"}])
        s = make_sfinfo("v.mp4", puid="fmt/199")
        _add_media_info(s, Bin.FFMPEG)
        assert s.media_info[0].name == "ffmpeg"
        assert '"codec_type": "video"' in s.media_info[0].msg

    def test_magick_appends_identify_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(conv_mod, "imagemagick_media_info", lambda path: "TIFF 10x10")
        s = make_sfinfo("i.tif", puid="fmt/353")
        _add_media_info(s, Bin.MAGICK)
        assert s.media_info[0].name == "imagemagick"
        assert s.media_info[0].msg == "TIFF 10x10"

    def test_other_bin_is_noop(self) -> None:
        s = make_sfinfo("d.docx", puid="fmt/412")
        _add_media_info(s, Bin.SOFFICE)
        assert not s.media_info


def test_convert_file_strips_abs_paths_from_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """convert_file removes root_folder/tdir prefixes from the tool log before attaching it."""
    origin = make_sfinfo("sub/orig.jpg", puid="fmt/43")
    origin.root_folder = Path("/root")
    origin.tdir = Path("/tmp/tdir")
    origin.status.pending = True

    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")
    monkeypatch.setattr(conv_mod, "convert", lambda s, a: (target, "the cmd", "/root/sub/orig.jpg -> /tmp/tdir/out"))
    monkeypatch.setattr(conv_mod, "_add_media_info", lambda s, b: None)

    policies = {"fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])}
    result, cmds, bin_log = convert_file(origin, policies)

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
    origin.root_folder = Path("/root")
    origin.tdir = Path("/tmp/tdir")
    origin.status.pending = True

    missing_target = tmp_path / "never-created.tif"  # never written -> conversion failed
    monkeypatch.setattr(conv_mod, "convert", lambda s, a: (missing_target, "the cmd", "magick: some fatal error"))

    policies = {"fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])}
    result, _, bin_log = convert_file(origin, policies)

    assert result is None
    # the failure reason is recorded on the origin, but the bin log is NOT (it only travels back to the caller)
    assert FPMsg.CONVFAILED in origin.processing_logs[-1].msg
    assert not any(log.name == "magick" for log in origin.processing_logs)
    assert bin_log is not None
    assert bin_log.name == "magick"
    assert bin_log.msg == "magick: some fatal error"

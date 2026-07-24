"""Unit tests for the MediaTool seam (wrappers.tools).

The adapters are tested directly through the interface: resolution, per-tool command construction,
probe-result mapping, media-info labels, the serialized-run flag, and log-stream selection.
The underlying wrapper functions are monkeypatched so no external binary runs.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fileidentification.definitions.models import PolicyParams
from fileidentification.definitions.settings import PDFSETTINGS, Bin
from fileidentification.wrappers import tools
from fileidentification.wrappers.tools import Ffmpeg, Imagemagick, Soffice, tool_for, tool_from_mime


class TestResolution:
    def test_tool_for_known_bins(self) -> None:
        assert isinstance(tool_for(Bin.FFMPEG), Ffmpeg)
        assert isinstance(tool_for(Bin.MAGICK), Imagemagick)
        assert isinstance(tool_for(Bin.SOFFICE), Soffice)

    def test_tool_for_plain_string_bin(self) -> None:
        # policies carry the bin as a plain str, which must resolve just like the enum
        assert isinstance(tool_for("magick"), Imagemagick)

    def test_tool_for_empty_or_unknown_is_none(self) -> None:
        assert tool_for("") is None
        assert tool_for("inkscape") is None

    def test_tool_from_mime(self) -> None:
        assert isinstance(tool_from_mime("image/jpeg"), Imagemagick)
        assert isinstance(tool_from_mime("audio/wav"), Ffmpeg)
        assert isinstance(tool_from_mime("video/mp4"), Ffmpeg)
        assert tool_from_mime("application/pdf") is None
        assert tool_from_mime("") is None

    def test_serial_flag_marks_soffice_only(self) -> None:
        # soffice can't run concurrent instances; the conversion module serializes tools flagged serial
        assert Soffice().serial is True
        assert Ffmpeg().serial is False
        assert Imagemagick().serial is False


class TestBuildCommand:
    def _args(self, **kw: object) -> PolicyParams:
        base = {"accepted": False, "target_container": "x", "expected": ["fmt/1"]}
        return PolicyParams(**{**base, "bin": "ffmpeg", **kw})  # type: ignore[arg-type]

    def test_ffmpeg_command(self) -> None:
        args = self._args(bin="ffmpeg", target_container="mkv", processing_args="-c:v ffv1")
        cmd = Ffmpeg().build_command(Path("/in/clip.mov"), args, Path("/w/clip.mkv"), Path("/w"))
        assert cmd[:4] == ["ffmpeg", "-y", "-i", "/in/clip.mov"]
        assert "-c:v" in cmd and "ffv1" in cmd
        assert cmd[-1] == "/w/clip.mkv"

    def test_magick_command(self) -> None:
        args = self._args(bin="magick", target_container="tif", processing_args="")
        cmd = Imagemagick().build_command(Path("/in/i.jpg"), args, Path("/w/i.tif"), Path("/w"))
        assert cmd[0] == "magick"
        assert cmd[-2:] == ["/in/i.jpg", "/w/i.tif"]

    def test_soffice_pdf_uses_pdf_filter(self) -> None:
        args = self._args(bin="soffice", target_container="pdf", processing_args="--headless --convert-to")
        cmd = Soffice().build_command(Path("/in/d.docx"), args, Path("/w/d.pdf"), Path("/w"))
        assert f"pdf{PDFSETTINGS}" in cmd
        assert cmd[-2:] == ["--outdir", "/w"]

    def test_soffice_non_pdf_uses_plain_container(self) -> None:
        args = self._args(bin="soffice", target_container="docx", processing_args="--headless --convert-to")
        cmd = Soffice().build_command(Path("/in/d.doc"), args, Path("/w/d.docx"), Path("/w"))
        assert "docx" in cmd


class TestReadLog:
    def _result(self) -> SimpleNamespace:
        return SimpleNamespace(stdout="OUT", stderr="ERR")

    def test_default_reads_stderr(self) -> None:
        assert Ffmpeg().read_log(self._result()) == "ERR"  # type: ignore[arg-type]
        assert Imagemagick().read_log(self._result()) == "ERR"  # type: ignore[arg-type]

    def test_soffice_reads_stdout_and_stderr(self) -> None:
        assert Soffice().read_log(self._result()) == "OUTERR"  # type: ignore[arg-type]


class TestProbe:
    def test_ffmpeg_flags_reencode_from_warnings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            tools, "ffmpeg_collect_warnings", lambda path, verbose: (False, "A non-intra slice in an IDR NAL unit", "s")
        )
        result = Ffmpeg().probe(Path("v.mp4"), verbose=False)
        assert result is not None
        assert result.needs_reencode is True
        assert result.is_corrupt is False
        assert result.specs == "s"

    def test_ffmpeg_no_reencode_on_plain_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "ffmpeg_collect_warnings", lambda path, verbose: (True, "boom", ""))
        result = Ffmpeg().probe(Path("v.mp4"), verbose=False)
        assert result is not None
        assert result.needs_reencode is False
        assert result.is_corrupt is True

    def test_magick_probe_maps_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            tools, "imagemagick_collect_warnings", lambda path, verbose: (True, "identify: Cannot read", "spec")
        )
        result = Imagemagick().probe(Path("i.jpg"), verbose=False)
        assert result is not None
        assert result.is_corrupt is True
        assert result.warnings == "identify: Cannot read"

    def test_soffice_does_not_probe(self) -> None:
        assert Soffice().probe(Path("d.docx"), verbose=False) is None


class TestMediaInfo:
    def test_ffmpeg_media_info_is_json_labelled_ffmpeg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "ffmpeg_media_info", lambda path: [{"codec_type": "video"}])
        log = Ffmpeg().media_info(Path("v.mp4"))
        assert log is not None
        assert log.name == "ffmpeg"
        assert '"codec_type": "video"' in log.msg

    def test_magick_media_info_is_labelled_imagemagick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tools, "imagemagick_media_info", lambda path: "TIFF 10x10")
        log = Imagemagick().media_info(Path("i.tif"))
        assert log is not None
        assert log.name == "imagemagick"
        assert log.msg == "TIFF 10x10"

    def test_soffice_has_no_media_info(self) -> None:
        assert Soffice().media_info(Path("d.docx")) is None

"""Unit tests for the ffmpeg / imagemagick wrapper helpers.

subprocess.run is monkeypatched so the corrupt-detection and command-construction
logic is exercised without invoking ffprobe / ffmpeg / magick.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fileidentification.wrappers import ffmpeg as ff
from fileidentification.wrappers import imagemagick as im


class TestFfmpeg:
    @staticmethod
    def _patch(
        monkeypatch: pytest.MonkeyPatch,
        *,
        probe_stdout: str = "",
        verbose_stderr: str = "",
        media_json: list[dict[str, Any]] | None = None,
        media_rc: int = 0,
    ) -> list[list[str]]:
        """Fake subprocess.run that answers each of ffmpeg.py's three call shapes."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append(cmd)
            if "-show_entries" in cmd:  # ffmpeg_media_info (bytes stdout, no text=True)
                out = json.dumps({"streams": media_json}).encode() if media_json is not None else b"{}"
                return SimpleNamespace(stdout=out, stderr=b"", returncode=media_rc)
            if cmd[0] == "ffmpeg":  # verbose decode pass
                return SimpleNamespace(stdout="", stderr=verbose_stderr, returncode=0)
            return SimpleNamespace(stdout=probe_stdout, stderr="", returncode=0)  # ffprobe -show_error

        monkeypatch.setattr("fileidentification.wrappers.ffmpeg.subprocess.run", fake_run)
        return calls

    def test_non_verbose_uses_show_error_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch, media_json=[{"codec_type": "video"}])
        ff.ffmpeg_collect_warnings(Path("v.mp4"), verbose=False)
        assert ["ffprobe", "-hide_banner", "-show_error", "v.mp4"] in calls
        assert not any(c[0] == "ffmpeg" for c in calls)  # no decode pass without verbose

    def test_verbose_runs_decode_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch, verbose_stderr="frame drop", media_json=[])
        corrupt, std_out, _specs = ff.ffmpeg_collect_warnings(Path("v.mp4"), verbose=True)
        assert any(c[0] == "ffmpeg" and "-v" in c and "error" in c for c in calls)
        assert std_out == "frame drop"  # verbose std_out comes from the decode stderr
        assert corrupt is False  # ffprobe stdout was empty

    def test_ffprobe_stdout_means_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, probe_stdout="moov atom not found", media_json=[])
        corrupt, std_out, _ = ff.ffmpeg_collect_warnings(Path("v.mp4"), verbose=False)
        assert corrupt is True
        assert "moov atom not found" in std_out

    def test_specs_serialised_from_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        streams = [{"codec_type": "video", "codec_name": "h264"}]
        self._patch(monkeypatch, media_json=streams)
        _corrupt, _std, specs = ff.ffmpeg_collect_warnings(Path("v.mp4"), verbose=False)
        assert json.loads(specs) == streams

    def test_specs_empty_when_media_info_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, media_rc=1)  # ffprobe media pass fails
        _corrupt, _std, specs = ff.ffmpeg_collect_warnings(Path("v.mp4"), verbose=False)
        assert specs == ""

    def test_media_info_none_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, media_rc=2)
        assert ff.ffmpeg_media_info(Path("v.mp4")) is None

    def test_media_info_returns_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        streams = [{"index": 0, "codec_type": "audio"}]
        self._patch(monkeypatch, media_json=streams)
        result: Any = ff.ffmpeg_media_info(Path("v.mp4"))
        assert result == streams


class TestImagemagick:
    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", stderr: str = "") -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)

        monkeypatch.setattr("fileidentification.wrappers.imagemagick.subprocess.run", fake_run)
        return calls

    def test_non_verbose_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch)
        im.imagemagick_collect_warnings(Path("i.jpg"), verbose=False)
        assert "-verbose" not in calls[0]
        assert "-regard-warnings" not in calls[0]

    def test_verbose_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch)
        im.imagemagick_collect_warnings(Path("i.jpg"), verbose=True)
        assert "-verbose" in calls[0]
        assert "-regard-warnings" in calls[0]

    def test_known_error_string_is_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, stderr="identify: Cannot read image")
        corrupt, std_err, _ = im.imagemagick_collect_warnings(Path("i.jpg"), verbose=False)
        assert corrupt is True
        assert "Cannot read" in std_err

    def test_benign_warning_is_not_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # a warning that is not one of the ErrMsgIM strings -> file is readable
        self._patch(monkeypatch, stderr='Wrong data type 3 for "GainControl"; tag ignored.')
        corrupt, std_err, _ = im.imagemagick_collect_warnings(Path("i.tiff"), verbose=True)
        assert corrupt is False
        assert "GainControl" in std_err

    def test_clean_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, stdout="TIFF 30x20")
        corrupt, std_err, specs = im.imagemagick_collect_warnings(Path("i.tiff"), verbose=False)
        assert corrupt is False
        assert std_err == ""
        assert specs == "TIFF 30x20"

    def test_libtiff_can_not_read_spelling_is_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # regex `can ?not read` catches libtiff's spaced spelling, which the old
        # exact substring "identify: Cannot read" missed.
        self._patch(monkeypatch, stderr="identify: i.tif: Can not read TIFF directory count.")
        corrupt, _, _ = im.imagemagick_collect_warnings(Path("i.tif"), verbose=False)
        assert corrupt is True

    def test_premature_end_warning_is_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # a truncated JPEG surfaces only as a *warning* (identify exits 0); we
        # still flag it via the message.
        self._patch(monkeypatch, stderr="identify: Premature end of JPEG file")
        corrupt, _, _ = im.imagemagick_collect_warnings(Path("i.jpg"), verbose=False)
        assert corrupt is True

    def test_magick_prefix_is_matched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # patterns are prefix-agnostic: `magick:`/`convert:` output matches too.
        self._patch(monkeypatch, stderr="magick: corrupt image")
        corrupt, _, _ = im.imagemagick_collect_warnings(Path("i.gif"), verbose=False)
        assert corrupt is True

    def test_unknown_tiff_tag_is_not_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, stderr="identify: Unknown field with tag 42112 (0xa480) encountered.")
        corrupt, _, _ = im.imagemagick_collect_warnings(Path("i.tif"), verbose=True)
        assert corrupt is False

    def test_media_info_returns_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch(monkeypatch, stdout="JPEG 100x100")
        assert im.imagemagick_media_info(Path("i.jpg")) == "JPEG 100x100"
        assert "-ping" in calls[0]

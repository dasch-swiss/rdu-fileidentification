"""Unit tests for apply_policy.

The A/V stream inspection (`_has_invalid_streams`) shells out to ffprobe, so the
ffmpeg helper is monkeypatched to keep these tests pure and fast.
"""

from typing import Any

import pytest

from fileidentification.definitions.models import LogTables, PolicyParams
from fileidentification.definitions.settings import PLMsg
from fileidentification.tasks import policies as policies_mod
from fileidentification.tasks.policies import apply_policy
from tests.conftest import make_sfinfo

ACCEPTED = PolicyParams(format_name="JPEG", accepted=True)
CONVERT = PolicyParams(format_name="JPEG", accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])


def test_no_puid_is_noop() -> None:
    s = make_sfinfo(puid="UNKNOWN", warning="no match")  # processed_as is None
    apply_policy(s, {}, LogTables(), strict=False)
    assert not s.status.pending


def test_already_pending_is_noop() -> None:
    s = make_sfinfo()
    s.status.pending = True
    apply_policy(s, {"fmt/43": CONVERT}, LogTables(), strict=False)
    assert s.status.pending  # unchanged, no error


def test_accepted_file_stays() -> None:
    s = make_sfinfo()
    apply_policy(s, {"fmt/43": ACCEPTED}, LogTables(), strict=False)
    assert not s.status.pending


def test_not_accepted_marks_pending() -> None:
    s = make_sfinfo()
    apply_policy(s, {"fmt/43": CONVERT}, LogTables(), strict=False)
    assert s.status.pending


def test_missing_policy_non_strict_is_skipped() -> None:
    s = make_sfinfo()
    apply_policy(s, {}, LogTables(), strict=False)
    assert not s.status.pending
    assert any(PLMsg.SKIPPED in log.msg for log in s.processing_logs)


def test_missing_policy_strict_calls_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[Any] = []
    monkeypatch.setattr(policies_mod, "remove", lambda sfinfo, lt: removed.append(sfinfo))
    s = make_sfinfo()
    apply_policy(s, {}, LogTables(), strict=True)
    assert removed == [s]
    assert any(PLMsg.NOTINPOLICIES in log.msg for log in s.processing_logs)


class TestInvalidStreams:
    """fmt/199 (mp4) wants h264+aac; fmt/569 (mkv) wants ffv1 video."""

    def _patch_streams(self, monkeypatch: pytest.MonkeyPatch, streams: list[dict[str, str]] | None) -> None:
        monkeypatch.setattr(policies_mod, "ffmpeg_media_info", lambda path: streams)

    def test_mp4_with_correct_codecs_not_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(
            monkeypatch,
            [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "aac"}],
        )
        s = make_sfinfo("v.mp4", puid="fmt/199", mime="video/mp4")
        apply_policy(s, {"fmt/199": ACCEPTED}, LogTables(), strict=False)
        assert not s.status.pending

    def test_mp4_with_wrong_codec_is_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(
            monkeypatch,
            [{"codec_type": "video", "codec_name": "hevc"}, {"codec_type": "audio", "codec_name": "aac"}],
        )
        s = make_sfinfo("v.mp4", puid="fmt/199", mime="video/mp4")
        apply_policy(s, {"fmt/199": ACCEPTED}, LogTables(), strict=False)
        assert s.status.pending

    def test_mkv_with_ffv1_not_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(monkeypatch, [{"codec_type": "video", "codec_name": "ffv1"}])
        s = make_sfinfo("v.mkv", puid="fmt/569", mime="video/x-matroska")
        apply_policy(s, {"fmt/569": ACCEPTED}, LogTables(), strict=False)
        assert not s.status.pending

    def test_mkv_without_ffv1_is_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(monkeypatch, [{"codec_type": "video", "codec_name": "h264"}])
        s = make_sfinfo("v.mkv", puid="fmt/569", mime="video/x-matroska")
        apply_policy(s, {"fmt/569": ACCEPTED}, LogTables(), strict=False)
        assert s.status.pending

    def test_unprobeable_av_file_is_not_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(monkeypatch, None)
        s = make_sfinfo("v.mp4", puid="fmt/199", mime="video/mp4")
        apply_policy(s, {"fmt/199": ACCEPTED}, LogTables(), strict=False)
        assert not s.status.pending

"""Unit tests for tasks.policies: resolving/generating policies (read_policies, build_policies) and applying them
(apply_policy).

read_policies / build_policies are tested directly, with no FileHandler, no shared state, and no SystemExit.
The A/V stream inspection (`_has_invalid_streams`) shells out to ffprobe, so the ffmpeg helper is monkeypatched
to keep those tests pure and fast.
"""

import json
from typing import Any

import pytest

from fileidentification.definitions.models import LogTables, Mode, PolicyParams
from fileidentification.definitions.settings import DEFAULTPOLICIES, FMT2EXT, PLMsg
from fileidentification.tasks import policies as policies_mod
from fileidentification.tasks.policies import apply_policy, build_policies
from tests.conftest import make_sfinfo, make_ws


def _unknown_puid() -> str:
    """A PUID that exists in FMT2EXT but has no default policy."""
    defaults = json.loads(DEFAULTPOLICIES.read_text())["policies"]
    return next(p for p in FMT2EXT if p not in defaults)


ACCEPTED = PolicyParams(format_name="JPEG", accepted=True)
CONVERT = PolicyParams(format_name="JPEG", accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
WS = make_ws()


def test_no_puid_is_noop() -> None:
    s = make_sfinfo(puid="UNKNOWN", warning="no match")  # processed_as is None
    apply_policy(s, {}, WS, LogTables(), strict=False)
    assert not s.status.pending


def test_already_pending_is_noop() -> None:
    s = make_sfinfo()
    s.status.pending = True
    apply_policy(s, {"fmt/43": CONVERT}, WS, LogTables(), strict=False)
    assert s.status.pending  # unchanged, no error


def test_accepted_file_stays() -> None:
    s = make_sfinfo()
    apply_policy(s, {"fmt/43": ACCEPTED}, WS, LogTables(), strict=False)
    assert not s.status.pending


def test_not_accepted_marks_pending() -> None:
    s = make_sfinfo()
    apply_policy(s, {"fmt/43": CONVERT}, WS, LogTables(), strict=False)
    assert s.status.pending


def test_missing_policy_non_strict_is_skipped() -> None:
    s = make_sfinfo()
    apply_policy(s, {}, WS, LogTables(), strict=False)
    assert not s.status.pending
    assert any(PLMsg.SKIPPED in log.msg for log in s.processing_logs)


def test_missing_policy_strict_calls_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[Any] = []
    monkeypatch.setattr(policies_mod, "remove", lambda sfinfo, ws, lt: removed.append(sfinfo))
    s = make_sfinfo()
    apply_policy(s, {}, WS, LogTables(), strict=True)
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
        apply_policy(s, {"fmt/199": ACCEPTED}, WS, LogTables(), strict=False)
        assert not s.status.pending

    def test_mp4_with_wrong_codec_is_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(
            monkeypatch,
            [{"codec_type": "video", "codec_name": "hevc"}, {"codec_type": "audio", "codec_name": "aac"}],
        )
        s = make_sfinfo("v.mp4", puid="fmt/199", mime="video/mp4")
        apply_policy(s, {"fmt/199": ACCEPTED}, WS, LogTables(), strict=False)
        assert s.status.pending

    def test_mkv_with_ffv1_not_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(monkeypatch, [{"codec_type": "video", "codec_name": "ffv1"}])
        s = make_sfinfo("v.mkv", puid="fmt/569", mime="video/x-matroska")
        apply_policy(s, {"fmt/569": ACCEPTED}, WS, LogTables(), strict=False)
        assert not s.status.pending

    def test_mkv_without_ffv1_is_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(monkeypatch, [{"codec_type": "video", "codec_name": "h264"}])
        s = make_sfinfo("v.mkv", puid="fmt/569", mime="video/x-matroska")
        apply_policy(s, {"fmt/569": ACCEPTED}, WS, LogTables(), strict=False)
        assert s.status.pending

    def test_unprobeable_av_file_is_not_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_streams(monkeypatch, None)
        s = make_sfinfo("v.mp4", puid="fmt/199", mime="video/mp4")
        apply_policy(s, {"fmt/199": ACCEPTED}, WS, LogTables(), strict=False)
        assert not s.status.pending

    def test_mp4_ignores_non_av_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # a subtitle stream must be skipped, not treated as a codec violation
        self._patch_streams(
            monkeypatch,
            [
                {"codec_type": "subtitle", "codec_name": "mov_text"},
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        )
        s = make_sfinfo("v.mp4", puid="fmt/199", mime="video/mp4")
        apply_policy(s, {"fmt/199": ACCEPTED}, WS, LogTables(), strict=False)
        assert not s.status.pending


class TestBuildPolicies:
    def test_blank_makes_one_accepted_entry_per_puid(self) -> None:
        policies, blank = build_policies(["fmt/43", "fmt/11"], {}, Mode(), blank=True)
        assert set(policies) == {"fmt/43", "fmt/11"}
        assert all(p.accepted for p in policies.values())
        assert blank == []  # blank generation does not track a blank list

    def test_blank_respects_remove_original(self) -> None:
        policies, _ = build_policies(["fmt/43"], {}, Mode(REMOVEORIGINAL=True), blank=True)
        assert policies["fmt/43"].remove_original is True

    def test_default_maps_known_puid(self) -> None:
        defaults = {"fmt/43": PolicyParams(format_name="JPEG from default", accepted=True)}
        policies, blank = build_policies(["fmt/43"], defaults, Mode())
        assert policies["fmt/43"].format_name == "JPEG from default"
        assert blank == []

    def test_unknown_puid_gets_blank_fallback_when_not_strict(self) -> None:
        unknown = _unknown_puid()
        policies, blank = build_policies([unknown], {}, Mode())
        assert policies[unknown].accepted is True
        assert blank == [unknown]

    def test_strict_drops_unknown_puid(self) -> None:
        unknown = _unknown_puid()
        policies, blank = build_policies([unknown], {}, Mode(STRICT=True))
        assert unknown not in policies
        assert blank == []

    def test_extend_keeps_existing_and_unblanks(self) -> None:
        unknown = _unknown_puid()
        existing = {unknown: PolicyParams(format_name="hand-tuned")}
        policies, blank = build_policies([unknown], {}, Mode(), extend=True, existing=existing)
        assert policies[unknown].format_name == "hand-tuned"
        assert blank == []  # promoted out of the blank list

    def test_remove_original_propagates_to_default_policy(self) -> None:
        defaults = {"fmt/43": PolicyParams(format_name="JPEG", accepted=True)}
        policies, _ = build_policies(["fmt/43"], defaults, Mode(REMOVEORIGINAL=True))
        assert policies["fmt/43"].remove_original is True

"""Unit tests for tasks.inspection.

inspect_file / assert_file_integrity / _rename / _has_error all shell out to
ffmpeg / imagemagick in real use. Here the MediaTool probe (via the underlying
``tools.ffmpeg_collect_warnings`` / ``tools.imagemagick_collect_warnings``) and the
side-effecting helpers (``remove``, ``_rename``, ``inspect_file``) are monkeypatched so
the branch logic is exercised without any external binary or (except for ``_rename``) filesystem.
"""

from pathlib import Path
from typing import Any

import pytest

from fileidentification.definitions.models import PolicyParams, RunJournal
from fileidentification.definitions.settings import Bin, FDMsg, FPMsg, REencMsg
from fileidentification.tasks import inspection as insp
from fileidentification.tasks.inspection import _has_error, _rename, assert_file_integrity, inspect_file
from fileidentification.wrappers import tools
from fileidentification.wrappers.tools import tool_for
from tests.conftest import make_sfinfo, make_ws

WS = make_ws()


class TestInspectFileBinSelection:
    """inspect_file picks the probing tool from the policy, then the siegfried mime, then FMT2EXT."""

    @staticmethod
    def _capture_tool(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_has_error(sfinfo: Any, tool: Any, ws: Any, journal: Any, verbose: bool) -> bool:
            captured["tool"] = tool
            return False

        monkeypatch.setattr(insp, "_has_error", fake_has_error)
        return captured

    def test_bin_from_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._capture_tool(monkeypatch)
        s = make_sfinfo(puid="fmt/43", mime="image/jpeg")
        inspect_file(s, {"fmt/43": PolicyParams(format_name="x", bin="ffmpeg")}, WS, RunJournal(), verbose=False)
        assert captured["tool"].bin == Bin.FFMPEG  # policy wins over the image mime

    def test_bin_from_siegfried_mime_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._capture_tool(monkeypatch)
        s = make_sfinfo(puid="fmt/43", mime="image/jpeg")
        inspect_file(s, {}, WS, RunJournal(), verbose=False)
        assert captured["tool"].bin == Bin.MAGICK
        assert any("bin not specified" in log.msg for log in s.processing_logs)

    def test_bin_from_siegfried_mime_video(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._capture_tool(monkeypatch)
        s = make_sfinfo(puid="fmt/199", mime="video/mp4")
        inspect_file(s, {}, WS, RunJournal(), verbose=False)
        assert captured["tool"].bin == Bin.FFMPEG

    def test_bin_from_fmt2ext_when_no_siegfried_mime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._capture_tool(monkeypatch)
        # empty siegfried mime forces the FMT2EXT fallback; fmt/43 is image/jpeg there
        s = make_sfinfo(puid="fmt/43", mime="")
        inspect_file(s, {}, WS, RunJournal(), verbose=False)
        assert captured["tool"].bin == Bin.MAGICK

    def test_no_bin_when_no_mime_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._capture_tool(monkeypatch)
        # fmt/569 (Matroska) has no mime key in FMT2EXT and we pass no siegfried mime
        s = make_sfinfo(puid="fmt/569", mime="")
        inspect_file(s, {}, WS, RunJournal(), verbose=False)
        assert captured["tool"] is None


class TestInspectFileOutcomes:
    def test_no_puid_records_processing_error(self) -> None:
        s = make_sfinfo(puid="UNKNOWN", warning="no match")  # processed_as is None
        lt = RunJournal()
        assert inspect_file(s, {}, WS, lt, verbose=False) is None
        assert len(lt.processing_errors) == 1
        assert FPMsg.PUIDFAIL in lt.processing_errors[0][0].msg

    def test_empty_source_is_flagged_but_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(insp, "_has_error", lambda *a, **k: False)
        s = make_sfinfo(puid="fmt/43", mime="image/jpeg")
        s.errors = FDMsg.EMPTYSOURCE
        assert inspect_file(s, {}, WS, RunJournal(), verbose=False) is None
        assert any(FDMsg.EMPTYSOURCE in log.msg for log in s.processing_logs)

    def test_extension_mismatch_is_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(insp, "_has_error", lambda *a, **k: False)
        s = make_sfinfo(puid="fmt/43", mime="image/jpeg", warning=FDMsg.EXTMISMATCH)
        lt = RunJournal()
        assert inspect_file(s, {}, WS, lt, verbose=False) == FDMsg.EXTMISMATCH
        assert FDMsg.EXTMISMATCH.name in lt.diagnostics
        assert any("expecting one of the following ext" in log.msg for log in s.processing_logs)

    def test_corrupt_short_circuits_to_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(insp, "_has_error", lambda *a, **k: True)
        # even with an ext-mismatch warning, a corrupt file returns ERROR first
        s = make_sfinfo(puid="fmt/43", mime="image/jpeg", warning=FDMsg.EXTMISMATCH)
        assert inspect_file(s, {}, WS, RunJournal(), verbose=False) == FDMsg.ERROR


class TestHasError:
    """_has_error probes with the given MediaTool and interprets the collected warnings."""

    def _patch_ffmpeg(self, monkeypatch: pytest.MonkeyPatch, ret: tuple[bool, str, str]) -> None:
        monkeypatch.setattr(tools, "ffmpeg_collect_warnings", lambda path, verbose: ret)

    def _patch_magick(self, monkeypatch: pytest.MonkeyPatch, ret: tuple[bool, str, str]) -> None:
        monkeypatch.setattr(tools, "imagemagick_collect_warnings", lambda path, verbose: ret)

    def test_ffmpeg_reencode_flag_sets_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_ffmpeg(monkeypatch, (False, str(REencMsg.ffmpeg1), ""))
        s = make_sfinfo("v.mp4", puid="fmt/199")
        assert _has_error(s, tool_for(Bin.FFMPEG), WS, RunJournal(), verbose=False) is False
        assert s.status.pending is True
        assert any("reencoding" in log.msg for log in s.processing_logs)

    def test_ffmpeg_error_is_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_ffmpeg(monkeypatch, (True, "boom", "specs"))
        s = make_sfinfo("v.mp4", puid="fmt/199")
        lt = RunJournal()
        assert _has_error(s, tool_for(Bin.FFMPEG), WS, lt, verbose=False) is True
        assert s.media_info and s.media_info[0].msg == "specs"
        assert any(log.msg == "boom" for log in s.processing_logs)
        assert FDMsg.ERROR.name in lt.diagnostics

    def test_ffmpeg_warning_but_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_ffmpeg(monkeypatch, (False, "just a warning", ""))
        s = make_sfinfo("v.mp4", puid="fmt/199")
        lt = RunJournal()
        assert _has_error(s, tool_for(Bin.FFMPEG), WS, lt, verbose=False) is False
        assert any(log.msg == "just a warning" for log in s.processing_logs)
        assert FDMsg.WARNING.name in lt.diagnostics
        assert s.status.pending is False

    def test_magick_error_is_corrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_magick(monkeypatch, (True, "identify: Cannot read", "specs"))
        s = make_sfinfo("i.jpg", puid="fmt/43")
        assert _has_error(s, tool_for(Bin.MAGICK), WS, RunJournal(), verbose=False) is True

    def test_soffice_and_empty_bin_are_noops(self) -> None:
        s = make_sfinfo("d.docx", puid="fmt/412")
        assert _has_error(s, tool_for(Bin.SOFFICE), WS, RunJournal(), verbose=False) is False
        assert _has_error(s, tool_for(""), WS, RunJournal(), verbose=False) is False
        assert not s.processing_logs and not s.media_info

    def test_specs_not_overwritten_when_media_info_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fileidentification.definitions.models import LogMsg

        self._patch_ffmpeg(monkeypatch, (False, "", "new-specs"))
        s = make_sfinfo("v.mp4", puid="fmt/199")
        s.media_info.append(LogMsg(name="ffmpeg", msg="pre-existing"))
        _has_error(s, tool_for(Bin.FFMPEG), WS, RunJournal(), verbose=False)
        assert len(s.media_info) == 1  # existing media_info left untouched


class TestRename:
    def _staged(self, tmp_path: Path, name: str = "clip") -> tuple[Any, Any]:
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / name).write_bytes(b"data")
        s = make_sfinfo(f"sub/{name}", md5="abcdef0000")
        return s, make_ws(root, tmp_path / "tdir")

    def test_renames_to_extension(self, tmp_path: Path) -> None:
        s, ws = self._staged(tmp_path)
        _rename(s, ".avi", ws, RunJournal())
        assert (tmp_path / "root" / "sub" / "clip.avi").is_file()
        assert s.filename == Path("sub/clip.avi")
        assert ws.abs_path(s.filename) == tmp_path / "root" / "sub" / "clip.avi"
        assert any("did rename" in log.msg for log in s.processing_logs)

    def test_collision_appends_md5(self, tmp_path: Path) -> None:
        s, ws = self._staged(tmp_path)
        (tmp_path / "root" / "sub" / "clip.avi").write_bytes(b"pre-existing")  # occupy the target name
        _rename(s, ".avi", ws, RunJournal())
        assert (tmp_path / "root" / "sub" / "clip_abcdef.avi").is_file()
        assert (tmp_path / "root" / "sub" / "clip.avi").read_bytes() == b"pre-existing"
        assert s.filename == Path("sub/clip_abcdef.avi")

    def test_oserror_is_recorded(self, tmp_path: Path) -> None:
        s = make_sfinfo("sub/gone", md5="abcdef0000")  # abs_path resolves under root but was never created
        ws = make_ws(tmp_path, tmp_path / "tdir")
        lt = RunJournal()
        _rename(s, ".avi", ws, lt)
        assert lt.processing_errors
        assert not any("did rename" in log.msg for log in s.processing_logs)


class TestAssertFileIntegrity:
    """assert_file_integrity acts on the inspect_file verdict: remove / rename / warn / noop."""

    @staticmethod
    def _spy(monkeypatch: pytest.MonkeyPatch, verdict: FDMsg | None) -> dict[str, Any]:
        calls: dict[str, Any] = {"removed": [], "renamed": []}
        monkeypatch.setattr(insp, "inspect_file", lambda *a, **k: verdict)
        monkeypatch.setattr(insp, "remove", lambda sfinfo, ws, lt: calls["removed"].append(sfinfo))
        monkeypatch.setattr(insp, "_rename", lambda sfinfo, ext, ws, lt: calls["renamed"].append(ext))
        return calls

    def test_corrupt_is_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch, FDMsg.ERROR)
        s = make_sfinfo(puid="fmt/43")
        assert_file_integrity(s, {}, WS, RunJournal(), verbose=False)
        assert calls["removed"] == [s]
        assert not calls["renamed"]

    def test_single_extension_is_autorenamed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch, FDMsg.EXTMISMATCH)
        s = make_sfinfo(puid="fmt/5")  # AVI: exactly one known extension
        assert_file_integrity(s, {}, WS, RunJournal(), verbose=False)
        assert calls["renamed"] == [".avi"]
        assert not calls["removed"]

    def test_multiple_extensions_only_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fileidentification.definitions.models import LogMsg

        calls = self._spy(monkeypatch, FDMsg.EXTMISMATCH)
        s = make_sfinfo(puid="fmt/43")  # JPEG: several known extensions
        s.processing_logs.append(LogMsg(name="filehandler", msg="expecting one of ..."))
        assert_file_integrity(s, {}, WS, RunJournal(), verbose=False)
        assert not calls["renamed"]
        assert not calls["removed"]

    def test_clean_file_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy(monkeypatch, None)
        s = make_sfinfo(puid="fmt/43")
        assert_file_integrity(s, {}, WS, RunJournal(), verbose=False)
        assert not calls["removed"] and not calls["renamed"]

"""Unit tests for FileHandler orchestration that does not need real tooling.

The heavy steps (convert / remove_tmp) are replaced with recorders so the tests
assert on control flow and mode handling rather than on actual conversions.
"""

from pathlib import Path

import pytest

from fileidentification.filehandling import FileHandler
from tests.conftest import make_sfinfo


class TestSilentlyReencode:
    """`-i` without `-a` triggers a quiet, original-replacing re-encode pass."""

    def test_forces_quiet_and_remove_original_then_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        calls: list[str] = []
        monkeypatch.setattr(fh, "convert", lambda: calls.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda root: calls.append(f"remove_tmp:{root}"))

        fh._silently_reencode(Path("/some/root"))

        assert fh.mode.QUIET is True
        assert fh.mode.REMOVEORIGINAL is True
        assert calls == ["convert", "remove_tmp:/some/root"]


class TestConvertNoPending:
    def test_convert_is_noop_without_pending_files(self) -> None:
        fh = FileHandler()
        fh.stack = [make_sfinfo("a.jpg"), make_sfinfo("b.jpg")]  # none pending
        # must not raise and must not touch the (empty) policies dict
        fh.convert()
        assert all(not s.status.added for s in fh.stack)


class TestRunTriggersReencode:
    """run() with assert_integrity=True and apply=False must call _silently_reencode."""

    def test_reencode_called_when_i_without_a(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fh = FileHandler()
        order: list[str] = []
        # stub out every heavy step so we only observe the branch logic in run()
        monkeypatch.setattr("fileidentification.filehandling.set_filepaths", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_load_sfinfos", lambda root: order.append("load"))
        monkeypatch.setattr(fh, "_manage_policies", lambda *a, **k: order.append("policies"))
        monkeypatch.setattr(fh, "assert_integrity", lambda: order.append("assert"))
        monkeypatch.setattr(fh, "_silently_reencode", lambda root: order.append("reencode"))
        monkeypatch.setattr(fh, "apply_policies", lambda: order.append("apply"))
        monkeypatch.setattr(fh, "convert", lambda: order.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda root: order.append("remove_tmp"))
        monkeypatch.setattr(fh, "write_logs", lambda to_csv=False: order.append("logs"))

        fh.run(root_folder=tmp_path, assert_integrity=True, apply=False, remove_tmp=False)

        assert "assert" in order
        assert "reencode" in order
        assert "apply" not in order  # apply was False

    def test_reencode_not_called_when_apply_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fh = FileHandler()
        order: list[str] = []
        monkeypatch.setattr("fileidentification.filehandling.set_filepaths", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_load_sfinfos", lambda root: None)
        monkeypatch.setattr(fh, "_manage_policies", lambda *a, **k: None)
        monkeypatch.setattr(fh, "assert_integrity", lambda: order.append("assert"))
        monkeypatch.setattr(fh, "_silently_reencode", lambda root: order.append("reencode"))
        monkeypatch.setattr(fh, "apply_policies", lambda: order.append("apply"))
        monkeypatch.setattr(fh, "convert", lambda: order.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda root: None)
        monkeypatch.setattr(fh, "write_logs", lambda to_csv=False: None)

        fh.run(root_folder=tmp_path, assert_integrity=True, apply=True, remove_tmp=False)

        assert "reencode" not in order
        assert "apply" in order

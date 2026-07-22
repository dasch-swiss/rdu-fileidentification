"""Unit tests for filesystem helpers in tasks.os_tasks."""

from pathlib import Path

import pytest

from fileidentification.definitions.models import PolicyParams, RunJournal, SfInfo
from fileidentification.definitions.settings import RMV_DIR
from fileidentification.tasks.os_tasks import move_tmp, remove
from fileidentification.workspace import Workspace
from tests.conftest import make_sfinfo, make_ws


class TestRemove:
    def test_moves_file_to_removed_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        f = root / "bad.mp4"
        f.write_bytes(b"data")
        ws = make_ws(root, tmp_path / "tdir")
        s = make_sfinfo("bad.mp4")

        remove(s, ws, RunJournal())

        assert s.status.removed
        assert not f.exists()
        assert (ws.tmp_dir / RMV_DIR / "bad.mp4").is_file()

    def test_missing_source_records_processing_error(self, tmp_path: Path) -> None:
        ws = make_ws(tmp_path / "root", tmp_path / "tdir")
        s = make_sfinfo("gone.mp4")  # abs_path resolves under root but the file was never created
        lt = RunJournal()

        remove(s, ws, lt)

        assert not s.status.removed
        assert lt.processing_errors  # the OSError was captured


class TestMoveTmp:
    """move_tmp relocates converted files from the working dir next to their originals."""

    @staticmethod
    def _scenario(
        tmp_path: Path, *, conv_md5: str = "bbbbbb", remove_in_policy: bool = False
    ) -> tuple[list[SfInfo], SfInfo, SfInfo, dict[str, PolicyParams], Path, Workspace]:
        """Lay out a realistic post-conversion state on disk.

        root/sub/orig.jpg                             original file
        tdir/orig.jpg_<hash>/orig.tif                 converted file, in its working dir (to be moved)

        The converted SfInfo's filename is already its relative home (sub/orig.tif); the physical file
        lives at ws.working_file(original.filename, "orig.tif"). Returns (stack, original, converted,
        policies, root, ws).
        """
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "orig.jpg").write_bytes(b"original")

        ws = make_ws(root, tmp_path / "tdir")

        original = make_sfinfo("sub/orig.jpg", puid="fmt/43", md5="aaaaaa")

        converted = make_sfinfo("sub/orig.tif", puid="fmt/353", md5=conv_md5, mime="image/tiff")
        converted.dest = Path("sub")
        converted.derived_from = original

        # place the physical converted file where move_tmp will look for it
        workfile = ws.working_file(original.filename, "orig.tif")
        workfile.parent.mkdir(parents=True)
        workfile.write_bytes(b"converted")

        policies = {
            "fmt/43": PolicyParams(
                accepted=False,
                bin="magick",
                target_container="tif",
                expected=["fmt/353"],
                remove_original=remove_in_policy,
            )
        }
        return [original, converted], original, converted, policies, root, ws

    def test_moves_converted_file_next_to_original(self, tmp_path: Path) -> None:
        stack, original, converted, policies, root, ws = self._scenario(tmp_path)
        workdir = ws.working_file(original.filename, "orig.tif").parent

        moved = move_tmp(stack, ws, policies, RunJournal(), remove_original=False)

        assert moved is True
        assert (root / "sub" / "orig.tif").is_file()
        assert not workdir.exists()  # working dir cleaned up
        assert converted.status.added is True
        assert converted.dest is None
        assert converted.filename == Path("sub/orig.tif")
        assert ws.abs_path(original.filename).is_file()  # original kept (no remove flag)

    def test_filename_collision_appends_md5(self, tmp_path: Path) -> None:
        stack, _original, converted, policies, root, ws = self._scenario(tmp_path, conv_md5="abc123def")
        # a file already sits at the destination name
        existing = root / "sub" / "orig.tif"
        existing.write_bytes(b"pre-existing")

        move_tmp(stack, ws, policies, RunJournal(), remove_original=False)

        assert existing.read_bytes() == b"pre-existing"  # untouched
        assert (root / "sub" / "orig_abc123.tif").is_file()  # md5[:6] suffix
        assert converted.filename == Path("sub/orig_abc123.tif")

    def test_remove_original_flag_quarantines_source(self, tmp_path: Path) -> None:
        stack, original, _converted, policies, root, ws = self._scenario(tmp_path)

        move_tmp(stack, ws, policies, RunJournal(), remove_original=True)

        assert original.status.removed is True
        assert not ws.abs_path(original.filename).exists()
        assert (ws.tmp_dir / RMV_DIR / "sub" / "orig.jpg").is_file()
        assert (root / "sub" / "orig.tif").is_file()  # conversion still placed

    def test_policy_remove_original_quarantines_even_without_flag(self, tmp_path: Path) -> None:
        stack, original, _converted, policies, _root, ws = self._scenario(tmp_path, remove_in_policy=True)

        move_tmp(stack, ws, policies, RunJournal(), remove_original=False)

        assert original.status.removed is True
        assert not ws.abs_path(original.filename).exists()

    def test_nothing_to_move_returns_false(self) -> None:
        plain = make_sfinfo("a.jpg")  # no dest -> not a converted file
        assert move_tmp([plain], make_ws(), {}, RunJournal(), remove_original=False) is False

    def test_move_failure_records_processing_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stack, _original, converted, policies, _root, ws = self._scenario(tmp_path)

        def boom(*_a: object, **_k: object) -> None:
            raise OSError

        monkeypatch.setattr("fileidentification.tasks.os_tasks.shutil.move", boom)
        lt = RunJournal()

        move_tmp(stack, ws, policies, lt, remove_original=False)

        assert lt.processing_errors  # the OSError was captured
        assert not converted.status.added  # move did not complete

"""Unit tests for filesystem helpers in tasks.os_tasks."""

from pathlib import Path

import pytest

from fileidentification.definitions.models import FilePaths, LogTables, PolicyParams, SfInfo
from fileidentification.definitions.settings import LOGJSON, POLJSON, RMV_DIR, TMP_DIR
from fileidentification.tasks.os_tasks import move_tmp, remove, set_filepaths
from tests.conftest import make_sfinfo


class TestSetFilepaths:
    def test_directory_root(self, tmp_path: Path) -> None:
        fp = FilePaths()
        set_filepaths(fp, tmp_path)
        assert fp.TMP_DIR == tmp_path / TMP_DIR
        assert fp.TMP_DIR.is_dir()
        assert fp.LOGJSON == fp.TMP_DIR / LOGJSON
        assert fp.POLJSON == fp.TMP_DIR / POLJSON

    def test_file_root_uses_stem(self, tmp_path: Path) -> None:
        f = tmp_path / "movie.mp4"
        f.write_bytes(b"x")
        fp = FilePaths()
        set_filepaths(fp, f)
        assert fp.TMP_DIR == tmp_path / "movie"
        assert fp.TMP_DIR.is_dir()

    def test_custom_tmp_dir_overrides(self, tmp_path: Path) -> None:
        custom = tmp_path / "elsewhere"
        fp = FilePaths()
        set_filepaths(fp, tmp_path, tmp_dir=custom)
        assert fp.TMP_DIR == custom
        assert custom.is_dir()

    def test_nonexistent_root_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            set_filepaths(FilePaths(), tmp_path / "does-not-exist")

    def test_dot_root_exits(self) -> None:
        with pytest.raises(SystemExit):
            set_filepaths(FilePaths(), Path())


class TestRemove:
    def test_moves_file_to_removed_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        f = root / "bad.mp4"
        f.write_bytes(b"data")
        s = make_sfinfo("bad.mp4")
        s.path = f
        s.filename = Path("bad.mp4")
        s.tdir = tmp_path / "tdir"

        remove(s, LogTables())

        assert s.status.removed
        assert not f.exists()
        assert (s.tdir / RMV_DIR / "bad.mp4").is_file()

    def test_missing_source_records_processing_error(self, tmp_path: Path) -> None:
        s = make_sfinfo("gone.mp4")
        s.path = tmp_path / "gone.mp4"  # never created
        s.filename = Path("gone.mp4")
        s.tdir = tmp_path / "tdir"
        lt = LogTables()

        remove(s, lt)

        assert not s.status.removed
        assert lt.processing_errors  # the OSError was captured


class TestMoveTmp:
    """move_tmp relocates converted files from the working dir next to their originals."""

    @staticmethod
    def _scenario(
        tmp_path: Path, *, conv_md5: str = "bbbbbb", remove_in_policy: bool = False
    ) -> tuple[list[SfInfo], SfInfo, SfInfo, dict[str, PolicyParams], Path]:
        """Lay out a realistic post-conversion state on disk.

        root/sub/orig.jpg          original file
        work/orig.jpg_xxxxxx/orig.tif   converted file (to be moved)

        Returns (stack, original, converted, policies, root).
        """
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        orig_path = root / "sub" / "orig.jpg"
        orig_path.write_bytes(b"original")

        workdir = tmp_path / "work" / "orig.jpg_xxxxxx"
        workdir.mkdir(parents=True)
        conv_path = workdir / "orig.tif"
        conv_path.write_bytes(b"converted")

        original = make_sfinfo("sub/orig.jpg", puid="fmt/43", md5="aaaaaa")
        original.path = orig_path
        original.root_folder = root
        original.tdir = tmp_path / "tdir"

        converted = make_sfinfo(conv_path, puid="fmt/353", md5=conv_md5, mime="image/tiff")
        converted.filename = conv_path  # source path for the move
        converted.dest = Path("sub")
        converted.derived_from = original
        converted.root_folder = root

        policies = {
            "fmt/43": PolicyParams(
                accepted=False,
                bin="magick",
                target_container="tif",
                expected=["fmt/353"],
                remove_original=remove_in_policy,
            )
        }
        return [original, converted], original, converted, policies, root

    def test_moves_converted_file_next_to_original(self, tmp_path: Path) -> None:
        stack, original, converted, policies, root = self._scenario(tmp_path)
        workdir = converted.filename.parent

        moved = move_tmp(stack, policies, LogTables(), remove_original=False)

        assert moved is True
        assert (root / "sub" / "orig.tif").is_file()
        assert not workdir.exists()  # working dir cleaned up
        assert converted.status.added is True
        assert converted.dest is None
        assert converted.filename == Path("sub/orig.tif")
        assert original.path.is_file()  # original kept (no remove flag)

    def test_filename_collision_appends_md5(self, tmp_path: Path) -> None:
        stack, _original, converted, policies, root = self._scenario(tmp_path, conv_md5="abc123def")
        # a file already sits at the destination name
        existing = root / "sub" / "orig.tif"
        existing.write_bytes(b"pre-existing")

        move_tmp(stack, policies, LogTables(), remove_original=False)

        assert existing.read_bytes() == b"pre-existing"  # untouched
        assert (root / "sub" / "orig_abc123.tif").is_file()  # md5[:6] suffix
        assert converted.filename == Path("sub/orig_abc123.tif")

    def test_remove_original_flag_quarantines_source(self, tmp_path: Path) -> None:
        stack, original, _converted, policies, root = self._scenario(tmp_path)

        move_tmp(stack, policies, LogTables(), remove_original=True)

        assert original.status.removed is True
        assert not original.path.exists()
        assert (original.tdir / RMV_DIR / "sub" / "orig.jpg").is_file()
        assert (root / "sub" / "orig.tif").is_file()  # conversion still placed

    def test_policy_remove_original_quarantines_even_without_flag(self, tmp_path: Path) -> None:
        stack, original, _converted, policies, _root = self._scenario(tmp_path, remove_in_policy=True)

        move_tmp(stack, policies, LogTables(), remove_original=False)

        assert original.status.removed is True
        assert not original.path.exists()

    def test_nothing_to_move_returns_false(self) -> None:
        plain = make_sfinfo("a.jpg")  # no dest -> not a converted file
        assert move_tmp([plain], {}, LogTables(), remove_original=False) is False

    def test_move_failure_records_processing_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stack, _original, converted, policies, _root = self._scenario(tmp_path)

        def boom(*_a: object, **_k: object) -> None:
            raise OSError

        monkeypatch.setattr("fileidentification.tasks.os_tasks.shutil.move", boom)
        lt = LogTables()

        move_tmp(stack, policies, lt, remove_original=False)

        assert lt.processing_errors  # the OSError was captured
        assert not converted.status.added  # move did not complete

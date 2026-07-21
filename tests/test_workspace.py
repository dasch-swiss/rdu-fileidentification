"""Unit tests for the Workspace path calculator.

Pure path arithmetic — no disk is touched except the single-file-target case (which needs a real file
so `is_file()` fires) and nothing shells out. Covers the portability, cross-volume tmp, and
duplicate-basename cases the refactor depends on.
"""

from pathlib import Path

from fileidentification.workspace import Workspace


def _ws(root: str = "/data/root", tdir: str = "/data/root/__fileidentification") -> Workspace:
    return Workspace(Path(root), Path(tdir))


class TestRelativize:
    def test_makes_scanned_path_relative_to_root(self) -> None:
        assert _ws().relativize(Path("/data/root/sub/a.jpg")) == Path("sub/a.jpg")

    def test_top_level_file(self) -> None:
        assert _ws().relativize(Path("/data/root/a.jpg")) == Path("a.jpg")


class TestAbsPath:
    def test_joins_root_and_relative(self) -> None:
        assert _ws().abs_path(Path("sub/a.jpg")) == Path("/data/root/sub/a.jpg")

    def test_roundtrips_with_relativize(self) -> None:
        ws = _ws()
        abs_p = Path("/data/root/sub/a.jpg")
        assert ws.abs_path(ws.relativize(abs_p)) == abs_p


class TestWorkingDir:
    def test_under_tdir_with_hashed_basename(self) -> None:
        wd = _ws().working_dir(Path("sub/a.jpg"))
        assert wd.parent == Path("/data/root/__fileidentification")
        assert wd.name.startswith("a.jpg_")
        suffix = wd.name.rsplit("_", 1)[1]
        assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)

    def test_duplicate_basename_different_path_gets_distinct_dirs(self) -> None:
        ws = _ws()
        assert ws.working_dir(Path("a/clip.mp4")) != ws.working_dir(Path("b/clip.mp4"))

    def test_is_deterministic(self) -> None:
        ws = _ws()
        assert ws.working_dir(Path("sub/a.jpg")) == ws.working_dir(Path("sub/a.jpg"))


class TestWorkingFile:
    def test_inside_origin_working_dir(self) -> None:
        ws = _ws()
        origin = Path("sub/orig.jpg")
        assert ws.working_file(origin, "orig.tif") == ws.working_dir(origin) / "orig.tif"


class TestRemovedDest:
    def test_under_removed_preserving_subpath(self) -> None:
        assert _ws().removed_dest(Path("sub/a.jpg")) == Path("/data/root/__fileidentification/_REMOVED/sub/a.jpg")


class TestCrossVolumeTdir:
    def test_tdir_outside_root_is_independent(self) -> None:
        # --tmp-dir on a different volume: abs paths resolve under root, working paths under tdir
        ws = Workspace(Path("/data/root"), Path("/mnt/external/tmp"))
        assert ws.abs_path(Path("sub/a.jpg")) == Path("/data/root/sub/a.jpg")
        assert ws.working_dir(Path("sub/a.jpg")).parent == Path("/mnt/external/tmp")
        assert ws.removed_dest(Path("a.jpg")) == Path("/mnt/external/tmp/_REMOVED/a.jpg")


class TestSingleFileTarget:
    def test_root_folder_normalized_to_parent(self, tmp_path: Path) -> None:
        f = tmp_path / "solo.jpg"
        f.write_bytes(b"x")
        ws = Workspace(f, tmp_path / "tmp")
        assert ws.root_folder == tmp_path
        assert ws.abs_path(Path("solo.jpg")) == tmp_path / "solo.jpg"

    def test_directory_target_is_left_as_is(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path, tmp_path / "tmp")
        assert ws.root_folder == tmp_path

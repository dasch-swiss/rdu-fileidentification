"""Unit tests for the Workspace path module.

The frozen dataclass is pure path arithmetic — no disk, no shelling out — so it is constructed directly.
The `for_run` factory is the impure entry point (validate root, normalize a single-file target, create the
tmp dir); its tests touch a real tmp dir. Covers the portability, cross-volume tmp, and duplicate-basename
cases the refactor depends on.
"""

from pathlib import Path

import pytest

from fileidentification.definitions.settings import LOGJSON, POLJSON, TMP_DIR
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


class TestDerivedPaths:
    def test_logjson_and_poljson_derive_from_tmp_dir(self) -> None:
        ws = Workspace(Path("/data/root"), Path("/data/root/__fileidentification"))
        assert ws.logjson == Path("/data/root/__fileidentification") / LOGJSON
        assert ws.poljson == Path("/data/root/__fileidentification") / POLJSON

    def test_report_json_is_dated_under_tmp_dir(self) -> None:
        ws = Workspace(Path("/data/root"), Path("/data/root/__fileidentification"))
        assert ws.report_json("240101") == Path("/data/root/__fileidentification/240101_report.json")


class TestForRun:
    def test_directory_root_defaults_tmp_and_creates_it(self, tmp_path: Path) -> None:
        ws = Workspace.for_run(tmp_path)
        assert ws.root_folder == tmp_path
        assert ws.tmp_dir == tmp_path / TMP_DIR
        assert ws.tmp_dir.is_dir()
        assert ws.logjson == tmp_path / TMP_DIR / LOGJSON

    def test_file_root_normalizes_to_parent_and_uses_stem_tmp(self, tmp_path: Path) -> None:
        f = tmp_path / "solo.jpg"
        f.write_bytes(b"x")
        ws = Workspace.for_run(f)
        assert ws.root_folder == tmp_path  # single file -> parent is the root
        assert ws.tmp_dir == tmp_path / "solo"  # stem is the default tmp dir
        assert ws.tmp_dir.is_dir()
        assert ws.abs_path(Path("solo.jpg")) == tmp_path / "solo.jpg"

    def test_custom_tmp_dir_overrides_and_is_created(self, tmp_path: Path) -> None:
        custom = tmp_path / "elsewhere"
        ws = Workspace.for_run(tmp_path, tmp_dir=custom)
        assert ws.tmp_dir == custom
        assert custom.is_dir()

    def test_nonexistent_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="root folder not found"):
            Workspace.for_run(tmp_path / "does-not-exist")

    def test_dot_root_raises(self) -> None:
        with pytest.raises(ValueError, match="root folder not found"):
            Workspace.for_run(Path())

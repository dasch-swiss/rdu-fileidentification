"""
The Workspace: the single run-scoped path module, built once per run by `Workspace.for_run` and never mutated.

A file's portable relative `filename` (as stored in _log.json) is resolved against wherever the run currently
lives; `tmp_dir` is kept independent of `root_folder` so it can sit on another volume (--tmp-dir). The frozen
dataclass is pure path math — all I/O and the single-file decision live in `for_run`, so a plain
`Workspace(root, tmp)` is safe to build in tests.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from fileidentification.definitions.settings import LOGJSON, POLJSON, RMV_DIR, TMP_DIR


@dataclass(frozen=True)
class Workspace:
    """Maps a file's portable relative `filename` to its absolute, working, and removed locations on disk."""

    root_folder: Path
    tmp_dir: Path

    @classmethod
    def for_run(cls, root_folder: Path, tmp_dir: Path | None = None) -> "Workspace":
        """
        Resolve the workspace for a run: validate the root, make the single-file decision once, create the tmp dir.

        A single-file target lives in its parent dir, and its default tmp dir is <parent>/<stem>. A directory target
        defaults to <root>/__fileidentification. An explicit tmp_dir overrides the default (it may be on another
        volume). Raises ValueError if the root folder does not exist or is the bare current dir; the caller decides
        how to surface that (print + exit).
        """
        if root_folder.__fspath__() == "." or not root_folder.exists():
            raise ValueError(f"root folder not found: {root_folder}")  # noqa: EM102, TRY003

        if root_folder.is_file():
            root_folder, default_tmp = root_folder.parent, root_folder.parent / root_folder.stem
        else:
            default_tmp = root_folder / TMP_DIR
        tmp_dir = tmp_dir or default_tmp

        if not tmp_dir.is_dir():
            tmp_dir.mkdir(parents=True)

        return cls(root_folder, tmp_dir)

    @property
    def logjson(self) -> Path:
        """The run's cumulative log file (read by _build_stack, default write target)."""
        return self.tmp_dir / LOGJSON

    @property
    def poljson(self) -> Path:
        """The run's policies file."""
        return self.tmp_dir / POLJSON

    def report_json(self, ymd: str) -> Path:
        """Return the dated inspect-report path (a write target kept separate from a processing run's _log.json)."""
        return self.tmp_dir / f"{ymd}_report.json"

    def relativize(self, scanned: Path) -> Path:
        """Make a freshly scanned absolute path relative to root_folder (the portable form persisted in _log.json)."""
        return scanned.parent.relative_to(self.root_folder) / scanned.name

    def abs_path(self, filename: Path) -> Path:
        """Absolute location of a file given its portable relative filename."""
        return self.root_folder / filename

    def working_dir(self, filename: Path) -> Path:
        """
        Per-file conversion working dir under tmp_dir. The md5 of the relative path keeps duplicate files
        with the same basename at different paths from colliding.
        """
        path_hash = hashlib.md5(str(filename).encode()).hexdigest()[:6]  # noqa: S324
        return self.tmp_dir / f"{filename.name}_{path_hash}"

    def removed_dest(self, filename: Path) -> Path:
        """Location under _REMOVED for a corrupt / replaced file (the relative subpath is preserved)."""
        return self.tmp_dir / RMV_DIR / filename

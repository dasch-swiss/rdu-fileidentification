"""
Run-scoped path types: FilePaths (the resolved tmp dir + its two JSON files) and Workspace (the path calculator).

The Workspace is constructed fresh each run from the CLI root_folder and the tmp dir; never persisted.
Portability comes from the relative `filename` stored in _log.json — the Workspace only resolves that portable
name against wherever the run happens to be right now. `tdir` may live on a different volume (--tmp-dir /
external drive), so it is kept independent of root_folder.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from fileidentification.definitions.settings import RMV_DIR


class FilePaths(BaseModel, validate_assignment=True):
    """Resolved output paths used throughout a FileHandler run (the tmp dir and the two JSON files in it)."""

    TMP_DIR: Path = Field(default_factory=Path)
    POLJSON: Path = Field(default_factory=Path)
    LOGJSON: Path = Field(default_factory=Path)


@dataclass(frozen=True)
class Workspace:
    """Maps a file's portable relative `filename` to its absolute, working, and removed locations on disk."""

    root_folder: Path
    tdir: Path

    def __post_init__(self) -> None:
        # single-file target: the files live in the parent dir (the normalization set_processing_paths did).
        # frozen dataclass -> object.__setattr__
        if self.root_folder.is_file():
            object.__setattr__(self, "root_folder", self.root_folder.parent)

    def relativize(self, scanned: Path) -> Path:
        """Make a freshly scanned absolute path relative to root_folder (the portable form persisted in _log.json)."""
        return scanned.parent.relative_to(self.root_folder) / scanned.name

    def abs_path(self, filename: Path) -> Path:
        """Absolute location of a file given its portable relative filename."""
        return self.root_folder / filename

    def working_dir(self, filename: Path) -> Path:
        """
        Per-file conversion working dir under tdir. The md5 of the relative path keeps duplicate files
        with the same basename at different paths from colliding.
        """
        path_hash = hashlib.md5(str(filename).encode()).hexdigest()[:6]  # noqa: S324
        return self.tdir / f"{filename.name}_{path_hash}"

    def working_file(self, origin_filename: Path, output_name: str) -> Path:
        """Where a converted file physically sits before it is moved: inside its origin's working dir."""
        return self.working_dir(origin_filename) / output_name

    def removed_dest(self, filename: Path) -> Path:
        """Location under _REMOVED for a corrupt / replaced file (the relative subpath is preserved)."""
        return self.tdir / RMV_DIR / filename

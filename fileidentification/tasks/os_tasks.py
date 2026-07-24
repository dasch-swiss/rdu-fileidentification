import os
import shutil
from pathlib import Path

from fileidentification.definitions.models import LogMsg, Policies, RunJournal, SfInfo
from fileidentification.workspace import Workspace


def prune_empty_dirs(root: Path) -> None:
    """Recursively remove empty directories under `root` (bottom-up); no-op if `root` isn't a directory."""
    if not root.is_dir():
        return
    for path, _, _ in os.walk(root, topdown=False):
        if not os.listdir(path):  # noqa: PTH208
            Path(path).rmdir()


def remove(sfinfo: SfInfo, ws: Workspace, journal: RunJournal) -> None:
    """Move the file to _REMOVED under the tmp dir and mark it removed; record a processing error if the move fails."""
    dest = ws.removed_dest(sfinfo.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(ws.abs_path(sfinfo.filename), dest)
        sfinfo.status.removed = True
    except OSError as e:
        journal.record_error(LogMsg(name="filehandler", msg=str(e)), sfinfo)


def move_tmp(
    stack: list[SfInfo], ws: Workspace, policies: Policies, journal: RunJournal, remove_original: bool
) -> bool:
    """
    Move converted files from the tmp working directory next to their originals.
    If remove_original is set (or the policy has remove_original=True), the source file is moved to _REMOVED.
    Returns True if any files were moved (so the caller can report it).
    """
    by_filename = {sfinfo.filename: sfinfo for sfinfo in stack}
    moved: bool = False

    for sfinfo in stack:
        # only converted files awaiting a move carry a dest (their future home dir next to the original)
        if not sfinfo.dest:
            continue
        moved = True
        # remove the original if the origin's policy or the run flag says so
        # fetch the live stack entry to set removed flag properly for the status log
        if policies[sfinfo.derived_from.processed_as].remove_original or remove_original:  # type: ignore[union-attr, index]
            live_origin = by_filename.get(sfinfo.derived_from.filename)  # type: ignore[union-attr]
            if live_origin and ws.abs_path(live_origin.filename).is_file():
                remove(live_origin, ws, journal)
        # filename is the converted file's location in the working dir (relative to tmp_dir)
        source = ws.tmp_dir / sfinfo.filename
        abs_dest = ws.abs_path(sfinfo.dest / sfinfo.filename.name)
        # append hash to filename if the path already exists
        if abs_dest.is_file():
            abs_dest = abs_dest.parent / f"{sfinfo.filename.stem}_{sfinfo.md5[:6]}{sfinfo.filename.suffix}"
        # move the file
        try:
            shutil.move(source, abs_dest)
            # set the (possibly collision-renamed) relative path in sfinfo.filename, set flags
            sfinfo.filename = sfinfo.dest / abs_dest.name
            sfinfo.status.added = True
            sfinfo.dest = None
        except OSError as e:
            journal.record_error(LogMsg(name="filehandler", msg=str(e)), sfinfo)

    prune_empty_dirs(ws.tmp_dir)

    return moved

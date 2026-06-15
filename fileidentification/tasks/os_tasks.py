import shutil
import sys
from pathlib import Path

from typer import colors, secho

from fileidentification.definitions.models import FilePaths, LogMsg, LogTables, Policies, SfInfo
from fileidentification.definitions.settings import LOGJSON, POLJSON, RMV_DIR, TMP_DIR


def remove(sfinfo: SfInfo, log_tables: LogTables) -> None:
    """Move a file from its sfinfo path to tmp dir / _REMOVED / ..."""
    dest: Path = sfinfo.tdir / RMV_DIR / sfinfo.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(sfinfo.path, dest)
        sfinfo.status.removed = True
        #  sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="file removed"))
    except OSError as e:
        secho(f"{e}", fg=colors.RED)
        log_tables.processing_error_add(LogMsg(name="filehandler", msg=str(e)), sfinfo)


def move_tmp(stack: list[SfInfo], policies: Policies, log_tables: LogTables, remove_original: bool) -> bool:
    """
    Move converted files from the tmp working directory next to their originals.
    If remove_original is set (or the policy has remove_original=True), the source file is moved to _REMOVED.
    Returns True if any files were moved (i.e. logs should be written).
    """
    write_logs: bool = False

    for sfinfo in stack:
        # if it has a dest, it needs to be moved
        if sfinfo.dest:
            write_logs = True
            # remove the original if its mentioned and flag it accordingly
            if policies[sfinfo.derived_from.processed_as].remove_original or remove_original:  # type: ignore[index, union-attr]
                derived_from = next(sfi for sfi in stack if sfi.filename == sfinfo.derived_from.filename)  # type: ignore[union-attr]
                if derived_from.path.is_file():
                    remove(derived_from, log_tables)
            # create absolute filepath
            abs_dest = sfinfo.root_folder / sfinfo.dest / sfinfo.filename.name
            # append hash to filename if the path already exists
            if abs_dest.is_file():
                abs_dest = Path(abs_dest.parent, f"{sfinfo.filename.stem}_{sfinfo.md5[:6]}{sfinfo.filename.suffix}")
            # move the file
            try:
                shutil.move(sfinfo.filename, abs_dest)
                if sfinfo.filename.parent.is_dir():
                    shutil.rmtree(sfinfo.filename.parent)
                # set relative path in sfinfo.filename, set flags
                sfinfo.filename = sfinfo.dest / abs_dest.name
                sfinfo.status.added = True
                sfinfo.dest = None
            except OSError as e:
                secho(f"{e}", fg=colors.RED)
                log_tables.processing_error_add(LogMsg(name="filehandler", msg=str(e)), sfinfo)

    return write_logs


def set_filepaths(fp: FilePaths, root_folder: Path, tmp_dir: Path | None = None) -> None:
    """
    Resolve and create the tmp directory and set LOGJSON / POLJSON paths on fp.
    Defaults to <root_folder>/__fileidentification; if root_folder is a file, uses <parent>/<stem>.
    An explicit tmp_dir overrides the default.
    """
    # assert rootfolder
    if root_folder.__fspath__() == "." or not root_folder.exists():
        secho("root folder not found", fg=colors.RED)
        sys.exit(1)
    fp.TMP_DIR = root_folder / TMP_DIR
    # if its a file, use stem as tmp dir
    if root_folder.is_file():
        fp.TMP_DIR = root_folder.parent / root_folder.stem
    # if tmp dir is passed externally
    if tmp_dir:
        fp.TMP_DIR = tmp_dir

    if not fp.TMP_DIR.is_dir():
        fp.TMP_DIR.mkdir(parents=True)

    fp.LOGJSON = fp.TMP_DIR / LOGJSON
    fp.POLJSON = fp.TMP_DIR / POLJSON

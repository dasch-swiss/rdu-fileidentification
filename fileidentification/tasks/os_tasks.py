import shutil

from fileidentification.definitions.models import LogMsg, LogTables, Policies, SfInfo
from fileidentification.tasks.console_output import print_os_error
from fileidentification.workspace import Workspace


def remove(sfinfo: SfInfo, ws: Workspace, log_tables: LogTables) -> None:
    """Move a file from its location to tmp dir / _REMOVED / ..."""
    dest = ws.removed_dest(sfinfo.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(ws.abs_path(sfinfo.filename), dest)
        sfinfo.status.removed = True
        #  sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="file removed"))
    except OSError as e:
        print_os_error(str(e))
        log_tables.processing_error_add(LogMsg(name="filehandler", msg=str(e)), sfinfo)


def move_tmp(
    stack: list[SfInfo], ws: Workspace, policies: Policies, log_tables: LogTables, remove_original: bool
) -> bool:
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
                if ws.abs_path(derived_from.filename).is_file():
                    remove(derived_from, ws, log_tables)
            # the converted file still sits in its working dir; its final home is abs_path(filename)
            source = ws.working_file(sfinfo.derived_from.filename, sfinfo.filename.name)  # type: ignore[union-attr]
            abs_dest = ws.abs_path(sfinfo.filename)
            # append hash to filename if the path already exists
            if abs_dest.is_file():
                abs_dest = abs_dest.parent / f"{sfinfo.filename.stem}_{sfinfo.md5[:6]}{sfinfo.filename.suffix}"
            # move the file
            try:
                shutil.move(source, abs_dest)
                if source.parent.is_dir():
                    shutil.rmtree(source.parent)
                # set the (possibly collision-renamed) relative path in sfinfo.filename, set flags
                sfinfo.filename = sfinfo.dest / abs_dest.name
                sfinfo.status.added = True
                sfinfo.dest = None
            except OSError as e:
                print_os_error(str(e))
                log_tables.processing_error_add(LogMsg(name="filehandler", msg=str(e)), sfinfo)

    return write_logs

from typer import colors, secho

from fileidentification.definitions.models import LogMsg, LogTables, Policies, SfInfo
from fileidentification.definitions.settings import FMT2EXT, Bin, FDMsg, FPMsg, REencMsg
from fileidentification.tasks.os_tasks import remove
from fileidentification.wrappers.ffmpeg import ffmpeg_collect_warnings
from fileidentification.wrappers.imagemagick import imagemagick_collect_warnings


def assert_file_integrity(sfinfo: SfInfo, policies: Policies, log_tables: LogTables, verbose: bool) -> None:
    """
    Probe the file and act on the result: remove it if corrupt, rename it if the extension is wrong.
    If the format has only one known extension, the rename is done automatically;
    otherwise a manual rename warning is printed.
    """
    res: FDMsg | None = inspect_file(sfinfo, policies, log_tables, verbose)
    if res == FDMsg.ERROR:
        remove(sfinfo, log_tables)
    if res == FDMsg.EXTMISMATCH:
        if len(FMT2EXT[sfinfo.processed_as]["file_extensions"]) == 1:  # type: ignore[index]
            ext = "." + FMT2EXT[sfinfo.processed_as]["file_extensions"][-1]  # type: ignore[index]
            _rename(sfinfo, ext, log_tables)
        else:
            secho(f"\nWARNING: you should manually rename {sfinfo.filename}", fg=colors.YELLOW)
            secho(f"{sfinfo.processing_logs[0].msg}", fg=colors.YELLOW)


def inspect_file(sfinfo: SfInfo, policies: Policies, log_tables: LogTables, verbose: bool) -> FDMsg | None:
    """
    Probe the file without making any filesystem changes.
    Returns ERROR if the file is corrupt, EXTMISMATCH if the extension is wrong, or None if the file is OK.
    Populates sfinfo.media_info and sfinfo.warnings with the probe output.
    """
    if not sfinfo.processed_as:
        msg = LogMsg(name="filehandler", msg=f"{FPMsg.PUIDFAIL} for {sfinfo.filename}")
        log_tables.processing_error_add(msg, sfinfo)
        return None

    # select bin out of mimetype if not specified in policies
    pbin = policies[sfinfo.processed_as].bin if sfinfo.processed_as in policies else ""
    if pbin == "" and sfinfo.matches[0]["mime"] != "":  # noqa: SIM102
        if sfinfo.matches[0]["mime"].split("/")[0] in ["image", "audio", "video"]:
            mime = sfinfo.matches[0]["mime"].split("/")[0]
            pbin = Bin.MAGICK if mime == "image" else Bin.FFMPEG
            msgm = f"bin not specified in policies, using {pbin} according to the file mimetype for probing"
            sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=msgm))
    # check if the file throws any error, warnings while open/processing it with the respective bin
    if _has_error(sfinfo, pbin, log_tables, verbose):
        return FDMsg.ERROR

    if sfinfo.errors == FDMsg.EMPTYSOURCE:
        sfinfo.processing_logs.append(LogMsg(name="siegfried", msg=FDMsg.EMPTYSOURCE))
        secho(f"\nWARNING: {sfinfo.filename} has empty source", fg=colors.YELLOW)
        log_tables.diagnostics_add(sfinfo, FDMsg.WARNING)

    # extension mismatch
    if sfinfo.matches[0]["warning"] == FDMsg.EXTMISMATCH:
        msg_txt = f"expecting one of the following ext: {list(FMT2EXT[sfinfo.processed_as]['file_extensions'])}"
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=msg_txt))
        log_tables.diagnostics_add(sfinfo, FDMsg.EXTMISMATCH)
        return FDMsg.EXTMISMATCH

    return None


def _rename(sfinfo: SfInfo, ext: str, log_tables: LogTables) -> None:
    """
    Rename the file on disk to the given extension and update sfinfo.path and sfinfo.filename.
    If a file with the target name already exists, the MD5 prefix is appended to avoid collision.
    """
    dest = sfinfo.path.with_suffix(ext)
    # if a file with same name and extension already there, append file hash to name
    if dest.is_file():
        dest = sfinfo.path.parent / f"{sfinfo.path.stem}_{sfinfo.md5[:6]}{ext}"
    try:
        sfinfo.path.rename(dest)
        msg = f"did rename {sfinfo.path.name} -> {dest.name}"
        sfinfo.path, sfinfo.filename = dest, dest.relative_to(sfinfo.root_folder)
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=msg))
    except OSError as e:
        secho(f"{e}", fg=colors.RED)
        log_tables.processing_error_add(LogMsg(name="filehandler", msg=str(e)), sfinfo)


def _has_error(sfinfo: SfInfo, pbin: str, log_tables: LogTables, verbose: bool) -> bool:
    """
    Check if the file throws any error or warning while opening or playing.
    returns True if file is corrupt
    :param sfinfo the metadata of the file to analyse
    :param pbin the exec to probe the file
    :param log_tables the logtables
    :param verbose if true it does more detailed inspections
    """

    # get the specs and errors
    match pbin:
        case Bin.FFMPEG:
            error, stderr, specs = ffmpeg_collect_warnings(sfinfo.path, verbose=verbose)
            # see if warning needs file to be re-encoded
            if any(msg in stderr for msg in REencMsg):
                sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="file flagged for reencoding"))
                sfinfo.status.pending = True
        case Bin.MAGICK:
            error, stderr, specs = imagemagick_collect_warnings(sfinfo.path, verbose=verbose)
        case _:
            # returns False if bin is soffice or empty string (means no tests)
            # TODO: inspection for other files than Audio/Video/IMAGE
            return False

    if specs and not sfinfo.media_info:
        sfinfo.media_info.append(LogMsg(name=pbin, msg=specs))
    if error:
        sfinfo.warnings.append(LogMsg(name=pbin, msg=stderr))
        log_tables.diagnostics_add(sfinfo, FDMsg.ERROR)
        return True
    # if warnings but file is readable
    if stderr:
        sfinfo.warnings.append(LogMsg(name=pbin, msg=stderr))
        log_tables.diagnostics_add(sfinfo, FDMsg.WARNING)
    return False

from fileidentification.definitions.models import LogMsg, Policies, RunJournal, SfInfo
from fileidentification.definitions.settings import FMT2EXT, FDMsg, FPMsg
from fileidentification.tasks.os_tasks import remove
from fileidentification.workspace import Workspace
from fileidentification.wrappers.tools import MediaTool, tool_for, tool_from_mime


def assert_file_integrity(
    sfinfo: SfInfo, policies: Policies, ws: Workspace, journal: RunJournal, verbose: bool
) -> None:
    """
    Probe the file and act on the result: remove it if corrupt, rename it if the extension is wrong.
    If the format has only one known extension, the rename is done automatically;
    otherwise it is flagged in the diagnostics for a manual rename.
    """
    res: FDMsg | None = inspect_file(sfinfo, policies, ws, journal, verbose)
    if res == FDMsg.ERROR:
        remove(sfinfo, ws, journal)
    if res == FDMsg.EXTMISMATCH:
        if len(FMT2EXT[sfinfo.processed_as]["file_extensions"]) == 1:  # type: ignore[index]
            ext = "." + FMT2EXT[sfinfo.processed_as]["file_extensions"][-1]  # type: ignore[index]
            _rename(sfinfo, ext, ws, journal)
        else:
            sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="you should manually rename the file"))


def inspect_file(
    sfinfo: SfInfo, policies: Policies, ws: Workspace, journal: RunJournal, verbose: bool
) -> FDMsg | None:
    """
    Probe the file without making any filesystem changes.
    Returns ERROR if the file is corrupt, EXTMISMATCH if the extension is wrong, or None if the file is OK.
    Populates sfinfo.media_info and records any warnings / errors in the journal (which also logs them on the file).
    """
    if not sfinfo.processed_as:
        msg = LogMsg(name="filehandler", msg=f"{FPMsg.PUIDFAIL} for {sfinfo.filename}")
        journal.record_error(msg, sfinfo)
        return None

    # select the tool out of the mimetype if not specified in policies: siegfried mime first, then the FMT2EXT fallback
    tool = tool_for(policies[sfinfo.processed_as].bin) if sfinfo.processed_as in policies else None
    if not tool:
        for mime in (sfinfo.matches[0]["mime"], FMT2EXT[sfinfo.processed_as].get("mime", "")):
            tool = tool_from_mime(mime)
            if tool:
                msgm = f"bin not specified in policies, using {tool.bin} according to the file mimetype for probing"
                sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=msgm))
                break
    # check if the file throws any error, warnings while open/processing it with the respective tool
    if _has_error(sfinfo, tool, ws, journal, verbose):
        return FDMsg.ERROR

    if sfinfo.errors == FDMsg.EMPTYSOURCE:
        # record as a warning so the end-of-phase report surfaces it
        journal.diagnose(sfinfo, FDMsg.WARNING, LogMsg(name="siegfried", msg=FDMsg.EMPTYSOURCE))

    # extension mismatch
    if sfinfo.matches[0]["warning"] == FDMsg.EXTMISMATCH:
        msg_txt = f"expecting one of the following ext: {list(FMT2EXT[sfinfo.processed_as]['file_extensions'])}"
        journal.diagnose(sfinfo, FDMsg.EXTMISMATCH, LogMsg(name="filehandler", msg=msg_txt))
        return FDMsg.EXTMISMATCH

    return None


def _rename(sfinfo: SfInfo, ext: str, ws: Workspace, journal: RunJournal) -> None:
    """
    Rename the file on disk to the given extension and update sfinfo.filename to the new portable relative path.
    If a file with the target name already exists, the MD5 prefix is appended to avoid collision.
    """
    source = ws.abs_path(sfinfo.filename)
    dest = source.with_suffix(ext)
    # if a file with same name and extension already there, append file hash to name
    if dest.is_file():
        dest = source.parent / f"{source.stem}_{sfinfo.md5[:6]}{ext}"
    try:
        source.rename(dest)
        msg = f"did rename {source.name} -> {dest.name}"
        sfinfo.filename = ws.relativize(dest)
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=msg))
    except OSError as e:
        journal.record_error(LogMsg(name="filehandler", msg=str(e)), sfinfo)


def _has_error(sfinfo: SfInfo, tool: MediaTool | None, ws: Workspace, journal: RunJournal, verbose: bool) -> bool:
    """
    Probe the file with the given tool and interpret the result.
    :param tool: the MediaTool used to probe the file; None or a non-probing tool (soffice) means no test.
    :param verbose: if True, do more detailed inspections
    :returns: True if the file is corrupt
    """
    # no tool, or a tool that does not probe (soffice) -> no test
    # TODO: inspection for other files than Audio/Video/IMAGE
    if tool is None:
        return False
    result = tool.probe(ws.abs_path(sfinfo.filename), verbose)
    if result is None:
        return False

    # see if a warning needs the file to be re-encoded
    if result.needs_reencode:
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="file flagged for reencoding"))
        sfinfo.status.pending = True

    if result.specs and not sfinfo.media_info:
        sfinfo.media_info.append(LogMsg(name=tool.bin, msg=result.specs))
    if result.is_corrupt:
        journal.diagnose(sfinfo, FDMsg.ERROR, LogMsg(name=tool.bin, msg=result.warnings))
        return True
    # if warnings but file is readable
    if result.warnings:
        journal.diagnose(sfinfo, FDMsg.WARNING, LogMsg(name=tool.bin, msg=result.warnings))
    return False

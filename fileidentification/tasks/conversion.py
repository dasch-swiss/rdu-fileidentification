import shlex
import subprocess
from pathlib import Path

import pygfried

from fileidentification.definitions.models import LogMsg, Policies, PolicyParams, SfInfo
from fileidentification.definitions.settings import FPMsg, LogLevel
from fileidentification.workspace import Workspace
from fileidentification.wrappers.tools import MediaTool, tool_for


def _add_media_info(sfinfo: SfInfo, tool: MediaTool | None, path: Path) -> None:
    """
    Attach technical metadata (codec/stream info) of the converted file to sfinfo.media_info, if tool supports it.
    `path` is the physical location of the file to probe (the working-dir output), not sfinfo.filename, which by
    now holds the file's future relative home.
    """
    if tool is None:
        return
    media_info = tool.media_info(path)
    if media_info:
        sfinfo.media_info.append(media_info)


def _verify(target: Path, sfinfo: SfInfo, expected: list[str], ws: Workspace) -> SfInfo | None:
    """
    Identify the converted file with pygfried and verify it matches the expected format.
    Returns an SfInfo for the new file (linked back to the origin via derived_from) on success, or None if the
    conversion produced no file or the wrong format; in either failure case a log entry is added to the origin sfinfo.
    :param expected: the PUIDs the converted file must match to count as a successful conversion
    """
    target_sfinfo = None
    if target.is_file():
        # generate a SfInfo of the converted file
        target_sfinfo = SfInfo(**pygfried.identify(f"{target}", detailed=True)["files"][0])  # type: ignore[arg-type]
        # only add postprocessing information if conversion was successful
        if target_sfinfo.processed_as in expected:
            # filename points at where the file physically is (relative to tmp_dir, in its working dir);
            # dest holds the future home next to the original. move_tmp relocates it and rewrites filename.
            target_sfinfo.filename = target.relative_to(ws.tmp_dir)
            target_sfinfo.dest = sfinfo.filename.parent
            target_sfinfo.derived_from = sfinfo
            sfinfo.status.pending = False

        else:
            p_error = f" did expect {expected}, got {target_sfinfo.processed_as} instead"
            sfinfo.processing_logs.append(
                LogMsg(name="filehandler", msg=f"{FPMsg.NOTEXPECTEDFMT}{p_error}", level=LogLevel.ERROR)
            )
            target_sfinfo = None

    else:
        # conversion error, nothing to analyse
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{FPMsg.CONVFAILED}", level=LogLevel.ERROR))

    return target_sfinfo


# file migration
def _run_tool(sfinfo: SfInfo, args: PolicyParams, tool: MediaTool, ws: Workspace) -> tuple[Path, str, str]:
    """
    Run the tool's conversion command in a per-file working directory.
    Returns the constructed target path, a shell-quoted command string (for logging), and the tool's captured log.
    """
    wdir = ws.working_dir(sfinfo.filename)
    if not wdir.exists():
        wdir.mkdir(parents=True)

    target = wdir / f"{sfinfo.filename.stem}.{args.target_container}"

    cmd_list = tool.build_command(ws.abs_path(sfinfo.filename), args, target, wdir)
    res = subprocess.run(cmd_list, check=False, capture_output=True, text=True)

    cmd_str = " ".join(shlex.quote(p) for p in cmd_list)
    return target, cmd_str, tool.read_log(res)


def convert_file(sfinfo: SfInfo, policies: Policies, ws: Workspace) -> tuple[SfInfo | None, list[str], LogMsg | None]:
    """
    Convert a file according to its policy, then re-identify and verify the output.
    Returns (target_sfinfo, [cmd], bin_log): target_sfinfo is the SfInfo of the verified converted file, or None
    if the conversion failed or produced an unexpected format; cmd is the converter command string (for logging);
    bin_log is the converter's log output on failure (for the caller to attach to the error), else None.
    """

    args: PolicyParams = policies[sfinfo.processed_as]  # type: ignore[index]
    tool = tool_for(args.bin)
    if tool is None:
        raise ValueError(f"no conversion tool for bin {args.bin!r}")  # noqa: EM102, TRY003

    target_path, cmd, logtext = _run_tool(sfinfo, args, tool, ws)

    # strip abs paths from log output
    processing_log = None
    logtext = logtext.replace(f"{ws.root_folder}/", "").replace(f"{ws.tmp_dir}/", "")
    if logtext:
        processing_log = LogMsg(name=f"{args.bin}", msg=logtext)

    # create an SfInfo for target and verify output, add codec and processing logs
    target_sfinfo = _verify(target_path, sfinfo, args.expected, ws)
    if target_sfinfo:
        _add_media_info(target_sfinfo, tool, target_path)
        if processing_log:
            target_sfinfo.processing_logs.append(processing_log)
        processing_log = None  # consumed by the successful target; nothing left for the caller

    return target_sfinfo, [cmd], processing_log

import shlex
import subprocess
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import pygfried

from fileidentification.definitions.models import LogMsg, Policies, PolicyParams, SfInfo
from fileidentification.definitions.settings import FPMsg, LogLevel
from fileidentification.workspace import Workspace
from fileidentification.wrappers.tools import MediaTool, tool_for

# a tool flagged `serial` (e.g. soffice) cannot run concurrent instances; serialize its subprocess across the pool
_serial_lock = threading.Semaphore(1)


@dataclass
class ConversionResult:
    """
    Outcome of convert_file. `converted` is the verified output SfInfo on success (else None), `cmd` the
    shell-quoted command run (always set, for logging/display). On failure `error` is the summary to record and
    `bin_log` the converter's own output (an errors-only detail); both are None on success.
    """

    converted: SfInfo | None
    cmd: str
    error: LogMsg | None = None
    bin_log: LogMsg | None = None


def _add_media_info(sfinfo: SfInfo, tool: MediaTool | None, path: Path) -> None:
    """
    Attach the converted file's technical metadata (codec/stream info) to sfinfo.media_info, if the tool has any.
    `path` is the physical file to probe — sfinfo.filename is a relative path, not usable for probing here.
    """
    if tool is None:
        return
    media_info = tool.media_info(path)
    if media_info:
        sfinfo.media_info.append(media_info)


def _verify(target: Path, sfinfo: SfInfo, expected: list[str], ws: Workspace) -> tuple[SfInfo | None, LogMsg | None]:
    """
    Identify the converted file with pygfried and verify it matches an expected PUID.
    :param expected: the PUIDs the converted file must match to count as a successful conversion
    """
    if not target.is_file():
        # conversion error, nothing to analyse
        return None, LogMsg(name="filehandler", msg=f"{FPMsg.CONVFAILED}", level=LogLevel.ERROR)

    target_sfinfo = SfInfo(**pygfried.identify(f"{target}", detailed=True)["files"][0])  # type: ignore[arg-type]
    if target_sfinfo.processed_as not in expected:
        p_error = f" did expect {expected}, got {target_sfinfo.processed_as} instead"
        return None, LogMsg(name="filehandler", msg=f"{FPMsg.NOTEXPECTEDFMT}{p_error}", level=LogLevel.ERROR)

    # success: dest holds the future home next to the original. move_tmp relocates it and rewrites filename.
    target_sfinfo.filename = target.relative_to(ws.tmp_dir)
    target_sfinfo.dest = sfinfo.filename.parent
    target_sfinfo.derived_from = sfinfo
    sfinfo.status.pending = False
    sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"converted -> {target_sfinfo.filename}"))
    return target_sfinfo, None


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
    with _serial_lock if tool.serial else nullcontext():
        res = subprocess.run(cmd_list, check=False, capture_output=True, text=True)

    cmd_str = " ".join(shlex.quote(p) for p in cmd_list)
    return target, cmd_str, tool.read_log(res)


def convert_file(sfinfo: SfInfo, policies: Policies, ws: Workspace) -> ConversionResult:
    """
    Convert a file per its policy, then re-identify and verify the output. On success the converter's log is
    attached to the returned SfInfo; on failure the reason and the converter's log ride back in the result.
    """

    args: PolicyParams = policies[sfinfo.processed_as]  # type: ignore[index]
    tool = tool_for(args.bin)
    if tool is None:
        raise ValueError(f"no conversion tool for bin {args.bin!r}")  # noqa: EM102, TRY003

    target_path, cmd, logtext = _run_tool(sfinfo, args, tool, ws)

    # strip abs paths from log output
    logtext = logtext.replace(f"{ws.root_folder}/", "").replace(f"{ws.tmp_dir}/", "")
    bin_log = LogMsg(name=f"{args.bin}", msg=logtext) if logtext else None

    # create an SfInfo for target and verify output, add codec and processing logs
    target_sfinfo, reason = _verify(target_path, sfinfo, args.expected, ws)
    if target_sfinfo:
        _add_media_info(target_sfinfo, tool, target_path)
        if bin_log:
            target_sfinfo.processing_logs.append(bin_log)
        return ConversionResult(converted=target_sfinfo, cmd=cmd)

    return ConversionResult(converted=None, cmd=cmd, error=reason, bin_log=bin_log)

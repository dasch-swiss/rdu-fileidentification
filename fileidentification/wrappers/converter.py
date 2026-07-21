import shlex
import subprocess
from pathlib import Path

from fileidentification.definitions.models import PolicyParams, SfInfo
from fileidentification.workspace import Workspace
from fileidentification.wrappers.tools import tool_for


def convert(sfinfo: SfInfo, args: PolicyParams, ws: Workspace) -> tuple[Path, str, str]:
    """
    Convert a file to the desired format passed by the args.
    :param args: how to convert the file ('bin', 'processing_args', 'target_container')
    :returns: the constructed target path, a human-readable command string, and the captured log output
    """

    tool = tool_for(args.bin)
    if tool is None:
        raise ValueError(f"no conversion tool for bin {args.bin!r}")  # noqa: EM102, TRY003

    wdir = ws.working_dir(sfinfo.filename)
    if not wdir.exists():
        wdir.mkdir(parents=True)

    target = wdir / f"{sfinfo.filename.stem}.{args.target_container}"

    cmd_list = tool.build_command(ws.abs_path(sfinfo.filename), args, target, wdir)
    res = subprocess.run(cmd_list, check=False, capture_output=True, text=True)
    logtext = tool.read_log(res)

    cmd_str = " ".join(shlex.quote(p) for p in cmd_list)
    return target, cmd_str, logtext

import hashlib
import shlex
import subprocess
from pathlib import Path

from fileidentification.definitions.models import PolicyParams, SfInfo
from fileidentification.wrappers.tools import tool_for


def convert(sfinfo: SfInfo, args: PolicyParams) -> tuple[Path, str, str]:
    """
    Convert a file to the desired format passed by the args.
    :param args: how to convert the file ('bin', 'processing_args', 'target_container')
    :returns: the constructed target path, a human-readable command string, and the captured log output
    """

    tool = tool_for(args.bin)
    if tool is None:
        raise ValueError(f"no conversion tool for bin {args.bin!r}")  # noqa: EM102, TRY003

    path_hash = hashlib.md5(str(sfinfo.filename).encode()).hexdigest()[:6]  # noqa: S324
    wdir = sfinfo.tdir / f"{sfinfo.filename.name}_{path_hash}"
    if not wdir.exists():
        wdir.mkdir(parents=True)

    target = Path(wdir / f"{sfinfo.filename.stem}.{args.target_container}")

    cmd_list = tool.build_command(sfinfo.path, args, target, wdir)
    res = subprocess.run(cmd_list, check=False, capture_output=True, text=True)
    logtext = tool.read_log(res)

    cmd_str = " ".join(shlex.quote(p) for p in cmd_list)
    return target, cmd_str, logtext

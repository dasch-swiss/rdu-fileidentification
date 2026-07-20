import platform
import shlex
import subprocess
from pathlib import Path

from fileidentification.definitions.models import PolicyParams, SfInfo
from fileidentification.definitions.settings import PDFSETTINGS, Bin, LOPath

SOFFICE = LOPath.Linux if platform.system() == LOPath.Linux.name else LOPath.Darwin


def convert(sfinfo: SfInfo, args: PolicyParams) -> tuple[Path, str, str]:
    """
    Convert a file to the desired format passed by the args.
    :param args: how to convert the file ('bin', 'processing_args', 'target_container')
    :returns: the constructed target path, a human-readable command string, and the captured log output
    """

    wdir = Path(sfinfo.tdir / f"{sfinfo.filename.name}_{sfinfo.md5[:6]}")
    if not wdir.exists():
        wdir.mkdir(parents=True)

    target = Path(wdir / f"{sfinfo.filename.stem}.{args.target_container}")

    cmd_list: list[str] = []

    match args.bin:
        case Bin.FFMPEG:
            cmd_list = ["ffmpeg", "-y", "-i", str(sfinfo.path), *shlex.split(args.processing_args), str(target)]
        case Bin.MAGICK:
            cmd_list = ["magick", *shlex.split(args.processing_args), str(sfinfo.path), str(target)]
        # case Bin.INCSCAPE:
        #     cmd_list = ["inkscape", f"--export-filename={str(target)}", *shlex.split(args.processing_args), str(sfinfo.path)]
        case Bin.SOFFICE:
            soffice_filter = f"pdf{PDFSETTINGS}" if args.target_container == "pdf" else args.target_container
            cmd_list = [
                str(SOFFICE),
                *shlex.split(args.processing_args),
                soffice_filter,
                str(sfinfo.path),
                "--outdir",
                str(wdir),
            ]

    res = subprocess.run(cmd_list, check=False, capture_output=True, text=True)
    # soffice reports on stdout; ffmpeg / magick on stderr
    logtext = res.stdout + res.stderr if args.bin == Bin.SOFFICE else res.stderr

    cmd_str = " ".join(shlex.quote(p) for p in cmd_list)
    return target, cmd_str, logtext

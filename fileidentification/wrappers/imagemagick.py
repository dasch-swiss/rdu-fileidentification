import subprocess
from pathlib import Path

from fileidentification.definitions.settings import ErrMsgIM


def imagemagick_collect_warnings(file: Path, verbose: bool) -> tuple[bool, str, str]:
    """
    Check for errors with magick identify.
    Returns True if file is corrupt, stdout, technical metadata of the image
    """

    cmd = ["identify", "-format", "%m %wx%h %g %z-bit %[channels]", str(file)]
    if verbose:
        cmd = ["identify", "-verbose", "-regard-warnings", "-format", "%m %wx%h %g %z-bit %[channels]", str(file)]

    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    specs = res.stdout.replace(f"{file.parent}/", "")
    std_err = res.stderr.replace(f"{file.parent}/", "")

    # check if the warnings have an error that the file is not or only partially readable
    if std_err and any(msg in std_err for msg in ErrMsgIM):
        return True, std_err, specs
    return False, std_err, specs


def imagemagick_media_info(file: Path) -> str:
    """Return image technical metadata string (format, dimensions, bit depth, channels) using magick identify -ping."""
    cmd = ["identify", "-ping", "-format", "%m %wx%h %g %z-bit %[channels]", str(file)]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return res.stdout.replace(f"{file.parent}/", "")

import re
import subprocess
from pathlib import Path

from fileidentification.definitions.settings import ErrMsgIM

# Compile the corruption patterns once. Anything that matches means the file is
# not, or only partially, readable (see ErrMsgIM).
_CORRUPT_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in ErrMsgIM]


def imagemagick_collect_warnings(file: Path, verbose: bool) -> tuple[bool, str, str]:
    """
    Probe the file with magick identify.
    Returns a tuple (is_corrupt, warnings, specs):
      is_corrupt: True if the warnings match an ErrMsgIM pattern indicating an unreadable / partially readable file;
      warnings: identify's stderr output (paths stripped);
      specs: the image technical metadata string (format, dimensions, bit depth, channels).
    """

    cmd = ["identify", "-format", "%m %wx%h %g %z-bit %[channels]", str(file)]
    if verbose:
        cmd = ["identify", "-verbose", "-regard-warnings", "-format", "%m %wx%h %g %z-bit %[channels]", str(file)]

    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    specs = res.stdout.replace(f"{file.parent}/", "")
    std_err = res.stderr.replace(f"{file.parent}/", "")

    # check if the warnings match a pattern indicating the file is not or only partially readable
    if std_err and any(pattern.search(std_err) for pattern in _CORRUPT_PATTERNS):
        return True, std_err, specs
    return False, std_err, specs


def imagemagick_media_info(file: Path) -> str:
    """Return image technical metadata string (format, dimensions, bit depth, channels) using magick identify -ping."""
    cmd = ["identify", "-ping", "-format", "%m %wx%h %g %z-bit %[channels]", str(file)]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return res.stdout.replace(f"{file.parent}/", "")

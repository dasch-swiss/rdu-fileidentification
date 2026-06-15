import json
import subprocess
from pathlib import Path
from typing import Any


def ffmpeg_collect_warnings(file: Path, verbose: bool) -> tuple[bool, str, str]:
    """
    Check for errors with ffprobe -show_error or ffmpeg dropping frames.
    Returns True if file is corrupt, stdout, technical metadata of the video
    """

    cmd = ["ffprobe", "-hide_banner", "-show_error", str(file)]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    std_out = res.stdout.replace(f"{file.parent}/", "")

    if verbose:
        cmd_verbose = ["ffmpeg", "-v", "error", "-i", str(file), "-f", "null", "-"]
        res_verbose = subprocess.run(cmd_verbose, check=False, capture_output=True, text=True)
        # ffmpeg catches errors in stderr, map the errors to stdout
        std_out = res_verbose.stderr.replace(f"{file.parent}/", "")

    streams = ffmpeg_media_info(file)
    specs = json.dumps(streams) if streams else ""

    # rely on ffprobe whether file is corrupt
    if res.stdout:
        return True, std_out, specs
    return False, std_out, specs


def ffmpeg_media_info(file: Path) -> dict[str, Any] | None:
    cmd: list[str] = [
        "ffprobe",
        str(file),
        "-hide_banner",
        "-show_entries",
        "stream=index,codec_type,codec_name,codec_long_name,profile,"
        "codec_tag,pix_fmt,color_space,coded_width,coded_height,r_frame_rate,bit_rate,channels,channel_layout,"
        "sample_aspect_ratio,display_aspect_ratio",
        "-output_format",
        "json",
    ]
    res = subprocess.run(cmd, check=False, capture_output=True)
    if res.returncode == 0:
        streams: dict[str, Any] = json.loads(res.stdout)["streams"]
        return streams
    return None

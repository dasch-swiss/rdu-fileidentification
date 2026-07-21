import json
from pathlib import Path

import pygfried

from fileidentification.definitions.models import LogMsg, Policies, PolicyParams, SfInfo
from fileidentification.definitions.settings import Bin, FPMsg
from fileidentification.tasks.console_output import print_conversion_failed_error, print_unexpected_format_error
from fileidentification.wrappers.converter import convert
from fileidentification.wrappers.ffmpeg import ffmpeg_media_info
from fileidentification.wrappers.imagemagick import imagemagick_media_info


def _add_media_info(sfinfo: SfInfo, _bin: str) -> None:
    """Attach technical metadata (codec/stream info) of the converted file to sfinfo.media_info, if _bin supports it."""
    match _bin:
        case Bin.FFMPEG:
            streams = ffmpeg_media_info(sfinfo.filename)
            sfinfo.media_info.append(LogMsg(name="ffmpeg", msg=json.dumps(streams)))
        case Bin.MAGICK:
            sfinfo.media_info.append(LogMsg(name="imagemagick", msg=imagemagick_media_info(sfinfo.filename)))
        case _:
            pass


def _verify(target: Path, sfinfo: SfInfo, expected: list[str]) -> SfInfo | None:
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
            target_sfinfo.dest = sfinfo.filename.parent
            target_sfinfo.derived_from = sfinfo
            sfinfo.status.pending = False

        else:
            p_error = f" did expect {expected}, got {target_sfinfo.processed_as} instead"
            sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{FPMsg.NOTEXPECTEDFMT}{p_error}"))
            print_unexpected_format_error(p_error, sfinfo.filename, target)
            target_sfinfo = None

    else:
        # conversion error, nothing to analyse
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{FPMsg.CONVFAILED}"))
        print_conversion_failed_error(sfinfo.filename, target)

    return target_sfinfo


# file migration
def convert_file(sfinfo: SfInfo, policies: Policies) -> tuple[SfInfo | None, list[str], LogMsg | None]:
    """
    Convert a file according to its policy, then re-identify and verify the output.
    Returns (target_sfinfo, [cmd], bin_log): target_sfinfo is the SfInfo of the verified converted file, or None
    if the conversion failed or produced an unexpected format; cmd is the converter command string (for logging);
    bin_log is the converter's log output on failure (for the caller to attach to the error), else None.
    """

    args: PolicyParams = policies[sfinfo.processed_as]  # type: ignore[index]

    target_path, cmd, logtext = convert(sfinfo, args)

    # strip abs paths from log output
    processing_log = None
    logtext = logtext.replace(f"{sfinfo.root_folder}/", "").replace(f"{sfinfo.tdir}/", "")
    if logtext:
        processing_log = LogMsg(name=f"{args.bin}", msg=logtext)

    # create an SfInfo for target and verify output, add codec and processing logs
    target_sfinfo = _verify(target_path, sfinfo, args.expected)
    if target_sfinfo:
        _add_media_info(target_sfinfo, args.bin)
        if processing_log:
            target_sfinfo.processing_logs.append(processing_log)
        processing_log = None  # consumed by the successful target; nothing left for the caller

    return target_sfinfo, [cmd], processing_log

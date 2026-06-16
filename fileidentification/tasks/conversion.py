import json
from pathlib import Path

import pygfried
from typer import colors, secho

from fileidentification.definitions.models import LogMsg, Policies, PolicyParams, SfInfo
from fileidentification.definitions.settings import Bin, FPMsg
from fileidentification.wrappers.converter import convert
from fileidentification.wrappers.ffmpeg import ffmpeg_media_info
from fileidentification.wrappers.imagemagick import imagemagick_media_info


def _add_media_info(sfinfo: SfInfo, _bin: str) -> None:
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
    Analyse the created file with pygfried, returns a SfInfo for the new file if verification passed,
    :param sfinfo the metadata of the origin
    :param target the path to the converted file to analyse with siegfried
    :param expected the expected file format, to verify the conversion
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
            secho(f"\tERROR: {p_error} when converting {sfinfo.filename} to {target}", fg=colors.YELLOW, bold=True)
            target_sfinfo = None

    else:
        # conversion error, nothing to analyse
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{FPMsg.CONVFAILED}"))
        secho(f"\tERROR failed to convert {sfinfo.filename} to {target}", fg=colors.RED, bold=True)

    return target_sfinfo


# file migration
def convert_file(sfinfo: SfInfo, policies: Policies) -> tuple[SfInfo | None, list[str]]:
    """
    Convert a file, returns the metadata of the converted file as SfInfo
    :param sfinfo the metadata of the file to convert
    :param policies the policies for fileconversion
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

    return target_sfinfo, [cmd]

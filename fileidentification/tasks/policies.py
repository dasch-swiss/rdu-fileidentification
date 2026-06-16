from typer import colors, secho

from fileidentification.definitions.models import LogMsg, LogTables, Policies, SfInfo
from fileidentification.definitions.settings import PLMsg
from fileidentification.tasks.os_tasks import remove
from fileidentification.wrappers.ffmpeg import ffmpeg_media_info


def apply_policy(sfinfo: SfInfo, policies: Policies, log_tables: LogTables, strict: bool) -> None:
    """
    Decide what to do with the file based on its policy entry.
    Sets sfinfo.status.pending=True if the file needs conversion.
    In strict mode, files with no policy entry are moved to _REMOVED; otherwise they are skipped with a log entry.
    Files marked accepted=True are also checked for invalid A/V streams (fmt/199, fmt/569) and flagged for
    re-encoding if needed.
    """
    puid = sfinfo.processed_as
    if not puid:
        return
    if sfinfo.status.pending:
        return

    if puid not in policies:
        # in strict mode, move file
        if strict:
            sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{PLMsg.NOTINPOLICIES}"))
            remove(sfinfo, log_tables)
            return
        # just flag it as skipped
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{PLMsg.SKIPPED}"))
        return

    # case where file needs to be converted
    if not policies[puid].accepted:
        sfinfo.status.pending = True
        return

    # check if mp4 / mkv has correct stream (i.e. h264 and aac)
    if puid in ["fmt/199", "fmt/569"] and _has_invalid_streams(sfinfo, puid):
        sfinfo.status.pending = True
        return


def _has_invalid_streams(sfinfo: SfInfo, puid: str) -> bool:
    """Return true if video and audio codec differ from archival standards"""
    streams = ffmpeg_media_info(sfinfo.path)
    if not streams:
        secho(f"\t{sfinfo.filename} throwing errors. consider file", fg=colors.RED, bold=True)
        return False
    if puid in ["fmt/569"]:
        # only the video codec has to be ffv1 -> return false as soon as any stream is ffv1
        return all(stream["codec_name"] not in ["ffv1"] for stream in streams)  # type: ignore[index]
    if puid in ["fmt/199"]:
        # video codec has to be h264, audio codec aac -> return true if any a/v stream does not match
        for stream in streams:
            if stream["codec_type"] not in ["video", "audio"]:  # type: ignore[index]
                continue
            if stream["codec_name"] not in ["h264", "aac"]:  # type: ignore[index]
                return True
    return False

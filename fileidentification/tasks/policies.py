from collections.abc import Iterable

from fileidentification.definitions.models import LogMsg, LogTables, Mode, Policies, PolicyParams, SfInfo
from fileidentification.definitions.settings import FMT2EXT, FDMsg, PLMsg
from fileidentification.tasks.os_tasks import remove
from fileidentification.workspace import Workspace
from fileidentification.wrappers.ffmpeg import ffmpeg_media_info


def build_policies(
    puids: Iterable[str],
    default_policies: Policies,
    mode: Mode,
    *,
    blank: bool = False,
    extend: bool = False,
    existing: Policies | None = None,
) -> tuple[Policies, list[str]]:
    """
    Build the policy map for the encountered puids. Pure: no file I/O, no shared-state mutation.

    blank: one accept-by-default entry per puid (no default lookup).
    Otherwise each puid takes its default policy; puids without a default get an empty fallback policy
    (unless in strict mode, where they are dropped).
    extend: puids already present in `existing` keep their existing (hand-tuned) policy.
    remove_original mode is propagated onto every resulting policy.

    Returns (policies, blank_puids) where blank_puids are the puids that received an empty fallback policy.
    """
    policies: Policies = {}
    blank_puids: list[str] = []

    if blank:
        for puid in puids:
            policies[puid] = PolicyParams(format_name=FMT2EXT[puid]["name"], remove_original=mode.REMOVEORIGINAL)
        return policies, blank_puids

    for puid in puids:
        if puid in default_policies:
            policies[puid] = default_policies[puid]
        # no default for this filetype and not strict: add an empty (blank) policy
        if not mode.STRICT and puid not in default_policies:
            policies[puid] = PolicyParams(format_name=FMT2EXT[puid]["name"])
            blank_puids.append(puid)
        # extend: keep an already existing policy for this puid
        if extend and existing and puid in existing:
            policies[puid] = existing[puid]
            if puid in blank_puids:
                blank_puids.remove(puid)
        # propagate remove_original mode
        if puid in policies and mode.REMOVEORIGINAL:
            policies[puid].remove_original = mode.REMOVEORIGINAL

    return policies, blank_puids


def apply_policy(sfinfo: SfInfo, policies: Policies, ws: Workspace, log_tables: LogTables, strict: bool) -> None:
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
            remove(sfinfo, ws, log_tables)
            return
        # just flag it as skipped
        sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=f"{PLMsg.SKIPPED}"))
        return

    # case where file needs to be converted
    if not policies[puid].accepted:
        sfinfo.status.pending = True
        return

    # check if mp4 / mkv has correct stream (i.e. h264 and aac)
    if puid in ["fmt/199", "fmt/569"] and _has_invalid_streams(sfinfo, puid, ws, log_tables):
        sfinfo.status.pending = True
        return


def _has_invalid_streams(sfinfo: SfInfo, puid: str, ws: Workspace, log_tables: LogTables) -> bool:
    """Return true if video and audio codec differ from archival standards"""
    streams = ffmpeg_media_info(ws.abs_path(sfinfo.filename))
    if not streams:
        # ffprobe read no streams for a file we meant to stream-check: record a warning for the end-of-phase report
        sfinfo.warnings.append(LogMsg(name="ffmpeg", msg="could not read streams for the a/v stream check"))
        log_tables.diagnostics_add(sfinfo, FDMsg.WARNING)
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

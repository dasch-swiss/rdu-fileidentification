import hashlib
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from fileidentification.definitions.settings import Bin, FDMsg, LogLevel, PLMsg, PVErr


class LogMsg(BaseModel):
    """A single timestamped log entry attached to an SfInfo (media_info, processing_logs)."""

    name: str
    msg: str
    level: LogLevel = LogLevel.INFO
    timestamp: datetime | None = None

    def model_post_init(self, context: Any, /) -> None:
        """Set timestamp to the current UTC time if not provided."""
        if not self.timestamp:
            self.timestamp = datetime.now(UTC)


class Status(BaseModel):
    """
    Processing state of a file.
    removed: moved to _REMOVED (corrupt or replaced by a conversion).
    pending: flagged for conversion but not yet converted.
    added: it is a conversion output that was added to the stack.
    probed: integrity has been probed.
    applied: policies have been applied.
    """

    removed: bool = False
    pending: bool = False
    added: bool = False
    probed: bool = False
    applied: bool = False


# main metadata object where information is stored and added
class SfInfo(BaseModel):
    """file info object mapped from siegfried output, gets extended while processing."""

    # output from siegfried
    filename: Path  # portable, relative to root_folder — except a converted file awaiting move (dest set),
    # whose filename is its working-dir location relative to tmp_dir until move_tmp relocates it
    filesize: int
    modified: str
    errors: str
    md5: str = Field(default_factory=str)
    matches: list[dict[str, Any]] = Field(default_factory=list)
    # added during processing
    status: Status = Field(default_factory=Status)
    processed_as: str | None = None
    media_info: list[LogMsg] = Field(default_factory=list[LogMsg])
    processing_logs: list[LogMsg] = Field(default_factory=list[LogMsg])
    # if converted
    derived_from: Self | None = None
    dest: Path | None = None  # future home dir (relative to root_folder); set until move_tmp relocates the file

    def model_post_init(self, context: Any, /) -> None:
        """Derive the fields not provided by siegfried: status, processed_as, md5"""
        if not self.status:
            self.status = Status()
        if not self.processed_as:
            self.processed_as = self._fetch_puid()
        if not self.md5:
            self.md5 = get_md5(self.filename)

    def _fetch_puid(self) -> str | None:
        """
        Extract the PUID from the first siegfried match.
        If the match is UNKNOWN, fall back to any PUID suggested by siegfried based on the file extension.
        Returns None if no PUID can be determined.
        """
        if self.matches:
            if self.matches[0]["id"] == "UNKNOWN":
                # check whether pygfried suggest puid according to extension
                fmts = re.findall(r"(fmt|x-fmt)/([\d]+)", self.matches[0]["warning"])
                fmts_s: list[str] = [f"{el[0]}/{el[1]}" for el in fmts]
                if fmts:
                    self.processing_logs.append(LogMsg(name="filehandler", msg=PLMsg.FALLBACK))
                    return fmts_s[0]
                return None
            puid: str = self.matches[0]["id"]
            return puid
        return None

    @property
    def is_active(self) -> bool:
        return not (self.status.removed or self.dest)


class LogOutput(BaseModel):
    """Top-level structure written to _log.json: all files, processing errors, and duplicates."""

    duplicates: dict[str, list[Path]] | None
    files: list[SfInfo] | None = None
    errors: list[SfInfo] | None = None


class RunJournal(BaseModel):
    """
    Run-wide record of what happened to each file, shared across worker threads.
    Always mutate via `diagnose` / `record_error`, which hold the internal lock.
    """

    diagnostics: dict[str, list[SfInfo]] = Field(default_factory=dict)
    processing_errors: list[tuple[LogMsg, SfInfo, list[LogMsg]]] = Field(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def diagnose(self, sfinfo: SfInfo, severity: FDMsg, msg: LogMsg) -> None:
        """
        Record a diagnostic for the console report: set `msg`'s level from `severity`, append it to the SfInfo's
        processing_logs, and bucket the SfInfo under `severity`. The report prints each bucketed file's full
        processing_logs. Thread-safe.
        """
        msg.level = LogLevel.ERROR if severity == FDMsg.ERROR else LogLevel.WARNING
        with self._lock:
            sfinfo.processing_logs.append(msg)
            self.diagnostics.setdefault(severity.name, []).append(sfinfo)

    def record_error(self, msg: LogMsg, sfinfo: SfInfo, details: list[LogMsg] | None = None) -> None:
        """
        Thread-safely append a processing error (the summary is marked error-level).
        details are extra LogMsgs (e.g. the converter's output) recorded only in the "errors" copy and not printed.
        """
        msg.level = LogLevel.ERROR
        with self._lock:
            self.processing_errors.append((msg, sfinfo, details or []))

    def error_records(self) -> list[SfInfo] | None:
        """
        Return a copy of each SfInfo that hit a processing error, with the summary LogMsg and any detail LogMsgs
        appended to its processing_logs. The originals (which also live in the stack / _log.json "files") are left
        untouched, so the error entry and its details are recorded only in the "errors" section, not in "files".
        Non-destructive: callers may print the errors and build the persisted copy in either order.
        """
        if not self.processing_errors:
            return None
        return [
            sfinfo.model_copy(update={"processing_logs": [*sfinfo.processing_logs, msg, *details]})
            for msg, sfinfo, details in self.processing_errors
        ]


class BasicAnalytics(BaseModel):
    """
    Indexes the scanned files for analytics and policy generation:
    by MD5 (to find duplicates) and by PUID (puid_unique, the distinct formats found in the folder).
    siegfried_errors holds files siegfried could not read; blank tracks PUIDs generated without a default policy.
    """

    filehashes: dict[str, list[Path]] = Field(default_factory=dict)
    puid_unique: dict[str, list[SfInfo]] = Field(default_factory=dict)
    siegfried_errors: list[SfInfo] = Field(default_factory=list)
    blank: list[str] | None = None

    def append(self, sfinfo: SfInfo) -> None:
        """Index sfinfo by PUID and MD5, and record it in siegfried_errors if siegfried reported a read error."""
        if sfinfo.processed_as:
            if sfinfo.md5 not in self.filehashes:
                self.filehashes[sfinfo.md5] = []
            self.filehashes[sfinfo.md5].append(sfinfo.filename)
            if sfinfo.processed_as not in self.puid_unique:
                self.puid_unique[sfinfo.processed_as] = []
            self.puid_unique[sfinfo.processed_as].append(sfinfo)
        if sfinfo.errors and sfinfo.errors != FDMsg.EMPTYSOURCE:
            self.siegfried_errors.append(sfinfo)

    def smallest_file(self, puid: str) -> SfInfo:
        """Return the SfInfo with the smallest filesize for the given PUID (used as a conversion test sample)."""
        return sorted(self.puid_unique[puid], key=lambda x: x.filesize, reverse=False)[0]

    @property
    def duplicates(self) -> dict[str, list[Path]]:
        """Return only the MD5 entries that have more than one file (i.e. actual duplicates)."""
        return {k: v for k, v in self.filehashes.items() if len(v) != 1}


# models for policies
class PolicyParams(BaseModel):
    """
    One policy entry, keyed by PUID in a policies.json.
    accepted=True means the format is kept as-is; accepted=False means it must be converted, in which case
    bin, target_container and expected are required (enforced by assert_conv_args).
    bin: the converter to use (ffmpeg / magick / soffice).
    target_container: the output file extension / container to convert to.
    processing_args: extra arguments passed to the converter (no ';', see allowed_args).
    expected: PUIDs the converted file is verified against to confirm the conversion succeeded.
    remove_original: whether the source file is moved to _REMOVED after a successful conversion.
    """

    format_name: str = Field(default_factory=str)
    bin: str = Field(default="")
    accepted: bool = Field(default=True)
    target_container: str = Field(default="")
    processing_args: str = Field(default="")
    expected: list[str] = Field(default=[""])
    remove_original: bool = Field(default=False)

    @field_validator("bin", mode="after")
    @classmethod
    def allowed_bin(cls, value: str) -> str:
        """Validate that bin is one of the supported executables (ffmpeg, magick, soffice, or empty)."""
        if value not in Bin:
            raise ValueError(f"{value} is not an allowed bin")  # noqa: EM102, TRY003
        return value

    @field_validator("processing_args", mode="after")
    @classmethod
    def allowed_args(cls, value: str) -> str:
        """Reject semicolons in processing_args to prevent shell command injection."""
        if ";" in value:
            raise ValueError(PVErr.SEMICOLON)
        return value

    @model_validator(mode="after")
    def assert_conv_args(self) -> Self:
        """Require target_container, expected, and bin to be set whenever accepted=False."""
        if self.accepted is False:
            if self.target_container == "":
                raise ValueError(PVErr.MISS_CON)
            if self.expected == [""]:
                raise ValueError(PVErr.MISS_EXP)
            if self.bin == "":
                raise ValueError(PVErr.MISS_BIN)
        return self


Policies = dict[str, PolicyParams]


class PoliciesFile(BaseModel):
    """On-disk structure of a policies.json: the output path (name), a generated comment, and the PUID->policy map."""

    name: Path = Field(default_factory=Path)
    comment: str = Field(default_factory=str)
    policies: Policies = Field(default_factory=Policies)


# Settings for the Filehandler Class
class Mode(BaseModel):
    """
    the different modes for the FileHandler.
    REMOVEORIGINAL: bool, whether to remove the original files of the files that got converted
    VERBOSE: bool, do verbose analysis of video and image files
    STRICT: bool, move files that are not listed in policies to FAILED instead of skipping them
    QUIET: bool, just print warnings and errors
    """

    REMOVEORIGINAL: bool = False
    VERBOSE: bool = False
    STRICT: bool = False
    QUIET: bool = False


def get_md5(path: str | Path) -> str:
    """Compute and return the MD5 hex digest of the file at path."""
    md5 = hashlib.md5()  # noqa: S324
    with open(path, "rb") as s:  # noqa: PTH123
        for chunk in iter(lambda: s.read(4096), b""):
            md5.update(chunk)
    return md5.hexdigest()


def sfinfo2csv(sfinfo: SfInfo) -> dict[str, str | int]:
    """Flatten an SfInfo into a dict of scalar values suitable for CSV output."""
    res: dict[str, str | int] = {
        "filename": f"{sfinfo.filename}",
        "filesize": sfinfo.filesize,
        "modified": sfinfo.modified,
        "errors": sfinfo.errors,
        "md5": sfinfo.md5,
    }
    if sfinfo.status.pending:
        res.update({"status": "pending"})
    if sfinfo.status.added:
        res.update({"status": "added"})
    if sfinfo.status.removed:
        res.update({"status": "removed"})
    if sfinfo.processed_as:
        res["processed_as"] = sfinfo.processed_as
    if sfinfo.media_info:
        res["media_info"] = sfinfo.media_info[0].msg
    if sfinfo.processing_logs:
        res["processing_logs"] = " ; ".join([f"{el.level}: {el.msg}" for el in sfinfo.processing_logs])
    if sfinfo.derived_from:
        res["derived_from"] = f"{sfinfo.derived_from.filename}"
    return res

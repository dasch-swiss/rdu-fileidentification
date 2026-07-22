import math
from pathlib import Path

from rich import box
from rich.console import Console
from rich.style import Style
from rich.table import Table
from typer import colors, secho

from fileidentification.definitions.models import BasicAnalytics, LogMsg, Mode, Policies, RunJournal
from fileidentification.definitions.settings import FMT2EXT, FDMsg


def print_siegfried_errors(ba: BasicAnalytics) -> None:
    """Print files for which siegfried reported a read error during identification."""
    if ba.siegfried_errors:
        secho("got the following errors from siegfried", bold=True)
        for sfinfo in ba.siegfried_errors:
            secho(f"{sfinfo.filename} \n{sfinfo.errors}", fg=colors.RED)


def print_fmts(puids: list[str], ba: BasicAnalytics, policies: Policies, mode: Mode) -> None:
    """
    Print a summary table of all encountered file formats with their PUID, name, file count,
    combined size, and policy status.
    Rows are white (policy set), yellow (blank/missing), or red (missing in strict mode).
    """
    if mode.QUIET:
        return
    table = Table(title="", box=box.SIMPLE)
    table.add_column("PUID")
    table.add_column("Format Name")
    table.add_column("File Count")
    table.add_column("Combined Size")
    table.add_column("Policy")

    for puid in puids:
        bytes_size: int = 0
        for sfinfo in ba.puid_unique[puid]:
            bytes_size += sfinfo.filesize
        size = _format_bite_size(bytes_size)
        po = ""
        style = Style(color=colors.WHITE)
        if puid not in policies:
            po = "missing"
            style = Style(color=colors.YELLOW)
            if mode.STRICT:
                style = Style(color=colors.RED)
        if puid in policies and not policies[puid].accepted:
            po = policies[puid].bin
        if ba.blank and puid in ba.blank:
            po = "blank"
            style = Style(color=colors.YELLOW)
        table.add_row(puid, f"{FMT2EXT[puid]['name']}", f"{len(ba.puid_unique[puid])}", size, po, style=style)
    console = Console()
    console.print(table)


def _print_bucket(journal: RunJournal, severity: FDMsg, title: str) -> None:
    """Print one diagnostics bucket: each file, then its processing logs for context."""
    sfinfos = journal.diagnostics.get(severity.name)
    if not sfinfos:
        return
    color = colors.RED if severity == FDMsg.ERROR else colors.YELLOW
    secho(f"\n----------- {title} -----------", bold=True)
    for sfinfo in sfinfos:
        secho(f"\n{_format_bite_size(sfinfo.filesize): >10}    {sfinfo.filename}", fg=color, bold=True)
        _print_logs(sfinfo.processing_logs)


def print_diagnostic(journal: RunJournal, mode: Mode) -> None:
    """Print corruption errors always, and (unless quiet) warnings and extension mismatches."""
    if not mode.QUIET:
        _print_bucket(journal, FDMsg.EXTMISMATCH, "Extension mismatch")
        _print_bucket(journal, FDMsg.WARNING, "Warnings")
    _print_bucket(journal, FDMsg.ERROR, "Errors")


def print_duplicates(duplicates: dict[str, list[Path]], mode: Mode) -> None:
    """Print files that share the same MD5 checksum, grouped by hash."""
    if mode.QUIET:
        return
    if duplicates:
        secho("\n----------- Duplicates -----------", bold=True)
        secho("\nBased on their MD5 checksum, the following files are duplicates:")
        for k in duplicates:  # noqa: PLC0206
            secho(f"\nMD5 {k}: ", bold=True)
            for path in duplicates[k]:
                secho(f"- {path}")
        secho("\n")


def print_processing_errors(journal: RunJournal) -> None:
    """Print files that encountered an error during conversion or filesystem operations."""
    if journal.processing_errors:
        secho("\n----------- Processing errors -----------", bold=True)
        for err in journal.processing_errors:
            secho(f"\n{_format_bite_size(err[1].filesize): >10}    {err[1].filename}", fg=colors.RED, bold=True)
            _print_logs([err[0]])


def _print_logs(logs: list[LogMsg]) -> None:
    """Print a list of LogMsg entries as single-line timestamped entries."""
    for log in logs:
        secho(f"{log.timestamp}    {log.name}:    {log.msg.replace('\n', ' ')}")


def print_msg(msg: str, quiet: bool) -> None:
    """Print msg unless quiet mode is active."""
    if not quiet:
        secho(msg)


def print_root_not_found() -> None:
    secho("root folder not found", fg=colors.RED)


def _format_bite_size(bytes_size: int) -> str:
    """Convert a byte count to a human-readable MB / GB / TB string."""
    tmp = bytes_size / (1024**2)
    if math.ceil(tmp) > 1000:
        tmp = tmp / 1024
        if math.ceil(tmp) > 1000:
            tmp = tmp / 1024
            return f"{round(tmp, 3)} TB"
        return f"{round(tmp, 3)} GB"
    return f"{round(tmp, 3)} MB"

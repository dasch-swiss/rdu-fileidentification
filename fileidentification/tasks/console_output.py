from pathlib import Path

from rich import box
from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from typer import colors, secho

from fileidentification.definitions.models import BasicAnalytics, LogMsg, Mode, Policies, RunJournal
from fileidentification.definitions.settings import FMT_INFO, FDMsg
from fileidentification.tasks.conversion import ConversionResult

# one shared console for all structured output; dynamic content is printed as literal Text (never markup) so
# values containing brackets (e.g. "conversion failed [magick] boom") render verbatim.
console = Console()


def print_siegfried_errors(ba: BasicAnalytics) -> None:
    """Print files for which siegfried reported a read error during identification."""
    if not ba.siegfried_errors:
        return
    console.line()
    console.rule("[bold red]siegfried read errors", style="red", align="left")
    for sfinfo in ba.siegfried_errors:
        console.print(Text(f"{sfinfo.filename}\n{sfinfo.errors}", style="red"), soft_wrap=True)


def print_fmts(puids: list[str], ba: BasicAnalytics, policies: Policies, mode: Mode) -> None:
    """
    Print a summary table of all encountered file formats with their PUID, name, file count,
    combined size, and policy status.
    Rows are white (policy set), yellow (blank/missing), or red (missing in strict mode).
    """
    if mode.QUIET:
        return
    table = Table(
        title="File formats found",
        box=box.SIMPLE,
        title_style="bold",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("PUID")
    table.add_column("Format Name")
    table.add_column("File Count", justify="right")
    table.add_column("Combined Size", justify="right")
    table.add_column("Policy")

    for puid in puids:
        size = _format_bite_size(sum(s.filesize for s in ba.puid_unique[puid]))
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
        table.add_row(puid, f"{FMT_INFO[puid].name}", f"{len(ba.puid_unique[puid])}", size, po, style=style)
    console.print(table)


def _print_bucket(journal: RunJournal, severity: FDMsg, title: str) -> None:
    """Print one diagnostics bucket: each file, then its processing logs for context."""
    sfinfos = journal.diagnostics.get(severity.name)
    if not sfinfos:
        return
    style = "red" if severity == FDMsg.ERROR else "yellow"
    console.line()
    console.rule(f"[bold {style}]{title}", style=style, align="left")
    for sfinfo in sfinfos:
        _print_file_header(sfinfo.filename, sfinfo.filesize, style)
        _print_logs(sfinfo.processing_logs)


def print_diagnostic(journal: RunJournal, mode: Mode) -> None:
    """Print corruption errors always, and (unless quiet) warnings and extension mismatches."""
    if not mode.QUIET:
        _print_bucket(journal, FDMsg.EXTMISMATCH, "Extension mismatch")
        _print_bucket(journal, FDMsg.WARNING, "Warnings")
    _print_bucket(journal, FDMsg.ERROR, "Errors")


def print_duplicates(duplicates: dict[str, list[Path]], mode: Mode) -> None:
    """Print files that share the same MD5 checksum, grouped under each hash as a tree."""
    if mode.QUIET or not duplicates:
        return
    console.line()
    console.rule("[bold]Duplicates", align="left")
    console.print("Based on their MD5 checksum, the following files are duplicates:")
    for md5, paths in duplicates.items():
        tree = Tree(Text(f"MD5 {md5}", style="bold"))
        for path in paths:
            tree.add(Text(f"{path}", style="dim"))
        console.print(tree)


def print_processing_errors(journal: RunJournal) -> None:
    """Print files that encountered an error during conversion or filesystem operations."""
    if not journal.processing_errors:
        return
    console.line()
    console.rule("[bold red]Processing errors", style="red", align="left")
    for msg, sfinfo, _ in journal.processing_errors:
        _print_file_header(sfinfo.filename, sfinfo.filesize, "red")
        _print_logs([msg])


def _print_file_header(filename: Path, filesize: int, style: str) -> None:
    """Print a file's name (in the section color) with its size dimmed alongside."""
    console.print(
        Text.assemble((f"{filename}", f"bold {style}"), (f"  ({_format_bite_size(filesize)})", "dim")),
        soft_wrap=True,
    )


def _print_logs(logs: list[LogMsg]) -> None:
    """Print LogMsg entries indented under a file: dimmed short time + source, then the message."""
    for log in logs:
        stamp = log.timestamp.strftime("%H:%M:%S") if log.timestamp else "--:--:--"
        line = Text()
        line.append(f"  {stamp}  {log.name}  ", style="dim")
        line.append(log.msg.replace("\n", " "))
        console.print(line, soft_wrap=True)


def print_policy_test(puid: str, result: ConversionResult, workdir: Path) -> None:
    """Print one policy test: the puid, then the command (green) on success, or the reason + logs (red) on failure."""
    console.print(Text(puid, style="bold yellow"), soft_wrap=True)
    if result.converted:
        console.print(Text(result.cmd, style="green"), soft_wrap=True)
    else:
        console.print(Text(result.error.msg if result.error else "conversion failed", style="bold red"), soft_wrap=True)
        console.print(Text(result.cmd, style="dim"), soft_wrap=True)
        if result.bin_log:
            console.print(Text(f"{result.bin_log.name}: {result.bin_log.msg}", style="dim"), soft_wrap=True)
    console.print(Text(f"file (if any) in {workdir}", style="dim"), soft_wrap=True)


def print_msg(msg: str, quiet: bool) -> None:
    """Print msg unless quiet mode is active."""
    if not quiet:
        secho(msg)


def print_error(msg: str) -> None:
    secho(msg, fg=colors.RED)


def _format_bite_size(bytes_size: int) -> str:
    """Convert a byte count to a human-readable string (B / KB / MB / GB / TB)."""
    size = float(bytes_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{round(size, 2)} {unit}"
        size /= 1024
    return f"{round(size, 2)} TB"

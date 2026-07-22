import csv
import json
import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pygfried
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from typer import Exit, colors, secho

from fileidentification.definitions.models import (
    BasicAnalytics,
    LogMsg,
    LogOutput,
    Mode,
    PolicyParams,
    RunJournal,
    SfInfo,
    sfinfo2csv,
)
from fileidentification.definitions.settings import CSVFIELDS, MAX_WORKERS, PYG_WORKERS, Bin
from fileidentification.tasks.console_output import (
    print_diagnostic,
    print_duplicates,
    print_fmts,
    print_msg,
    print_processing_errors,
    print_root_not_found,
    print_siegfried_errors,
)
from fileidentification.tasks.conversion import convert_file
from fileidentification.tasks.inspection import assert_file_integrity, inspect_file
from fileidentification.tasks.os_tasks import move_tmp
from fileidentification.tasks.policies import PolicyError, apply_policy, resolve_policies
from fileidentification.workspace import Workspace


class FileHandler:
    """Main class. It can create, verify and apply policies, test the files on errors, convert and move them."""

    def __init__(self) -> None:
        self.mode: Mode = Mode()
        self.policies: dict[str, PolicyParams] = {}
        self.journal = RunJournal()
        self.ba = BasicAnalytics()
        self.stack: list[SfInfo] = []
        self.ws: Workspace = Workspace(Path(), Path())  # replaced in run() once root_folder / tmp are resolved
        self._stack_lock = threading.Lock()
        self._soffice_lock = threading.Semaphore(1)

    def _build_stack(self, root_folder: Path) -> None:
        """
        Populate self.stack: reload the sfinfos from an existing _log.json at the default location if present,
        otherwise scan root_folder with pygfried and add its output as sfinfos.
        """
        # if there is a log, try to read from there
        if self.ws.logjson.is_file():
            self.stack.extend([SfInfo(**metadata) for metadata in json.loads(self.ws.logjson.read_text())["files"]])

        # scan the root_folder with pygfried only when nothing was reloaded; those files then need relativizing
        initial = not self.stack
        if initial:
            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True
            ) as prog:
                prog.add_task(description="Analysing files with pygfried ...", total=None)
                if root_folder.is_file():
                    scanned = pygfried.identify(f"{root_folder}", detailed=True)["files"]
                else:
                    scanned = pygfried.identify_dir(f"{root_folder}", workers=PYG_WORKERS)["files"]
                self.stack.extend(SfInfo(**sfi) for sfi in scanned)  # type: ignore[arg-type]

        # relativize freshly scanned filenames (portable form), run basic analytics
        for sfinfo in self.stack:
            if initial and not sfinfo.status.removed:
                sfinfo.filename = self.ws.relativize(sfinfo.filename)
            if sfinfo.is_active:
                self.ba.append(sfinfo)

        print_siegfried_errors(ba=self.ba)
        print_duplicates(duplicates=self.ba.duplicates, mode=self.mode)

    # policies stuff
    def _resolve_policies(self, policies_path: Path | None = None, blank: bool = False, extend: bool = False) -> None:
        """
        Set self.policies for the run via the policy-resolution module (generate, read the default location, or
        read an external file). A missing or invalid external file is fatal: persist state, then exit.
        """
        try:
            resolution = resolve_policies(
                self.ba.puid_unique,
                self.ws.poljson,
                self.mode,
                policies_path=policies_path,
                blank=blank,
                extend=extend,
                emit=lambda msg: print_msg(msg, self.mode.QUIET),
            )
        except PolicyError as e:
            secho(str(e), fg=colors.RED)
            self.write_logs()
            sys.exit(1)

        self.policies = resolution.policies
        self.ba.blank = resolution.blank
        print_fmts(list(self.ba.puid_unique), self.ba, self.policies, self.mode)

    def _test_policies(self, puid: str | None = None) -> None:
        """
        Test a policies.json with the smallest files of the directory. if puid is passed, it only tests the puid
        of the policies.
        """

        puids = [puid] if puid else [puid for puid in self.ba.puid_unique if not self.policies[puid].accepted]

        if not puids:
            print_msg("No files found that should be converted with given policies", self.mode.QUIET)
        else:
            print_msg("\n --- Testing policies with a sample from the directory ---", self.mode.QUIET)

            for puid in puids:  # noqa: PLR1704
                # test on a copy: convert_file mutates the sfinfo (logs, status.pending), and this is a
                # diagnostic run that must not pollute the real stack object persisted to _log.json
                sample = self.ba.smallest_file(puid).model_copy(deep=True)
                secho(f"\n{puid}", fg=colors.YELLOW)
                t_sfinfo, cmd, bin_log = convert_file(sample, self.policies, self.ws)
                if t_sfinfo:
                    secho(f"{cmd}", fg=colors.GREEN, bold=True)
                else:
                    # the conversion test failed: surface why (this path is interactive, so print it now)
                    reason = sample.processing_logs[-1].msg if sample.processing_logs else "conversion failed"
                    secho(f"{reason}", fg=colors.RED, bold=True)
                    secho(f"{cmd}")
                    if bin_log:
                        secho(f"{bin_log.name}: {bin_log.msg}")
                secho(f"You find the file (if any) in {self.ws.working_dir(sample.filename)}")

    def _run_parallel(self, items: list[SfInfo], description: str, work: Callable[[SfInfo], object]) -> None:
        """
        Run `work` over `items` on the thread pool, showing a progress bar (labelled `description`) that
        advances as each file completes. Exceptions raised by `work` propagate via future.result().
        """
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(complete_style="green", finished_style="green"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task(description=description, total=len(items))
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for future in as_completed([executor.submit(work, item) for item in items]):
                    future.result()
                    prog.advance(task)

    def inspect(self, to_csv: bool = False) -> None:
        """Probe all active files and write a dated report JSON without modifying the source files."""
        self.ws.poljson.unlink(missing_ok=True)
        self.write_logs()  # persist the bare inventory so a rerun skips the pygfried scan
        active = [s for s in self.stack if s.is_active]
        self._run_parallel(
            active,
            "Probing the files ...",
            lambda sfinfo: inspect_file(sfinfo, self.policies, self.ws, self.journal, self.mode.VERBOSE),
        )

        print_diagnostic(journal=self.journal, mode=self.mode)
        self.write_logs(to_csv=to_csv, target=self.ws.report_json(datetime.now(UTC).strftime("%y%m%d")))

    def assert_integrity(self) -> None:
        """Probe active, not-yet-probed files: remove corrupt ones and rename files with extension mismatches."""
        active = [s for s in self.stack if s.is_active and not s.status.probed]
        self._run_parallel(
            active,
            "Probing the files ...",
            lambda sfinfo: assert_file_integrity(sfinfo, self.policies, self.ws, self.journal, self.mode.VERBOSE),
        )

        print_diagnostic(journal=self.journal, mode=self.mode)

    def _silently_reencode(self, root_folder: Path) -> None:
        """
        Silently convert and clean up files that were flagged for re-encoding during integrity check
        (e.g. non-intra slices in IDR NAL units) without producing console output.
        Called when -i is used without -a.
        """
        self.mode.QUIET = True
        self.mode.REMOVEORIGINAL = True
        self.convert()
        self.remove_tmp(root_folder)

    def apply_policies(self) -> None:
        """Evaluate the policy for active, not-yet-applied files and mark those that need conversion as pending."""
        active = [s for s in self.stack if s.is_active and not s.status.applied]
        self._run_parallel(
            active,
            "Applying policies ...",
            lambda sfinfo: apply_policy(sfinfo, self.policies, self.ws, self.journal, self.mode.STRICT),
        )

    def convert(self) -> None:
        """Convert files whose metadata status pending is True"""

        pending: list[SfInfo] = [sfinfo for sfinfo in self.stack if sfinfo.status.pending]

        if not pending:
            print_msg("There was nothing to convert", self.mode.QUIET)
            return

        def _convert_one(sfinfo: SfInfo) -> None:
            # soffice cannot run concurrent instances, so serialize its conversions through the lock
            soffice = self.policies[sfinfo.processed_as].bin == Bin.SOFFICE  # type: ignore[index]
            ctx = self._soffice_lock if soffice else nullcontext()
            with ctx:
                conv_sfinfo, cmd, bin_log = convert_file(sfinfo, self.policies, self.ws)
            if conv_sfinfo:
                msg = f"converted -> {conv_sfinfo.filename}"
                sfinfo.processing_logs.append(LogMsg(name="filehandler", msg=msg))
                with self._stack_lock:
                    self.stack.append(conv_sfinfo)
            else:
                lmsg = sfinfo.processing_logs.pop()
                lmsg.msg += f". cmd={cmd} "
                # the bin's log (if any) goes in as a detail: recorded in the "errors" copy but not printed
                self.journal.record_error(lmsg, sfinfo, [bin_log] if bin_log else None)

        self._run_parallel(pending, "Converting ...", _convert_one)

    def remove_tmp(self, root_folder: Path) -> None:
        """Move converted files from the tmp dir to their destinations and clean up empty tmp folders."""
        # move converted files from the working dir to its destination
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Moving files ...", total=None)
            files_moved = move_tmp(self.stack, self.ws, self.policies, self.journal, self.mode.REMOVEORIGINAL)

        # remove empty folders in working dir
        if self.ws.tmp_dir.is_dir():
            for path, _, _ in os.walk(self.ws.tmp_dir, topdown=False):
                if len(os.listdir(path)) == 0:  # noqa: PTH208
                    Path(path).rmdir()
        if files_moved:
            print_msg(f"\nMoved the files from {self.ws.tmp_dir.stem} to {root_folder.stem} ...", self.mode.QUIET)

    def write_logs(self, to_csv: bool = False, target: Path | None = None) -> None:
        """
        Write the run state to `target` (default: _log.json) and optionally export a CSV alongside it.
        inspect() passes a dated report path so its read-only output stays separate from a processing run.
        """
        dest = target or self.ws.logjson
        print_processing_errors(journal=self.journal)

        logoutput = LogOutput(files=self.stack, errors=self.journal.error_records(), duplicates=self.ba.duplicates)
        dest.write_text(logoutput.model_dump_json(indent=4, exclude_none=True))

        if to_csv:
            with open(f"{dest}.csv", "w") as f:  # noqa: PTH123
                w = csv.DictWriter(f, CSVFIELDS)
                w.writeheader()
                [w.writerow(sfinfo2csv(el)) for el in self.stack]

    # default run, has a typer interface for the params in identify.py
    def run(  # noqa: C901 flat task orchestration; complexity is from the flag branches, not nesting
        self,
        root_folder: Path | str,
        mode: Mode,
        *,
        assert_integrity: bool = False,
        apply: bool = False,
        remove_tmp: bool = False,
        convert: bool = False,
        policies_path: Path | None = None,
        blank: bool = False,
        extend: bool = False,
        test_puid: str | None = None,
        test_policies: bool = False,
        to_csv: bool = False,
        tmp_dir: Path | None = None,
        inspect: bool = False,
    ) -> None:
        root_folder = Path(root_folder)
        self.mode = mode
        # resolve the run's paths (validates the root, normalizes a single-file target, creates the tmp dir)
        try:
            self.ws = Workspace.for_run(root_folder, tmp_dir)
        except ValueError:
            print_root_not_found()
            raise Exit(1) from None
        # generate a list of SfInfo objects out of the target folder
        self._build_stack(root_folder)
        # the stack is now complete; from here on, persist it on any failure so a restart
        # reloads a full inventory (an incomplete _log.json would suppress a rescan).
        try:
            # generate policies
            self._resolve_policies(policies_path, blank, extend)
            # inspect is a terminal, read-only mode: write the dated report and stop before any file-altering step
            if inspect:
                self.inspect(to_csv=to_csv)
                return
            if assert_integrity:
                self.assert_integrity()
                if not apply:
                    # this triggers -qarx (to catch fixes with reencoding)
                    self._silently_reencode(root_folder)
            # policies testing
            if test_puid:
                self._test_policies(puid=test_puid)
            if test_policies:
                self._test_policies()
            # apply policies
            if apply:
                self.apply_policies()
                self.convert()
            if convert:
                self.convert()
            # remove tmp files
            if remove_tmp:
                self.remove_tmp(root_folder)
            self.write_logs(to_csv=to_csv)
        except Exception:
            if self.stack:
                self.write_logs()
            raise

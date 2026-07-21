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
    LogTables,
    Mode,
    Policies,
    PoliciesFile,
    PolicyParams,
    SfInfo,
    sfinfo2csv,
)
from fileidentification.definitions.settings import CSVFIELDS, DEFAULTPOLICIES, MAX_WORKERS, PYG_WORKERS, Bin
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
from fileidentification.tasks.policies import apply_policy, build_policies
from fileidentification.workspace import Workspace


class FileHandler:
    """Main class. It can create, verify and apply policies, test the files on errors, convert and move them."""

    def __init__(self) -> None:
        self.mode: Mode = Mode()
        self.policies: dict[str, PolicyParams] = {}
        self.log_tables = LogTables()
        self.ba = BasicAnalytics()
        self.stack: list[SfInfo] = []
        self.ws: Workspace = Workspace(Path(), Path())  # replaced in run() once root_folder / tmp are resolved
        self._stack_lock = threading.Lock()
        self._soffice_lock = threading.Semaphore(1)

    def _build_stack(self, root_folder: Path) -> None:
        """
        Add sfinfos to stack.
        Checks whether a log json at default location exists. if so, it adds the sfinfos to the stack from there,
        otherwhise it scans the root_folder with pygfried and adds its output as sfinfos to the stack
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
    def _read_policies(self, policies_path: Path) -> Policies:
        """Read and validate an existing policies.json. A missing or invalid file is fatal: exit after logging."""
        if not policies_path.is_file():
            secho(f"{policies_path} not found", fg=colors.RED)
            self.write_logs()
            sys.exit(1)
        try:
            file: PoliciesFile = PoliciesFile(**json.loads(policies_path.read_text()))
        except ValueError as e:
            secho(e, fg=colors.RED)
            self.write_logs()
            sys.exit(1)

        return file.policies

    def _gen_policies(self, outpath: Path, blank: bool = False, extend: bool = False) -> None:
        """
        Generate a policies.json with the default values of the encountered fileformats and write it to outpath.
        :param blank: if True, generate a blank policies.json
        :param extend: if True, expand the loaded policies with filetypes found in root_folder that are not in the
        loaded policies and write out an updated policies.json
        """
        default_policies: Policies = {} if blank else self._read_policies(DEFAULTPOLICIES)
        policies, blank_puids = build_policies(
            self.ba.puid_unique,
            default_policies,
            self.mode,
            blank=blank,
            extend=extend,
            existing=self.policies,
        )
        if not blank:
            self.ba.blank = blank_puids

        comment = "autogenerated"
        if blank:
            comment += " blank policies"
        else:
            comment += f" using default policies {DEFAULTPOLICIES}"
            if self.mode.STRICT:
                comment += " in strict mode"
            if extend:
                comment += f" updating from {outpath}"
        jsonfile = PoliciesFile(name=outpath, comment=comment, policies=policies)
        jsonfile.name.write_text(jsonfile.model_dump_json(indent=4, exclude_none=True))
        self.policies = policies

    def _resolve_policies(self, policies_path: Path | None = None, blank: bool = False, extend: bool = False) -> None:
        """
        Set the policies according to the parameters passed. either default policies, external passed policies or
        blank.
        """
        # default policies found and no external policies are passed
        if not policies_path and self.ws.poljson.is_file():
            # set default location
            policies_path = self.ws.poljson
        # no default policies found or the blank option is given:
        # fallback: generate the policies with optional flag blank
        if not policies_path or blank:
            policies_path = self.ws.poljson
            print_msg("Generating policies", self.mode.QUIET)
            self._gen_policies(policies_path, blank=blank)
        # load the external passed policies with option -p or default location
        else:
            print_msg(f"Loading policies from {policies_path}", self.mode.QUIET)
            self.policies = self._read_policies(policies_path)

        # expand a passed policies with the filetypes found in root_folder that are not yet in the policies
        if extend and policies_path:
            print_msg(f"Updating the filetypes in policies {self.ws.poljson}", self.mode.QUIET)
            self._gen_policies(self.ws.poljson, extend=extend)

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
                # we want the smallest file first for running the test
                sample = self.ba.smallest_file(puid)
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
        active = [s for s in self.stack if s.is_active]
        self._run_parallel(
            active,
            "Probing the files ...",
            lambda sfinfo: inspect_file(sfinfo, self.policies, self.ws, self.log_tables, self.mode.VERBOSE),
        )

        print_diagnostic(log_tables=self.log_tables, mode=self.mode)
        self.write_logs(to_csv=to_csv, target=self.ws.report_json(datetime.now(UTC).strftime("%y%m%d")))

    def assert_integrity(self) -> None:
        """Probe all active files: remove corrupt ones and rename files with extension mismatches."""
        active = [s for s in self.stack if s.is_active]
        self._run_parallel(
            active,
            "Probing the files ...",
            lambda sfinfo: assert_file_integrity(sfinfo, self.policies, self.ws, self.log_tables, self.mode.VERBOSE),
        )

        print_diagnostic(log_tables=self.log_tables, mode=self.mode)

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
        """Evaluate the policy for every active file and mark those that need conversion as pending."""
        active = [s for s in self.stack if s.is_active]
        self._run_parallel(
            active,
            "Applying policies ...",
            lambda sfinfo: apply_policy(sfinfo, self.policies, self.ws, self.log_tables, self.mode.STRICT),
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
                self.log_tables.processing_error_add(lmsg, sfinfo, [bin_log] if bin_log else None)

        self._run_parallel(pending, "Converting ...", _convert_one)

    def remove_tmp(self, root_folder: Path) -> None:
        """Move converted files from the tmp dir to their destinations and clean up empty tmp folders."""
        # move converted files from the working dir to its destination
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Moving files ...", total=None)
            files_moved = move_tmp(self.stack, self.ws, self.policies, self.log_tables, self.mode.REMOVEORIGINAL)

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
        print_processing_errors(log_tables=self.log_tables)

        logoutput = LogOutput(files=self.stack, errors=self.log_tables.dump_errors(), duplicates=self.ba.duplicates)
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
        assert_integrity: bool = True,
        apply: bool = True,
        remove_tmp: bool = True,
        convert: bool = False,
        policies_path: Path | None = None,
        blank: bool = False,
        extend: bool = False,
        test_puid: str | None = None,
        test_policies: bool = False,
        remove_original: bool = False,
        mode_strict: bool = False,
        mode_verbose: bool = True,
        mode_quiet: bool = True,
        to_csv: bool = False,
        tmp_dir: Path | None = None,
        inspect: bool = False,
    ) -> None:
        root_folder = Path(root_folder)
        # resolve the run's paths (validates the root, normalizes a single-file target, creates the tmp dir)
        try:
            self.ws = Workspace.for_run(root_folder, tmp_dir)
        except ValueError:
            print_root_not_found()
            raise Exit(1) from None
        # set the mode
        self.mode.REMOVEORIGINAL = remove_original
        self.mode.VERBOSE = mode_verbose
        self.mode.STRICT = mode_strict
        self.mode.QUIET = mode_quiet
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

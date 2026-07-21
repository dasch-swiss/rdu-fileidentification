import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pygfried
from rich.progress import Progress, SpinnerColumn, TextColumn
from typer import colors, secho

from fileidentification.definitions.models import (
    BasicAnalytics,
    FilePaths,
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
from fileidentification.definitions.settings import CSVFIELDS, DEFAULTPOLICIES, MAX_WORKERS, PYG_WORKERS
from fileidentification.tasks.console_output import (
    print_diagnostic,
    print_duplicates,
    print_fmts,
    print_msg,
    print_processing_errors,
    print_siegfried_errors,
)
from fileidentification.tasks.conversion import convert_file
from fileidentification.tasks.inspection import assert_file_integrity, inspect_file
from fileidentification.tasks.os_tasks import move_tmp, set_filepaths
from fileidentification.tasks.policies import apply_policy, build_policies
from fileidentification.workspace import Workspace
from fileidentification.wrappers.tools import tool_for


class FileHandler:
    """Main class. It can create, verify and apply policies, test the files on errors, convert and move them."""

    def __init__(self) -> None:
        self.mode: Mode = Mode()
        self.policies: dict[str, PolicyParams] = {}
        self.log_tables = LogTables()
        self.ba = BasicAnalytics()
        self.stack: list[SfInfo] = []
        self.fp: FilePaths = FilePaths()
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
        if self.fp.LOGJSON.is_file():
            self.stack.extend([SfInfo(**metadata) for metadata in json.loads(self.fp.LOGJSON.read_text())["files"]])

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
            if not (sfinfo.status.removed or sfinfo.dest):
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
        if not policies_path and self.fp.POLJSON.is_file():
            # set default location
            policies_path = self.fp.POLJSON
        # no default policies found or the blank option is given:
        # fallback: generate the policies with optional flag blank
        if not policies_path or blank:
            policies_path = self.fp.POLJSON
            print_msg("Generating policies", self.mode.QUIET)
            self._gen_policies(policies_path, blank=blank)
        # load the external passed policies with option -p or default location
        else:
            print_msg(f"Loading policies from {policies_path}", self.mode.QUIET)
            self.policies = self._read_policies(policies_path)

        # expand a passed policies with the filetypes found in root_folder that are not yet in the policies
        if extend and policies_path:
            print_msg(f"Updating the filetypes in policies {self.fp.POLJSON}", self.mode.QUIET)
            self._gen_policies(self.fp.POLJSON, extend=extend)

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
                t_sfinfo, cmd, _ = convert_file(sample, self.policies, self.ws)
                if t_sfinfo:
                    secho(f"{cmd}", fg=colors.GREEN, bold=True)
                    # the test output is not moved, so it lives in the sample's working dir
                    secho(f"You find the file with the log in {self.ws.working_dir(sample.filename)}")

    def inspect(self) -> None:
        """
        Probe all active files and write a dated report JSON without modifying any files.
        Deletes the policies file so the report is not conflated with a processing run.
        """
        self.fp.LOGJSON = self.fp.TMP_DIR / f"{datetime.now(UTC).strftime('%y%m%d')}_report.json"
        self.fp.POLJSON.unlink(missing_ok=True)
        active = [s for s in self.stack if not (s.status.removed or s.dest)]
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Probing the files ...", total=None)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                list(
                    executor.map(
                        lambda sfinfo: inspect_file(sfinfo, self.policies, self.ws, self.log_tables, self.mode.VERBOSE),
                        active,
                    )
                )

        print_diagnostic(log_tables=self.log_tables, mode=self.mode)

    def assert_integrity(self) -> None:
        """Probe all active files: remove corrupt ones and rename files with extension mismatches."""
        active = [s for s in self.stack if not (s.status.removed or s.dest)]
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Probing the files ...", total=None)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                list(
                    executor.map(
                        lambda sfinfo: assert_file_integrity(
                            sfinfo, self.policies, self.ws, self.log_tables, self.mode.VERBOSE
                        ),
                        active,
                    )
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
        active = [s for s in self.stack if not (s.status.removed or s.dest)]
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Applying policies ...", total=None)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                list(
                    executor.map(
                        lambda sfinfo: apply_policy(sfinfo, self.policies, self.ws, self.log_tables, self.mode.STRICT),
                        active,
                    )
                )

    def convert(self) -> None:
        """Convert files whose metadata status pending is True"""

        pending: list[SfInfo] = [sfinfo for sfinfo in self.stack if sfinfo.status.pending]

        if not pending:
            print_msg("There was nothing to convert", self.mode.QUIET)
            return

        def _convert_one(sfinfo: SfInfo) -> None:
            tool = tool_for(self.policies[sfinfo.processed_as].bin)  # type: ignore[index]
            ctx = self._soffice_lock if tool and tool.serialized_run else nullcontext()
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

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Converting ...", total=None)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                list(executor.map(_convert_one, pending))

    def remove_tmp(self, root_folder: Path) -> None:
        """Move converted files from the tmp dir to their destinations and clean up empty tmp folders."""
        # move converted files from the working dir to its destination
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as prog:
            prog.add_task(description="Moving files ...", total=None)
            files_moved = move_tmp(self.stack, self.ws, self.policies, self.log_tables, self.mode.REMOVEORIGINAL)

        # remove empty folders in working dir
        if self.fp.TMP_DIR.is_dir():
            for path, _, _ in os.walk(self.fp.TMP_DIR, topdown=False):
                if len(os.listdir(path)) == 0:  # noqa: PTH208
                    Path(path).rmdir()
        if files_moved:
            print_msg(f"\nMoved the files from {self.fp.TMP_DIR.stem} to {root_folder.stem} ...", self.mode.QUIET)

    def write_logs(self, to_csv: bool = False) -> None:
        """Write the run state to _log.json and optionally export a CSV alongside it."""
        print_processing_errors(log_tables=self.log_tables)

        logoutput = LogOutput(files=self.stack, errors=self.log_tables.dump_errors(), duplicates=self.ba.duplicates)
        self.fp.LOGJSON.write_text(logoutput.model_dump_json(indent=4, exclude_none=True))

        if to_csv:
            with open(f"{self.fp.LOGJSON}.csv", "w") as f:  # noqa: PTH123
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
        # set dirs / paths
        set_filepaths(self.fp, root_folder, tmp_dir)
        # the per-run path calculator (root_folder is normalized to the parent dir for a single-file target)
        self.ws = Workspace(root_folder, self.fp.TMP_DIR)
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
            # probing the files
            if inspect:
                self.inspect()
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

"""Unit tests for FileHandler orchestration that does not need real tooling.

The heavy steps (convert / remove_tmp) are replaced with recorders so the tests
assert on control flow and mode handling rather than on actual conversions.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from fileidentification.definitions.models import LogMsg, Mode, Policies, PolicyParams, SfInfo
from fileidentification.filehandling import FileHandler
from tests.conftest import fake_identify_payload, make_sfinfo, make_ws


def _fake_pygfried(puid: str = "fmt/43") -> SimpleNamespace:
    """A stand-in for the pygfried module.

    ``identify`` answers a single-file query; ``identify_dir`` walks the folder the
    way the real pygfried does and returns one ``files`` entry per file found.
    """

    def identify(path: str, detailed: bool = False) -> dict[str, Any]:
        return fake_identify_payload(path, puid=puid)

    def identify_dir(path: str, workers: int = 1) -> dict[str, Any]:
        return {"files": [identify(f"{f}")["files"][0] for f in sorted(Path(path).glob("**/*")) if f.is_file()]}

    return SimpleNamespace(identify=identify, identify_dir=identify_dir)


class _LockSpy:
    """A context manager that counts how many times it is entered."""

    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class TestBuildStack:
    """_build_stack either scans the folder with pygfried or reloads an existing _log.json."""

    def test_scan_populates_stack_and_analytics(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "a.jpg").write_bytes(b"x")
        (root / "sub" / "b.jpg").write_bytes(b"y")

        fh = FileHandler()
        fh.ws = make_ws(root, tmp_path / "tmp")  # ws.logjson absent -> forces a scan
        monkeypatch.setattr("fileidentification.filehandling.pygfried", _fake_pygfried())

        fh._build_stack(root)

        assert len(fh.stack) == 2
        # filenames were made relative to root (initial=True) and grouped by puid
        assert {s.filename for s in fh.stack} == {Path("a.jpg"), Path("sub/b.jpg")}
        assert set(fh.ba.puid_unique) == {"fmt/43"}
        assert len(fh.ba.puid_unique["fmt/43"]) == 2
        a = next(s for s in fh.stack if s.filename == Path("a.jpg"))
        assert fh.ws.abs_path(a.filename) == root / "a.jpg"

    def test_reload_from_log_skips_scan_and_removed_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fh = FileHandler()
        tmp = tmp_path / "tmp"
        tmp.mkdir()
        fh.ws = make_ws(tmp_path, tmp)

        active = make_sfinfo("sub/a.jpg", md5="a" * 32)
        removed = make_sfinfo("sub/b.jpg", md5="b" * 32)
        removed.status.removed = True
        fh.ws.logjson.write_text(
            json.dumps({"files": [json.loads(active.model_dump_json()), json.loads(removed.model_dump_json())]})
        )

        # an existing log must be reused verbatim; pygfried must not be invoked
        def boom(*_a: Any, **_k: Any) -> None:
            msg = "pygfried scanned the folder despite an existing _log.json"
            raise AssertionError(msg)

        monkeypatch.setattr("fileidentification.filehandling.pygfried", SimpleNamespace(identify=boom))

        fh._build_stack(tmp_path)

        assert len(fh.stack) == 2  # both entries reloaded
        # only the active file is grouped for processing; the removed one is skipped
        assert set(fh.ba.puid_unique) == {"fmt/43"}
        assert len(fh.ba.puid_unique["fmt/43"]) == 1
        loaded_active = next(s for s in fh.stack if not s.status.removed)
        loaded_removed = next(s for s in fh.stack if s.status.removed)
        # reloaded filenames are kept verbatim (already relative / portable)
        assert loaded_active.filename == Path("sub/a.jpg")
        assert loaded_removed.filename == Path("sub/b.jpg")


class TestSilentlyReencode:
    """`-i` without `-a` triggers a quiet, original-replacing re-encode pass."""

    def test_forces_quiet_and_remove_original_then_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        calls: list[str] = []
        monkeypatch.setattr(fh, "convert", lambda: calls.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda: calls.append("remove_tmp"))

        fh._silently_reencode()

        assert fh.mode.QUIET is True
        assert fh.mode.REMOVEORIGINAL is True
        assert calls == ["convert", "remove_tmp"]


class TestConvertNoPending:
    def test_convert_is_noop_without_pending_files(self) -> None:
        fh = FileHandler()
        fh.stack = [make_sfinfo("a.jpg"), make_sfinfo("b.jpg")]  # none pending
        # must not raise and must not touch the (empty) policies dict
        fh.convert()
        assert all(not s.status.added for s in fh.stack)


class TestSkipAlreadyProcessed:
    """assert_integrity / apply_policies skip files already probed / applied on an earlier run (the marking
    itself is done inside assert_file_integrity / apply_policy — see test_inspection / test_policies).
    """

    def test_assert_integrity_skips_already_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fresh, done = make_sfinfo("a.jpg"), make_sfinfo("b.jpg")
        done.status.probed = True  # already probed on a previous run
        fh.stack = [fresh, done]
        seen: list[SfInfo] = []
        monkeypatch.setattr("fileidentification.filehandling.assert_file_integrity", lambda s, *a: seen.append(s))
        monkeypatch.setattr("fileidentification.filehandling.print_diagnostic", lambda **k: None)

        fh.assert_integrity()

        assert seen == [fresh]  # the already-probed file was not re-probed

    def test_apply_policies_skips_already_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fresh, done = make_sfinfo("a.jpg"), make_sfinfo("b.jpg")
        done.status.applied = True  # policies already applied on a previous run
        fh.stack = [fresh, done]
        seen: list[SfInfo] = []
        monkeypatch.setattr("fileidentification.filehandling.apply_policy", lambda s, *a: seen.append(s))

        fh.apply_policies()

        assert seen == [fresh]  # the already-applied file was not re-evaluated


class TestRunTriggersReencode:
    """run() with assert_integrity=True and apply=False must call _silently_reencode."""

    def test_reencode_called_when_i_without_a(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fh = FileHandler()
        order: list[str] = []
        # stub out every heavy step so we only observe the branch logic in run()
        monkeypatch.setattr(fh, "_build_stack", lambda root: order.append("build"))
        monkeypatch.setattr(fh, "_resolve_policies", lambda *a, **k: order.append("policies"))
        monkeypatch.setattr(fh, "assert_integrity", lambda: order.append("assert"))
        monkeypatch.setattr(fh, "_silently_reencode", lambda: order.append("reencode"))
        monkeypatch.setattr(fh, "apply_policies", lambda: order.append("apply"))
        monkeypatch.setattr(fh, "convert", lambda: order.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda: order.append("remove_tmp"))
        monkeypatch.setattr(fh, "write_logs", lambda to_csv=False: order.append("logs"))

        fh.run(root_folder=tmp_path, mode=Mode(), assert_integrity=True, apply=False, remove_tmp=False)

        assert "assert" in order
        assert "reencode" in order
        assert "apply" not in order  # apply was False

    def test_reencode_not_called_when_apply_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fh = FileHandler()
        order: list[str] = []
        monkeypatch.setattr(fh, "_build_stack", lambda root: None)
        monkeypatch.setattr(fh, "_resolve_policies", lambda *a, **k: None)
        monkeypatch.setattr(fh, "assert_integrity", lambda: order.append("assert"))
        monkeypatch.setattr(fh, "_silently_reencode", lambda: order.append("reencode"))
        monkeypatch.setattr(fh, "apply_policies", lambda: order.append("apply"))
        monkeypatch.setattr(fh, "convert", lambda: order.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda: None)
        monkeypatch.setattr(fh, "write_logs", lambda to_csv=False: None)

        fh.run(root_folder=tmp_path, mode=Mode(), assert_integrity=True, apply=True, remove_tmp=False)

        assert "reencode" not in order
        assert "apply" in order


def _fh_with_puids(*puids: str) -> FileHandler:
    """A FileHandler whose BasicAnalytics already groups one sample SfInfo per PUID."""
    fh = FileHandler()
    for i, p in enumerate(puids):
        fh.ba.puid_unique[p] = [make_sfinfo(f"f{i}.x", puid=p, filesize=10 + i)]
    return fh


class TestResolvePoliciesFailure:
    """A missing / invalid external policies file is fatal: FileHandler persists state, then exits."""

    def test_missing_external_policies_persists_state_then_exits(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)  # write_logs (on failure) targets ws.logjson
        fh.stack = [make_sfinfo("a.jpg")]
        with pytest.raises(SystemExit):
            fh._resolve_policies(policies_path=tmp_path / "missing.json")
        assert fh.ws.logjson.is_file()  # run state was persisted before exiting


class TestConvert:
    def test_success_appends_converted_to_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        pending = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        pending.status.pending = True
        fh.stack = [pending]
        fh.policies = {"fmt/43": PolicyParams(format_name="JPEG", bin="magick")}

        converted = make_sfinfo("sub/orig.tif", puid="fmt/353")
        converted.filename = Path("sub/orig.tif")
        monkeypatch.setattr("fileidentification.filehandling.convert_file", lambda s, p, ws: (converted, ["cmd"], None))

        fh.convert()

        assert converted in fh.stack  # the "converted ->" log is written by _verify (see test_conversion)

    def test_soffice_conversion_is_serialized_by_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # soffice cannot run concurrent instances, so its conversions must go through _soffice_lock.
        fh = FileHandler()
        pending = make_sfinfo("sub/legacy.doc", puid="fmt/40")
        pending.status.pending = True
        fh.stack = [pending]
        fh.policies = {
            "fmt/40": PolicyParams(accepted=False, bin="soffice", target_container="docx", expected=["fmt/412"])
        }
        converted = make_sfinfo("sub/legacy.docx", puid="fmt/412")
        monkeypatch.setattr("fileidentification.filehandling.convert_file", lambda s, p, ws: (converted, ["cmd"], None))

        spy = _LockSpy()
        fh._soffice_lock = spy  # type: ignore[assignment]
        fh.convert()

        assert spy.entered == 1  # the soffice branch acquired the serialization lock
        assert converted in fh.stack

    def test_non_soffice_conversion_skips_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        pending = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        pending.status.pending = True
        fh.stack = [pending]
        fh.policies = {
            "fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
        }
        converted = make_sfinfo("sub/orig.tif", puid="fmt/353")
        monkeypatch.setattr("fileidentification.filehandling.convert_file", lambda s, p, ws: (converted, ["cmd"], None))

        spy = _LockSpy()
        fh._soffice_lock = spy  # type: ignore[assignment]
        fh.convert()

        assert spy.entered == 0  # non-soffice bins run unserialized (nullcontext)

    def test_failure_log_lands_in_errors_not_duplicated_in_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the failed sfinfo appears in both _log.json sections, but its failure entry is recorded only in "errors"
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)
        origin = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        origin.status.pending = True
        fh.stack = [origin]
        fh.policies = {"fmt/43": PolicyParams(format_name="JPEG", bin="magick")}

        def failing_convert(sfinfo: SfInfo, policies: Policies, ws: Any) -> tuple[None, list[str], LogMsg]:
            sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="conversion failed"))
            return None, ["thecmd"], LogMsg(name="magick", msg="magick boom detail")

        monkeypatch.setattr("fileidentification.filehandling.convert_file", failing_convert)

        fh.convert()
        fh.write_logs()

        data = json.loads(fh.ws.logjson.read_text())
        files_logs = " ".join(log["msg"] for f in data["files"] for log in f.get("processing_logs", []))
        errors_logs = " ".join(log["msg"] for e in data["errors"] for log in e.get("processing_logs", []))
        assert "conversion failed" not in files_logs and "magick boom detail" not in files_logs  # nothing in "files"
        assert "conversion failed" in errors_logs and "thecmd" in errors_logs  # summary in "errors"
        assert "magick boom detail" in errors_logs  # bin log detail in "errors" only

    def test_bin_log_recorded_only_in_errors_copy_and_not_printed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # end to end: the bin's log ends up only in the "errors" copy, not in "files", and is never printed
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)
        origin = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        origin.status.pending = True
        fh.stack = [origin]
        fh.policies = {
            "fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
        }

        missing = tmp_path / "never.tif"  # converter produces no file -> failure, with a bin log
        monkeypatch.setattr(
            "fileidentification.tasks.conversion._run_tool",
            lambda s, a, tool, ws: (missing, "the cmd", "magick: boom"),
        )

        fh.convert()

        # the summary is the failure reason (+cmd); the bin log rides along only as a detail
        msg, _sfinfo, details = fh.journal.processing_errors[0]
        assert "conversion failed" in msg.msg and "magick: boom" not in msg.msg
        assert [d.msg for d in details] == ["magick: boom"]
        # the origin (the "files" sfinfo) never receives the bin log
        assert not any(log.name == "magick" for log in origin.processing_logs)

        fh.write_logs()
        data = json.loads(fh.ws.logjson.read_text())
        files_logs = " ".join(log["msg"] for f in data["files"] for log in f.get("processing_logs", []))
        errors_logs = " ".join(log["msg"] for e in data["errors"] for log in e.get("processing_logs", []))
        assert "magick: boom" not in files_logs  # not in "files"
        assert "magick: boom" in errors_logs  # only in "errors"
        assert "magick: boom" not in capsys.readouterr().out  # not printed


class TestRemoveTmpCleanup:
    """remove_tmp moves converted files then prunes the empty folders left in the tmp dir."""

    def test_prunes_empty_dirs_but_keeps_nonempty(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.mode.QUIET = True
        fh.ws = make_ws(tmp_path, tmp_path / "tmp")
        (fh.ws.tmp_dir / "empty" / "nested").mkdir(parents=True)  # both levels empty
        (fh.ws.tmp_dir / "keep").mkdir()
        (fh.ws.tmp_dir / "keep" / "file.log").write_bytes(b"x")
        fh.stack = []  # nothing to move

        fh.remove_tmp()

        assert not (fh.ws.tmp_dir / "empty").exists()  # empty tree pruned bottom-up
        assert (fh.ws.tmp_dir / "keep" / "file.log").is_file()  # non-empty folder untouched
        assert fh.ws.tmp_dir.is_dir()  # the (non-empty) tmp root itself survives


class TestWriteLogs:
    def test_csv_export_writes_header_and_rows(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)
        fh.stack = [make_sfinfo("a.jpg"), make_sfinfo("b.jpg")]
        fh.write_logs(to_csv=True)

        assert fh.ws.logjson.is_file()
        csv_file = tmp_path / "_log.json.csv"
        assert csv_file.is_file()
        lines = csv_file.read_text().splitlines()
        assert lines[0].startswith("status,filename")
        assert len(lines) == 3  # header + two rows

    def test_no_csv_when_flag_unset(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)
        fh.stack = [make_sfinfo("a.jpg")]
        fh.write_logs(to_csv=False)
        assert not (tmp_path / "_log.json.csv").exists()

    def test_processing_errors_are_printed_and_persisted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # error_records() is non-destructive, so the same error is both printed and written to "errors"
        # regardless of order (the old print-before-dump ordering constraint is gone).
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)
        sfinfo = make_sfinfo("sub/orig.jpg")
        fh.stack = [sfinfo]
        fh.journal.record_error(LogMsg(name="magick", msg="conversion failed [magick] boom"), sfinfo)

        fh.write_logs()

        out = capsys.readouterr().out
        assert "Processing errors" in out
        assert "conversion failed [magick] boom" in out
        # and the same error survived into the persisted "errors" section (reading it did not clear the table)
        data = json.loads(fh.ws.logjson.read_text())
        errors_logs = " ".join(log["msg"] for e in data["errors"] for log in e.get("processing_logs", []))
        assert "conversion failed [magick] boom" in errors_logs


class TestInspectMode:
    def test_writes_dated_report_and_removes_policies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fh.ws = make_ws(tmp_path, tmp_path)
        fh.ws.poljson.write_text("{}")

        active = make_sfinfo("a.jpg")
        skipped_removed = make_sfinfo("b.jpg")
        skipped_removed.status.removed = True
        fh.stack = [active, skipped_removed]

        probed: list[Any] = []
        monkeypatch.setattr("fileidentification.filehandling.inspect_file", lambda s, *a: probed.append(s))

        fh.inspect()

        assert not fh.ws.poljson.exists()  # policies file deleted so report is standalone
        reports = list(fh.ws.tmp_dir.glob("*_report.json"))
        assert len(reports) == 1  # a dated report was written ...
        assert fh.ws.logjson.exists()  # ... and the inventory was persisted up front so a rerun skips the rescan
        assert probed == [active]  # removed files are skipped


class TestTestPolicies:
    def test_converts_smallest_sample_of_each_convertible_puid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = _fh_with_puids("fmt/199")
        small = make_sfinfo("small.mp4", puid="fmt/199", filesize=1)
        big = make_sfinfo("big.mp4", puid="fmt/199", filesize=999)
        fh.ba.puid_unique["fmt/199"] = [big, small]
        fh.policies = {
            "fmt/199": PolicyParams(accepted=False, bin="ffmpeg", target_container="mp4", expected=["fmt/199"])
        }
        seen: list[SfInfo] = []

        def record(sfinfo: SfInfo, policies: Policies, ws: Any) -> tuple[None, list[str], None]:
            seen.append(sfinfo)
            return None, ["cmd"], None

        monkeypatch.setattr("fileidentification.filehandling.convert_file", record)

        fh._test_policies()
        assert seen == [small]  # smallest file used as the sample

    def test_noop_when_all_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = _fh_with_puids("fmt/43")
        fh.policies = {"fmt/43": PolicyParams(format_name="JPEG", accepted=True)}
        called: list[SfInfo] = []

        def record(sfinfo: SfInfo, policies: Policies, ws: Any) -> tuple[None, list[str], None]:
            called.append(sfinfo)
            return None, ["cmd"], None

        monkeypatch.setattr("fileidentification.filehandling.convert_file", record)
        fh._test_policies()
        assert called == []

    def test_failed_policy_test_prints_reason_and_bin_log(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # a failing sample test must still surface the failure (this path prints immediately, no progress bar)
        fh = _fh_with_puids("fmt/199")
        sample = make_sfinfo("small.mp4", puid="fmt/199", filesize=1)
        sample.processing_logs.append(LogMsg(name="filehandler", msg="did expect ['fmt/199'], got fmt/5 instead"))
        fh.ba.puid_unique["fmt/199"] = [sample]
        fh.policies = {
            "fmt/199": PolicyParams(accepted=False, bin="ffmpeg", target_container="mp4", expected=["fmt/199"])
        }
        monkeypatch.setattr(
            "fileidentification.filehandling.convert_file",
            lambda s, p, ws: (None, ["ffmpeg -i in out"], LogMsg(name="ffmpeg", msg="stream error")),
        )

        fh._test_policies()

        out = capsys.readouterr().out
        assert "got fmt/5 instead" in out  # the failure reason from the sample's logs
        assert "stream error" in out  # the converter's own log

    def test_does_not_mutate_the_original_sample(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # the diagnostic test runs on a copy: the real stack sample's logs and status must be untouched
        fh = _fh_with_puids("fmt/199")
        sample = make_sfinfo("small.mp4", puid="fmt/199", filesize=1)
        fh.ba.puid_unique["fmt/199"] = [sample]
        fh.policies = {
            "fmt/199": PolicyParams(accepted=False, bin="ffmpeg", target_container="mp4", expected=["fmt/199"])
        }

        def failing(s: SfInfo, p: Policies, ws: Any) -> tuple[None, list[str], None]:
            # mimic convert_file's side effects on the sfinfo it receives
            s.processing_logs.append(LogMsg(name="filehandler", msg="conversion failed"))
            s.status.pending = True
            return None, ["cmd"], None

        monkeypatch.setattr("fileidentification.filehandling.convert_file", failing)
        fh._test_policies()

        assert sample.processing_logs == []  # original left untouched (the copy absorbed the mutation)
        assert sample.status.pending is False

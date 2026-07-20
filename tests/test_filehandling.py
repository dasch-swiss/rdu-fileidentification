"""Unit tests for FileHandler orchestration that does not need real tooling.

The heavy steps (convert / remove_tmp) are replaced with recorders so the tests
assert on control flow and mode handling rather than on actual conversions.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from fileidentification.definitions.models import LogMsg, Policies, PolicyParams, SfInfo
from fileidentification.definitions.settings import DEFAULTPOLICIES, FMT2EXT
from fileidentification.filehandling import FileHandler
from tests.conftest import fake_identify_payload, make_sfinfo


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


def _unknown_puid() -> str:
    """A PUID that exists in FMT2EXT but has no default policy (for blank/strict/extend paths)."""
    defaults = json.loads(DEFAULTPOLICIES.read_text())["policies"]
    return next(p for p in FMT2EXT if p not in defaults)


class TestLoadSfinfos:
    """_load_sfinfos either scans the folder with pygfried or reloads an existing _log.json."""

    def test_scan_populates_stack_and_analytics(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "a.jpg").write_bytes(b"x")
        (root / "sub" / "b.jpg").write_bytes(b"y")

        fh = FileHandler()
        fh.fp.TMP_DIR = tmp_path / "tmp"
        fh.fp.LOGJSON = fh.fp.TMP_DIR / "_log.json"  # absent -> forces a scan
        monkeypatch.setattr("fileidentification.filehandling.pygfried", _fake_pygfried())

        fh._load_sfinfos(root)

        assert len(fh.stack) == 2
        # filenames were made relative to root (initial=True) and grouped by puid
        assert {s.filename for s in fh.stack} == {Path("a.jpg"), Path("sub/b.jpg")}
        assert set(fh.ba.puid_unique) == {"fmt/43"}
        assert len(fh.ba.puid_unique["fmt/43"]) == 2
        assert next(s for s in fh.stack if s.filename == Path("a.jpg")).path == root / "a.jpg"

    def test_reload_from_log_skips_scan_and_removed_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fh = FileHandler()
        fh.fp.TMP_DIR = tmp_path / "tmp"
        fh.fp.TMP_DIR.mkdir()
        fh.fp.LOGJSON = fh.fp.TMP_DIR / "_log.json"

        active = make_sfinfo("sub/a.jpg", md5="a" * 32)
        removed = make_sfinfo("sub/b.jpg", md5="b" * 32)
        removed.status.removed = True
        fh.fp.LOGJSON.write_text(
            json.dumps({"files": [json.loads(active.model_dump_json()), json.loads(removed.model_dump_json())]})
        )

        # an existing log must be reused verbatim; pygfried must not be invoked
        def boom(*_a: Any, **_k: Any) -> None:
            msg = "pygfried scanned the folder despite an existing _log.json"
            raise AssertionError(msg)

        monkeypatch.setattr("fileidentification.filehandling.pygfried", SimpleNamespace(identify=boom))

        fh._load_sfinfos(tmp_path)

        assert len(fh.stack) == 2  # both entries reloaded
        # only the active file is grouped for processing; the removed one is skipped
        assert set(fh.ba.puid_unique) == {"fmt/43"}
        assert len(fh.ba.puid_unique["fmt/43"]) == 1
        loaded_active = next(s for s in fh.stack if not s.status.removed)
        loaded_removed = next(s for s in fh.stack if s.status.removed)
        assert loaded_active.path == tmp_path / "sub/a.jpg"  # paths set for active files
        assert loaded_removed.path == Path()  # removed files are left untouched


class TestSilentlyReencode:
    """`-i` without `-a` triggers a quiet, original-replacing re-encode pass."""

    def test_forces_quiet_and_remove_original_then_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        calls: list[str] = []
        monkeypatch.setattr(fh, "convert", lambda: calls.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda root: calls.append(f"remove_tmp:{root}"))

        fh._silently_reencode(Path("/some/root"))

        assert fh.mode.QUIET is True
        assert fh.mode.REMOVEORIGINAL is True
        assert calls == ["convert", "remove_tmp:/some/root"]


class TestConvertNoPending:
    def test_convert_is_noop_without_pending_files(self) -> None:
        fh = FileHandler()
        fh.stack = [make_sfinfo("a.jpg"), make_sfinfo("b.jpg")]  # none pending
        # must not raise and must not touch the (empty) policies dict
        fh.convert()
        assert all(not s.status.added for s in fh.stack)


class TestRunTriggersReencode:
    """run() with assert_integrity=True and apply=False must call _silently_reencode."""

    def test_reencode_called_when_i_without_a(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fh = FileHandler()
        order: list[str] = []
        # stub out every heavy step so we only observe the branch logic in run()
        monkeypatch.setattr("fileidentification.filehandling.set_filepaths", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_load_sfinfos", lambda root: order.append("load"))
        monkeypatch.setattr(fh, "_resolve_policies", lambda *a, **k: order.append("policies"))
        monkeypatch.setattr(fh, "assert_integrity", lambda: order.append("assert"))
        monkeypatch.setattr(fh, "_silently_reencode", lambda root: order.append("reencode"))
        monkeypatch.setattr(fh, "apply_policies", lambda: order.append("apply"))
        monkeypatch.setattr(fh, "convert", lambda: order.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda root: order.append("remove_tmp"))
        monkeypatch.setattr(fh, "write_logs", lambda to_csv=False: order.append("logs"))

        fh.run(root_folder=tmp_path, assert_integrity=True, apply=False, remove_tmp=False)

        assert "assert" in order
        assert "reencode" in order
        assert "apply" not in order  # apply was False

    def test_reencode_not_called_when_apply_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        fh = FileHandler()
        order: list[str] = []
        monkeypatch.setattr("fileidentification.filehandling.set_filepaths", lambda *a, **k: None)
        monkeypatch.setattr(fh, "_load_sfinfos", lambda root: None)
        monkeypatch.setattr(fh, "_resolve_policies", lambda *a, **k: None)
        monkeypatch.setattr(fh, "assert_integrity", lambda: order.append("assert"))
        monkeypatch.setattr(fh, "_silently_reencode", lambda root: order.append("reencode"))
        monkeypatch.setattr(fh, "apply_policies", lambda: order.append("apply"))
        monkeypatch.setattr(fh, "convert", lambda: order.append("convert"))
        monkeypatch.setattr(fh, "remove_tmp", lambda root: None)
        monkeypatch.setattr(fh, "write_logs", lambda to_csv=False: None)

        fh.run(root_folder=tmp_path, assert_integrity=True, apply=True, remove_tmp=False)

        assert "reencode" not in order
        assert "apply" in order


def _fh_with_puids(*puids: str) -> FileHandler:
    """A FileHandler whose BasicAnalytics already groups one sample SfInfo per PUID."""
    fh = FileHandler()
    for i, p in enumerate(puids):
        fh.ba.puid_unique[p] = [make_sfinfo(f"f{i}.x", puid=p, filesize=10 + i)]
    return fh


class TestGenPolicies:
    def test_blank_generates_one_entry_per_puid(self, tmp_path: Path) -> None:
        fh = _fh_with_puids("fmt/43", "fmt/11")
        out = tmp_path / "pol.json"
        fh._gen_policies(out, blank=True)

        assert out.is_file()
        assert set(fh.policies) == {"fmt/43", "fmt/11"}
        assert all(p.accepted for p in fh.policies.values())  # blank policies accept by default
        assert json.loads(out.read_text())["comment"].startswith("autogenerated")

    def test_blank_respects_remove_original_mode(self, tmp_path: Path) -> None:
        fh = _fh_with_puids("fmt/43")
        fh.mode.REMOVEORIGINAL = True
        fh._gen_policies(tmp_path / "pol.json", blank=True)
        assert fh.policies["fmt/43"].remove_original is True

    def test_default_maps_known_puid_to_default_policy(self, tmp_path: Path) -> None:
        fh = _fh_with_puids("fmt/43")
        fh._gen_policies(tmp_path / "pol.json")
        default = json.loads(DEFAULTPOLICIES.read_text())["policies"]["fmt/43"]
        assert fh.policies["fmt/43"].format_name == default["format_name"]

    def test_default_adds_blank_for_unknown_non_strict(self, tmp_path: Path) -> None:
        unknown = _unknown_puid()
        fh = _fh_with_puids(unknown)
        fh._gen_policies(tmp_path / "pol.json")
        assert unknown in fh.policies
        assert fh.policies[unknown].accepted is True  # blank fallback
        assert unknown in (fh.ba.blank or [])

    def test_default_strict_drops_unknown(self, tmp_path: Path) -> None:
        unknown = _unknown_puid()
        fh = _fh_with_puids(unknown)
        fh.mode.STRICT = True
        fh._gen_policies(tmp_path / "pol.json")
        assert unknown not in fh.policies
        assert unknown not in (fh.ba.blank or [])

    def test_extend_keeps_existing_policy(self, tmp_path: Path) -> None:
        # Regression guard: _read_policies(DEFAULTPOLICIES) inside _gen_policies must not clobber self.policies.
        unknown = _unknown_puid()
        fh = _fh_with_puids(unknown)
        fh.policies = {unknown: PolicyParams(format_name="hand-tuned")}
        fh._gen_policies(tmp_path / "pol.json", extend=True)
        assert fh.policies[unknown].format_name == "hand-tuned"
        assert unknown not in (fh.ba.blank or [])  # promoted out of the blank list

    def test_default_propagates_remove_original(self, tmp_path: Path) -> None:
        fh = _fh_with_puids("fmt/43")
        fh.mode.REMOVEORIGINAL = True
        fh._gen_policies(tmp_path / "pol.json")
        assert fh.policies["fmt/43"].remove_original is True


class TestReadPolicies:
    def test_missing_file_exits(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.fp.LOGJSON = tmp_path / "_log.json"
        with pytest.raises(SystemExit):
            fh._read_policies(tmp_path / "missing.json")

    def test_invalid_policy_exits(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.fp.LOGJSON = tmp_path / "_log.json"
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"policies": {"fmt/43": {"bin": "notabin"}}}))
        with pytest.raises(SystemExit):
            fh._read_policies(bad)

    def test_valid_policy_reads(self, tmp_path: Path) -> None:
        fh = FileHandler()
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"policies": {"fmt/43": {"format_name": "JPEG", "accepted": True}}}))
        result = fh._read_policies(good)
        assert "fmt/43" in result


class TestResolvePolicies:
    """_resolve_policies chooses between generating, reading the default location, or reading an external file."""

    def _spy(self, fh: FileHandler, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
        calls: dict[str, list[Any]] = {"gen": [], "read": []}
        monkeypatch.setattr(fh, "_gen_policies", lambda *a, **k: calls["gen"].append((a, k)))
        monkeypatch.setattr(fh, "_read_policies", calls["read"].append)
        monkeypatch.setattr("fileidentification.filehandling.print_fmts", lambda *a, **k: None)
        return calls

    def test_generates_when_nothing_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fh.fp.POLJSON = tmp_path / "_policies.json"  # does not exist
        calls = self._spy(fh, monkeypatch)
        fh._resolve_policies(None)
        assert calls["gen"] and not calls["read"]

    def test_reads_default_location_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fh.fp.POLJSON = tmp_path / "_policies.json"
        fh.fp.POLJSON.write_text("{}")
        calls = self._spy(fh, monkeypatch)
        fh._resolve_policies(None)
        assert calls["read"] == [fh.fp.POLJSON] and not calls["gen"]

    def test_reads_external_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fh.fp.POLJSON = tmp_path / "_policies.json"
        external = tmp_path / "ext.json"
        calls = self._spy(fh, monkeypatch)
        fh._resolve_policies(external)
        assert calls["read"] == [external] and not calls["gen"]

    def test_extend_triggers_gen_after_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fh.fp.POLJSON = tmp_path / "_policies.json"
        external = tmp_path / "ext.json"
        calls = self._spy(fh, monkeypatch)
        fh._resolve_policies(external, extend=True)
        assert calls["read"] == [external]
        assert calls["gen"] and calls["gen"][0][1].get("extend") is True


class TestConvert:
    def test_success_appends_converted_to_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        pending = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        pending.status.pending = True
        pending.root_folder = Path("/root")
        fh.stack = [pending]
        fh.policies = {"fmt/43": PolicyParams(format_name="JPEG", bin="magick")}

        converted = make_sfinfo("sub/orig.tif", puid="fmt/353")
        converted.filename = Path("sub/orig.tif")
        monkeypatch.setattr("fileidentification.filehandling.convert_file", lambda s, p: (converted, ["cmd"]))

        fh.convert()

        assert converted in fh.stack
        assert converted.root_folder == Path("/root")
        assert any("converted ->" in log.msg for log in pending.processing_logs)

    def test_soffice_conversion_is_serialized_by_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # soffice cannot run concurrent instances, so its conversions must go through _soffice_lock.
        fh = FileHandler()
        pending = make_sfinfo("sub/legacy.doc", puid="fmt/40")
        pending.status.pending = True
        pending.root_folder = Path("/root")
        fh.stack = [pending]
        fh.policies = {
            "fmt/40": PolicyParams(accepted=False, bin="soffice", target_container="docx", expected=["fmt/412"])
        }
        converted = make_sfinfo("sub/legacy.docx", puid="fmt/412")
        monkeypatch.setattr("fileidentification.filehandling.convert_file", lambda s, p: (converted, ["cmd"]))

        spy = _LockSpy()
        fh._soffice_lock = spy  # type: ignore[assignment]
        fh.convert()

        assert spy.entered == 1  # the soffice branch acquired the serialization lock
        assert converted in fh.stack

    def test_non_soffice_conversion_skips_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        pending = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        pending.status.pending = True
        pending.root_folder = Path("/root")
        fh.stack = [pending]
        fh.policies = {
            "fmt/43": PolicyParams(accepted=False, bin="magick", target_container="tif", expected=["fmt/353"])
        }
        converted = make_sfinfo("sub/orig.tif", puid="fmt/353")
        monkeypatch.setattr("fileidentification.filehandling.convert_file", lambda s, p: (converted, ["cmd"]))

        spy = _LockSpy()
        fh._soffice_lock = spy  # type: ignore[assignment]
        fh.convert()

        assert spy.entered == 0  # non-soffice bins run unserialized (nullcontext)

    def test_failure_records_processing_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        pending = make_sfinfo("sub/orig.jpg", puid="fmt/43")
        pending.status.pending = True
        fh.stack = [pending]
        fh.policies = {"fmt/43": PolicyParams(format_name="JPEG", bin="magick")}

        def failing_convert(sfinfo: SfInfo, policies: Policies) -> tuple[None, list[str]]:
            # convert_file leaves a diagnostic on the origin before signalling failure
            sfinfo.processing_logs.append(LogMsg(name="filehandler", msg="conversion failed"))
            return None, ["thecmd"]

        monkeypatch.setattr("fileidentification.filehandling.convert_file", failing_convert)

        fh.convert()

        assert len(fh.stack) == 1  # nothing appended
        assert fh.log_tables.processing_errors
        err_msg = fh.log_tables.processing_errors[0][0].msg
        assert "conversion failed" in err_msg and "thecmd" in err_msg


class TestRemoveTmpCleanup:
    """remove_tmp moves converted files then prunes the empty folders left in the tmp dir."""

    def test_prunes_empty_dirs_but_keeps_nonempty(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.mode.QUIET = True
        fh.fp.TMP_DIR = tmp_path / "tmp"
        (fh.fp.TMP_DIR / "empty" / "nested").mkdir(parents=True)  # both levels empty
        (fh.fp.TMP_DIR / "keep").mkdir()
        (fh.fp.TMP_DIR / "keep" / "file.log").write_bytes(b"x")
        fh.stack = []  # nothing to move

        fh.remove_tmp(tmp_path)

        assert not (fh.fp.TMP_DIR / "empty").exists()  # empty tree pruned bottom-up
        assert (fh.fp.TMP_DIR / "keep" / "file.log").is_file()  # non-empty folder untouched
        assert fh.fp.TMP_DIR.is_dir()  # the (non-empty) tmp root itself survives


class TestWriteLogs:
    def test_csv_export_writes_header_and_rows(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.fp.LOGJSON = tmp_path / "_log.json"
        fh.stack = [make_sfinfo("a.jpg"), make_sfinfo("b.jpg")]
        fh.write_logs(to_csv=True)

        assert fh.fp.LOGJSON.is_file()
        csv_file = tmp_path / "_log.json.csv"
        assert csv_file.is_file()
        lines = csv_file.read_text().splitlines()
        assert lines[0].startswith("status,filename")
        assert len(lines) == 3  # header + two rows

    def test_no_csv_when_flag_unset(self, tmp_path: Path) -> None:
        fh = FileHandler()
        fh.fp.LOGJSON = tmp_path / "_log.json"
        fh.stack = [make_sfinfo("a.jpg")]
        fh.write_logs(to_csv=False)
        assert not (tmp_path / "_log.json.csv").exists()


class TestInspectMode:
    def test_writes_dated_report_and_removes_policies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = FileHandler()
        fh.fp.TMP_DIR = tmp_path
        fh.fp.POLJSON = tmp_path / "_policies.json"
        fh.fp.POLJSON.write_text("{}")

        active = make_sfinfo("a.jpg")
        skipped_removed = make_sfinfo("b.jpg")
        skipped_removed.status.removed = True
        fh.stack = [active, skipped_removed]

        probed: list[Any] = []
        monkeypatch.setattr("fileidentification.filehandling.inspect_file", lambda s, *a: probed.append(s))

        fh.inspect()

        assert not fh.fp.POLJSON.exists()  # policies file deleted so report is standalone
        assert fh.fp.LOGJSON.name.endswith("_report.json")
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

        def record(sfinfo: SfInfo, policies: Policies) -> tuple[None, list[str]]:
            seen.append(sfinfo)
            return None, ["cmd"]

        monkeypatch.setattr("fileidentification.filehandling.convert_file", record)

        fh._test_policies()
        assert seen == [small]  # smallest file used as the sample

    def test_noop_when_all_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fh = _fh_with_puids("fmt/43")
        fh.policies = {"fmt/43": PolicyParams(format_name="JPEG", accepted=True)}
        called: list[SfInfo] = []

        def record(sfinfo: SfInfo, policies: Policies) -> tuple[None, list[str]]:
            called.append(sfinfo)
            return None, ["cmd"]

        monkeypatch.setattr("fileidentification.filehandling.convert_file", record)
        fh._test_policies()
        assert called == []

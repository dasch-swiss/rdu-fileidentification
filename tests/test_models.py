"""Unit tests for the pure data models in fileidentification.definitions.models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from fileidentification.definitions.models import (
    BasicAnalytics,
    LogMsg,
    PoliciesFile,
    PolicyParams,
    RunJournal,
    SfInfo,
    get_md5,
    sfinfo2csv,
)
from fileidentification.definitions.settings import FDMsg, PLMsg, PVErr
from tests.conftest import make_sfinfo


class TestGetMd5:
    def test_known_digest(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_bytes(b"hello")
        # md5("hello") is a well-known fixed value
        assert get_md5(f) == "5d41402abc4b2a76b9719d911017c592"


class TestLogMsg:
    def test_timestamp_autoset(self) -> None:
        msg = LogMsg(name="x", msg="y")
        assert msg.timestamp is not None

    def test_timestamp_preserved(self) -> None:
        from datetime import UTC, datetime

        ts = datetime(2020, 1, 1, tzinfo=UTC)
        assert LogMsg(name="x", msg="y", timestamp=ts).timestamp == ts


class TestSfInfoFetchPuid:
    def test_plain_match(self) -> None:
        assert make_sfinfo(puid="fmt/43").processed_as == "fmt/43"

    def test_no_matches_gives_none(self) -> None:
        s = SfInfo(filename=Path("x"), filesize=1, modified="m", errors="", md5="d", matches=[])
        assert s.processed_as is None

    def test_unknown_falls_back_to_extension_fmt(self) -> None:
        s = make_sfinfo(
            puid="UNKNOWN",
            warning="no match; possibilities based on extension are fmt/43",
        )
        assert s.processed_as == "fmt/43"
        assert any(log.msg == PLMsg.FALLBACK for log in s.processing_logs)

    def test_unknown_without_fmt_hint_gives_none(self) -> None:
        s = make_sfinfo(puid="UNKNOWN", warning="no match")
        assert s.processed_as is None

    def test_unknown_picks_first_fmt(self) -> None:
        s = make_sfinfo(puid="UNKNOWN", warning="extension are x-fmt/111 or fmt/222")
        assert s.processed_as == "x-fmt/111"


class TestSfInfoModelPostInit:
    def test_md5_computed_when_missing(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_bytes(b"hello")
        s = SfInfo(
            filename=f,
            filesize=5,
            modified="m",
            errors="",
            matches=[{"id": "fmt/43", "mime": "", "warning": ""}],
        )
        assert s.md5 == "5d41402abc4b2a76b9719d911017c592"


class TestSfInfoIsActive:
    def test_fresh_file_is_active(self) -> None:
        assert make_sfinfo().is_active

    def test_pending_file_is_still_active(self) -> None:
        s = make_sfinfo()
        s.status.pending = True
        assert s.is_active  # pending files are still in play

    def test_removed_file_is_not_active(self) -> None:
        s = make_sfinfo()
        s.status.removed = True
        assert not s.is_active

    def test_conversion_output_with_dest_is_not_active(self) -> None:
        s = make_sfinfo()
        s.dest = Path("sub")
        assert not s.is_active


class TestRunJournal:
    def test_diagnose_buckets_the_file_and_logs_the_message(self) -> None:
        j = RunJournal()
        a, b = make_sfinfo("a.jpg"), make_sfinfo("b.jpg")
        j.diagnose(a, FDMsg.ERROR, LogMsg(name="ffmpeg", msg="corrupt a"))
        j.diagnose(b, FDMsg.ERROR, LogMsg(name="ffmpeg", msg="corrupt b"))
        assert j.diagnostics[FDMsg.ERROR.name] == [a, b]  # files bucketed under the severity
        assert a.processing_logs[-1].msg == "corrupt a"  # message logged on the file's single log list
        assert b.processing_logs[-1].msg == "corrupt b"

    def test_diagnose_uses_processing_logs_for_every_severity(self) -> None:
        # extension mismatch is no longer special-cased: it writes to processing_logs like the others
        j = RunJournal()
        s = make_sfinfo()
        j.diagnose(s, FDMsg.EXTMISMATCH, LogMsg(name="filehandler", msg="wrong ext"))
        assert j.diagnostics[FDMsg.EXTMISMATCH.name] == [s]
        assert s.processing_logs[-1].msg == "wrong ext"

    def test_error_records_returns_copies_and_leaves_originals(self) -> None:
        j = RunJournal()
        s = make_sfinfo()
        msg = LogMsg(name="x", msg="boom")
        j.record_error(msg, s)
        dumped = j.error_records()
        assert dumped is not None
        assert msg in dumped[0].processing_logs  # error recorded in the returned (errors) copy
        assert msg not in s.processing_logs  # original (files) left untouched -> not duplicated
        assert dumped[0].filename == s.filename  # the copy is of the same file

    def test_error_records_is_non_destructive(self) -> None:
        # the ordering constraint is gone: reading the errors view must not clear the table
        j = RunJournal()
        s = make_sfinfo()
        j.record_error(LogMsg(name="x", msg="boom"), s)
        first = j.error_records()
        second = j.error_records()
        assert first is not None and second is not None
        assert len(j.processing_errors) == 1  # not cleared on read
        assert first[0].processing_logs[-1].msg == second[0].processing_logs[-1].msg  # stable across reads

    def test_error_records_empty_returns_none(self) -> None:
        assert RunJournal().error_records() is None


class TestBasicAnalytics:
    def test_append_indexes_by_puid_and_md5(self) -> None:
        ba = BasicAnalytics()
        s = make_sfinfo("a.jpg", puid="fmt/43", md5="abc")
        ba.append(s)
        assert ba.puid_unique["fmt/43"] == [s]
        assert ba.filehashes["abc"] == [Path("a.jpg")]

    def test_append_skips_when_no_puid(self) -> None:
        ba = BasicAnalytics()
        s = SfInfo(filename=Path("x"), filesize=1, modified="m", errors="", md5="d", matches=[])
        ba.append(s)
        assert ba.puid_unique == {}
        assert ba.filehashes == {}

    def test_append_records_siegfried_errors(self) -> None:
        ba = BasicAnalytics()
        ba.append(make_sfinfo("a.jpg", errors="read error"))
        assert len(ba.siegfried_errors) == 1

    def test_empty_source_is_not_a_siegfried_error(self) -> None:
        ba = BasicAnalytics()
        ba.append(make_sfinfo("a.jpg", errors=FDMsg.EMPTYSOURCE))
        assert ba.siegfried_errors == []

    def test_smallest_file(self) -> None:
        ba = BasicAnalytics()
        big = make_sfinfo("big.jpg", filesize=999, md5="b")
        small = make_sfinfo("small.jpg", filesize=1, md5="s")
        ba.append(big)
        ba.append(small)
        assert ba.smallest_file("fmt/43") is small

    def test_duplicates_only_returns_collisions(self) -> None:
        ba = BasicAnalytics()
        ba.append(make_sfinfo("a.jpg", md5="same"))
        ba.append(make_sfinfo("b.jpg", md5="same"))
        ba.append(make_sfinfo("c.jpg", md5="unique"))
        dups = ba.duplicates
        assert set(dups) == {"same"}
        assert len(dups["same"]) == 2


class TestPolicyParams:
    def test_accepted_default_is_valid(self) -> None:
        p = PolicyParams(format_name="JPEG")
        assert p.accepted is True

    def test_rejects_unknown_bin(self) -> None:
        with pytest.raises(ValidationError):
            PolicyParams(bin="notabin")

    def test_rejects_semicolon_in_args(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PolicyParams(processing_args="-i in.mp4; rm -rf /")
        assert PVErr.SEMICOLON in str(exc.value)

    def test_conversion_policy_requires_target_container(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PolicyParams(accepted=False, bin="ffmpeg", expected=["fmt/199"])
        assert PVErr.MISS_CON in str(exc.value)

    def test_conversion_policy_requires_expected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PolicyParams(accepted=False, bin="ffmpeg", target_container="mp4")
        assert PVErr.MISS_EXP in str(exc.value)

    def test_conversion_policy_requires_bin(self) -> None:
        with pytest.raises(ValidationError) as exc:
            PolicyParams(accepted=False, target_container="mp4", expected=["fmt/199"])
        assert PVErr.MISS_BIN in str(exc.value)

    def test_valid_conversion_policy(self) -> None:
        p = PolicyParams(accepted=False, bin="ffmpeg", target_container="mp4", expected=["fmt/199"])
        assert p.target_container == "mp4"


class TestPoliciesFile:
    def test_roundtrips_default_policies(self) -> None:
        import json

        from fileidentification.definitions.settings import DEFAULTPOLICIES

        pf = PoliciesFile(**json.loads(DEFAULTPOLICIES.read_text()))
        assert pf.policies  # non-empty
        # every entry is a validated PolicyParams
        assert all(isinstance(v, PolicyParams) for v in pf.policies.values())


class TestSfInfo2Csv:
    def test_basic_scalar_fields(self) -> None:
        row = sfinfo2csv(make_sfinfo("a.jpg", filesize=42, md5="abc"))
        assert row["filename"] == "a.jpg"
        assert row["filesize"] == 42
        assert row["md5"] == "abc"
        assert row["processed_as"] == "fmt/43"

    def test_status_and_logs(self) -> None:
        s = make_sfinfo()
        s.status.pending = True
        s.processing_logs.append(LogMsg(name="ffmpeg", msg="w1"))
        s.processing_logs.append(LogMsg(name="ffmpeg", msg="w2"))
        row = sfinfo2csv(s)
        assert row["status"] == "pending"
        assert row["processing_logs"] == "w1 ; w2"

    def test_media_info_and_derived_from(self) -> None:
        origin = make_sfinfo("sub/orig.jpg")
        s = make_sfinfo("sub/out.tif", puid="fmt/353")
        s.media_info.append(LogMsg(name="imagemagick", msg="TIFF 10x10"))
        s.processing_logs.append(LogMsg(name="filehandler", msg="converted"))
        s.derived_from = origin
        row = sfinfo2csv(s)
        assert row["media_info"] == "TIFF 10x10"
        assert row["processing_logs"] == "converted"
        assert row["derived_from"] == "sub/orig.jpg"

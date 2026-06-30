"""Unit tests for conversion._verify.

_verify re-identifies a freshly converted file with pygfried and decides whether
the conversion produced the expected format. pygfried is monkeypatched so the
tests are deterministic and do not depend on a real conversion having run.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fileidentification.definitions.settings import FPMsg
from fileidentification.tasks import conversion as conv_mod
from fileidentification.tasks.conversion import _verify
from tests.conftest import make_sfinfo


def _patch_identify(monkeypatch: pytest.MonkeyPatch, target: Path, puid: str) -> None:
    """Make pygfried.identify report `puid` for the converted target file."""

    def fake_identify(path: str, detailed: bool = False) -> dict[str, Any]:
        return {
            "files": [
                {
                    "filename": path,
                    "filesize": 1,
                    "modified": "2024-01-01T00:00:00+00:00",
                    "errors": "",
                    "md5": "f" * 32,
                    "matches": [{"id": puid, "mime": "image/tiff", "warning": ""}],
                }
            ]
        }

    monkeypatch.setattr(conv_mod, "pygfried", SimpleNamespace(identify=fake_identify))


def test_missing_target_is_conversion_failure(tmp_path: Path) -> None:
    """No output file on disk -> conversion failed, original logs CONVFAILED."""
    origin = make_sfinfo("sub/orig.jpg")
    result = _verify(tmp_path / "never-created.tif", origin, expected=["fmt/353"])
    assert result is None
    assert any(FPMsg.CONVFAILED in log.msg for log in origin.processing_logs)


def test_unexpected_format_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output exists but is the wrong format -> rejected, NOTEXPECTEDFMT logged."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/43")  # got jpeg, not the expected tiff

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    result = _verify(target, origin, expected=["fmt/353"])

    assert result is None
    assert origin.status.pending is True  # left pending: conversion did not succeed
    msgs = " ".join(log.msg for log in origin.processing_logs)
    assert FPMsg.NOTEXPECTEDFMT in msgs
    assert "fmt/353" in msgs and "fmt/43" in msgs  # expected vs. actual reported


def test_expected_format_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Output matches an expected PUID -> wired up as a derived file."""
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")

    origin = make_sfinfo("sub/orig.jpg")
    origin.status.pending = True
    result = _verify(target, origin, expected=["fmt/353"])

    assert result is not None
    assert result.processed_as == "fmt/353"
    assert result.derived_from is origin
    assert result.dest == Path("sub")  # placed next to the original
    assert origin.status.pending is False  # original is now resolved


def test_accepts_any_of_several_expected_formats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "orig.tif"
    target.write_bytes(b"data")
    _patch_identify(monkeypatch, target, puid="fmt/353")

    origin = make_sfinfo("sub/orig.jpg")
    result = _verify(target, origin, expected=["fmt/152", "fmt/353"])
    assert result is not None

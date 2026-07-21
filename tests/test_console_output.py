"""Presentation seam: the inline task messages now live in console_output and are testable in isolation."""

from pathlib import Path

import pytest

from fileidentification.tasks.console_output import (
    print_conversion_failed_error,
    print_empty_source_warning,
    print_invalid_streams_error,
    print_manual_rename_warning,
    print_os_error,
    print_root_not_found,
    print_unexpected_format_error,
)


def test_manual_rename_warning_shows_filename_and_hint(capsys: pytest.CaptureFixture[str]) -> None:
    print_manual_rename_warning(Path("sub/pic.jpg"), "expecting one of the following ext: ['tif']")
    out = capsys.readouterr().out
    assert "you should manually rename sub/pic.jpg" in out
    assert "expecting one of the following ext: ['tif']" in out


def test_empty_source_warning(capsys: pytest.CaptureFixture[str]) -> None:
    print_empty_source_warning(Path("empty.bin"))
    assert "empty.bin has empty source" in capsys.readouterr().out


def test_os_error_passthrough(capsys: pytest.CaptureFixture[str]) -> None:
    print_os_error("[Errno 13] Permission denied")
    assert "[Errno 13] Permission denied" in capsys.readouterr().out


def test_root_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    print_root_not_found()
    assert "root folder not found" in capsys.readouterr().out


def test_unexpected_format_error_names_source_and_target(capsys: pytest.CaptureFixture[str]) -> None:
    print_unexpected_format_error(" did expect ['fmt/353'], got fmt/43 instead", Path("a.jpg"), Path("a.tif"))
    out = capsys.readouterr().out
    assert "did expect ['fmt/353'], got fmt/43 instead" in out
    assert "converting a.jpg to a.tif" in out


def test_conversion_failed_error_names_source_and_target(capsys: pytest.CaptureFixture[str]) -> None:
    print_conversion_failed_error(Path("a.jpg"), Path("a.tif"))
    assert "failed to convert a.jpg to a.tif" in capsys.readouterr().out


def test_invalid_streams_error(capsys: pytest.CaptureFixture[str]) -> None:
    print_invalid_streams_error(Path("clip.mp4"))
    assert "clip.mp4 throwing errors" in capsys.readouterr().out

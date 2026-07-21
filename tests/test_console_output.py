"""Presentation seam: the console_output helpers, testable in isolation."""

import pytest

from fileidentification.tasks.console_output import print_root_not_found


def test_root_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    print_root_not_found()
    assert "root folder not found" in capsys.readouterr().out

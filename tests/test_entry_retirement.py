"""Retirement checks for the old entries: the desktop application owns management now."""
import re
import sys
from pathlib import Path

import pytest

LEGACY_ENTRIES = {"bili_assistant.cli", "companion_service.admin", "companion_service.bridge",
                  "companion_service.service", "companion_service.web"}


def test_retired_qiban_command_asks_for_the_desktop_application(monkeypatch, capsys):
    import qiban.cli

    before = set(sys.modules)
    monkeypatch.setattr(sys, "argv", ["qiban", "restart", "--project", "unused"])
    with pytest.raises(SystemExit) as exit_value:
        qiban.cli.main()
    captured = capsys.readouterr()
    assert exit_value.value.code == 2
    assert "Qiban.exe" in captured.err
    assert not LEGACY_ENTRIES & (set(sys.modules) - before)


def test_retired_qiban_command_help_has_no_side_effects(monkeypatch, capsys):
    import qiban.cli

    before = set(sys.modules)
    monkeypatch.setattr(sys, "argv", ["qiban", "--help"])
    assert qiban.cli.main() == 0
    assert "Qiban.exe" in capsys.readouterr().out
    assert not LEGACY_ENTRIES & (set(sys.modules) - before)


def test_manual_page_stays_a_static_in_application_page():
    manual = Path(__file__).resolve().parents[1] / "scripts/desktop_probe/Assets/manual.html"
    text = manual.read_text(encoding="utf8")
    assert "<h1>" in text
    # The page must not need a script or the network: the host serves it with a
    # restrictive CSP and it must also load when the backend is unavailable.
    assert "<script" not in text.lower()
    assert not re.search(r"<link\b[^>]*href=\"https?:", text)
    links = re.findall(r"<a\b[^>]*href=\"([^\"]*)\"", text)
    assert links
    assert all(link == "/" or link.startswith("#") for link in links)
    for link in links:
        if link.startswith("#"):
            assert 'id="' + link[1:] + '"' in text

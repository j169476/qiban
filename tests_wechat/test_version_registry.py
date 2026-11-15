"""Reject incomplete/ambiguous address maps before any native operation."""
import json
from pathlib import Path

import pytest

from companion_wechat.native import versions
from scripts.generate_wechat_versions import render


@pytest.mark.parametrize("mutation", ["missing", "zero", "outside", "abi", "duplicate", "hash", "memory"])
def test_invalid_registry_fails_closed(tmp_path, mutation):
    data = json.loads(Path(versions.__file__).with_name("versions.json").read_text(encoding="utf8"))
    first, second = data["profiles"][:2]
    if mutation == "missing":
        del first["rvas"]["text_builder"]
    elif mutation == "zero":
        first["rvas"]["schedule"] = "0x0"
    elif mutation == "outside":
        first["rvas"]["identity_getter"] = first["image_size"]
    elif mutation == "abi":
        first["abi"] = "unreviewed"
    elif mutation == "duplicate":
        second["id"] = first["id"]
    elif mutation == "hash":
        second["sha256"] = first["sha256"]
    elif mutation == "memory":
        second.update({key: first[key] for key in ("timestamp", "image_size")})
    file = tmp_path / "versions.json"
    file.write_text(json.dumps(data), encoding="utf8")
    with pytest.raises(ValueError, match="invalid_wechat_profile"):
        versions.load_profiles(file)


def test_native_configuration_matches_reader_and_has_no_unknown_fallback():
    header = Path(versions.__file__).with_name("core_version.h").read_text(encoding="utf8")
    assert header == render()
    assert "return legacy" not in header
    assert "InterlockedExchange(&core_profile_id, 0);" in header


@pytest.mark.parametrize("profile", versions.PROFILES)
def test_production_account_chain_detects_stale_or_changing_account(profile):
    # Run the memory fixture against the production reader as well as the legacy probe.
    from unittest.mock import patch

    from companion_wechat.native.protocol import ProbeFailure
    from companion_wechat.native.windows import Windows
    from tests_wechat import test_native_probe as fixture

    with patch.multiple(fixture, Windows=Windows, ProbeFailure=ProbeFailure):
        for mutation in (None, "getter", "timestamp", "identity_changed"):
            fixture.test_account_chain_matches_exact_build_and_detects_changes(profile, mutation)

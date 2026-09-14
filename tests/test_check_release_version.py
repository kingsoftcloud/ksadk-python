from unittest.mock import patch

import pytest

from scripts.check_release_version import check_project


@pytest.mark.parametrize("mode", ["release", "source-sync"])
def test_version_gate_rejects_downgrade_in_all_modes(mode):
    with patch("scripts.check_release_version.fetch_pypi_version", return_value="0.8.5"):
        assert check_project("ksadk", "0.8.4", False, mode=mode) == 1


def test_same_version_requires_explicit_source_sync():
    with patch("scripts.check_release_version.fetch_pypi_version", return_value="0.8.4"):
        assert check_project("ksadk", "0.8.4", False) == 1
        assert check_project("ksadk", "0.8.4", False, mode="source-sync") == 0


def test_source_sync_does_not_hide_registry_failure():
    with patch("scripts.check_release_version.fetch_pypi_version", return_value=None):
        assert check_project("ksadk", "0.8.4", False, mode="source-sync") == 2


@pytest.mark.parametrize("local,published", [
    ("0.8.4rc1", "0.8.4rc2"),
    ("0.8.4-rc1", "0.8.4-rc2"),
])
def test_source_sync_requires_exact_equality_for_prereleases(local, published):
    with patch("scripts.check_release_version.fetch_pypi_version", return_value=published):
        assert check_project("ksadk", local, False, mode="source-sync") == 1

"""Smoke tests for azure-penny.

Minimal, dependency-free checks that keep the DevEx Platform CI test job green
and feed real per-test telemetry to the DevEx portal (run-level DORA + flaky
view). Imports only the pure ``cost_categories`` module (plain set constants,
no Azure SDK or credentials required).
"""

from cost_categories import CAT_COMPUTE, CAT_DATABASE, CAT_STORAGE


def test_category_sets_non_empty():
    assert CAT_COMPUTE
    assert CAT_DATABASE
    assert CAT_STORAGE


def test_known_compute_service_classified():
    assert "Virtual Machines" in CAT_COMPUTE


def test_categories_are_string_sets():
    for value in CAT_COMPUTE:
        assert isinstance(value, str)

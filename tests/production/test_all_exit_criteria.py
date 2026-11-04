"""PP49: production-acceptance integration tests (separate CI stage).

Runs every exit-criterion check from ``scripts/verify_production_ready.py``
and asserts that none of them FAIL.  PENDING criteria (live-stand
measurements: C3/C5/C10/C13) are allowed here — their methodology lives in
the result details and they are upgraded to PASS by a live run of
``python scripts/verify_production_ready.py --live``.

Marked ``production_acceptance`` (registered in pyproject.toml)::

    python -m pytest tests/production -q

The heavy curated pytest groups (PP21/PP24, PP03-PP07, ...) are executed by
the verify script itself (``--no-tests`` disables them), so these tests run
the programmatic checks only (``with_tests=False``) to stay nested-pytest
free.
"""

from __future__ import annotations

import asyncio

import pytest

from scripts.verify_production_ready import CRITERIA, FAIL, run_checks

pytestmark = pytest.mark.production_acceptance


@pytest.fixture(scope="module")
def results_by_id() -> dict:
    results = asyncio.run(
        run_checks(only=None, quick=True, live=False, with_tests=False)
    )
    return {r.id: r for r in results}


@pytest.mark.parametrize(
    "cid,cid_name",
    [(cid, name) for cid, name, _ in CRITERIA],
    ids=[cid for cid, _, _ in CRITERIA],
)
def test_criterion_not_failed(results_by_id, cid: str, cid_name: str) -> None:
    result = results_by_id[cid]
    assert result.status != FAIL, (
        f"{cid} ({cid_name}) FAILED: {result.value}\n{result.details}"
    )


def test_all_15_criteria_covered(results_by_id) -> None:
    assert len(results_by_id) == 15
    assert set(results_by_id) == {f"C{i}" for i in range(1, 16)}


def test_no_criterion_crashed(results_by_id) -> None:
    for cid, result in results_by_id.items():
        assert not result.value.startswith("check raised"), (
            f"{cid} crashed: {result.value}\n{result.details}"
        )

"""What an episode cost, told apart from what it might have cost.

One number was shown as spend and it was not one number: it summed each row's
actual OR, where there was no actual, its estimate. Two screens then disagreed
with each other and with the engine's own ledger, and nothing said which was
the bill.
"""
import sys
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app import progress  # noqa: E402

SERIES, EPISODE = "island_of_no_witnesses", "s01e04"


class _Store:
    """Enough of the store to page rows, with offset, like the real one."""

    def __init__(self, costs, episode=None):
        self._costs = costs
        self._episode = episode if episode is not None else {"spent_usd": 0.0}

    def list(self, table, where=None, order=None, desc=False, limit=None, offset=0):
        rows = [r for r in (self._costs if table == "costs" else [])
                if all(r.get(k) == v for k, v in (where or {}).items())]
        rows = rows[offset or 0:]
        return rows[:limit] if limit else rows

    def get(self, table, where):
        return dict(self._episode) if table == "episodes" else None


def _row(take_id, estimated, actual, stage="live/video", row_id=None):
    return {"id": row_id or take_id, "series_id": SERIES, "episode_id": EPISODE,
            "stage": stage, "take_id": take_id,
            "estimated_usd": estimated, "actual_usd": actual}


@pytest.fixture
def ledger_of(monkeypatch):
    def build(costs, episode=None):
        monkeypatch.setattr(progress, "store", _Store(costs, episode))
        return progress.ledger(SERIES, EPISODE)
    return build


def test_an_estimate_is_never_added_to_what_was_charged(ledger_of):
    """The whole discrepancy in one line: a call that never settled to an
    amount had its estimate counted as spend."""
    out = ledger_of([_row("t1", 0.70, 0.70), _row("t2", 0.70, 0.0)],
                    {"spent_usd": 0.70})
    assert out["internal_actual"] == 0.70
    assert out["estimates_only"] == 0.70
    assert out["agrees"] is True


def test_spend_reports_only_what_settled(ledger_of, monkeypatch):
    monkeypatch.setattr(progress, "store",
                        _Store([_row("t1", 0.70, 0.70), _row("t2", 5.00, 0.0)],
                               {"spent_usd": 0.70}))
    assert progress.spend(SERIES, EPISODE) == 0.70


def test_one_request_counted_once_however_often_it_was_ingested(ledger_of):
    """Re-ingesting a checkpoint rewrites the rows. A second row for the same
    take is the same request, not a second charge."""
    out = ledger_of([_row("t1", 0.70, 0.70, row_id="a"),
                     _row("t1", 0.70, 0.70, row_id="b"),
                     _row("t1", 0.70, 0.70, row_id="c")],
                    {"spent_usd": 0.70})
    assert out["internal_actual"] == 0.70
    assert out["calls"] == 1
    assert out["superseded_rows"] == 2
    assert out["superseded_usd"] == 1.40


def test_a_superseded_row_does_not_come_back_into_the_total(ledger_of):
    """The later row stands: a take that settled again at a corrected amount
    must not be added to the amount it first reported."""
    out = ledger_of([_row("t1", 0.70, 0.70, row_id="first"),
                     _row("t1", 0.70, 0.35, row_id="corrected")],
                    {"spent_usd": 0.35})
    assert out["internal_actual"] == 0.35
    assert out["superseded_usd"] == 0.70


def test_a_disagreement_with_the_engine_is_shown_not_smoothed_over(ledger_of):
    """The engine's own spent_usd is the authoritative internal number. When
    the projected rows do not add up to it, that is reported rather than
    quietly preferring one of them."""
    out = ledger_of([_row("t1", 0.70, 0.70)], {"spent_usd": 5.47})
    assert out["agrees"] is False
    assert out["engine_recorded"] == 5.47
    assert out["internal_actual"] == 0.70


def test_nothing_here_is_called_provider_confirmed(ledger_of):
    """fal returns no price. Every amount in this studio is its own estimate
    at the published rate, and calling one of them a bill is how an estimate
    becomes one."""
    out = ledger_of([_row("t1", 0.70, 0.70)], {"spent_usd": 0.70})
    assert out["provider_confirmed"] is None
    assert out["unverified"] == out["internal_actual"]
    assert "fal does not return a price" in out["note"]


def test_a_mock_or_preview_row_is_not_this_episode_s_live_spend(ledger_of):
    out = ledger_of([_row("t1", 0.70, 0.70),
                     _row("p1", 1.20, 1.20, stage="clip_preview"),
                     _row("m1", 9.99, 9.99, stage="video")],
                    {"spent_usd": 0.70})
    assert out["internal_actual"] == 0.70
    assert out["calls"] == 1


def test_the_total_survives_a_second_pass_over_the_same_rows(ledger_of, monkeypatch):
    """A recovery or a deployment re-reads the same checkpoint. Reading it
    twice must not cost twice."""
    rows = [_row("t1", 0.70, 0.70), _row("t2", 0.08, 0.08)]
    monkeypatch.setattr(progress, "store", _Store(rows, {"spent_usd": 0.78}))
    first = progress.ledger(SERIES, EPISODE)
    second = progress.ledger(SERIES, EPISODE)
    assert first["internal_actual"] == second["internal_actual"] == 0.78


def test_an_empty_episode_costs_nothing_rather_than_failing(ledger_of):
    out = ledger_of([], {"spent_usd": 0.0})
    assert out["internal_actual"] == 0.0 and out["calls"] == 0 and out["agrees"] is True

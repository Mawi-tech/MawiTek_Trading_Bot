"""Tests for decision-log dedup (collapses repeated identical decisions)."""

import decision_log as dl


def _count_lines(path):
    try:
        return sum(1 for line in open(path) if line.strip())
    except FileNotFoundError:
        return 0


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DECISION_LOG_FILE", str(tmp_path / "dec.jsonl"))
    dl._last_decision.clear()   # reset the in-memory dedup cache


def test_identical_decisions_collapse(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for _ in range(10):
        dl.log_decision("AVGO", dl.ACTION_CONSIDERED, "score 45 < 50", score=45)
    assert _count_lines(str(tmp_path / "dec.jsonl")) == 1   # 10 identical → 1


def test_changed_reason_logs_again(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    dl.log_decision("AVGO", dl.ACTION_CONSIDERED, "score 45 < 50")
    dl.log_decision("AVGO", dl.ACTION_CONSIDERED, "score 48 < 50")   # reason changed
    assert _count_lines(str(tmp_path / "dec.jsonl")) == 2


def test_different_tickers_not_collapsed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    dl.log_decision("AVGO", dl.ACTION_CONSIDERED, "score 45 < 50")
    dl.log_decision("CRWD", dl.ACTION_CONSIDERED, "score 45 < 50")   # same reason, diff ticker
    assert _count_lines(str(tmp_path / "dec.jsonl")) == 2


def test_force_always_writes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    dl.log_decision("NVDA", dl.ACTION_TRADED, "filled", force=True)
    dl.log_decision("NVDA", dl.ACTION_TRADED, "filled", force=True)   # identical but forced
    assert _count_lines(str(tmp_path / "dec.jsonl")) == 2


def test_action_change_logs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    dl.log_decision("NVDA", dl.ACTION_CONSIDERED, "below threshold")
    dl.log_decision("NVDA", dl.ACTION_TRADED, "below threshold")   # action changed
    assert _count_lines(str(tmp_path / "dec.jsonl")) == 2

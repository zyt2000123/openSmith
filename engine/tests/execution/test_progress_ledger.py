from __future__ import annotations

import hashlib


def ledger(**kwargs):
    from engine.execution.react.progress import ProgressLedger
    return ProgressLedger(**kwargs)


def observe(subject, tool='poll', args=None, content='same', error=False):
    return subject.observe(tool, args or {}, hashlib.sha256(content.encode()).hexdigest(), is_error=error)


def test_progressing_poll_and_canonical_args():
    subject = ledger()
    for n in range(10):
        assert observe(subject, content=str(n)).kind == 'progress'
    subject = ledger()
    observe(subject, args={'a': 1, 'b': 2})
    observe(subject, args={'b': 2, 'a': 1})
    decision = observe(subject, args={'a': 1, 'b': 2})
    assert decision.kind == 'repeat' and decision.warn
    assert not observe(subject).warn  # new arguments


def test_repeat_warns_once_and_history_is_bounded():
    subject = ledger(window_size=30)
    decisions = [observe(subject) for _ in range(100)]
    assert sum(d.warn for d in decisions) == 1
    assert len(subject.history) == 30
    for n in range(40):
        observe(subject, content=str(n))
    assert len(subject.history) == 30
    assert observe(subject).kind == 'progress'


def test_alternating_cycle_and_changed_tail():
    subject = ledger()
    decisions = [observe(subject, tool=tool) for tool in ['a', 'b'] * 3]
    assert decisions[-1].kind == 'cycle'
    assert decisions[-1].period == 2
    assert decisions[-1].warn
    subject = ledger()
    for n in range(5):
        assert observe(subject, content='x' * 100000 + str(n)).kind == 'progress'


def test_failure_counts_reset_after_success():
    subject = ledger()
    assert observe(subject, error=True).failures == 1
    assert observe(subject, error=True).failures == 2
    observe(subject, content='done')
    assert observe(subject, error=True).failures == 1
    assert observe(subject, content='different', error=True).failures == 1


def test_invalid_window_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        ledger(window_size=2)

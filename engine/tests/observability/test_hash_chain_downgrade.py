"""Regression tests for the hash chain's legacy-tail downgrade attack.

``record_hash`` hashes a record with its own ``hash`` key removed, and
``verify_chain`` treated any record *lacking* a ``hash`` key as an unverified
"legacy" record.  Those two rules composed into a forgery that needed no
hashing capability at all: strip ``"hash"`` from record k and it becomes a
legacy record whose ``record_hash()`` is, by construction, exactly the value
record k+1 already stores in ``prev_hash``.  An arbitrary prefix of the log
could then be deleted and refilled while verification still reported ok.

The fix makes the first chained record *declare* whether a legacy tail
precedes it (``legacy_linked``).  An attacker cannot forge that declaration —
it is covered by the record's own hash — so a truncated log always presents a
first chained record that claims genesis while carrying some other prev_hash.
"""

from __future__ import annotations

import json
from pathlib import Path

from common.hash_chain import HashChainLog, genesis_hash, record_hash, verify_chain


NS = "audit"


def _log(tmp_path: Path, count: int = 8) -> tuple[Path, dict]:
    path = tmp_path / "audit.jsonl"
    log = HashChainLog(path, namespace=NS)
    for index in range(1, count + 1):
        log.append({"cmd": f"command-{index}"})
    anchor = log.seal()
    log.close()
    return path, anchor


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for r in records
        )
    )


def test_intact_chain_verifies(tmp_path: Path) -> None:
    path, anchor = _log(tmp_path)

    result = verify_chain(path, namespace=NS, anchor=anchor)

    assert result.ok
    assert result.records == 8
    assert result.anchor_matches


def test_prefix_deletion_with_stripped_hash_is_detected(tmp_path: Path) -> None:
    """The core attack: delete records 1-4, strip 'hash' from the new head."""
    path, anchor = _log(tmp_path)
    records = _lines(path)[4:]
    records[0].pop("hash")
    _write(path, records)

    result = verify_chain(path, namespace=NS, anchor=anchor)

    assert not result.ok, "a truncated, hash-stripped chain must not verify"


def test_prefix_deletion_and_forged_refill_is_detected(tmp_path: Path) -> None:
    """The attack's payoff — refilling the hole with fabricated records."""
    path, anchor = _log(tmp_path)
    records = _lines(path)[4:]
    records[0].pop("hash")
    _write(path, [{"cmd": "totally-innocent-1"}, {"cmd": "totally-innocent-2"}, *records])

    result = verify_chain(path, namespace=NS, anchor=anchor)

    assert not result.ok


def test_plain_prefix_deletion_is_detected(tmp_path: Path) -> None:
    """Deleting a prefix without stripping anything must also fail."""
    path, anchor = _log(tmp_path)
    _write(path, _lines(path)[4:])

    result = verify_chain(path, namespace=NS, anchor=anchor)

    assert not result.ok


def test_genuine_legacy_tail_still_verifies(tmp_path: Path) -> None:
    """Pre-chain records written before this scheme existed stay supported."""
    path = tmp_path / "audit.jsonl"
    _write(path, [{"cmd": "legacy-1"}, {"cmd": "legacy-2"}])

    log = HashChainLog(path, namespace=NS)
    log.append({"cmd": "chained-1"})
    log.append({"cmd": "chained-2"})
    anchor = log.seal()
    log.close()

    result = verify_chain(path, namespace=NS, anchor=anchor)

    assert result.ok, f"legacy tail rejected: {result.failure}"
    assert result.records == 4


def test_editing_the_last_legacy_record_is_detected(tmp_path: Path) -> None:
    """The legacy tail is bound into the chain via the first record's prev_hash."""
    path = tmp_path / "audit.jsonl"
    _write(path, [{"cmd": "legacy-1"}, {"cmd": "legacy-2"}])
    log = HashChainLog(path, namespace=NS)
    log.append({"cmd": "chained-1"})
    anchor = log.seal()
    log.close()

    records = _lines(path)
    records[1] = {"cmd": "legacy-2-TAMPERED"}
    _write(path, records)

    assert not verify_chain(path, namespace=NS, anchor=anchor).ok


def test_first_chained_record_claiming_genesis_must_match_genesis(tmp_path: Path) -> None:
    """The invariant the fix rests on, asserted directly."""
    path, _ = _log(tmp_path, count=3)
    records = _lines(path)

    assert "legacy_linked" not in records[0]
    assert records[0]["prev_hash"] == genesis_hash(NS)
    # And the declaration is inside the hashed payload, so it cannot be added.
    forged = dict(records[0])
    forged["legacy_linked"] = True
    assert forged["hash"] != record_hash(forged)

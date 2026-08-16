"""Tamper-evident hash chain for append-only JSONL audit sinks.

A hash chain makes a local JSONL log *tamper-evident*: every record carries
``seq``, the SHA-256 of the previous record (``prev_hash``), and its own
``hash``.  Editing a record, reordering records, deleting a middle record, or
inserting a new one breaks the chain and is reported by :func:`verify_chain`.

This is *not* cryptographic immutability against an attacker with write
access to the filesystem — anyone who can edit the log can also rewrite the
chain and the anchor.  What it provides is detection: accidental corruption
and casual editing are caught, and a rollback to a shorter-but-consistent
chain is caught when a sealed anchor (``<log>.head``) is compared.

Design constraints honored here (enforced by existing engine tests):

- ``_ensure_loaded`` scans the tail by streaming the file line-by-line; it
  must never call ``Path.read_text`` (``test_trace_store_recovers_sequence
  _without_reading_the_whole_file`` monkeypatches it to raise).
- :meth:`HashChainLog.append` performs at most one ``os.fsync`` per record,
  and only when the caller requests it (``test_trace_store_defers_sync_for
  _high_frequency_stream_events`` counts fsync calls).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

CHAIN_VERSION = 1
CHAIN_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700

logger = logging.getLogger(__name__)


def canonical_json(value: Any) -> str:
    """Deterministic JSON serialization for hashing (sorted keys, no spaces)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def genesis_hash(namespace: str) -> str:
    """Deterministic chain root for a namespace (one chain per log file)."""
    return sha256_hex(canonical_json({"genesis": namespace, "version": CHAIN_VERSION}))


def record_hash(record: dict) -> str:
    """Hash of one record over every field except its own ``hash``."""
    payload = {key: value for key, value in record.items() if key != "hash"}
    return sha256_hex(canonical_json(payload))


@dataclass(frozen=True)
class ChainVerification:
    """Outcome of walking one hash-chained log file."""

    ok: bool
    records: int = 0
    failure: str | None = None
    anchored: bool = False
    anchor_matches: bool | None = None
    # A trailing fragment without its newline: a crash tear, tolerated rather
    # than reported as tampering, and surfaced here so it is never silent.
    torn_tail: bool = False


class HashChainLog:
    """One hash-chained JSONL log file.

    ``append`` assigns ``seq``/``prev_hash``/``hash`` and writes the record.
    With ``keep_handle=True`` the file handle stays open across appends (the
    audit sink's existing single-handle behavior); otherwise each append opens
    the file with ``O_APPEND`` so records are written atomically per syscall.
    """

    def __init__(
        self,
        path: Path,
        *,
        namespace: str,
        keep_handle: bool = False,
    ) -> None:
        self.path = Path(path)
        self.namespace = namespace
        self.anchor_path = self.path.with_name(self.path.name + ".head")
        self._keep_handle = keep_handle
        self._lock = threading.Lock()
        self._loaded = False
        self._next_seq = 1
        self._prev_hash = genesis_hash(namespace)
        self._legacy_linked = False
        self._handle = None
        self._handle_path: Path | None = None
        # Size of the log as of our own last write.  A different size means some
        # other writer appended, so our cached seq/prev_hash are stale.
        self._observed_size: int | None = None
        # An anchor names an exact head, so it must be cleared before the chain
        # legitimately grows past it.  Set on construction (a previous process
        # may have sealed on shutdown) and again after every seal.
        self._anchor_pending_clear = True

    # ── public API ──────────────────────────────────────────────────────────

    @property
    def file_handle(self):
        """The open file handle when ``keep_handle=True``, else ``None``."""
        return self._handle

    def ensure_handle(self):
        """Open (or reuse) the persistent append handle."""
        if not self._keep_handle:
            return None
        if self._handle is None or self._handle_path != self.path:
            if self._handle is not None:
                self._handle.close()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
            self._handle = open(self.path, "a", encoding="utf-8")
            self._handle_path = self.path
            # open("a") 受 umask 影响；审计日志必须 0600，与 O_APPEND 分支一致。
            try:
                os.chmod(self.path, CHAIN_FILE_MODE)
            except OSError:
                logger.warning("failed to chmod chain log: %s", self.path, exc_info=True)
        return self._handle

    def append(self, record: dict, *, sync: bool = False) -> dict:
        """Chain and persist one record; returns the record as written.

        The whole read-state-then-write sequence runs under an ``flock`` on the
        log file: two *processes* whose in-memory ``seq``/``prev_hash`` were
        loaded from the same head would otherwise both extend it, forking the
        chain into a false "sequence gap" report.  The in-process ``threading``
        lock cannot see the other process; the size check in
        ``_reload_if_externally_appended`` narrows that window but a
        stat-then-write race remains without a file lock.

        A short write (``ENOSPC``) is rolled back by truncating the partial
        bytes and raised — returning normally would acknowledge a record the
        file does not hold, and leaving the bytes would fuse them with the next
        record into a line no verifier can parse.
        """
        with self._lock:
            self._drop_stale_anchor()
            handle = self.ensure_handle()
            if handle is not None:
                created = False
                lock_fd = handle.fileno()
                fd = None
            else:
                created = not self.path.exists()
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    CHAIN_FILE_MODE,
                )
                lock_fd = fd
            try:
                self._lock_log_file(lock_fd)
                self._repair_torn_tail()
                self._reload_if_externally_appended()
                self._ensure_loaded()
                chained = dict(record)
                chained["seq"] = self._next_seq
                chained["prev_hash"] = self._prev_hash
                if self._legacy_linked and self._next_seq == 1:
                    chained["legacy_linked"] = True
                chained["hash"] = record_hash(chained)
                payload = (canonical_json(chained) + "\n").encode("utf-8")

                try:
                    if handle is not None:
                        handle.write(payload.decode("utf-8"))
                        handle.flush()
                        if sync:
                            os.fsync(handle.fileno())
                    else:
                        assert fd is not None
                        written = os.write(fd, payload)
                        if written != len(payload):
                            try:
                                os.ftruncate(fd, os.fstat(fd).st_size - written)
                            except OSError:
                                logger.warning(
                                    "failed to roll back a short write: %s",
                                    self.path,
                                    exc_info=True,
                                )
                            raise OSError(
                                f"short write to {self.path}: "
                                f"{written}/{len(payload)} bytes"
                            )
                        if sync:
                            os.fsync(fd)
                except BaseException:
                    # The file may now hold bytes our cached state does not
                    # know about; force the next append to rescan and repair.
                    self._loaded = False
                    self._observed_size = None
                    raise
            finally:
                self._unlock_log_file(lock_fd)
                if fd is not None:
                    os.close(fd)
            if created:
                self.path.chmod(CHAIN_FILE_MODE)

            self._prev_hash = chained["hash"]
            self._next_seq += 1
            self._remember_size()
            return chained

    def seal(self) -> dict:
        """Anchor the chain head so a later rollback is detectable.

        The log is fsynced first so every previously appended record (including
        deferred-sync ones) is durable before the anchor names the head.
        """
        with self._lock:
            self._reload_if_externally_appended()
            self._ensure_loaded()
            if self._handle is not None:
                self._handle.flush()
                try:
                    os.fsync(self._handle.fileno())
                except OSError:
                    logger.warning("log fsync failed during seal: %s", self.path, exc_info=True)
            elif self.path.is_file():
                try:
                    fd = os.open(self.path, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError:
                    logger.warning("log fsync failed during seal: %s", self.path, exc_info=True)

            anchor = {
                "seq": self._next_seq - 1,
                "hash": self._prev_hash,
                "sealed_at": _now(),
            }
            temp = self.anchor_path.with_name(
                f".{self.anchor_path.name}.{uuid4().hex}.tmp"
            )
            self.anchor_path.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
            try:
                fd = os.open(
                    temp,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    CHAIN_FILE_MODE,
                )
                with os.fdopen(fd, "wb") as handle:
                    handle.write((canonical_json(anchor) + "\n").encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.anchor_path)
                self.anchor_path.chmod(CHAIN_FILE_MODE)
                _fsync_directory(self.anchor_path.parent)
                self._anchor_pending_clear = True
            except OSError:
                temp.unlink(missing_ok=True)
                logger.warning("failed to write chain anchor: %s", self.anchor_path, exc_info=True)
            return anchor

    def verify(self, anchor: dict | None = None) -> ChainVerification:
        """Walk this file and report the first integrity failure."""
        return verify_chain(
            self.path,
            namespace=self.namespace,
            anchor=anchor if anchor is not None else self._read_anchor(),
        )

    def unseal(self) -> None:
        """Remove the sealed anchor so the chain may be legitimately extended.

        A resumed run reuses its run_id: the previous run finished and sealed
        an anchor, then a recoverable run is continued with new records.  The
        stale anchor would make any :meth:`verify` report an ``anchor mismatch``
        for a legitimate extension, so the resume path clears it.  The chain
        itself is untouched — only the "this run is done" marker is dropped;
        the final RUN_FINISHED seals a fresh anchor.
        """
        with self._lock:
            try:
                self.anchor_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "failed to remove chain anchor: %s", self.anchor_path, exc_info=True
                )

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._handle.close()
                except OSError:
                    pass
                self._handle = None
                self._handle_path = None

    # ── internal state ──────────────────────────────────────────────────────

    @staticmethod
    def _lock_log_file(fd: int) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX platform
            return
        fcntl.flock(fd, fcntl.LOCK_EX)

    @staticmethod
    def _unlock_log_file(fd: int) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX platform
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - close() releases it anyway
            pass

    def _repair_torn_tail(self) -> None:
        """Truncate a crash-torn trailing fragment before the chain is extended.

        ``_ensure_loaded`` already treats an unparseable final line as a crash
        tear and links the chain from the previous valid record — but that is
        only the read side.  Left in place, the fragment fuses with the next
        ``O_APPEND`` write into one line no parser can read: ``verify_chain``
        reports tamper forever, the fused record is unreadable even though
        ``append`` acknowledged it, and the next load re-assigns its ``seq``.

        Only a fragment *without* its trailing newline is repaired — a
        canonical record never contains a raw newline, so a complete line that
        fails to parse is still treated as tampering by ``verify_chain``.  The
        removed bytes are preserved in a ``.torn`` sidecar first: repair of an
        audit log must not silently destroy bytes.  Runs under the append
        flock, so a concurrent writer cannot be mid-record here.
        """
        try:
            size = os.stat(self.path).st_size
        except OSError:
            return
        if size == 0:
            return
        with open(self.path, "rb") as source:
            source.seek(-1, os.SEEK_END)
            if source.read(1) == b"\n":
                return
            # Scan backwards for the last newline; the torn tail is small (at
            # most one record), so this touches a block or two.
            position = size
            last_newline = -1
            while position > 0 and last_newline < 0:
                step = min(4096, position)
                position -= step
                source.seek(position)
                chunk = source.read(step)
                index = chunk.rfind(b"\n")
                if index >= 0:
                    last_newline = position + index
            keep = last_newline + 1
            source.seek(keep)
            torn = source.read()

        sidecar = self.path.with_name(self.path.name + ".torn")
        try:
            sidecar_fd = os.open(
                sidecar,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                CHAIN_FILE_MODE,
            )
            try:
                os.write(sidecar_fd, torn + b"\n")
            finally:
                os.close(sidecar_fd)
        except OSError:
            logger.warning(
                "failed to preserve torn tail bytes: %s", sidecar, exc_info=True
            )
        os.truncate(self.path, keep)
        logger.warning(
            "repaired torn tail of %s: %d byte(s) preserved in %s",
            self.path,
            len(torn),
            sidecar.name,
        )

    def _read_anchor(self) -> dict | None:
        if not self.anchor_path.is_file():
            return None
        try:
            value = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _drop_stale_anchor(self) -> None:
        """Clear a sealed anchor before extending the chain past it.

        ``verify_chain`` requires the anchor to name the *current* head, so any
        log that legitimately grows after a seal would report ``anchor
        mismatch``.  The install-wide audit trail is sealed at shutdown and
        extended again on the next start, which would otherwise make every
        post-restart verification look like tampering.  Runs at most once per
        seal, so the ordinary append path costs nothing.  The next :meth:`seal`
        re-anchors the new head.
        """
        if not self._anchor_pending_clear:
            return
        self._anchor_pending_clear = False
        try:
            self.anchor_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "failed to clear chain anchor before append: %s",
                self.anchor_path,
                exc_info=True,
            )

    def _remember_size(self) -> None:
        try:
            self._observed_size = self.path.stat().st_size
        except OSError:
            self._observed_size = None

    def _reload_if_externally_appended(self) -> None:
        """Re-read the tail when another writer appended since our last write.

        ``seq``/``prev_hash`` are cached in memory and ``_ensure_loaded`` only
        ever runs once per instance, so a second writer on the same file forks
        the chain: both assign the same ``seq`` from the same ``prev_hash``, and
        :func:`verify_chain` then reports a sequence gap on a log nobody
        tampered with — a false tamper alarm produced by ordinary concurrent
        use.  Comparing the file size against our own last write costs one
        ``stat`` and, in the overwhelmingly common single-writer case, skips the
        rescan entirely.
        """
        if not self._loaded:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if self._observed_size is not None and size == self._observed_size:
            return
        self._loaded = False
        self._next_seq = 1
        self._prev_hash = genesis_hash(self.namespace)
        self._legacy_linked = False
        self._ensure_loaded()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # Stream the tail instead of reading the whole file: existing tests
        # assert sequence recovery never calls Path.read_text.
        last_valid: dict | None = None
        last_seq = 0
        saw_chain_record = False
        if self.path.is_file():
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # A torn final line from a crash; the chain ends at the
                        # previous valid record.
                        continue
                    if not isinstance(value, dict):
                        continue
                    last_valid = value
                    seq = value.get("seq")
                    if isinstance(seq, int):
                        last_seq = max(last_seq, seq)
                    if "hash" in value:
                        saw_chain_record = True
        if last_valid is not None:
            if "hash" in last_valid:
                self._prev_hash = last_valid["hash"]
                self._legacy_linked = False
            else:
                # Legacy pre-chain tail: link the chain to the last legacy
                # record so a later edit of it is detected.
                self._prev_hash = record_hash(last_valid)
                self._legacy_linked = True
            self._next_seq = last_seq + 1
        self._loaded = True


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(path: Path) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def verify_chain(
    path: Path,
    *,
    namespace: str,
    anchor: dict | None = None,
) -> ChainVerification:
    """Walk a hash-chained JSONL file and report the first integrity failure.

    Records before the first chained record are treated as a legacy tail: only
    the *last* legacy record is bound into the chain (via the first chained
    record's ``prev_hash``), so edits to any legacy record are still detected.
    """
    path = Path(path)
    if not path.is_file():
        if anchor is not None:
            return ChainVerification(
                ok=False,
                records=0,
                failure="anchor exists but the log file is missing",
                anchored=True,
                anchor_matches=False,
            )
        return ChainVerification(ok=True, records=0, anchored=False, anchor_matches=None)

    prev_chained_seq: int | None = None
    prev_chained_hash: str | None = None
    last_legacy: dict | None = None
    chain_started = False
    record_count = 0
    torn_tail = False
    with path.open(encoding="utf-8", newline="") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if not line.endswith("\n"):
                    # A fragment missing its newline can only be the file's
                    # final line: a crash tear, not tampering.  The writer
                    # truncates it on its next append (_repair_torn_tail); the
                    # chain ends at the previous record either way.  A
                    # *complete* unparseable line stays a failure — canonical
                    # records never contain a raw newline, so a tear cannot
                    # produce one.
                    torn_tail = True
                    break
                return ChainVerification(
                    ok=False,
                    records=record_count,
                    failure=f"unparseable record at line {record_count + 1}",
                )
            if not isinstance(value, dict):
                return ChainVerification(
                    ok=False,
                    records=record_count,
                    failure=f"record at line {record_count + 1} is not an object",
                )
            record_count += 1
            index = record_count

            if "hash" not in value:
                if chain_started:
                    return ChainVerification(
                        ok=False,
                        records=index - 1,
                        failure=f"unchained record {index} appears after the chain started",
                    )
                last_legacy = value
                continue
            chain_started = True

            seq = value.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool):
                return ChainVerification(
                    ok=False,
                    records=index - 1,
                    failure=f"chained record {index} has an invalid seq",
                )
            if prev_chained_seq is not None and seq != prev_chained_seq + 1:
                return ChainVerification(
                    ok=False,
                    records=index - 1,
                    failure=(
                        f"sequence gap at record {index}: expected "
                        f"{prev_chained_seq + 1}, got {seq}"
                    ),
                )
            if value.get("hash") != record_hash(value):
                return ChainVerification(
                    ok=False,
                    records=index - 1,
                    failure=f"hash mismatch at record {index}",
                )
            if prev_chained_hash is None:
                # The first chained record must *declare* a preceding legacy
                # tail before one is admitted.  Inferring it from the absence
                # of a ``hash`` key is forgeable: ``record_hash`` excludes that
                # key, so deleting it from record k turns k into a "legacy"
                # record whose record_hash() is, by construction, exactly the
                # prev_hash record k+1 already stores — letting any prefix be
                # dropped and refilled.  The declaration itself is covered by
                # the record's hash, so it cannot be added after the fact.
                declared_legacy = value.get("legacy_linked") is True
                if declared_legacy and last_legacy is None:
                    return ChainVerification(
                        ok=False,
                        records=index - 1,
                        failure=(
                            f"chained record {index} declares a legacy tail "
                            "but none precedes it"
                        ),
                    )
                if not declared_legacy and last_legacy is not None:
                    return ChainVerification(
                        ok=False,
                        records=index - 1,
                        failure=(
                            f"unchained records precede chained record {index}, "
                            "which does not declare a legacy tail"
                        ),
                    )
                expected_prev = (
                    record_hash(last_legacy) if declared_legacy else genesis_hash(namespace)
                )
            else:
                expected_prev = prev_chained_hash
            if value.get("prev_hash") != expected_prev:
                return ChainVerification(
                    ok=False,
                    records=index - 1,
                    failure=f"prev_hash mismatch at record {index}",
                )
            prev_chained_seq = seq
            prev_chained_hash = value["hash"]

    anchored = anchor is not None
    anchor_matches: bool | None = None
    if anchored:
        anchor_matches = (
            prev_chained_seq == anchor.get("seq")
            and prev_chained_hash == anchor.get("hash")
        )
        if not anchor_matches:
            return ChainVerification(
                ok=False,
                records=record_count,
                failure=(
                    f"anchor mismatch: log head (seq={prev_chained_seq}) does not "
                    f"match the sealed anchor (seq={anchor.get('seq')})"
                ),
                anchored=True,
                anchor_matches=False,
                torn_tail=torn_tail,
            )
    return ChainVerification(
        ok=True,
        records=record_count,
        anchored=anchored,
        anchor_matches=anchor_matches,
        torn_tail=torn_tail,
    )

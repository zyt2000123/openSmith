from __future__ import annotations

import asyncio
import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest


def store(root, **kwargs):
    from engine.tool.output_store import ToolOutputStore
    return ToolOutputStore(root, **kwargs)


def test_small_output_does_not_create_archive(tmp_path):
    capture = store(tmp_path / 'absent').capture('hello', 'read')
    assert capture.preview == 'hello'
    assert not capture.truncated and capture.path is None
    assert capture.raw_digest == hashlib.sha256(b'hello').hexdigest()
    assert not (tmp_path / 'absent').exists()


def test_concurrent_outputs_are_private_distinct_and_immutable(tmp_path):
    output = store(tmp_path, max_bytes=10)
    texts = [f'{i}:hello world' for i in range(20)]
    with ThreadPoolExecutor() as pool:
        captures = list(pool.map(lambda text: output.capture(text, '../../read'), texts))
    assert len({capture.path for capture in captures}) == len(texts)
    for capture, text in zip(captures, texts):
        assert capture.path.parent == tmp_path
        assert capture.path.read_text() == text
        assert stat.S_IMODE(capture.path.stat().st_mode) == 0o600


@pytest.mark.parametrize('text,max_bytes,max_lines', [('你' * 100, 10, 2), ('a\nb\nc', 100, 2), ('x' * 100, 10, 2)])
def test_preview_is_bounded_and_nonempty(tmp_path, text, max_bytes, max_lines):
    capture = store(tmp_path, max_bytes=max_bytes, max_lines=max_lines).capture(text, 'read')
    preview = capture.preview.split('\n\n...', 1)[0]
    assert preview
    assert len(preview.encode()) <= max_bytes
    assert len(preview.split('\n')) <= max_lines
    assert capture.size_bytes == len(text.encode())


def test_secret_redaction_and_raw_digest_are_separate(tmp_path):
    text = 'OPENAI_API_KEY=sk-' + 'a' * 60 + '\n' + 'x' * 100
    capture = store(tmp_path, max_bytes=10).capture(text, 'read')
    archived = capture.path.read_text()
    assert 'sk-' + 'a' * 60 not in archived
    assert capture.raw_digest == hashlib.sha256(text.encode()).hexdigest()
    assert capture.stored_digest == hashlib.sha256(archived.encode()).hexdigest()
    assert capture.raw_digest != capture.stored_digest


def test_save_failure_returns_bounded_preview_without_claiming_a_path(tmp_path):
    root = tmp_path / 'file'
    root.write_text('not a directory')
    capture = store(root, max_bytes=10).capture('x' * 100, 'read')
    assert capture.path is None
    assert 'Failed to save' in capture.preview
    assert 'Full output saved to:' not in capture.preview
    assert capture.preview.startswith('x' * 10)


def test_default_store_tracks_runtime_root(tmp_path):
    from common import config
    from common.paths import AppPaths
    from engine.tool.output_store import ToolOutputStore
    output = ToolOutputStore(max_bytes=1)
    roots = [tmp_path / 'one', tmp_path / 'two']
    for root in roots:
        config.reset_paths(AppPaths(root, AppPaths.defaults().project_root))
        assert output.capture('hello').path.parent == root / 'tool-output'


def test_registry_records_full_digest_before_truncation():
    from engine.tool.interface import ToolCall
    from engine.tool.registry import ToolRegistry
    async def run():
        registry = ToolRegistry()
        raw = 'prefix\n' * 10000 + 'tail'
        registry.register('read', 'Read', {}, lambda: raw)
        result = await registry.execute(ToolCall('id', 'read', {}))
        assert result.metadata['output']['raw_digest'] == hashlib.sha256(raw.encode()).hexdigest()
        assert result.metadata['output']['truncated'] is True
        assert result.metadata['output']['size_bytes'] == len(raw.encode())
    asyncio.run(run())


def test_write_and_cleanup_failure_do_not_hide_tool_result(tmp_path, monkeypatch):
    from engine.tool import output_store
    from pathlib import Path
    class BrokenFile:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def write(self, value):
            raise OSError('disk full')
    original_fdopen = output_store.os.fdopen
    def broken_fdopen(fd, mode):
        broken = BrokenFile()
        broken.file = original_fdopen(fd, mode)
        return broken
    def cannot_unlink(*args, **kwargs):
        raise OSError('filesystem unavailable')
    monkeypatch.setattr(output_store.os, 'fdopen', broken_fdopen)
    monkeypatch.setattr(Path, 'unlink', cannot_unlink)
    capture = store(tmp_path, max_bytes=1).capture('hello')
    assert capture.path is None and 'Failed to save' in capture.preview

from __future__ import annotations

import pytest

from app.schemas.session import MessageCreate


def test_message_create_requires_a_nonempty_working_directory() -> None:
    with pytest.raises(ValueError, match="working_dir"):
        MessageCreate(content="hello")

    with pytest.raises(ValueError, match="working_dir"):
        MessageCreate(content="hello", working_dir="   ")

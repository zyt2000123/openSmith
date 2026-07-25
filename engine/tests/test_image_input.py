from __future__ import annotations

import asyncio
import base64

import pytest

from engine.llm.adapters.anthropic import AnthropicAdapter
from engine.llm.contracts import ChatResponse, LLMResponseError
from engine.llm.image_input import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    find_images,
    resolve_image_messages,
)
from engine.llm.vision_probe import reset_vision_cache

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def write_png(directory, name="shot.png"):
    path = directory / name
    path.write_bytes(PNG_BYTES)
    return path


class _Model:
    provider = "openai"

    def __init__(self, model="main-model", *, sees_images=True, reply="described"):
        self.model = model
        self.sees_images = sees_images
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, prefix_cache_key=None):
        self.calls.append(messages)
        if not self.sees_images and _has_image(messages):
            raise LLMResponseError("LLM request failed (HTTP 400) after 1 attempt(s).")
        return ChatResponse(text=self.reply)


def _has_image(messages) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
    )


def resolve(text, working_dir, llm, vision_llm=None):
    reset_vision_cache()
    return asyncio.run(resolve_image_messages(text, working_dir, llm, vision_llm))


# --- path detection -------------------------------------------------------


def test_an_absolute_path_in_the_message_is_picked_up(tmp_path):
    path = write_png(tmp_path)

    found, skipped = find_images(f"看看 {path} 这个报错", tmp_path)

    assert [item.path for item in found] == [path]
    assert skipped == []


def test_a_dragged_path_with_escaped_spaces_resolves(tmp_path):
    """macOS 截图文件名一律带空格，终端拖入会转义成 '\\ '。这条不过，功能在
    Mac 上等于不存在。"""
    path = write_png(tmp_path, "Screenshot 2026-07-25 at 20.00.00.png")

    found, _ = find_images(f"{str(path).replace(' ', chr(92) + ' ')} 什么问题", tmp_path)

    assert [item.path for item in found] == [path]


def test_an_unescaped_path_with_spaces_resolves(tmp_path):
    """Finder 的「拷贝路径名」(⌥⌘C) 给的是裸空格，不像拖入那样转义。

    这条是真调验证抓出来的：单测当时全绿，而真实路径带裸空格时检测直接空手。
    """
    path = write_png(tmp_path, "probe card.png")

    found, _ = find_images(f"这张图里的数字是什么 {path} 谢谢", tmp_path)

    assert [item.path for item in found] == [path]


def test_the_fullest_path_wins_over_a_bare_tail(tmp_path):
    """左侧候选必须从长到短试：先命中的短尾巴会指向另一个文件。"""
    nested = tmp_path / "sub"
    nested.mkdir()
    write_png(tmp_path, "shot.png")
    target = write_png(nested, "shot.png")

    found, _ = find_images(f"look at {target}", tmp_path)

    assert [item.path for item in found] == [target]


@pytest.mark.parametrize("quote", ['"', "'"])
def test_a_quoted_path_with_spaces_resolves(tmp_path, quote):
    path = write_png(tmp_path, "my shot.png")

    found, _ = find_images(f"{quote}{path}{quote} 看下", tmp_path)

    assert [item.path for item in found] == [path]


def test_a_path_glued_to_chinese_text_still_resolves(tmp_path):
    """中文没有词间空格，'看看/Users/...' 里路径前面没有分隔符。"""
    path = write_png(tmp_path)

    found, _ = find_images(f"看看{path}", tmp_path)

    assert [item.path for item in found] == [path]


def test_a_relative_path_resolves_against_the_working_directory(tmp_path):
    write_png(tmp_path)

    found, _ = find_images("check shot.png please", tmp_path)

    assert [item.path.name for item in found] == ["shot.png"]


def test_the_same_image_mentioned_twice_is_attached_once(tmp_path):
    path = write_png(tmp_path)

    found, _ = find_images(f"{path} and again {path}", tmp_path)

    assert len(found) == 1


def test_a_path_that_does_not_exist_is_not_an_attachment(tmp_path):
    found, skipped = find_images("compare a.png with b.png", tmp_path)

    assert (found, skipped) == ([], [])


def test_a_non_image_extension_is_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")

    found, skipped = find_images("read notes.txt", tmp_path)

    assert (found, skipped) == ([], [])


def test_an_oversized_image_is_reported_rather_than_dropped(tmp_path):
    """静默丢弃会让用户以为图片已被读取。"""
    path = tmp_path / "huge.png"
    path.write_bytes(b"\x89PNG" + b"0" * MAX_IMAGE_BYTES)

    found, skipped = find_images(str(path), tmp_path)

    assert found == []
    assert skipped == ["huge.png"]


def test_attachments_stop_at_the_limit_and_the_rest_are_reported(tmp_path):
    paths = [write_png(tmp_path, f"s{index}.png") for index in range(MAX_IMAGES + 2)]

    found, skipped = find_images(" ".join(str(path) for path in paths), tmp_path)

    assert len(found) == MAX_IMAGES
    assert len(skipped) == 2


# --- capability routing ---------------------------------------------------


def test_a_vision_capable_main_model_receives_the_image_itself(tmp_path):
    path = write_png(tmp_path)
    llm = _Model(sees_images=True)

    messages = resolve(str(path), tmp_path, llm)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert _has_image(messages), "主模型能读图时必须把图本身送进去，而不是转述"


def test_a_blind_main_model_falls_back_to_the_configured_image_model(tmp_path):
    path = write_png(tmp_path)
    main = _Model("blind-model", sees_images=False)
    vision = _Model("image-model", sees_images=True, reply="A stack trace: KeyError")

    messages = resolve(str(path), tmp_path, main, vision)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert not _has_image(messages), "主模型读不了图，送图进去只会换回一个 400"
    assert "KeyError" in messages[0]["content"]
    assert _has_image(vision.calls[0]), "转述模型必须真的收到图"


def test_a_blind_main_model_with_no_image_model_tells_the_user(tmp_path):
    path = write_png(tmp_path)
    main = _Model("blind-model", sees_images=False)

    messages = resolve(str(path), tmp_path, main)

    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert not _has_image(messages)
    assert "shot.png" in messages[0]["content"]
    # 提示交给模型用用户的语言说，而不是硬编码中文/英文字符串。
    assert "user's language" in messages[0]["content"]


def test_a_failing_image_model_falls_through_to_the_notice(tmp_path):
    """转述模型挂掉时不能静默返回空——用户会以为图片已被理解。"""
    path = write_png(tmp_path)
    main = _Model("blind-model", sees_images=False)
    broken = _Model("image-model", sees_images=False)

    messages = resolve(str(path), tmp_path, main, broken)

    assert messages[0]["role"] == "system"
    assert "Do not guess" in messages[0]["content"]
    assert "described" not in messages[0]["content"], "不能把失败当成转述成功"


def test_a_message_without_images_costs_nothing(tmp_path):
    llm = _Model()

    messages = resolve("just a question", tmp_path, llm)

    assert messages == []
    assert llm.calls == [], "没有图片就绝不该触发探测"


def test_the_probe_runs_once_across_two_requests(tmp_path):
    path = write_png(tmp_path)
    llm = _Model(sees_images=True)
    reset_vision_cache()

    asyncio.run(resolve_image_messages(str(path), tmp_path, llm))
    asyncio.run(resolve_image_messages(str(path), tmp_path, llm))

    assert len(llm.calls) == 1


# --- anthropic wire format ------------------------------------------------


def test_anthropic_rewrites_openai_image_parts():
    """Anthropic 不认 image_url。不翻译就是一个 400。"""
    _, translated = AnthropicAdapter._translate_messages([
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]},
    ])

    assert translated[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_anthropic_merges_the_image_turn_into_the_user_turn():
    """引擎把图片放在独立消息里，靠这次合并才能变成一个 image+text 轮次。"""
    _, translated = AnthropicAdapter._translate_messages([
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]},
        {"role": "user", "content": "what is this"},
    ])

    assert len(translated) == 1
    assert [block["type"] for block in translated[0]["content"]] == ["image", "text"]


def test_anthropic_rejects_a_remote_image_url():
    with pytest.raises(LLMResponseError, match="base64 data"):
        AnthropicAdapter._translate_messages([
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.test/x.png"}},
            ]},
        ])

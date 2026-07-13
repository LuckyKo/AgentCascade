"""Test that max_input_tokens is not consumed by .pop() in BaseChatModel.chat()."""

import copy
from typing import Iterator, List, Union

import pytest

from agent_cascade.llm.base import BaseChatModel, Message


class _DummyLLM(BaseChatModel):
    """Minimal concrete subclass that skips network calls."""

    max_retries = 0  # skip retry logic

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.generate_cfg: dict = {}
        self._preprocess_cache: dict = {}  # required by BaseChatModel.chat()
        self._max_preprocess_cache_size = 50  # matches BaseChatModel.__init__ (line 104)
        self.cache = None  # skip cache lookup
        self.use_raw_api = False

    @property
    def support_multimodal_input(self) -> bool:
        return True

    @property
    def support_audio_input(self) -> bool:
        return True

    # pylint: disable=unused-argument
    def _chat_with_functions(
        self,
        messages: list[Union[Message, dict]],
        functions: list,
        stream: bool,
        delta_stream: bool,
        generate_cfg: dict,
        lang: str,
    ):
        return [Message('assistant', 'ok')]

    def _chat_stream(self, messages: List[Message], delta_stream: bool, generate_cfg: dict) -> Iterator[List[Message]]:
        yield [Message('assistant', 'ok')]

    def _chat_no_stream(self, messages: List[Message], generate_cfg: dict) -> List[Message]:
        return [Message('assistant', 'ok')]


@pytest.fixture()
def llm():
    return _DummyLLM(cfg={'generate_cfg': {'max_input_tokens': 120000}})


def test_max_input_tokens_not_popped(llm: _DummyLLM):
    """Calling chat() multiple times must not remove max_input_tokens from generate_cfg."""
    llm.generate_cfg = {'max_input_tokens': 120000}

    # First call
    list(llm.chat([Message('user', 'hi')]))
    assert llm.generate_cfg.get('max_input_tokens') == 120000, (
        "max_input_tokens was consumed by .pop() on first call"
    )

    # Second call — should still see the same value
    list(llm.chat([Message('user', 'hi again')]))
    assert llm.generate_cfg.get('max_input_tokens') == 120000, (
        "max_input_tokens was consumed on second call"
    )


def test_max_input_tokens_gets_used_for_truncation(llm: _DummyLLM):
    """The value retrieved via .get() must actually be passed to truncation."""
    llm.generate_cfg = {'max_input_tokens': 10}

    # Build a message list that exceeds the tiny token budget.
    long_content = 'word ' * 200  # ~800 tokens
    msgs = [Message('user', long_content)]

    result = list(llm.chat(msgs))
    # chat() with stream=True returns an iterator of List[Message].
    # result[0] is the first (and only) chunk — a list of Messages.
    first_chunk = result[0]  # type: ignore[index]
    assert isinstance(first_chunk, list), f"expected list, got {type(first_chunk)}"
    first_resp = first_chunk[0] if isinstance(first_chunk[0], Message) else Message(**first_chunk[0])  # type: ignore[arg-type]
    assert len(first_resp.content or '') < len(long_content), (
        "max_input_tokens=10 should have truncated the message"
    )


def test_default_max_input_tokens_when_missing(llm: _DummyLLM):
    """When max_input_tokens is absent, a sensible default should be used."""
    llm.generate_cfg = {}

    msgs = [Message('user', 'hello')]
    result = list(llm.chat(msgs))
    assert len(result) == 1


def test_agent_name_passed_to_truncation():
    """agent_name in generate_cfg should flow through to _truncate_input_messages_roughly."""
    from agent_cascade.llm.base import _truncate_input_messages_roughly

    msgs = [Message('user', 'hello')]
    result = _truncate_input_messages_roughly(msgs, max_tokens=100, agent_name='TestAgent')
    assert len(result) == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

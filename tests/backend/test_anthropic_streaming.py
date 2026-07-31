"""Hermetic coverage for the Anthropic streaming event branches.

The streaming loop collects the chunks that `_on_content_block_start` and
`_on_content_block_delta` return and yields them after dispatching each event,
so a regression that dropped either handler's result, or the trailing yield
loop, would silently discard Anthropic text and tool-call output while still
compiling cleanly. Every scenario below drives the real loop over scripted
Anthropic SDK events through an injected fake client, so no request is ever built
and no socket is opened.

The backend reads its credential from the environment as it is constructed, and
the root fixtures stub only the Mistral variable, so an autouse fixture here
replaces the Anthropic one with a fake for the duration of every test: whatever
the host happens to provide is never read, and the fake client asserts that the
fake value is the only credential it was ever handed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
import os
from typing import Any

import anthropic
from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
import pytest

from vibe.core.config import Backend, ModelConfig, ProviderConfig
from vibe.core.llm.backend.anthropic_llm import AnthropicBackend
from vibe.core.types import LLMChunk, LLMMessage, LLMUsage, Role, ToolCall

type StreamEvent = (
    RawMessageStartEvent
    | RawMessageDeltaEvent
    | RawContentBlockStartEvent
    | RawContentBlockDeltaEvent
)

PROMPT = "Just say hi"
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
# Recognisable on sight and valid nowhere, so a credential leaking into an
# assertion failure or a captured request is provably not a real one.
FAKE_API_KEY = "sk-ant-fake-key-for-tests"
PROVIDER = ProviderConfig(
    name="anthropic",
    api_base="https://api.anthropic.com",
    api_key_env_var=ANTHROPIC_API_KEY_VARIABLE,
    backend=Backend.ANTHROPIC,
)
MODEL = ModelConfig(
    name="claude-streaming-test", provider="anthropic", alias="claude-alias"
)


@pytest.fixture(autouse=True)
def _fake_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the host's Anthropic credential with a fake one for every test.

    `AnthropicBackend` resolves the variable named by its provider as it is
    constructed, so this has to be in place before any backend exists.
    """
    monkeypatch.setenv(ANTHROPIC_API_KEY_VARIABLE, FAKE_API_KEY)


class FakeMessageStream:
    """Async context manager and async iterator over scripted stream events."""

    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self._events = events

    async def __aenter__(self) -> FakeMessageStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def __aiter__(self) -> AsyncGenerator[StreamEvent]:
        for event in self._events:
            yield event


class FakeMessages:
    """The `messages` namespace of the fake client, recording every stream call."""

    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self._events = events
        self.stream_calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeMessageStream:
        self.stream_calls.append(kwargs)
        return FakeMessageStream(self._events)


class FakeAnthropicClient:
    """Stands in for `anthropic.AsyncAnthropic`: scripted events, no transport."""

    def __init__(self, events: Sequence[StreamEvent]) -> None:
        self.messages = FakeMessages(events)
        self.closed = False
        # Every set of arguments the backend constructed a client with, so the
        # credential it passed can be asserted rather than assumed.
        self.construction_kwargs: list[dict[str, Any]] = []

    def record_construction(self, **kwargs: Any) -> FakeAnthropicClient:
        """Stand in for the `anthropic.AsyncAnthropic` constructor itself.

        Returns:
            This same client, so every construction the backend performs is both
            recorded and served by the one fake
        """
        self.construction_kwargs.append(kwargs)
        return self

    async def close(self) -> None:
        self.closed = True


def message_start(input_tokens: int) -> RawMessageStartEvent:
    """Build the opening event that carries the prompt token count."""
    return RawMessageStartEvent(
        type="message_start",
        message=Message(
            id="msg_streaming_test",
            content=[],
            model=MODEL.name,
            role="assistant",
            stop_reason=None,
            stop_sequence=None,
            type="message",
            usage=Usage(input_tokens=input_tokens, output_tokens=0),
        ),
    )


def message_delta(output_tokens: int) -> RawMessageDeltaEvent:
    """Build the closing event that carries the completion token count."""
    return RawMessageDeltaEvent(
        type="message_delta",
        delta=Delta(stop_reason="end_turn", stop_sequence=None),
        usage=MessageDeltaUsage(output_tokens=output_tokens),
    )


def tool_use_start(index: int, tool_id: str, name: str) -> RawContentBlockStartEvent:
    """Build a content_block_start event opening a tool_use block."""
    return RawContentBlockStartEvent(
        type="content_block_start",
        index=index,
        content_block=ToolUseBlock(type="tool_use", id=tool_id, name=name, input={}),
    )


def text_block_start(index: int) -> RawContentBlockStartEvent:
    """Build a content_block_start event opening a plain text block."""
    return RawContentBlockStartEvent(
        type="content_block_start",
        index=index,
        content_block=TextBlock(type="text", text="", citations=None),
    )


def text_delta(index: int, text: str) -> RawContentBlockDeltaEvent:
    """Build a content_block_delta event carrying assistant text."""
    return RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=TextDelta(type="text_delta", text=text),
    )


def thinking_delta(index: int, thinking: str) -> RawContentBlockDeltaEvent:
    """Build a content_block_delta event carrying reasoning text."""
    return RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=ThinkingDelta(type="thinking_delta", thinking=thinking),
    )


def input_json_delta(index: int, partial_json: str) -> RawContentBlockDeltaEvent:
    """Build a content_block_delta event carrying partial tool arguments."""
    return RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=index,
        delta=InputJSONDelta(type="input_json_delta", partial_json=partial_json),
    )


async def stream_chunks(
    monkeypatch: pytest.MonkeyPatch, events: Sequence[StreamEvent]
) -> tuple[list[LLMChunk], FakeAnthropicClient]:
    """Run a streaming completion over scripted events against a fake client.

    The fake replaces `anthropic.AsyncAnthropic` itself, so the backend installs
    it through its own context manager exactly as it would a real client, and no
    real client is ever constructed.

    Returns:
        The chunks the backend emitted, and the fake client it used
    """
    client = FakeAnthropicClient(events)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", client.record_construction)

    backend = AnthropicBackend(provider=PROVIDER)
    async with backend:
        chunks = [
            chunk
            async for chunk in backend.complete_streaming(
                model=MODEL,
                messages=[LLMMessage(role=Role.user, content=PROMPT)],
                temperature=0.0,
                tools=None,
                max_tokens=None,
                tool_choice=None,
                extra_headers=None,
            )
        ]
    return chunks, client


def single_tool_call(chunk: LLMChunk) -> ToolCall:
    """Return the one tool call the chunk carries.

    Returns:
        The chunk's only tool call
    """
    tool_calls = chunk.message.tool_calls
    assert tool_calls is not None
    assert len(tool_calls) == 1
    return tool_calls[0]


class TestContentBlockStartEmission:
    @pytest.mark.asyncio
    async def test_a_tool_use_block_emits_its_registered_tool_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(
            monkeypatch, [tool_use_start(0, "toolu_start", "todo")]
        )

        assert len(chunks) == 1
        assert chunks[0].message.role == Role.assistant
        assert chunks[0].message.content == ""
        assert chunks[0].usage is None

        tool_call = single_tool_call(chunks[0])
        assert tool_call.id == "toolu_start"
        assert tool_call.index == 0
        assert tool_call.function.name == "todo"
        # The name arrives with the block; the arguments stream in as deltas.
        assert tool_call.function.arguments == ""

    @pytest.mark.asyncio
    async def test_a_text_block_start_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(monkeypatch, [text_block_start(0)])

        assert chunks == []


class TestContentBlockDeltaEmission:
    @pytest.mark.asyncio
    async def test_a_text_delta_emits_its_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(monkeypatch, [text_delta(0, "Hello, world.")])

        assert len(chunks) == 1
        assert chunks[0].message.role == Role.assistant
        assert chunks[0].message.content == "Hello, world."
        assert chunks[0].message.reasoning_content is None
        assert chunks[0].message.tool_calls is None
        assert chunks[0].usage is None

    @pytest.mark.asyncio
    async def test_a_thinking_delta_emits_reasoning_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(
            monkeypatch, [thinking_delta(0, "weighing the options")]
        )

        assert len(chunks) == 1
        assert chunks[0].message.content == ""
        assert chunks[0].message.reasoning_content == "weighing the options"
        assert chunks[0].message.tool_calls is None

    @pytest.mark.asyncio
    async def test_an_input_json_delta_emits_the_tool_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(
            monkeypatch,
            [
                tool_use_start(1, "toolu_args", "todo"),
                input_json_delta(1, '{"action":'),
                input_json_delta(1, ' "read"}'),
            ],
        )

        assert len(chunks) == 3
        # Each delta is emitted as its own fragment against the registered call,
        # which is what lets the caller reassemble the arguments in order.
        assert [single_tool_call(chunk).function.arguments for chunk in chunks] == [
            "",
            '{"action":',
            ' "read"}',
        ]
        assert all(single_tool_call(chunk).id == "toolu_args" for chunk in chunks)
        assert all(single_tool_call(chunk).index == 0 for chunk in chunks)
        # Only the opening block names the tool; the argument fragments do not.
        assert [single_tool_call(chunk).function.name for chunk in chunks] == [
            "todo",
            None,
            None,
        ]

    @pytest.mark.asyncio
    async def test_an_input_json_delta_for_an_unregistered_block_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(
            monkeypatch, [input_json_delta(7, '{"action": "read"}')]
        )

        assert chunks == []


class TestFullStreamEmission:
    @pytest.mark.asyncio
    async def test_every_branch_is_emitted_once_and_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, _ = await stream_chunks(
            monkeypatch,
            [
                message_start(11),
                text_delta(0, "Hello, "),
                thinking_delta(0, "weighing the options"),
                tool_use_start(1, "toolu_full", "todo"),
                input_json_delta(1, '{"action": "read"}'),
                message_delta(7),
            ],
        )

        # Exactly one chunk per emitting event, in event order: a duplicated or
        # dropped emission changes this list.
        assert len(chunks) == 5
        assert chunks[0].message.content == "Hello, "
        assert chunks[1].message.reasoning_content == "weighing the options"
        assert single_tool_call(chunks[2]).function.name == "todo"
        assert single_tool_call(chunks[3]).function.arguments == '{"action": "read"}'

        # The usage chunk pairs the prompt tokens from message_start with the
        # completion tokens from message_delta.
        assert chunks[4].message.content == ""
        assert chunks[4].message.tool_calls is None
        assert chunks[4].usage == LLMUsage(prompt_tokens=11, completion_tokens=7)
        assert [chunk.usage for chunk in chunks[:4]] == [None, None, None, None]


class TestStreamingIsHermetic:
    @pytest.mark.asyncio
    async def test_the_backend_streams_through_the_injected_client_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks, client = await stream_chunks(
            monkeypatch, [text_delta(0, "Hello, world.")]
        )

        assert len(chunks) == 1
        # One stream call carrying the mapped request, and the fake is the client
        # the backend opened and closed: no real client, request or socket exists.
        assert len(client.messages.stream_calls) == 1
        assert client.messages.stream_calls[0]["model"] == MODEL.name
        assert client.messages.stream_calls[0]["messages"] == [
            {"role": "user", "content": PROMPT}
        ]
        assert "tools" not in client.messages.stream_calls[0]
        assert client.closed is True

    @pytest.mark.asyncio
    async def test_the_backend_is_only_ever_handed_the_fake_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whatever the host exports, the autouse fixture is what the backend sees.
        assert os.environ[ANTHROPIC_API_KEY_VARIABLE] == FAKE_API_KEY

        _, client = await stream_chunks(monkeypatch, [text_delta(0, "Hello, world.")])

        # Every client the backend built was handed the fake key and nothing else,
        # so no host credential can reach a request, a log or a failure message.
        assert client.construction_kwargs != []
        assert {kwargs["api_key"] for kwargs in client.construction_kwargs} == {
            FAKE_API_KEY
        }

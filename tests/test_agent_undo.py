"""Tests for AgentLoop conversation rewinds and the /undo terminal command."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
import json
from pathlib import Path
from typing import NamedTuple
import unicodedata

import pytest
from textual.widget import Widget

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.chat_input import ChatInputContainer
from vibe.cli.textual_ui.widgets.messages import (
    AssistantMessage,
    ErrorMessage,
    UserCommandMessage,
    UserMessage,
)
from vibe.core.agent_loop import AgentLoop, AgentLoopStateError
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.config import (
    Backend,
    ModelConfig,
    ProviderConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.tools.base import BaseToolConfig, ToolPermission
from vibe.core.types import (
    AssistantEvent,
    FunctionCall,
    LLMMessage,
    Role,
    ToolCall,
    UserMessageEvent,
)

SENTINEL_RESPONSE = "sentinel response: a rewind must never consume this stream"


def make_config(
    *,
    system_prompt_id: str = "tests",
    active_model: str = "devstral-latest",
    enabled_tools: list[str] | None = None,
    tools: dict[str, BaseToolConfig] | None = None,
    session_logging: SessionLoggingConfig | None = None,
) -> VibeConfig:
    """Build a hermetic configuration for the stubbed undo scenarios.

    Auto-compaction is off so nothing rewrites the transcript mid-test, and no
    tool is enabled unless a scenario asks for one, so every other turn appends
    exactly two messages. Session logging is off unless a scenario supplies its
    own configuration pointing at a temporary directory.
    """
    models = [
        ModelConfig(
            name="mistral-vibe-cli-latest",
            provider="mistral",
            alias="devstral-latest",
            input_price=0.4,
            output_price=2.0,
        )
    ]
    providers = [
        ProviderConfig(
            name="mistral",
            api_base="https://api.mistral.ai/v1",
            api_key_env_var="MISTRAL_API_KEY",
            backend=Backend.MISTRAL,
        )
    ]
    return VibeConfig(
        session_logging=session_logging or SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id=system_prompt_id,
        include_project_context=False,
        include_prompt_detail=False,
        active_model=active_model,
        models=models,
        providers=providers,
        enabled_tools=enabled_tools or [],
        tools=tools or {},
    )


def backend_with_sentinel(*responses: str) -> FakeBackend:
    """Stub one stream per response, followed by a sentinel stream.

    Each scripted completion consumes one stream, so an unscripted one consumes
    the sentinel instead of the stub quietly serving an empty assistant message.
    """
    streams = [
        [mock_llm_chunk(content=response)]
        for response in (*responses, SENTINEL_RESPONSE)
    ]
    return FakeBackend(streams)


class BackendActivity(NamedTuple):
    """Tracked backend activity: logged calls and unconsumed stubbed streams."""

    completions: int
    token_counts: int
    unconsumed_streams: int


def backend_activity(backend: FakeBackend) -> BackendActivity:
    """Snapshot the tracked activity, for comparison against a later snapshot."""
    return BackendActivity(
        completions=len(backend.requests_messages),
        token_counts=len(backend._count_tokens_calls),
        unconsumed_streams=len(backend._streams),
    )


@contextmanager
def no_backend_contact(backend: FakeBackend) -> Iterator[None]:
    """Fail unless the wrapped block leaves tracked backend activity unchanged."""
    served = backend_activity(backend)
    yield
    assert backend_activity(backend) == served


@contextmanager
def unchanged_transcript(agent: AgentLoop) -> Iterator[None]:
    """Fail unless the wrapped block leaves the same message objects and values.

    Deep copies catch an in-place edit; identity catches an equal replacement.
    """
    values = [message.model_copy(deep=True) for message in agent.messages]
    objects = list(agent.messages)
    yield
    assert agent.messages == values
    assert all(
        surviving is original
        for surviving, original in zip(agent.messages, objects, strict=True)
    )


def capture_system_message(agent: AgentLoop) -> tuple[LLMMessage, LLMMessage]:
    """Return the live message object at index 0 and a deep copy of its values."""
    system_message = agent.messages[0]
    return system_message, system_message.model_copy(deep=True)


def assert_system_message_intact(
    agent: AgentLoop, original: LLMMessage, values: LLMMessage
) -> None:
    """Assert index 0 is still the captured object, carrying its captured values."""
    assert agent.messages[0] is original
    assert agent.messages[0] == values
    assert agent.messages[0].role == Role.system


def logging_config(save_dir: Path) -> VibeConfig:
    """Build the stubbed configuration with session logging on, under save_dir.

    Returns:
        A configuration whose session logger writes into the given directory
    """
    return make_config(
        session_logging=SessionLoggingConfig(enabled=True, save_dir=str(save_dir))
    )


class SessionLogState(NamedTuple):
    """What a session directory holds: its message records and its cursor."""

    logged: list[tuple[str, str]]
    cursor: int


def read_session_log(save_dir: Path) -> SessionLogState:
    """Read the single session directory the stubbed turns wrote.

    Returns:
        The role and content of every persisted message, in order, and the
        message count the logger resumes appending from
    """
    session_dirs = sorted(save_dir.glob("session_*"))
    assert len(session_dirs) == 1
    session_dir = session_dirs[0]

    lines = (session_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    metadata = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in lines]
    return SessionLogState(
        logged=[(record["role"], record["content"]) for record in records],
        cursor=metadata["total_messages"],
    )


def persisted_first_two_turns() -> list[tuple[str, str]]:
    """Return the records the two stubbed opening turns leave in a session log.

    Returns:
        The role and content of every message those two turns persist, in order
    """
    return [
        ("user", "First"),
        ("assistant", "R1"),
        ("user", "Second"),
        ("assistant", "R2"),
    ]


class TestUndoRewindsOneTurn:
    @pytest.mark.asyncio
    async def test_undo_restores_end_of_first_turn(self) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)
        assert len(agent.messages) == 1

        async for _ in agent.act("First"):
            pass

        assert len(agent.messages) == 3

        async for _ in agent.act("Second"):
            pass

        assert len(agent.messages) == 5
        # Each boundary is the message count captured before its user message was
        # appended, so index 0 always stays out of reach of the truncation.
        assert agent._turn_boundaries == [1, 3]

        turn_one_user = agent.messages[1]
        turn_one_assistant = agent.messages[2]
        turn_one_values = [
            message.model_copy(deep=True) for message in agent.messages[1:3]
        ]

        with no_backend_contact(backend):
            removed = agent.undo_last_turn()

        assert removed == "Second"
        assert len(agent.messages) == 3
        assert_system_message_intact(agent, system_message, system_values)
        assert agent.messages[1] is turn_one_user
        assert agent.messages[2] is turn_one_assistant
        assert agent.messages[1:] == turn_one_values
        assert agent.messages[1].role == Role.user
        assert agent.messages[1].content == "First"
        assert agent.messages[2].role == Role.assistant
        assert agent.messages[2].content == "R1"

    @pytest.mark.asyncio
    async def test_second_undo_leaves_only_the_system_message(self) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        with no_backend_contact(backend):
            assert agent.undo_last_turn() == "Second"
        with no_backend_contact(backend):
            assert agent.undo_last_turn() == "First"

        assert len(agent.messages) == 1
        assert_system_message_intact(agent, system_message, system_values)

    @pytest.mark.asyncio
    async def test_third_undo_is_a_safe_no_op(self) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        with no_backend_contact(backend):
            assert agent.undo_last_turn() == "Second"
        with no_backend_contact(backend):
            assert agent.undo_last_turn() == "First"

        with no_backend_contact(backend), unchanged_transcript(agent):
            assert agent.undo_last_turn() is None

        assert len(agent.messages) == 1
        assert_system_message_intact(agent, system_message, system_values)


class TestUndoRemovesTheWholeTurnTail:
    """A rewind must discard everything the turn appended, not a fixed tail.

    This single-tool turn appends four messages rather than two: the user message,
    the assistant message carrying the tool call, the tool response, and the
    assistant reply that follows it. Truncating back to the recorded boundary
    removes all four; dropping a fixed two-message tail would strand the
    assistant tool call and its tool response in the transcript.
    """

    @pytest.mark.asyncio
    async def test_undo_removes_the_tool_call_and_tool_response_tail(self) -> None:
        tool_call = ToolCall(
            id="call_undo",
            index=0,
            function=FunctionCall(name="todo", arguments='{"action": "read"}'),
        )
        # The fourth stream is the sentinel: an unscripted completion consumes it
        # instead of the stub quietly serving an empty assistant message.
        backend = FakeBackend([
            [mock_llm_chunk(content="R1")],
            [mock_llm_chunk(content="Checking your todos.", tool_calls=[tool_call])],
            [mock_llm_chunk(content="You have no todos.")],
            [mock_llm_chunk(content=SENTINEL_RESPONSE)],
        ])
        agent = AgentLoop(
            make_config(
                enabled_tools=["todo"],
                tools={"todo": BaseToolConfig(permission=ToolPermission.ALWAYS)},
            ),
            agent_name=BuiltinAgentName.AUTO_APPROVE,
            backend=backend,
        )
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("First"):
            pass

        assert len(agent.messages) == 3
        turn_one_objects = list(agent.messages[1:])
        turn_one_values = [
            message.model_copy(deep=True) for message in agent.messages[1:]
        ]

        async for _ in agent.act("Read my todos"):
            pass

        # The tool-bearing turn really did append a four-message tail, so the
        # assertions below are not vacuous.
        assert [message.role for message in agent.messages] == [
            Role.system,
            Role.user,
            Role.assistant,
            Role.user,
            Role.assistant,
            Role.tool,
            Role.assistant,
        ]
        assert agent.messages[4].tool_calls is not None
        assert agent.messages[5].tool_call_id == "call_undo"
        assert agent.messages[5].name == "todo"
        assert agent._turn_boundaries == [1, 3]

        with no_backend_contact(backend):
            removed = agent.undo_last_turn()

        assert removed == "Read my todos"
        assert len(agent.messages) == 3
        assert all(message.role != Role.tool for message in agent.messages)
        assert all(message.tool_calls is None for message in agent.messages)
        assert agent._turn_boundaries == [1]

        # The preserved turn is the very same message objects, unmodified.
        assert_system_message_intact(agent, system_message, system_values)
        assert all(
            surviving is original
            for surviving, original in zip(
                agent.messages[1:], turn_one_objects, strict=True
            )
        )
        assert agent.messages[1:] == turn_one_values
        assert agent.messages[1].content == "First"
        assert agent.messages[2].content == "R1"


class TestUndoPreservesCumulativeStats:
    @pytest.mark.asyncio
    async def test_cumulative_session_stats_survive_every_undo(self) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        prompt_tokens = agent.stats.session_prompt_tokens
        completion_tokens = agent.stats.session_completion_tokens
        total_llm_tokens = agent.stats.session_total_llm_tokens
        session_cost = agent.stats.session_cost
        steps = agent.stats.steps

        # Guard against a broken stub making the comparisons below vacuous.
        assert prompt_tokens > 0
        assert completion_tokens > 0
        assert steps >= 2

        # Two rewinds plus the exhausted-stack no-op: the session totals and the
        # step count must be unchanged throughout, as they are across a reload.
        for _ in range(3):
            with no_backend_contact(backend):
                agent.undo_last_turn()

            assert agent.stats.session_prompt_tokens == prompt_tokens
            assert agent.stats.session_completion_tokens == completion_tokens
            assert agent.stats.session_total_llm_tokens == total_llm_tokens
            assert agent.stats.session_cost == session_cost
            assert agent.stats.steps == steps


class TestUndoKeepsTheMessageObserverInStep:
    """Truncation must pull the observed-message index back with it.

    The flush routine returns early once that index reaches the length of the
    message list, so a rewind that left it pointing past the new end would make
    every later message invisible to programmatic and Agent Client Protocol
    embedders, which are the consumers that supply an observer.
    """

    @pytest.mark.asyncio
    async def test_messages_appended_after_a_rewind_are_still_observed(self) -> None:
        observed: list[tuple[Role, str | None]] = []

        def observer(message: LLMMessage) -> None:
            observed.append((message.role, message.content))

        backend = backend_with_sentinel("R1", "R2", "R3")
        agent = AgentLoop(make_config(), message_observer=observer, backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        assert observed == [
            (Role.system, agent.messages[0].content),
            (Role.user, "First"),
            (Role.assistant, "R1"),
            (Role.user, "Second"),
            (Role.assistant, "R2"),
        ]
        assert agent._last_observed_message_index == len(agent.messages) == 5

        with no_backend_contact(backend):
            assert agent.undo_last_turn() == "Second"

        assert agent._last_observed_message_index == len(agent.messages) == 3

        observed.clear()

        async for _ in agent.act("Third"):
            pass

        # Each replacement message is observed exactly once, and the index has
        # caught up with the transcript again.
        assert observed == [(Role.user, "Third"), (Role.assistant, "R3")]
        assert agent._last_observed_message_index == len(agent.messages) == 5


class TestUndoAfterHistoryReset:
    @pytest.mark.asyncio
    async def test_clear_history_invalidates_every_boundary(self) -> None:
        backend = backend_with_sentinel("Response")
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("Hello"):
            pass

        assert agent._turn_boundaries == [1]

        await agent.clear_history()

        # Asserted on the stack directly because undo_last_turn's out-of-range
        # guard would discard this boundary too, masking a missing invalidation.
        assert agent._turn_boundaries == []
        assert len(agent.messages) == 1

    @pytest.mark.asyncio
    async def test_undo_after_clear_history_is_a_no_op(self) -> None:
        backend = backend_with_sentinel("Response")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("Hello"):
            pass

        assert len(agent.messages) == 3

        await agent.clear_history()

        assert len(agent.messages) == 1

        with no_backend_contact(backend), unchanged_transcript(agent):
            assert agent.undo_last_turn() is None

        assert len(agent.messages) == 1
        assert_system_message_intact(agent, system_message, system_values)

    @pytest.mark.asyncio
    async def test_compact_invalidates_every_boundary(self) -> None:
        backend = backend_with_sentinel("First response", "<summary>")
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("Build something"):
            pass

        assert agent._turn_boundaries == [1]

        await agent.compact()

        assert agent._turn_boundaries == []
        assert len(agent.messages) == 2

    @pytest.mark.asyncio
    async def test_undo_after_compact_is_a_no_op(self) -> None:
        # The second stubbed stream is consumed by the summarization turn that
        # compact() drives; count_tokens is served locally by the fake backend,
        # which is why the token-count counter is non-zero before the rewind and
        # must still be non-zero and unchanged afterwards.
        backend = backend_with_sentinel("First response", "<summary>")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("Build something"):
            pass

        await agent.compact()

        assert len(agent.messages) == 2
        assert backend_activity(backend).token_counts == 1

        # A boundary of 1 is still in range for this two-message list, so only
        # compact dropping its boundaries can make the rewind return None.
        with no_backend_contact(backend), unchanged_transcript(agent):
            assert agent.undo_last_turn() is None

        assert len(agent.messages) == 2
        assert agent.messages[1].content == "<summary>"
        assert_system_message_intact(agent, system_message, system_values)


class TestUndoDiscardsStaleBoundaries:
    @pytest.mark.asyncio
    async def test_stale_boundary_is_discarded_before_an_older_one_is_used(
        self,
    ) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        # Shorten the transcript behind the boundary stack's back, leaving the
        # boundary 3 out of range and the boundary 1 still in range.
        agent.messages = agent.messages[:3]
        assert agent._turn_boundaries == [1, 3]

        with no_backend_contact(backend):
            removed = agent.undo_last_turn()

        assert removed == "First"
        assert len(agent.messages) == 1
        assert agent._turn_boundaries == []
        assert_system_message_intact(agent, system_message, system_values)

    @pytest.mark.asyncio
    async def test_all_stale_boundaries_leave_the_transcript_untouched(self) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)
        system_message, system_values = capture_system_message(agent)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        agent.messages = agent.messages[:1]

        with no_backend_contact(backend), unchanged_transcript(agent):
            assert agent.undo_last_turn() is None

        assert agent._turn_boundaries == []
        assert len(agent.messages) == 1
        assert_system_message_intact(agent, system_message, system_values)


class TestUndoNeverContactsTheBackend:
    @pytest.mark.asyncio
    async def test_no_rewind_reaches_the_backend(self) -> None:
        backend = backend_with_sentinel("R1", "R2")
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        # Guards against a vacuous comparison: the two turns really did reach the
        # backend, no turn needed a token count, and the sentinel stream is still
        # unserved, so a hidden completion would consume it and be counted here.
        served = backend_activity(backend)
        assert served.completions == 2
        assert served.token_counts == 0
        assert served.unconsumed_streams == 1

        assert agent.undo_last_turn() == "Second"
        assert backend_activity(backend) == served

        assert agent.undo_last_turn() == "First"
        assert backend_activity(backend) == served

        assert agent.undo_last_turn() is None
        assert backend_activity(backend) == served


class TestUndoIsRefusedWhileTheHistoryIsClaimed:
    """An operation that still owns the transcript must block a rewind.

    A turn suspended between two of its own yields is still resumable, so
    truncating the message list underneath it would let it reach the backend and
    append its reply to a history the caller was already told had been rewound.
    """

    @pytest.mark.asyncio
    async def test_undo_is_refused_while_a_turn_is_suspended(self) -> None:
        backend = FakeBackend([[mock_llm_chunk(content="R1")]])
        agent = AgentLoop(make_config(), backend=backend)

        events = agent.act("Sensitive request")
        first_event = await anext(events)

        # The turn is parked on its very first yield: the user message is
        # already recorded, but the backend has not been reached yet.
        assert isinstance(first_event, UserMessageEvent)
        assert backend.requests_messages == []

        with pytest.raises(AgentLoopStateError):
            agent.undo_last_turn()

        assert len(agent.messages) == 2
        assert agent._turn_boundaries == [1]

        async for _ in events:
            pass

        # The refused rewind left the turn free to finish exactly as it would
        # have: one provider call and a coherent three-message transcript, with
        # no assistant reply orphaned onto a truncated history.
        assert len(backend.requests_messages) == 1
        assert [msg.role for msg in agent.messages] == [
            Role.system,
            Role.user,
            Role.assistant,
        ]
        assert agent.messages[2].content == "R1"

        assert agent.undo_last_turn() == "Sensitive request"
        assert len(agent.messages) == 1

    @pytest.mark.asyncio
    async def test_undo_is_refused_while_a_stream_is_open(self) -> None:
        # Six chunks: the assistant-event batch flushes at five, so the turn
        # parks inside the still-open stream, before the aggregated assistant
        # message has been appended.
        backend = FakeBackend([
            [mock_llm_chunk(content=f"C{index}") for index in range(6)]
        ])
        agent = AgentLoop(make_config(), backend=backend, enable_streaming=True)

        events = agent.act("Stream then rewind")

        assert isinstance(await anext(events), UserMessageEvent)
        assert isinstance(await anext(events), AssistantEvent)

        with pytest.raises(AgentLoopStateError):
            agent.undo_last_turn()

        assert [msg.role for msg in agent.messages] == [Role.system, Role.user]

        async for _ in events:
            pass

        assert [msg.role for msg in agent.messages] == [
            Role.system,
            Role.user,
            Role.assistant,
        ]
        assert agent.messages[2].content == "C0C1C2C3C4C5"

        assert agent.undo_last_turn() == "Stream then rewind"
        assert len(agent.messages) == 1

    @pytest.mark.asyncio
    async def test_closing_an_abandoned_turn_releases_the_claim(self) -> None:
        backend = FakeBackend([[mock_llm_chunk(content="R1")]])
        agent = AgentLoop(make_config(), backend=backend)

        events = agent.act("Abandoned request")
        await anext(events)
        await events.aclose()

        # Closing the generator hands the transcript back, so the abandoned turn
        # becomes rewindable - and the backend was never reached at all.
        assert agent.undo_last_turn() == "Abandoned request"
        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.system
        assert backend.requests_messages == []

    @pytest.mark.asyncio
    async def test_undo_is_refused_while_a_compaction_is_in_flight(self) -> None:
        counted: list[int] = []

        def count_tokens_and_attempt_undo(messages: list[LLMMessage]) -> int:
            # count_tokens is served locally by the fake backend and runs while
            # compact() still owns the transcript it has just replaced: a rewind
            # landing here would be silently overwritten by the summary.
            with pytest.raises(AgentLoopStateError):
                agent.undo_last_turn()
            counted.append(len(messages))
            return len(messages)

        backend = FakeBackend(
            [[mock_llm_chunk(content="R1")], [mock_llm_chunk(content="<summary>")]],
            token_counter=count_tokens_and_attempt_undo,
        )
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("Build something"):
            pass

        await agent.compact()

        assert counted == [2]
        assert [msg.role for msg in agent.messages] == [Role.system, Role.user]
        assert agent.messages[1].content == "<summary>"

    @pytest.mark.asyncio
    async def test_undo_is_refused_while_a_clear_is_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saving = asyncio.Event()
        release = asyncio.Event()

        async def suspended_save(*_: object) -> None:
            saving.set()
            await release.wait()

        backend = backend_with_sentinel("R1")
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("First"):
            pass

        # clear_history persists the transcript before truncating it, so
        # suspending that save parks the clear while the history it is about to
        # discard is still intact - the window in which a rewind would race it.
        monkeypatch.setattr(agent.session_logger, "save_interaction", suspended_save)
        clearing = asyncio.create_task(agent.clear_history())
        await saving.wait()

        assert len(agent.messages) == 3
        assert agent._turn_boundaries == [1]

        with pytest.raises(AgentLoopStateError):
            agent.undo_last_turn()

        assert len(agent.messages) == 3
        assert agent._turn_boundaries == [1]

        release.set()
        await clearing

        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.system
        assert agent._turn_boundaries == []
        assert agent.undo_last_turn() is None

    @pytest.mark.asyncio
    async def test_a_nested_claim_does_not_release_the_turn(self) -> None:
        backend = FakeBackend([[mock_llm_chunk(content="<summary>")]])
        agent = AgentLoop(make_config(), backend=backend)

        events = agent.act("First")
        await anext(events)

        # Claims nest, exactly as they do when auto-compaction runs from inside
        # a turn: releasing the compaction's claim must not release the turn's.
        await agent.compact()

        with pytest.raises(AgentLoopStateError):
            agent.undo_last_turn()

        await events.aclose()

        # The turn's claim is released only now, and compact() already dropped
        # the boundaries it invalidated, so there is nothing left to undo.
        assert agent.undo_last_turn() is None


class TestUndoLeavesTheWrittenSessionLogAlone:
    @pytest.mark.asyncio
    async def test_undo_itself_writes_nothing_to_the_session_log(
        self, tmp_path: Path
    ) -> None:
        backend = backend_with_sentinel("R1", "R2")
        save_dir = tmp_path / "sessions"
        agent = AgentLoop(logging_config(save_dir), backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        persisted = read_session_log(save_dir)
        assert persisted.logged == persisted_first_two_turns()
        assert persisted.cursor == 4

        assert agent.undo_last_turn() == "Second"

        # The rewind is purely in memory and performs no I/O whatsoever, so the
        # session keeps every record it had written, cursor included.
        assert read_session_log(save_dir) == persisted

    @pytest.mark.asyncio
    async def test_a_turn_refilling_the_rewound_span_is_not_appended(
        self, tmp_path: Path
    ) -> None:
        backend = backend_with_sentinel("R1", "R2", "R2 again", "R3")
        save_dir = tmp_path / "sessions"
        agent = AgentLoop(logging_config(save_dir), backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass
        assert agent.undo_last_turn() == "Second"

        async for _ in agent.act("Second again"):
            pass

        # The logger appends from the count it last persisted, and a rewind moves
        # neither that cursor nor the records already written, so a turn refilling
        # the rewound span sits inside the persisted range and is omitted.
        refilled = read_session_log(save_dir)
        assert refilled.logged == persisted_first_two_turns()
        assert refilled.cursor == 4
        assert agent.messages[3].content == "Second again"

        async for _ in agent.act("Third"):
            pass

        # A turn past the persisted range is appended as usual, so logging
        # resumes rather than stopping: only the refilling turn stays missing.
        resumed = read_session_log(save_dir)
        assert resumed.logged[:4] == persisted_first_two_turns()
        assert resumed.logged[4:] == [("user", "Third"), ("assistant", "R3")]
        assert resumed.cursor == 6
        assert all("again" not in content for _, content in resumed.logged)


# Terminal interface scenarios

BUSY_REFUSAL = "Cannot undo while agent loop is processing. Please wait."
# Long enough that the stand-in turn is still pending when the assertions run,
# and always cancelled by the scenario rather than waited on.
STALLED_TURN_TIMEOUT = 30.0
# The request below is 89 characters, so it overruns the eighty-character
# confirmation budget: the handler keeps the first 79 characters and spends the
# eightieth on a single ellipsis. The expected summary is spelled out rather than
# recomputed so the assertion cannot drift with the production formatting, and it
# shows the comma backslash-escaped, because the confirmation widget renders its
# content as Markdown and the recovered prompt has to stay literal.
LONG_REQUEST_HEAD = (
    "Refactor the authentication middleware, then extend the retry policy and log ev"
)
LONG_REQUEST = f"{LONG_REQUEST_HEAD}ery retry."
LONG_REQUEST_SUMMARY = (
    "Refactor the authentication middleware\\, then extend the retry policy and log ev…"
)
MARKDOWN_REQUEST = "Explain **bold** and `code` in [docs](https://x.test)"
MARKDOWN_REQUEST_SUMMARY = (
    "Explain \\*\\*bold\\*\\* and \\`code\\` in \\[docs\\]\\(https\\:\\/\\/x\\.test\\)"
)
# A prompt carrying an ESC-introduced screen erase, a BEL, a raw C1 control
# sequence introducer, a right-to-left override with its terminating pop, and a
# DEL. Markdown escaping neuters none of them, and the renderer beneath the
# widgets filters only a handful of C0 codes, so the handler has to remove them
# before the confirmation can be written to a terminal. Every visible character
# survives, including the erase sequence's now-inert "[2J" payload.
CONTROL_REQUEST = "Delete \x1b[2Jthe repo\x07 \x9b31m \u202ered \u202cnow\x7f"
CONTROL_REQUEST_SUMMARY = "Delete \\[2Jthe repo 31m red now"
# Window-title and hyperlink operating-system-command sequences, terminated by
# BEL and by ESC-backslash respectively, alongside a bidirectional isolate pair
# and a zero-width space.
OSC_REQUEST = (
    "Set \x1b]0;pwned\x07 title \u2066and\u2069 \u200blink"
    " \x1b]8;;https://x.test\x1b\\here\x1b]8;;\x1b\\"
)
# Enough lines that copying the prompt or listing its lines to find the first one
# would be plainly wasteful.
HOSTILE_LINE_COUNT = 20_000


def make_ui_config() -> VibeConfig:
    """Build the configuration the interface scenarios mount an application on.

    Returns:
        The stubbed configuration with update checks off as well, so mounting the
        application neither schedules a notification nor reaches the network
    """
    return make_config().model_copy(update={"enable_update_checks": False})


async def agent_with_turns(*prompts: str) -> AgentLoop:
    """Drive one stubbed turn per prompt against a fake backend.

    Turns run through the real conversation loop, so the rewind points the handler
    relies on are recorded by production code rather than by the scenario.

    Returns:
        An agent loop whose transcript and rewind stack were built by real turns
    """
    backend = backend_with_sentinel(
        *(f"R{number}" for number, _ in enumerate(prompts, start=1))
    )
    agent = AgentLoop(make_ui_config(), backend=backend)
    for prompt in prompts:
        async for _ in agent.act(prompt):
            pass
    return agent


class GuardedPrompt(str):
    """A prompt that refuses the whole-prompt operations a preview must avoid.

    Recovering one short line must not copy the entire prompt, nor build a list
    holding every line of it, so a prompt pasted at any size costs the same as a
    short one. Both operations are trapped here rather than timed, which keeps the
    guarantee deterministic instead of dependent on how fast the host is.
    """

    __slots__ = ()

    def strip(self, chars: str | None = None, /) -> str:
        raise AssertionError("the preview must not copy the whole prompt")

    def splitlines(self, keepends: bool = False) -> list[str]:
        raise AssertionError("the preview must not materialise every line")


def start_stalled_turn(app: VibeApp) -> asyncio.Task[None]:
    """Put the application into the state it holds while a turn is streaming.

    The task stands in for the conversation worker: it never finishes on its own,
    so the submit path has a live turn it could cancel.

    Returns:
        The task the application will treat as the running turn
    """
    turn = asyncio.create_task(asyncio.sleep(STALLED_TURN_TIMEOUT))
    app._agent_running = True
    app._agent_task = turn
    return turn


async def finish_stalled_turn(app: VibeApp, turn: asyncio.Task[None]) -> None:
    """Cancel the stand-in turn and clear the busy state it established."""
    turn.cancel()
    with suppress(asyncio.CancelledError):
        await turn
    app._agent_running = False
    app._agent_task = None


def widget_content(widget: Widget) -> str | None:
    """Return the text a mounted message widget was constructed with.

    Returns:
        The widget's content, or None for a widget that carries none
    """
    match widget:
        case UserMessage() | AssistantMessage() | UserCommandMessage():
            return widget._content
        case ErrorMessage():
            return widget._error
        case _:
            return None


def rendered(app: VibeApp) -> list[tuple[str, str | None]]:
    """Project the message area onto (widget class name, content) pairs.

    Returns:
        One pair per mounted widget, in mount order
    """
    return [
        (type(widget).__name__, widget_content(widget))
        for widget in app.query_one("#messages").children
    ]


def test_the_registered_handler_name_resolves_to_a_coroutine_on_the_app() -> None:
    # Registry dispatch is reflective, so a renamed or missing handler would only
    # surface as an AttributeError the first time a user types the command.
    app = VibeApp(agent_loop=AgentLoop(make_ui_config(), backend=FakeBackend([])))
    command = app.commands.find_command("/undo")

    assert command is not None

    handler = getattr(app, command.handler, None)

    assert handler is not None
    assert asyncio.iscoroutinefunction(handler)


class TestUndoRendersTheRewoundTranscript:
    @pytest.mark.asyncio
    async def test_the_command_rewinds_rebuilds_and_confirms(self) -> None:
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "Second"),
                ("AssistantMessage", "R2"),
            ]

            handled = await app._handle_command("/undo")

            assert handled is True
            # The surviving turn is re-rendered first, then the echo the clear
            # destroyed, then the confirmation: mounting the echo before the
            # rebuild would leave the transcript out entirely.
            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "/undo"),
                ("UserCommandMessage", "Undid last turn: Second"),
            ]

        assert [message.role for message in agent.messages] == [
            Role.system,
            Role.user,
            Role.assistant,
        ]
        assert agent._turn_boundaries == [1]

    @pytest.mark.asyncio
    async def test_a_second_undo_leaves_only_the_echo_and_confirmation(self) -> None:
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")
            await app._handle_command("/undo")

            # Only the system message survives, so the rebuild correctly renders
            # nothing and this invocation's own widgets are all that remain.
            assert rendered(app) == [
                ("UserMessage", "/undo"),
                ("UserCommandMessage", "Undid last turn: First"),
            ]

        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.system
        assert agent._turn_boundaries == []

    @pytest.mark.asyncio
    async def test_a_streaming_message_is_finalized_before_the_area_is_cleared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            # Leave a streaming assistant message open, as an interrupted turn
            # would, so the finalization step has something to close out.
            await app._mount_and_scroll(AssistantMessage("streaming reply"))

            assert app._current_streaming_message is not None

            populated = len(app.query_one("#messages").children)
            children_when_finalized: list[int] = []
            finalize = app._finalize_current_streaming_message

            async def recording_finalize() -> None:
                children_when_finalized.append(len(app.query_one("#messages").children))
                await finalize()

            monkeypatch.setattr(
                app, "_finalize_current_streaming_message", recording_finalize
            )

            await app._undo_last_turn()

            assert children_when_finalized[0] == populated
            assert app._current_streaming_message is None
            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "/undo"),
                ("UserCommandMessage", "Undid last turn: Second"),
            ]


class TestUndoConfirmationSummary:
    @pytest.mark.asyncio
    async def test_the_confirmation_names_only_the_first_line(self) -> None:
        agent = await agent_with_turns(
            "  Add a health endpoint  \nand document it in the readme  "
        )
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            assert rendered(app)[-1] == (
                "UserCommandMessage",
                "Undid last turn: Add a health endpoint",
            )

    @pytest.mark.asyncio
    async def test_a_long_first_line_is_truncated_with_an_ellipsis(self) -> None:
        agent = await agent_with_turns(LONG_REQUEST)
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            assert rendered(app)[-1] == (
                "UserCommandMessage",
                f"Undid last turn: {LONG_REQUEST_SUMMARY}",
            )
            assert LONG_REQUEST_SUMMARY.count("…") == 1
            assert "ery retry." not in LONG_REQUEST_SUMMARY

    @pytest.mark.asyncio
    async def test_the_confirmation_keeps_markdown_punctuation_literal(self) -> None:
        agent = await agent_with_turns(MARKDOWN_REQUEST)
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            # The confirmation widget renders Markdown, so an unescaped summary
            # would style itself instead of echoing what was typed: the asterisks
            # would embolden, the backticks would become an inline-code span and
            # the bracket-parenthesis pair would swallow the URL. Escaping every
            # ASCII punctuation character keeps the prompt literal, and the
            # escapes themselves are consumed by the parser rather than shown.
            assert rendered(app)[-1] == (
                "UserCommandMessage",
                f"Undid last turn: {MARKDOWN_REQUEST_SUMMARY}",
            )

    @pytest.mark.asyncio
    async def test_a_blank_first_line_confirms_without_a_summary(self) -> None:
        agent = await agent_with_turns("   \n  ")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            assert rendered(app)[-1] == ("UserCommandMessage", "Undid last turn.")

    @pytest.mark.asyncio
    async def test_a_blank_first_line_never_quotes_the_line_beneath_it(self) -> None:
        # The preview is confined to the first line, which means the extraction has
        # to be anchored at the very first character: a search that merely found
        # the first non-blank character anywhere would reach past the line break
        # and quote content the reader would then take for the opening line.
        agent = await agent_with_turns("   \nSECOND LINE", "\n\nTHIRD LINE")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            assert rendered(app)[-1] == ("UserCommandMessage", "Undid last turn.")

            await app._handle_command("/undo")

            assert rendered(app)[-1] == ("UserCommandMessage", "Undid last turn.")


class TestUndoConfirmationIsSafeToRender:
    """The confirmation quotes recovered text, so its preview stays bounded.

    The quoted prompt reaches a widget that renders Markdown and is ultimately
    written to a terminal, and it can be large: pasted, restored from a session or
    supplied by an embedder. The fixtures below carry the escape introducers, C1
    codes, zero-width characters and direction controls the preview removes, plus
    prompts of a line count no preview may walk.
    """

    @pytest.mark.asyncio
    async def test_terminal_escape_and_direction_controls_are_stripped(self) -> None:
        agent = await agent_with_turns(CONTROL_REQUEST)
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            assert rendered(app)[-1] == (
                "UserCommandMessage",
                f"Undid last turn: {CONTROL_REQUEST_SUMMARY}",
            )

    @pytest.mark.asyncio
    async def test_no_control_or_format_character_survives_the_preview(self) -> None:
        agent = await agent_with_turns(OSC_REQUEST)
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            confirmation = rendered(app)[-1][1]

            assert confirmation is not None
            # Every escape introducer, C1 code, bidirectional isolate and
            # zero-width character this prompt carries is in the removed set, so
            # none of them survives into a control or format category here.
            assert not [
                character
                for character in confirmation
                if unicodedata.category(character).startswith("C")
            ]
            # Only the invisible characters were removed: the words remain, so a
            # reader still recognises the prompt that was undone.
            assert "Set " in confirmation
            assert "pwned" in confirmation
            assert "title" in confirmation

    @pytest.mark.asyncio
    async def test_a_prompt_of_countless_lines_is_previewed_without_copying_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)
        hostile = GuardedPrompt(
            "Refactor everything" + "\ntrailing noise" * HOSTILE_LINE_COUNT
        )

        async with app.run_test() as pilot:
            await pilot.pause()

            # Returning the prompt straight from the rewind keeps the guarded
            # object intact all the way into the handler.
            monkeypatch.setattr(agent, "undo_last_turn", lambda: hostile)

            await app._handle_command("/undo")

            # The first line is quoted and the rest is never looked at: a copy of
            # the prompt or a list of its lines would have raised out of
            # GuardedPrompt and surfaced here as a failure message instead.
            assert rendered(app)[-1] == (
                "UserCommandMessage",
                "Undid last turn: Refactor everything",
            )

    @pytest.mark.asyncio
    async def test_countless_blank_lines_are_dismissed_without_copying_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The mirror case, and the one that could tempt an implementation into
        # scanning: however much text follows, an empty first line ends the search
        # at the first character rather than hunting down the prompt for one.
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)
        hostile = GuardedPrompt(
            "\n" * HOSTILE_LINE_COUNT + "Refactor everything" * HOSTILE_LINE_COUNT
        )

        async with app.run_test() as pilot:
            await pilot.pause()

            monkeypatch.setattr(agent, "undo_last_turn", lambda: hostile)

            await app._handle_command("/undo")

            assert rendered(app)[-1] == ("UserCommandMessage", "Undid last turn.")


class TestUndoWithNothingToUndo:
    @pytest.mark.asyncio
    async def test_a_no_op_reports_itself_and_destroys_nothing(self) -> None:
        # A transcript restored from an earlier session records no rewind point,
        # so it is displayed but not rewindable: exactly the no-op case.
        agent = AgentLoop(make_ui_config(), backend=FakeBackend([]))
        agent.messages.extend([
            LLMMessage(role=Role.user, content="Restored question"),
            LLMMessage(role=Role.assistant, content="Restored answer"),
        ])
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            restored = list(app.query_one("#messages").children)

            assert rendered(app) == [
                ("UserMessage", "Restored question"),
                ("AssistantMessage", "Restored answer"),
            ]

            handled = await app._handle_command("/undo")

            assert handled is True
            assert rendered(app) == [
                ("UserMessage", "Restored question"),
                ("AssistantMessage", "Restored answer"),
                ("UserMessage", "/undo"),
                ("UserCommandMessage", "Nothing to undo."),
            ]
            # Nothing was torn down, so the restored widgets are still the very
            # same objects and the dispatcher's own echo survived untouched.
            surviving = list(app.query_one("#messages").children)
            assert surviving[0] is restored[0]
            assert surviving[1] is restored[1]

        assert len(agent.messages) == 3


class TestUndoIsRefusedWhileTheAgentIsRunning:
    @pytest.mark.asyncio
    async def test_a_busy_loop_refuses_the_rewind_and_keeps_the_transcript(
        self,
    ) -> None:
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            app._agent_running = True
            handled = await app._handle_command("/undo")

            assert handled is True
            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "Second"),
                ("AssistantMessage", "R2"),
                ("UserMessage", "/undo"),
                ("ErrorMessage", BUSY_REFUSAL),
            ]

            refusal = app.query_one(ErrorMessage)

            assert refusal.collapsed is app._tools_collapsed
            # The widget prefixes its own "Error:" when expanded, so the message
            # must not start with that word itself.
            assert not refusal._error.startswith("Error")

        assert len(agent.messages) == 5
        assert agent._turn_boundaries == [1, 3]

    def test_only_undo_asks_the_submit_path_to_leave_a_turn_running(self) -> None:
        # Which inputs reach their handler with the turn still running is decided
        # by the registry lookup, so the classification is asserted directly as
        # well: every other command, and every plain message, still interrupts.
        app = VibeApp(agent_loop=AgentLoop(make_ui_config(), backend=FakeBackend([])))

        assert app._requires_idle_agent("/undo") is True
        assert app._requires_idle_agent("  /UNDO  ") is True
        assert app._requires_idle_agent("/status") is False
        assert app._requires_idle_agent("/clear") is False
        assert app._requires_idle_agent("/compact") is False
        assert app._requires_idle_agent("Refactor the retry policy") is False

    @pytest.mark.asyncio
    async def test_submitting_undo_leaves_the_running_turn_alone(self) -> None:
        # The guard is only worth anything if the real submit path reaches it with
        # the turn still running. Every other submission interrupts first, which
        # would cancel the turn - possibly after a tool had already had an effect -
        # and then let the rewind proceed as if nothing had been busy.
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()
            turn = start_stalled_turn(app)

            app.post_message(ChatInputContainer.Submitted("/undo"))
            await pilot.pause()

            assert app._agent_running is True
            assert app._interrupt_requested is False
            assert not turn.done()
            # No interrupt widget, so nothing pretended the turn had ended.
            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "Second"),
                ("AssistantMessage", "R2"),
                ("UserMessage", "/undo"),
                ("ErrorMessage", BUSY_REFUSAL),
            ]

            await finish_stalled_turn(app, turn)

        assert len(agent.messages) == 5
        assert agent._turn_boundaries == [1, 3]

    @pytest.mark.asyncio
    async def test_submitting_another_command_still_interrupts_as_before(self) -> None:
        # Only /undo bypasses the interrupt; every other command retains the
        # default interrupt behavior.
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()
            turn = start_stalled_turn(app)

            app.post_message(ChatInputContainer.Submitted("/status"))
            await pilot.pause()

            assert turn.cancelled()
            assert app._agent_running is False
            assert [name for name, _ in rendered(app)] == [
                "UserMessage",
                "AssistantMessage",
                "InterruptMessage",
                "UserMessage",
                "UserCommandMessage",
            ]

            await finish_stalled_turn(app, turn)


class TestUndoMountsWithoutSplittingALiveReply:
    """A refused rewind arrives beneath a reply that is still streaming.

    Anything mounted between two chunks normally closes the open assistant widget
    out, because it is arriving after the model finished. For this one command it
    is not: the submit path deliberately leaves the turn running, so closing the
    widget would send the next chunk into a second one and split a single reply in
    two, leaving the view disagreeing with the one assistant message the
    transcript holds.
    """

    @pytest.mark.asyncio
    async def test_the_echo_and_the_refusal_keep_the_open_reply_open(self) -> None:
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._mount_and_scroll(AssistantMessage("chunk one "))
            streaming = app._current_streaming_message

            assert streaming is not None

            app._agent_running = True
            await app._handle_command("/undo")

            assert app._current_streaming_message is streaming
            assert [name for name, _ in rendered(app)] == [
                "UserMessage",
                "AssistantMessage",
                "AssistantMessage",
                "UserMessage",
                "ErrorMessage",
            ]

            await app._mount_and_scroll(AssistantMessage("chunk two"))

            assert app._current_streaming_message is streaming
            assert [name for name, _ in rendered(app)] == [
                "UserMessage",
                "AssistantMessage",
                "AssistantMessage",
                "UserMessage",
                "ErrorMessage",
            ]

    @pytest.mark.asyncio
    async def test_an_ordinary_mount_still_closes_the_open_reply(self) -> None:
        # The preserve-stream behavior is opt-in; ordinary mounts still finalize
        # the current stream.
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._mount_and_scroll(AssistantMessage("chunk one "))
            streaming = app._current_streaming_message

            assert streaming is not None

            await app._mount_and_scroll(UserCommandMessage("Agent statistics."))

            assert app._current_streaming_message is None

            await app._mount_and_scroll(AssistantMessage("a new reply"))

            assert app._current_streaming_message is not None
            assert app._current_streaming_message is not streaming
            assert [name for name, _ in rendered(app)] == [
                "UserMessage",
                "AssistantMessage",
                "AssistantMessage",
                "UserCommandMessage",
                "AssistantMessage",
            ]


class TestUndoFailuresAreReported:
    @pytest.mark.asyncio
    async def test_a_failing_rewind_is_reported_as_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)

        def failing_rewind() -> str | None:
            raise RuntimeError("rewind exploded")

        async with app.run_test() as pilot:
            await pilot.pause()

            monkeypatch.setattr(agent, "undo_last_turn", failing_rewind)
            await app._handle_command("/undo")

            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "/undo"),
                ("ErrorMessage", "Failed to undo: rewind exploded"),
            ]

    @pytest.mark.asyncio
    async def test_a_failure_after_the_rewind_is_reported_as_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await agent_with_turns("First")
        app = VibeApp(agent_loop=agent)

        async def failing_rebuild() -> None:
            raise RuntimeError("rebuild exploded")

        async with app.run_test() as pilot:
            await pilot.pause()

            monkeypatch.setattr(app, "_rebuild_history_from_messages", failing_rebuild)
            await app._handle_command("/undo")

            # Re-rendering is attempted again before reporting, but a failure that
            # persists cannot be recovered from: what matters is that the user is
            # told, rather than left with an unexplained blank transcript.
            assert rendered(app) == [
                ("ErrorMessage", "Failed to undo: rebuild exploded")
            ]

        assert len(agent.messages) == 1

    @pytest.mark.asyncio
    async def test_a_failure_before_the_teardown_still_converges_on_the_rewind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The rewind commits before anything is re-rendered, so a failure in the
        # very first rendering step would otherwise leave the removed turn on
        # screen: a transcript the user was told had been rewound.
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)
        attempts: list[int] = []
        screen_at_failure: list[list[tuple[str, str | None]]] = []

        async with app.run_test() as pilot:
            await pilot.pause()
            render = app._render_rewound_transcript

            async def render_failing_once() -> None:
                attempts.append(len(attempts))
                if not attempts[-1]:
                    screen_at_failure.append(rendered(app))
                    raise RuntimeError("render exploded")
                await render()

            monkeypatch.setattr(app, "_render_rewound_transcript", render_failing_once)
            await app._handle_command("/undo")

            # At the moment of failure the screen still showed the turn the core
            # had already removed: precisely the divergence to be closed.
            assert screen_at_failure[0] == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "Second"),
                ("AssistantMessage", "R2"),
                ("UserMessage", "/undo"),
            ]
            assert len(attempts) == 2
            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "/undo"),
                ("ErrorMessage", "Failed to undo: render exploded"),
            ]

        assert [message.role for message in agent.messages] == [
            Role.system,
            Role.user,
            Role.assistant,
        ]

    @pytest.mark.asyncio
    async def test_a_failure_after_the_teardown_still_converges_on_the_rewind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The mirror case: the area has already been emptied when the failure
        # lands, so without a recovery the user would be left staring at nothing.
        agent = await agent_with_turns("First", "Second")
        app = VibeApp(agent_loop=agent)
        attempts: list[int] = []

        async with app.run_test() as pilot:
            await pilot.pause()
            rebuild = app._rebuild_history_from_messages

            async def rebuild_failing_once() -> None:
                attempts.append(len(attempts))
                if not attempts[-1]:
                    raise RuntimeError("rebuild exploded")
                await rebuild()

            monkeypatch.setattr(
                app, "_rebuild_history_from_messages", rebuild_failing_once
            )
            await app._handle_command("/undo")

            assert len(attempts) == 2
            assert rendered(app) == [
                ("UserMessage", "First"),
                ("AssistantMessage", "R1"),
                ("UserMessage", "/undo"),
                ("ErrorMessage", "Failed to undo: rebuild exploded"),
            ]

        assert len(agent.messages) == 3

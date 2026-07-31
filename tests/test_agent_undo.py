from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
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

    A tool-bearing turn appends four messages rather than two: the user message,
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
        # No fragment of the tool-bearing turn survives anywhere in the history.
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

        # clear_history removes every recorded boundary, so undo must be a local
        # no-op that preserves the remaining system message.
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

        # Leave both recorded boundaries out of range for the truncated list.
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

        # Nothing was popped and nothing was truncated.
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

        # Once the turn has released the transcript the same rewind succeeds.
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

        # The refusal popped no boundary and truncated nothing, so the clear
        # still owns exactly the transcript it saved.
        assert len(agent.messages) == 3
        assert agent._turn_boundaries == [1]

        release.set()
        await clearing

        # The clear then completes normally and drops what it invalidated, so
        # the rewind that was refused is now a no-op rather than a rewind.
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

        # Pinned deliberately rather than fixed. The logger appends whatever the
        # transcript holds beyond the count it last persisted
        # (vibe/core/session/session_logger.py), and a rewind lowers that count
        # without touching what was written, so a turn refilling the rewound span
        # sits inside the already-persisted range and is not appended. Undo
        # neither rewrites the log nor rolls the session over the way /clear and
        # /compact do, because it rewinds the transcript rather than discarding
        # the session. Revisit this assertion with the logger if its cursoring
        # ever becomes identity-based.
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

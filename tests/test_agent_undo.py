from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentLoop, AgentLoopStateError
from vibe.core.config import (
    Backend,
    ModelConfig,
    ProviderConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.types import AssistantEvent, LLMMessage, Role, UserMessageEvent

SENTINEL_RESPONSE = "sentinel response: a rewind must never consume this stream"


def make_config(
    *, system_prompt_id: str = "tests", active_model: str = "devstral-latest"
) -> VibeConfig:
    """Build a hermetic configuration for the stubbed undo scenarios.

    Session logging and auto-compaction are both off so nothing rewrites the
    transcript mid-test, and no tool is enabled so each turn is two messages.
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
        session_logging=SessionLoggingConfig(enabled=False),
        auto_compact_threshold=0,
        system_prompt_id=system_prompt_id,
        include_project_context=False,
        include_prompt_detail=False,
        active_model=active_model,
        models=models,
        providers=providers,
        enabled_tools=[],
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

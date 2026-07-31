from __future__ import annotations

import pytest

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentLoop
from vibe.core.config import (
    Backend,
    ModelConfig,
    ProviderConfig,
    SessionLoggingConfig,
    VibeConfig,
)
from vibe.core.types import Role


def make_config(
    *, system_prompt_id: str = "tests", active_model: str = "devstral-latest"
) -> VibeConfig:
    """Build a hermetic configuration for driving stubbed undo scenarios.

    Session logging is disabled so no interaction file is written mid-test, and
    the auto-compaction threshold is pinned to zero so the auto-compact
    middleware is never installed. Without that pin a turn could compact itself
    behind the suite's back, replacing the message list and clearing the turn
    boundaries that every assertion below depends on. No tool is enabled, which
    keeps each turn exactly two messages long: the user message and the
    assistant reply.

    Returns:
        A configuration bound to a single priced Mistral model, one provider and
        no tools
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


class TestUndoRewindsOneTurn:
    """Rewind semantics: one turn per call, then a safe no-op."""

    @pytest.mark.asyncio
    async def test_undo_restores_end_of_first_turn(self) -> None:
        backend = FakeBackend([
            [mock_llm_chunk(content="R1")],
            [mock_llm_chunk(content="R2")],
        ])
        agent = AgentLoop(make_config(), backend=backend)
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

        turn_one_user = agent.messages[1].content
        turn_one_assistant = agent.messages[2].content

        removed = agent.undo_last_turn()

        assert removed == "Second"
        assert len(agent.messages) == 3
        assert agent.messages[0].role == Role.system
        assert agent.messages[1].role == Role.user
        assert agent.messages[1].content == turn_one_user
        assert agent.messages[2].role == Role.assistant
        assert agent.messages[2].content == turn_one_assistant

    @pytest.mark.asyncio
    async def test_second_undo_leaves_only_the_system_message(self) -> None:
        backend = FakeBackend([
            [mock_llm_chunk(content="R1")],
            [mock_llm_chunk(content="R2")],
        ])
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        assert agent.undo_last_turn() == "Second"
        assert agent.undo_last_turn() == "First"

        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.system

    @pytest.mark.asyncio
    async def test_third_undo_is_a_safe_no_op(self) -> None:
        backend = FakeBackend([
            [mock_llm_chunk(content="R1")],
            [mock_llm_chunk(content="R2")],
        ])
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("First"):
            pass
        async for _ in agent.act("Second"):
            pass

        assert agent.undo_last_turn() == "Second"
        assert agent.undo_last_turn() == "First"

        # The boundary stack is now exhausted, so further calls report that there
        # is nothing to undo instead of touching the surviving system message.
        assert agent.undo_last_turn() is None
        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.system


class TestUndoPreservesCumulativeStats:
    """Undo rewinds the transcript only, never the cumulative statistics."""

    @pytest.mark.asyncio
    async def test_cumulative_session_stats_survive_every_undo(self) -> None:
        backend = FakeBackend([
            [mock_llm_chunk(content="R1")],
            [mock_llm_chunk(content="R2")],
        ])
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

        # Two real rewinds followed by a no-op: none of them may move a counter,
        # matching how a reload preserves cumulative session totals.
        for _ in range(3):
            agent.undo_last_turn()

            assert agent.stats.session_prompt_tokens == prompt_tokens
            assert agent.stats.session_completion_tokens == completion_tokens
            assert agent.stats.session_total_llm_tokens == total_llm_tokens
            assert agent.stats.session_cost == session_cost
            assert agent.stats.steps == steps


class TestUndoAfterHistoryReset:
    """Boundaries recorded before a clear or a compaction must never be reused."""

    @pytest.mark.asyncio
    async def test_undo_after_clear_history_is_a_no_op(self) -> None:
        backend = FakeBackend(mock_llm_chunk(content="Response"))
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("Hello"):
            pass

        assert len(agent.messages) == 3

        await agent.clear_history()

        assert len(agent.messages) == 1

        # The surviving boundary of 1 now addresses past the end of the truncated
        # list, so it must be discarded rather than raising an IndexError.
        assert agent.undo_last_turn() is None
        assert len(agent.messages) == 1
        assert agent.messages[0].role == Role.system

    @pytest.mark.asyncio
    async def test_undo_after_compact_is_a_no_op(self) -> None:
        # The second stubbed stream is consumed by the summarization turn that
        # compact() drives; count_tokens is served locally by the fake backend.
        backend = FakeBackend([
            [mock_llm_chunk(content="First response")],
            [mock_llm_chunk(content="<summary>")],
        ])
        agent = AgentLoop(make_config(), backend=backend)

        async for _ in agent.act("Build something"):
            pass

        await agent.compact()

        assert len(agent.messages) == 2

        # A boundary of 1 still addresses inside this two-message list, so the
        # staleness guard alone would hand back the summary. Returning None
        # proves compact() dropped the boundaries it invalidated.
        assert agent.undo_last_turn() is None
        assert len(agent.messages) == 2
        assert agent.messages[0].role == Role.system

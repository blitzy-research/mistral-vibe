"""Coverage for the `/undo` terminal interface handler and its dispatch.

The handler is reached reflectively through the command registry, and its
teardown order is load-bearing: the streaming widget must be finalized before
the message area is emptied, and the surviving transcript must be re-rendered
before the command echo is re-mounted, because the rebuild routine returns early
whenever the area still has children. None of that is checked by a type checker,
so every scenario below drives the real application over a stubbed backend and
asserts on the widgets actually mounted, in the order they were mounted.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widget import Widget

from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.cli.textual_ui.app import VibeApp
from vibe.cli.textual_ui.widgets.messages import (
    AssistantMessage,
    ErrorMessage,
    UserCommandMessage,
    UserMessage,
)
from vibe.core.agent_loop import AgentLoop
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.types import LLMMessage, Role

BUSY_REFUSAL = "Cannot undo while agent loop is processing. Please wait."
# The request below is 89 characters, so it overruns the eighty-character
# confirmation budget: the handler keeps the first 79 characters and spends the
# eightieth on a single ellipsis. The expected summary is spelled out rather
# than recomputed so the test cannot drift with the production formatting, and
# it shows the comma backslash-escaped, because the confirmation widget renders
# its content as Markdown and the recovered prompt has to stay literal.
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


def make_config() -> VibeConfig:
    """Build a hermetic configuration for the stubbed interface scenarios.

    Session logging, update checks and auto-compaction are all off so nothing
    writes to disk, reaches the network or rewrites the transcript mid-test.
    """
    return VibeConfig(
        session_logging=SessionLoggingConfig(enabled=False),
        enable_update_checks=False,
        auto_compact_threshold=0,
        system_prompt_id="tests",
        include_project_context=False,
        include_prompt_detail=False,
        enabled_tools=[],
    )


async def agent_with_turns(*prompts: str) -> AgentLoop:
    """Drive one stubbed turn per prompt against a fake backend.

    Turns are driven through the real conversation loop, so the rewind points the
    handler relies on are recorded by production code rather than by the test.

    Returns:
        An agent loop whose transcript and rewind stack were built by real turns
    """
    backend = FakeBackend([
        [mock_llm_chunk(content=f"R{number}")]
        for number, _ in enumerate(prompts, start=1)
    ])
    agent = AgentLoop(make_config(), backend=backend)
    for prompt in prompts:
        async for _ in agent.act(prompt):
            pass
    return agent


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
    app = VibeApp(agent_loop=AgentLoop(make_config(), backend=FakeBackend([])))
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

            # The handler finalized while every widget was still mounted, so no
            # streaming widget is orphaned by the removal that follows.
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
            # The budget is spent on visible characters, so exactly one ellipsis
            # closes the line and the discarded tail never reaches the widget.
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
        # A rewindable turn whose content is only blank space has nothing worth
        # previewing, so the confirmation must not trail an empty colon phrase.
        agent = await agent_with_turns("   \n  ")
        app = VibeApp(agent_loop=agent)

        async with app.run_test() as pilot:
            await pilot.pause()

            await app._handle_command("/undo")

            assert rendered(app)[-1] == ("UserCommandMessage", "Undid last turn.")


class TestUndoWithNothingToUndo:
    @pytest.mark.asyncio
    async def test_a_no_op_reports_itself_and_destroys_nothing(self) -> None:
        # A transcript restored from an earlier session records no rewind point,
        # so it is displayed but not rewindable: exactly the no-op case.
        agent = AgentLoop(make_config(), backend=FakeBackend([]))
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

        # The refusal is a message, not a rewind.
        assert len(agent.messages) == 5
        assert agent._turn_boundaries == [1, 3]


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

            # The failure is surfaced and nothing was torn down.
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

            # A failure raised after the area was cleared must still be reported
            # rather than swallowed, leaving the user with a blank transcript.
            assert rendered(app) == [
                ("ErrorMessage", "Failed to undo: rebuild exploded")
            ]

        assert len(agent.messages) == 1

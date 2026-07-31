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
from contextlib import suppress
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
from vibe.core.agent_loop import AgentLoop
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.types import LLMMessage, Role

BUSY_REFUSAL = "Cannot undo while agent loop is processing. Please wait."
# Long enough that the stand-in turn is still pending when the assertions run,
# and always cancelled by the test rather than waited on.
STALLED_TURN_TIMEOUT = 30.0
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
# Enough blank lines before, and content lines after, that copying the prompt or
# listing its lines to find the first one would be plainly wasteful.
HOSTILE_LINE_COUNT = 20_000


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


class GuardedPrompt(str):
    """A prompt that refuses the whole-prompt operations a preview must avoid.

    Recovering one short line must not copy the entire prompt, nor build a list
    holding every line of it, so a prompt pasted at any size costs the same as a
    short one. Both operations are trapped here rather than timed, which keeps the
    guarantee deterministic instead of dependent on how fast the test host is.
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


class TestUndoConfirmationIsSafeToRender:
    """The confirmation quotes recovered text, so it must be inert and bounded.

    The quoted prompt reaches a widget that renders Markdown and is ultimately
    written to a terminal, and it can be arbitrarily large and arbitrarily
    hostile: pasted, restored from a session or supplied by an embedder.
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
            # Nothing in the Unicode control or format categories is left, which
            # covers every escape introducer, the C1 range and the bidirectional
            # and zero-width characters, whatever their combination.
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
            "\n" * HOSTILE_LINE_COUNT
            + "Refactor everything"
            + "\ntrailing noise" * HOSTILE_LINE_COUNT
        )

        async with app.run_test() as pilot:
            await pilot.pause()

            # Returning the prompt straight from the rewind keeps the guarded
            # object intact all the way into the handler.
            monkeypatch.setattr(agent, "undo_last_turn", lambda: hostile)

            await app._handle_command("/undo")

            # The leading blank lines are skipped and the first line with content
            # is quoted, exactly as before, but a copy of the prompt or a list of
            # its lines would have raised out of GuardedPrompt and surfaced here
            # as a failure message instead of this confirmation.
            assert rendered(app)[-1] == (
                "UserCommandMessage",
                "Undid last turn: Refactor everything",
            )


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

    @pytest.mark.asyncio
    async def test_submitting_undo_leaves_the_running_turn_alone(self) -> None:
        # The guard is only worth anything if the real submit path reaches it
        # with the turn still running. Every other submission interrupts first,
        # which would cancel the turn - possibly after a tool had already had an
        # effect - and then let the rewind proceed as if nothing had been busy.
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

        # The transcript the running turn owns is untouched.
        assert len(agent.messages) == 5
        assert agent._turn_boundaries == [1, 3]

    @pytest.mark.asyncio
    async def test_submitting_another_command_still_interrupts_as_before(self) -> None:
        # Only undo opts out of the interrupt: preserving what every other
        # command does matters as much as fixing the rewind.
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
            # The recovery re-rendered from the truncated message list, so the
            # undone turn is gone from the screen as well as from the transcript,
            # and the failure is still reported.
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

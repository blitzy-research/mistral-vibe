from __future__ import annotations

from vibe.cli.commands import CommandRegistry


def test_undo_command_is_registered() -> None:
    registry = CommandRegistry()

    command = registry.find_command("/undo")

    # The registered handler names the Textual application coroutine, which is
    # resolved reflectively with getattr, so nothing but this assertion guards
    # its spelling: the leading underscore is deliberate and load-bearing.
    assert command is not None
    assert command.handler == "_undo_last_turn"
    assert command.description == "Undo the last conversation turn"
    assert command.aliases == frozenset(["/undo"])
    assert command.exits is False


def test_help_text_lists_undo_command() -> None:
    help_text = CommandRegistry().get_help_text()

    # Asserted against the rendered output rather than a re-implementation of
    # the renderer, so the help section stays covered by the real formatting.
    assert "- `/undo`: Undo the last conversation turn" in help_text


def test_undo_lookup_ignores_case_and_surrounding_whitespace() -> None:
    registry = CommandRegistry()
    expected = registry.find_command("/undo")

    # Identity holds only within a single registry, because every construction
    # builds fresh Command objects. The final lookup pins exact alias matching,
    # so a longer string that merely starts with the alias resolves to nothing.
    assert expected is not None
    assert registry.find_command("  /UNDO  ") is expected
    assert registry.find_command("/Undo") is expected
    assert registry.find_command("  /undo") is expected
    assert registry.find_command("/undone") is None


def test_registry_keeps_existing_commands_alongside_undo() -> None:
    registry = CommandRegistry()

    clear_command = registry.find_command("/clear")
    compact_command = registry.find_command("/compact")

    # Nine pre-existing commands plus /undo. The two history-mutating siblings
    # are spot-checked because they are the commands the rewind interacts with.
    assert len(registry.commands) == 10
    assert clear_command is not None
    assert clear_command.handler == "_clear_history"
    assert compact_command is not None
    assert compact_command.handler == "_compact_history"

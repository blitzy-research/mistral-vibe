from __future__ import annotations

from vibe.cli.commands import CommandRegistry

UNDO_ALIAS = "/undo"
UNDO_DESCRIPTION = "Undo the last conversation turn"
UNDO_HANDLER = "_undo_last_turn"
REGISTERED_COMMAND_NAMES = frozenset({
    "clear",
    "compact",
    "config",
    "exit",
    "help",
    "log",
    "reload",
    "status",
    "terminal-setup",
    "undo",
})


def test_undo_command_is_registered() -> None:
    registry = CommandRegistry()

    command = registry.find_command(UNDO_ALIAS)

    # The handler name is resolved reflectively at runtime; this assertion pins
    # the registry value, whose leading underscore is deliberate.
    assert command is not None
    assert command.handler == UNDO_HANDLER
    assert command.description == UNDO_DESCRIPTION
    assert command.aliases == frozenset([UNDO_ALIAS])
    assert command.exits is False


def test_help_text_lists_undo_command() -> None:
    help_text = CommandRegistry().get_help_text()

    assert f"- `{UNDO_ALIAS}`: {UNDO_DESCRIPTION}" in help_text


def test_undo_lookup_ignores_case_and_surrounding_whitespace() -> None:
    registry = CommandRegistry()
    expected = registry.find_command(UNDO_ALIAS)

    # Identity holds only within a single registry, because every construction
    # builds fresh Command objects. The final lookup pins exact alias matching,
    # so a longer string that merely starts with the alias resolves to nothing.
    assert expected is not None
    assert registry.find_command("  /UNDO  ") is expected
    assert registry.find_command("/Undo") is expected
    assert registry.find_command("  /undo") is expected
    assert registry.find_command("  /Undo  ") is expected
    assert registry.find_command("/undone") is None


def test_registry_keeps_existing_commands_alongside_undo() -> None:
    registry = CommandRegistry()

    clear_command = registry.find_command("/clear")
    compact_command = registry.find_command("/compact")

    # The registry retains the full command set; clear and compact are the
    # history-operation siblings, while exit remains the sole exiting command.
    assert frozenset(registry.commands) == REGISTERED_COMMAND_NAMES
    assert len(registry.commands) == len(REGISTERED_COMMAND_NAMES)
    assert clear_command is not None
    assert clear_command.handler == "_clear_history"
    assert compact_command is not None
    assert compact_command.handler == "_compact_history"
    assert registry.commands["exit"].exits is True

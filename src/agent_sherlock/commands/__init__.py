from __future__ import annotations

from agent_sherlock.commands.base import Command
from agent_sherlock.commands.connections import COMMAND as connections_command

# The registry of available `sherlock <command>` subcommands.
#
# To add a command: copy `_template.py`, implement it, import the Command
# instance here, and append it to this list. Order controls help display.
COMMANDS: list[Command] = [connections_command]

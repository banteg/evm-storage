"""Human and machine output helpers."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from evm_storage.model import ResolvedChange

stderr = Console(stderr=True)
stdout = Console()


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def print_changes(changes: Iterable[ResolvedChange]) -> None:
    groups: dict[str, list[ResolvedChange]] = defaultdict(list)
    for change in changes:
        groups[change.change.address].append(change)
    for address, items in groups.items():
        stdout.print(Text(address, style="bold"))
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("path", overflow="fold")
        table.add_column("before", overflow="fold")
        table.add_column("after", overflow="fold")
        table.add_column("confidence")
        for item in items:
            path = item.path or f"slot {item.change.slot:#066x}"
            path_text = Text(path)
            if item.type_label:
                path_text.append(f" ({item.type_label})", style="dim")
            table.add_row(
                path_text,
                Text(str(item.before)),
                Text(str(item.after)),
                Text(item.confidence),
            )
            if item.reason:
                table.add_row(Text(f"  {item.reason}", style="dim"), "", "", "")
        stdout.print(table)


def warning(message: str) -> None:
    stderr.print(Text.assemble(("warning:", "yellow"), " ", message))

"""Numbered menu shown when `pitwall` runs with no command (or the .exe is double-clicked)."""

from __future__ import annotations

from collections.abc import Callable

ITEMS: tuple[tuple[str, list[str]], ...] = (
    ("Start Pitwall", ["start"]),
    ("Check setup (ports, telemetry, speech)", ["doctor"]),
    ("Radio check", ["speak"]),
    ("Debrief last session", ["debrief"]),
    ("Show learned values", ["stats", "--learned"]),
    ("Clean up old files", ["cleanup"]),
    ("Run upkeep now", ["maintain"]),
)


def render() -> str:
    lines = ["", "PITWALL"]
    lines += [f"  {n}  {label}" for n, (label, _) in enumerate(ITEMS, 1)]
    lines += ["  0  Quit"]
    return "\n".join(lines)


def run_menu(
    dispatch: Callable[[list[str]], int],
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> int:
    """Loop until Quit; each choice runs the matching `pitwall` command."""
    while True:
        write(render())
        try:
            choice = read("Choose: ").strip()
        except (EOFError, KeyboardInterrupt):
            write("")
            return 0
        if choice in ("0", "q", "quit", "exit"):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(ITEMS):
            write(f"Type a number 0-{len(ITEMS)}.")
            continue
        label, argv = ITEMS[int(choice) - 1]
        write(f"> pitwall {' '.join(argv)}")
        try:
            dispatch(argv)
        except KeyboardInterrupt:
            write(f"{label}: stopped")
        except SystemExit:
            pass

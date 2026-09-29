from __future__ import annotations

from pitwall.terminal_menu import ITEMS, run_menu


def test_menu_runs_choices_until_quit() -> None:
    ran: list[list[str]] = []
    out: list[str] = []
    answers = iter(["5", "x", "9", "1", "0"])
    code = run_menu(lambda a: ran.append(a) or 0, lambda _: next(answers), out.append)
    assert code == 0
    assert ran == [["stats", "--learned"], ["start"]]
    assert sum("Type a number" in line for line in out) == 2
    assert len(ITEMS) == 7


def test_menu_survives_ctrl_c_and_errors() -> None:
    def dispatch(argv: list[str]) -> int:
        if argv == ["start"]:
            raise KeyboardInterrupt
        raise SystemExit(2)

    answers = iter(["1", "2"])

    def read(_: str) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    out: list[str] = []
    assert run_menu(dispatch, read, out.append) == 0
    assert "Start Pitwall: stopped" in out

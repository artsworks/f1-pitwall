from __future__ import annotations

import argparse
from pathlib import Path

from pitwall.audio.grades import format_table, load_workbook, replay
from pitwall.config.loader import ConfigStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay radio priority grades")
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path("scenarios/radio-priority-grades.yaml"),
    )
    parser.add_argument("--id", action="append", dest="ids")
    args = parser.parse_args()
    scenarios = load_workbook(args.workbook)
    if args.ids:
        requested = set(args.ids)
        scenarios = [scenario for scenario in scenarios if scenario.id in requested]
        missing = requested - {scenario.id for scenario in scenarios}
        if missing:
            parser.error(f"unknown scenario id(s): {', '.join(sorted(missing))}")
    settings = ConfigStore(isolated=True).current()
    results = [replay(scenario, settings) for scenario in scenarios]
    print(format_table(results))
    failures = sum(len(result.mismatches) for result in results)
    passed = len(results) - sum(bool(result.mismatches) for result in results)
    print(f"{passed}/{len(results)} scenarios pass")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

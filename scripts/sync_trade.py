"""Run one trade export sync pass, suitable for an existing task scheduler."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent.trade_sync import sync, write_report  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import configured CSV/XLSX prices and stock (one pass)")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--report", type=Path, help="optional additional JSON report")
    args = parser.parse_args(argv)
    if args.report and args.report.resolve() in (args.config.resolve(), args.db.resolve()):
        parser.error("--report must differ from --config and --db")
    try:
        report = sync(args.config, args.db)
        if args.report:
            write_report(args.report, report)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

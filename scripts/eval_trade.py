"""Run the synthetic trade benchmark offline: python scripts/eval_trade.py."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent.trade_demo import DEMO_ROOT, evaluate


def main(argv=None):
    parser = argparse.ArgumentParser(description="ДЕМО: offline synthetic trade evaluation (no models)")
    parser.add_argument("--data", type=Path, default=DEMO_ROOT, help="Root containing agri, auto, drawings")
    parser.add_argument("--output", type=Path, help="Optional JSON report file")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.data)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ДЕМО evaluation failed: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    # Escaping only for terminals that cannot display Cyrillic; report files are UTF-8.
    print(rendered.encode(sys.stdout.encoding or "utf-8", errors="backslashreplace").decode(sys.stdout.encoding or "utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

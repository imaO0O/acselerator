"""Сторож: один проход — что из моделей, чата и REST API не отвечает, то запускается (kbagent/watchdog.py).

    python scripts/watchdog.py            проверить и перезапустить упавшее
    python scripts/watchdog.py --status   только показать, что работает

По расписанию его запускает задача KBAgent-watchdog (scripts/install_tasks.bat): при входе в Windows и каждые
2 минуты. Режим моделей — KB_MODEL_MODE в data/settings.env: gpu (по умолчанию), smallgpu, cpu.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import watchdog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Сторож моделей, чата и API")
    ap.add_argument("--status", action="store_true", help="только показать состояние")
    args = ap.parse_args()
    names = watchdog.watched()
    if args.status:
        for name, ok in watchdog.check(names).items():
            print(f"{watchdog.TITLES[name]}: {'работает' if ok else 'не отвечает'}")
        return 0
    state = watchdog.run_once(names)
    down = [n for n in names if not state["services"][n].get("up")]
    for name in names:
        s = state["services"][name]
        print(f"{watchdog.TITLES[name]}: " + ("работает" if s.get("up") else f"не отвечает — {s.get('last_error', '')}"))
    return 1 if down else 0


if __name__ == "__main__":
    sys.exit(main())

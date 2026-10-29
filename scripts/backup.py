"""Резервная копия данных заказчика и перенос в новую версию (kbagent/backup.py).

    python scripts/backup.py create                     архив в backups/ (хранятся 10 последних)
    python scripts/backup.py verify backups/agent_backup_….zip
    python scripts/backup.py restore путь/к/agent_backup_….zip

Обновление версии: в старой установке — create; новая поставка ставится рядом (install.bat); в новой — restore
с архивом из старой, затем python scripts/check_demo.py. Перед restore остановите чат и REST API
(scripts/stop_servers.bat останавливает модели; чат — закрыть окно run_chat).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import backup  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Резервная копия данных заказчика")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create", help="создать архив").add_argument("--to", type=Path, help="папка для архива")
    sub.add_parser("verify", help="проверить архив").add_argument("archive", type=Path)
    r = sub.add_parser("restore", help="восстановить из архива")
    r.add_argument("archive", type=Path)
    r.add_argument("--force", action="store_true", help="не проверять, запущены ли чат и API")
    args = ap.parse_args()
    if args.cmd == "create":
        path = backup.create(out_dir=args.to)
        print(f"Архив: {path}\n{backup.summary(backup.verify(path))}")
        if backup.copy_dir():                        # копия на другом диске (KB_BACKUP_COPY)
            rep = backup.copy_to(path)
            if not rep["ok"]:
                print(f"Копия на «{rep['target']}» не записана: {rep['error']}")
                return 1
            print(f"Копия: {rep['path']} — проверена" + (" (тот же диск, что и система!)" if rep["same_disk"] else ""))
        return 0
    if args.cmd == "verify":
        try:
            print("Архив цел: " + backup.summary(backup.verify(args.archive)))
            return 0
        except (ValueError, KeyError) as e:
            print(f"Архив повреждён: {e}")
            return 1
    busy = backup.running()
    if busy and not args.force:
        print(f"Сейчас работают: {', '.join(busy)}. Остановите их и повторите — восстанавливать поверх работающей "
              "системы нельзя.")
        return 1
    manifest, safety = backup.restore(args.archive)
    print(f"Восстановлено: {backup.summary(manifest)}\nСостояние до восстановления сохранено: {safety}\n"
          "Дальше: python scripts/check_demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

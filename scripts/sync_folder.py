r"""Папка документов отдела → база знаний (kbagent/folder_sync.py). Нужен запущенный сервер векторов bge-m3.

    python scripts/sync_folder.py                          папка — KB_DOCS_FOLDER
    python scripts/sync_folder.py --source \\server\docs\ТКП   другая папка
    python scripts/sync_folder.py --watch 600              проверять каждые 10 минут (оставить работать)
    python scripts/sync_folder.py --allow-mass-removal     из папки намеренно удалено много файлов — убрать их

Чат подхватывает изменения сам: перед ответом он перечитывает индекс, если тот изменился.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import folder_sync  # noqa: E402
from kbagent.config import settings  # noqa: E402
from kbagent.store import Index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Папка документов отдела → база знаний")
    ap.add_argument("--source", default=settings.docs_folder, help="папка (по умолчанию — KB_DOCS_FOLDER)")
    ap.add_argument("--watch", type=int, default=0, help="проверять папку каждые N секунд")
    ap.add_argument("--allow-mass-removal", action="store_true", help="разрешить удалить больше половины файлов")
    args = ap.parse_args()
    if not args.source:
        print("Укажите папку: --source или переменная KB_DOCS_FOLDER")
        return 2
    if not (settings.index_dir / "meta.json").exists():
        print("Индекса нет — сначала python scripts/build_index.py")
        return 2
    index = Index.load(settings.index_dir)
    while True:
        rep = folder_sync.sync(index, Path(args.source), max_removed_share=1.0 if args.allow_mass_removal else 0.5)
        print(f"{rep.started}: добавлено {len(rep.added)}, обновлено {len(rep.updated)}, удалено {len(rep.removed)}, "
              f"без изменений {rep.unchanged}, ошибок {len(rep.errors)} — {rep.seconds} с")
        for name, error in rep.errors:
            print(f"  ошибка: {name}: {error}")
        if rep.warning:
            print(f"  внимание: {rep.warning}")
        if not args.watch:
            return 1 if rep.errors or rep.warning else 0
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())

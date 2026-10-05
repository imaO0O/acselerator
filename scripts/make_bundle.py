"""Поставка для закрытого контура: одна папка, которая ставится на компьютер без интернета.

Собирается на компьютере с интернетом (там, где проект уже работает):

    python scripts/make_bundle.py E:\\agent_bundle             полная поставка (около 9 ГБ: модели — 7,3 ГБ)
    python scripts/make_bundle.py E:\\agent_bundle --no-models  без моделей — проверить сборку и установку

В папке поставки:
    agent\\            код, документы базы знаний, индекс, документы для показа, модели, llama.cpp (видеокарта NVIDIA), языки распознавания
    llama-cpu\\        llama.cpp для компьютера без видеокарты NVIDIA (install.bat подставит сам)
    wheels\\           пакеты Python для установки без интернета
    python-3.12.exe   установщик Python (если на компьютере его нет)
    install.bat       установка: Python → окружение .venv → пакеты из wheels\\ → нужная сборка llama.cpp
    SHA256SUMS.txt    контрольные суммы всех файлов — для проверки службой безопасности после переноса
    УСТАНОВКА.txt     что делать по шагам
"""
import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from setup import LLAMA_BUILD, LLAMA_URL, fetch  # noqa: E402

PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"

INSTALL_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка AI-агента по документам (без интернета) ===

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден - устанавливаю Python 3.12 для текущего пользователя...
    python-3.12.exe /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=0
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
)

echo Окружение Python и пакеты из папки wheels...
python -m venv agent\.venv || goto :error
agent\.venv\Scripts\python -m pip install --no-index --find-links wheels -r agent\requirements.txt || goto :error

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo Видеокарта NVIDIA не найдена - ставлю сборку llama.cpp для процессора
    rmdir /s /q agent\tools\llama.cpp
    xcopy /e /i /q llama-cpu agent\tools\llama.cpp >nul
    echo Запуск моделей: agent\scripts\start_servers.bat -Cpu
) else (
    echo Запуск моделей: agent\scripts\start_servers.bat
)
echo Чат: agent\scripts\run_chat.bat   Проверка: agent\.venv\Scripts\python agent\scripts\check_demo.py
echo === Готово ===
pause
exit /b 0

:error
echo Ошибка установки - см. сообщения выше.
pause
exit /b 1
"""

README = """Установка AI-агента на компьютер без доступа в интернет

1. Проверьте целостность после переноса (по желанию службы безопасности):
   certutil -hashfile <файл> SHA256 — сравните с SHA256SUMS.txt.
2. Запустите install.bat. Он поставит Python 3.12 (если его нет), создаст окружение agent\\.venv,
   установит пакеты из папки wheels и выберет сборку llama.cpp: для видеокарты NVIDIA или для процессора.
   Интернет не нужен.
3. Запустите модели: agent\\scripts\\start_servers.bat (с видеокартой) или agent\\scripts\\start_servers.bat -Cpu.
4. Запустите чат: agent\\scripts\\run_chat.bat — откроется в браузере на этом компьютере.
5. Перед работой смените тестовые пароли: agent\\.venv\\Scripts\\python agent\\scripts\\make_users.py
   (пароли — в самом скрипте), документы предприятия добавляются на странице «Администрирование».

Требования: Windows 10/11 x64, 16 ГБ оперативной памяти, около 12 ГБ на диске; видеокарта NVIDIA
от 4 ГБ — по желанию (без неё ответ около 4,5 с, с ней — 1–2 с).
"""


def copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description="Поставка для закрытого контура")
    ap.add_argument("out", type=Path, help="папка поставки")
    ap.add_argument("--no-models", action="store_true", help="без моделей (проверить сборку и установку)")
    args = ap.parse_args()
    out, agent = args.out, args.out / "agent"
    out.mkdir(parents=True, exist_ok=True)

    print("1. Код и документы (из git — только то, что в репозитории)")
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=zip", "HEAD"],
                             capture_output=True, check=True).stdout
    zipfile.ZipFile(io.BytesIO(archive)).extractall(agent)

    print("2. Индекс, документы для показа, языки распознавания, llama.cpp" + ("" if args.no_models else ", модели"))
    if not (ROOT / "data" / "show").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_show_docs.py")], check=True)
    for part in ["data/index", "data/show", "tools/tessdata", "tools/llama.cpp"] + ([] if args.no_models else ["models"]):
        copy(ROOT / part, agent / part)
        print(f"   {part}")

    print("3. llama.cpp для компьютера без видеокарты NVIDIA")
    cpu_zip = ROOT / "tools" / f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"
    fetch(f"{LLAMA_URL}/{cpu_zip.name}", cpu_zip)
    with zipfile.ZipFile(cpu_zip) as z:
        z.extractall(out / "llama-cpu")

    print("4. Пакеты Python и установщик Python")
    subprocess.run([sys.executable, "-m", "pip", "download", "-q", "-r", str(ROOT / "requirements.txt"),
                    "-d", str(out / "wheels")], check=True)
    fetch(PYTHON_URL, out / "python-3.12.exe")

    (out / "install.bat").write_text(INSTALL_BAT, encoding="utf-8")
    (out / "УСТАНОВКА.txt").write_text(README, encoding="utf-8-sig")

    print("5. Контрольные суммы")
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS.txt")
    (out / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(p)}  {p.relative_to(out).as_posix()}\n" for p in files), encoding="utf-8")
    size = sum(p.stat().st_size for p in files) / 2**30
    print(f"\nГотово: {out} — {len(files)} файлов, {size:.1f} ГБ. Дальше — УСТАНОВКА.txt")


if __name__ == "__main__":
    main()

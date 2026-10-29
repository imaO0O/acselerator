r"""HTTPS для чата и REST API (kbagent/tls.py). После любой команды перезапустите чат и API.

    python scripts/make_cert.py                         сертификат на имя и адреса этого компьютера (2 года)
    python scripts/make_cert.py --name agent.zavod.local --ip 10.0.0.15   ещё имена и адреса
    python scripts/make_cert.py --pfx agent.pfx         сертификат от службы ИТ (пароль PFX спросит)
    python scripts/make_cert.py --show                  что установлено сейчас
    python scripts/make_cert.py --remove                вернуть HTTP

Свой сертификат подписывается центром сертификации агента: data\tls\agent-ca.cer один раз установите на
компьютерах сотрудников в «Доверенные корневые центры сертификации» (двойной щелчок → «Установить сертификат» →
«Локальный компьютер»; на много компьютеров — групповой политикой), и браузер перестанет предупреждать.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent import tls  # noqa: E402


def show() -> None:
    i = tls.info()
    if i is None:
        print("HTTPS не настроен: чат и API работают по HTTP.")
        return
    who = "центр сертификации агента" if i["own_ca"] else i["issuer"]
    print(f"Сертификат: {', '.join(i['names']) or '—'}; действует до {i['until']} (осталось {i['days_left']} дн.); "
          f"выдал: {who}")
    if i["days_left"] < tls.WARN_DAYS:
        print("Срок подходит к концу — перевыпустите: python scripts/make_cert.py")


def main() -> int:
    ap = argparse.ArgumentParser(description="HTTPS для чата и REST API")
    ap.add_argument("--name", action="append", default=[], help="ещё имя сервера (можно несколько раз)")
    ap.add_argument("--ip", action="append", default=[], help="ещё адрес сервера (можно несколько раз)")
    ap.add_argument("--days", type=int, default=tls.SERVER_DAYS, help="срок сертификата, дней")
    ap.add_argument("--pfx", type=Path, help="PFX от службы ИТ вместо своего сертификата")
    ap.add_argument("--show", action="store_true", help="что установлено")
    ap.add_argument("--remove", action="store_true", help="убрать сертификат — вернуть HTTP")
    args = ap.parse_args()
    if args.show:
        show()
        return 0
    if args.remove:
        tls.remove()
        print("Сертификат сервера убран: после перезапуска чат и API — по HTTP.")
        return 0
    try:
        if args.pfx:
            tls.from_pfx(args.pfx.read_bytes(), getpass.getpass("Пароль PFX (пусто — без пароля): "))
        else:
            names, ips = tls.default_names()
            tls.issue(list(dict.fromkeys(names + args.name)), list(dict.fromkeys(ips + args.ip)), args.days)
    except (OSError, ValueError) as e:
        print(f"Не получилось: {e}")
        return 1
    show()
    host = next(iter((tls.info() or {}).get("names", [])), "localhost")
    print(f"Перезапустите чат (scripts\\run_chat.bat) и API — адрес станет https://{host}:8501")
    if not args.pfx:
        print(f"Сотрудникам: установите {tls.CA_PUBLIC} в «Доверенные корневые центры сертификации».")
    return 0


if __name__ == "__main__":
    sys.exit(main())

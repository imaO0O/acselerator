"""Запуск чата из корня проекта (там лежит .streamlit/config.toml с оформлением).

    python scripts/run_chat.py
    python scripts/run_chat.py --server.port 8502 --server.headless true

Если есть сертификат (scripts/make_cert.py) — чат открывается по HTTPS.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    os.chdir(ROOT)
    from streamlit.web import cli

    from kbagent import tls

    flags = tls.streamlit_flags(sys.argv[1:])
    if flags:
        print("HTTPS: сертификат data/tls/cert.pem — открывайте чат по адресу https://…")
    sys.argv = ["streamlit", "run", str(ROOT / "app" / "streamlit_app.py"), *sys.argv[1:], *flags]
    sys.exit(cli.main())

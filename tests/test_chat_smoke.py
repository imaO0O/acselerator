"""Чат запускается и отдаёт страницу браузеру — как после scripts\\run_chat.bat.

Обычные автотесты проверяют логику, а не запуск: 30.10 скрипт scripts/watchdog.py подменил библиотеку, через
которую Streamlit следит за файлами, и чат перестал открываться — тесты были зелёными. Здесь чат запускается
настоящим сервером, к нему подключаются так же, как браузер, и просят отрисовать страницу: должна прийти
страница входа (или первого запуска), а в журнале сервера — ни одной ошибки."""
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _texts(msg) -> list[str]:
    """Тексты элементов страницы из сообщения сервера (ForwardMsg)."""
    out = []
    if msg.WhichOneof("type") != "delta":
        return out
    delta = msg.delta
    if delta.WhichOneof("type") == "new_element":
        el = delta.new_element
        kind = el.WhichOneof("type")
        if kind == "exception":
            out.append(f"ОШИБКА: {el.exception.type}: {el.exception.message}")
        elif kind in ("markdown", "text"):
            out.append(getattr(el, kind).body)
        elif kind == "button":
            out.append(el.button.label)
        elif kind in ("text_input",):
            out.append(el.text_input.label)
    return out


def test_chat_starts_and_serves_a_page_to_the_browser(tmp_path):
    from kbagent import tls
    if tls.paths():
        pytest.skip("на этом компьютере чат работает по HTTPS — проверка по ws:// не к месту")
    from streamlit.proto.BackMsg_pb2 import BackMsg
    from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
    port = _free_port()
    log = tmp_path / "chat.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "run_chat.py"), "--server.port", str(port),
                                 "--server.headless", "true"], stdout=out, stderr=subprocess.STDOUT, cwd=ROOT)
    try:
        deadline = time.time() + 90
        while True:
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2).status == 200:
                    break
            except OSError:
                pass
            assert proc.poll() is None, "чат не запустился:\n" + log.read_text(encoding="utf-8", errors="replace")
            assert time.time() < deadline, "чат не ответил за 90 с"
            time.sleep(0.5)
        texts, broken = [], None
        try:
            texts = _page(connect, port, BackMsg, ForwardMsg)
        except ConnectionClosed as e:             # сервер оборвал сеанс браузера — причина в журнале сервера
            broken = e
        time.sleep(1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    server = log.read_text(encoding="utf-8", errors="replace")
    assert "Traceback" not in server and "Exception in ASGI" not in server, server[-3000:]
    assert broken is None, f"сервер оборвал сеанс браузера: {broken}"
    assert not [t for t in texts if t.startswith("ОШИБКА")], texts
    page = " ".join(texts)
    assert "Войти" in page or "администратор" in page.lower(), texts[:20]


def _page(connect, port, BackMsg, ForwardMsg) -> list[str]:
    """Подключиться как браузер, попросить отрисовать страницу и собрать её тексты."""
    texts = []
    with connect(f"ws://127.0.0.1:{port}/_stcore/stream", subprotocols=["streamlit"], open_timeout=20,
                 max_size=None) as ws:
        back = BackMsg()
        back.rerun_script.query_string = ""
        back.rerun_script.page_script_hash = ""
        ws.send(back.SerializeToString())
        end = time.time() + 40
        while time.time() < end:
            try:
                raw = ws.recv(timeout=max(end - time.time(), 0.1))
            except TimeoutError:
                break
            msg = ForwardMsg()
            msg.ParseFromString(raw)
            texts += _texts(msg)
            if msg.WhichOneof("type") == "script_finished":
                break
    return texts

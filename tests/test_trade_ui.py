from streamlit.testing.v1 import AppTest


def test_trade_page_has_working_offline_result_exports(tmp_path, monkeypatch, tmp_settings):
    from app import ui_trade
    monkeypatch.setattr(ui_trade, 'DATA', tmp_path / 'trade')
    script = '''
import streamlit as st
from app.ui_trade import trade_page
st.session_state.ctx = {'login': 'seller', 'history_key': 'seller', 'role': 'Техпродажи и сервис', 'is_admin': False}
st.session_state.trade_state_seller = {'started': 9999999999, 'version': 0, 'search_seconds': 0.1,
 'result': [{'line': 1, 'query': 'DEMO-01', 'qty': 2, 'status': 'exact', 'candidates': [],
 'selected': {'article': 'DEMO-01', 'name': 'Демо-фильтр', 'price': 120, 'stock': 3, 'vat_rate': 20,
 'source': 'demo.csv', 'source_row': 2, 'lead_time': '1 день'}}]}
trade_page()
'''
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert any('Результат подбора' in h.value for h in app.subheader)
    assert len(app.get('download_button')) == 2


def test_trade_page_denies_non_sales_roles(tmp_path, monkeypatch, tmp_settings):
    from app import ui_trade
    monkeypatch.setattr(ui_trade, 'DATA', tmp_path / 'trade')
    app = AppTest.from_string('''
import streamlit as st
from app.ui_trade import trade_page
st.session_state.ctx = {'login':'engineer','history_key':'engineer','role':'Инженер производства','is_admin':False}
trade_page()
''').run()
    assert not app.exception
    assert app.error


def test_drawing_demo_opens_real_pdf_and_does_not_record_pilot(tmp_path, monkeypatch, tmp_settings):
    from app import ui_trade
    monkeypatch.setattr(ui_trade, 'DATA', tmp_path / 'trade')
    app = AppTest.from_string('''
import streamlit as st
from app.ui_trade import trade_page
st.session_state.ctx = {'login':'admin','history_key':'admin','role':'Администратор','is_admin':True}
trade_page()
''').run()
    next(w for w in app.selectbox if w.label == 'Сценарий').select('drawings').run()
    next(w for w in app.button if w.label == 'Открыть демо-кейс').click().run()
    assert not app.exception
    state = app.session_state['trade_state_admin']
    assert state['fields'][0]['designation'] == 'DEMO-DR-001'
    assert len(state['items']) == 2
    next(w for w in app.button if w.label == 'Подобрать все позиции').click().run()
    assert not app.exception
    assert all(row['status'] == 'exact' for row in app.session_state['trade_state_admin']['result'])
    assert not any(w.label == 'Завершить проверку и записать замер' for w in app.button)

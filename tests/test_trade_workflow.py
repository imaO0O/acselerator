"""Offline integration coverage; all databases, originals and logs are temporary.

Run AppTest outside the Windows sandbox because Streamlit's local socketpair
can hang inside it.
"""
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from contextlib import closing
from decimal import Decimal

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook

from kbagent import llm, trade_sources
from kbagent.trade_catalog import TradeCatalog
from kbagent.trade_export import calculate, export_xlsx, proposal_docx
from kbagent.trade_requests import match_batch, parse_letter_with_gigachat, read_file, text_items


HEADERS = ['article', 'name', 'price', 'stock', 'vat_rate', 'cross_numbers', 'lead_time']
ROWS = [
    ['00120', 'Oil pump', '120', '1000', '20', '', '1 day'],
    ['ALT900', 'Replacement valve', '55', '1000', '10', 'OEM700', '2 days'],
    ['LOW500', 'Bearing', '30', '1', '20', '', '3 days'],
]
QUERIES = ['00120', 'OEM700', 'LOW500', 'ZZZ99999999']
STATUS_LABELS = {'exact': 'Точное совпадение', 'review': 'Требует проверки',
                 'out_of_stock': 'Недостаточно на складе', 'not_found': 'Не найдено',
                 'analog': 'Аналог подтверждён'}


def catalog_bytes(rows=ROWS):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def import_catalog(root, rows=ROWS):
    root.mkdir(parents=True, exist_ok=True)
    catalog = TradeCatalog(root / 'catalog.sqlite')
    data = catalog_bytes(rows)
    report = catalog.import_table('supplier.csv', data, {key: key for key in HEADERS})
    digest = trade_sources.archive(root, data)
    assert report['committed'] and report['imported'] == len(rows)
    assert digest == hashlib.sha256(data).hexdigest()
    return catalog, digest


def request_xlsx(count):
    book = Workbook()
    book.active.title = 'Order'
    book.active.append(['Артикул', 'Количество'])
    for i in range(count):
        book.active.append([QUERIES[i % 4], 2])
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def xlsx_values(data, count):
    book = load_workbook(io.BytesIO(data), data_only=True)
    try:
        values = list(book.active.values)
        header = next(i for i, row in enumerate(values) if row[:3] == ('№', 'Запрос', 'Статус'))
        rows = [dict(zip(values[header], row)) for row in values[header + 1:header + 1 + count]]
        totals = {row[0]: row[7] for row in values if isinstance(row[0], str) and row[0].startswith('Итого')}
        return rows, totals
    finally:
        book.close()


def word_values(data):
    document = Document(io.BytesIO(data))
    paragraphs = '\n'.join(p.text for p in document.paragraphs)
    sources = {number: (source, row) for number, source, row in re.findall(
        r'(?m)^\[(\d+)\] (.+); строка источника: (.+)$', paragraphs)}
    rows = []
    for row in document.tables[0].rows[1:]:
        cells = [cell.text for cell in row.cells]
        status, offered = cells[8].split('\n')
        reference = re.search(r'\[(\d+)\]$', cells[1]).group(1)
        source, source_row = sources[reference]
        rate, vat = cells[5].split('\n')
        rows.append({'Артикул': cells[1].split(' — ', 1)[0], 'Количество': cells[2],
                     'Цена за единицу': cells[3], 'Сумма в базе цены': cells[4],
                     'НДС, %': rate.removesuffix(' %'), 'НДС': vat, 'Наличие': cells[6],
                     'Статус': status, 'Включено в предложение': 'Да' if offered == 'Включено' else 'Нет',
                     'Источник': source, 'Строка источника': source_row})
    return rows, paragraphs


@pytest.mark.parametrize('count', [20, 100])
@pytest.mark.parametrize('inclusive', [True, False])
def test_import_request_match_and_office_values(tmp_path, count, inclusive):
    catalog, digest = import_catalog(tmp_path / 'trade')
    items = read_file('order.xlsx', request_xlsx(count))['items']
    assert len(items) == count
    assert [item['source_row'] for item in items] == list(range(2, count + 2))
    assert all(item['source'] == 'order.xlsx / Order' and item['qty'] == 2 for item in items)
    result = match_batch(catalog, items, catalog='Основной')
    assert [p['status'] for p in result] == ['exact', 'review', 'out_of_stock', 'not_found'] * (count // 4)
    assert all(p['selected'] is None for p in result if p['status'] in ('review', 'not_found'))
    cross = result[1]['candidates'][0]
    assert cross['article'] == 'ALT900' and cross['match_type'] == 'cross' and cross['needs_review']
    for position in result:
        for candidate in position['candidates']:
            assert candidate['source_sha256'] == digest
            assert trade_sources.read(tmp_path / 'trade', candidate['source_sha256']) == catalog_bytes()
    gross_each, net_each, vat_each = (240, 200, 40) if inclusive else (288, 240, 48)
    copies = count // 4
    expected = {'С НДС': gross_each * copies, 'Без НДС': net_each * copies, 'НДС': vat_each * copies}
    calculated = calculate(result, inclusive)
    assert calculated['offered_count'] == copies and calculated['excluded_count'] == count - copies
    assert calculated['total'] == Decimal(expected['С НДС'])
    assert calculated['totals_complete'] and not calculated['complete']
    sheet, totals = xlsx_values(export_xlsx(result, customer='Workflow buyer', prices_include_vat=inclusive), count)
    word, paragraphs = word_values(proposal_docx(result, customer='Workflow buyer', prices_include_vat=inclusive))
    assert len(sheet) == len(word) == count
    for i, (excel, doc) in enumerate(zip(sheet, word)):
        assert excel['Количество'] == 2 and Decimal(doc['Количество']) == 2
        assert excel['Статус'] == doc['Статус'] == STATUS_LABELS[result[i]['status']]
        assert excel['Включено в предложение'] == doc['Включено в предложение'] == ('Да' if i % 4 == 0 else 'Нет')
        if i % 4 in (0, 2):
            assert excel['Артикул'] == doc['Артикул'] == QUERIES[i % 4]
            assert excel['Источник'] == doc['Источник'] == 'supplier.csv'
            assert int(doc['Строка источника']) == excel['Строка источника'] == (2 if i % 4 == 0 else 4)
        if i % 4 == 0:
            assert excel['Цена за единицу'] == Decimal(doc['Цена за единицу']) == 120
            assert excel['Наличие'] == Decimal(doc['Наличие']) == 1000
            assert excel['С НДС'] == gross_each
            assert excel['Сумма в базе цены'] == Decimal(doc['Сумма в базе цены']) == 240
            assert excel['НДС'] == Decimal(doc['НДС']) == vat_each
    for label, value in expected.items():
        assert totals['Итого предлагаемых строк — ' + label] == value
        assert f'Итого предлагаемых строк — {label}: {value:.2f}' in paragraphs
    assert 'Workflow buyer' in paragraphs and 'ЧЕРНОВИК' in paragraphs


def test_archived_original_remains_verifiable_after_catalog_replacement(tmp_path):
    catalog, old_digest = import_catalog(tmp_path)
    old_hit = catalog.search('00120')[0]
    updated = [list(row) for row in ROWS]
    updated[0][2] = '999'
    _, new_digest = import_catalog(tmp_path, updated)
    assert old_digest != new_digest
    assert catalog.search('00120')[0]['price'] == 999
    assert old_hit['source_sha256'] == old_digest
    assert trade_sources.read(tmp_path, old_digest) == catalog_bytes()
    assert trade_sources.read(tmp_path, new_digest) == catalog_bytes(updated)
    (tmp_path / 'sources' / (old_digest + '.bin')).write_bytes(b'corrupted supplier data')
    with pytest.raises(ValueError, match='checksum'):
        trade_sources.read(tmp_path, old_digest)
    assert trade_sources.read(tmp_path, new_digest) == catalog_bytes(updated)


def ui_app(monkeypatch, root, *, page='selection', exports=None):
    from app import ui_trade
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(ui_trade, 'DATA', root)
    monkeypatch.setattr(ui_trade, 'log_action', lambda *a, **kw: None)
    if exports is not None:
        def capture_excel(*args, **kwargs):
            exports['xlsx'] = export_xlsx(*args, **kwargs)
            return exports['xlsx']

        def capture_word(*args, **kwargs):
            exports['docx'] = proposal_docx(*args, **kwargs)
            return exports['docx']

        monkeypatch.setattr(ui_trade, 'export_xlsx', capture_excel)
        monkeypatch.setattr(ui_trade, 'proposal_docx', capture_word)
    script = f'''
import streamlit as st
from app import ui_trade
st.session_state.setdefault('ctx', {{'login': 'workflow', 'history_key': 'workflow',
    'role': 'Техпродажи и сервис', 'is_admin': True}})
ui_trade.{page}()
'''
    return AppTest.from_string(script, default_timeout=30)


def button(app, label):
    return next(widget for widget in app.button if widget.label == label)


def assert_ui_ok(app):
    assert not app.exception, [exception.message for exception in app.exception]


def test_ui_catalog_upload_archives_identical_original(tmp_path, monkeypatch, tmp_settings):
    app = ui_app(monkeypatch, tmp_path, page='import_catalog').run()
    assert_ui_ok(app)
    app.file_uploader[0].set_value(('supplier.csv', catalog_bytes(), 'text/csv')).run()
    assert_ui_ok(app)
    next(box for box in app.checkbox if 'подтверждаю' in box.label).check()
    button(app, 'Загрузить каталог').click().run()
    assert_ui_ok(app)
    assert app.success
    selected = TradeCatalog(tmp_path / 'catalog.sqlite').search('00120')[0]
    assert selected['price'] == 120 and selected['stock'] == 1000
    assert trade_sources.read(tmp_path, selected['source_sha256']) == catalog_bytes()


@pytest.mark.parametrize('count', [20, 100])
def test_ui_batch_manual_cross_confirmation_changes_actual_exports(tmp_path, monkeypatch, tmp_settings, count):
    import_catalog(tmp_path)
    exports = {}
    app = ui_app(monkeypatch, tmp_path, exports=exports).run()
    assert_ui_ok(app)
    app.selectbox(key='trade-filter-catalog').select('Основной').run()
    assert_ui_ok(app)
    request = '\n'.join(f'{QUERIES[i % 4]} — 2 шт.' for i in range(count))
    app.chat_input[0].set_value(request).run()
    assert_ui_ok(app)
    button(app, 'Подобрать все позиции').click().run()
    assert_ui_ok(app)
    result = app.session_state['trade_state_workflow']['result']
    assert len(result) == count
    assert [p['status'] for p in result] == ['exact', 'review', 'out_of_stock', 'not_found'] * (count // 4)
    assert len(app.get('download_button')) >= 2
    before, before_total = xlsx_values(exports['xlsx'], count)
    assert before[1]['Включено в предложение'] == 'Нет'
    assert before_total['Итого предлагаемых строк — С НДС'] == 240 * (count // 4)
    version = app.session_state['trade_state_workflow']['version']
    app.selectbox(key=f'trade-choice-{version}-2').select(0).run()
    app.button(key='trade-confirm-2').click().run()
    assert_ui_ok(app)
    result = app.session_state['trade_state_workflow']['result']
    assert result[1]['status'] == 'analog' and result[1]['selected']['article'] == 'ALT900'
    assert result[0]['status'] == 'exact' and result[2]['status'] == 'out_of_stock'
    assert result[5]['status'] == 'review'  # Approval is per request line.
    excel, totals = xlsx_values(exports['xlsx'], count)
    word, paragraphs = word_values(exports['docx'])
    assert excel[1]['Артикул'] == word[1]['Артикул'] == 'ALT900'
    assert excel[1]['Количество'] == Decimal(word[1]['Количество']) == 2
    assert excel[1]['Цена за единицу'] == Decimal(word[1]['Цена за единицу']) == 55
    assert excel[1]['НДС'] == Decimal(word[1]['НДС']) == 10
    assert excel[1]['С НДС'] == Decimal(word[1]['Сумма в базе цены']) == 110
    assert excel[1]['Включено в предложение'] == word[1]['Включено в предложение'] == 'Да'
    assert totals['Итого предлагаемых строк — С НДС'] == 240 * (count // 4) + 110
    assert f'Итого предлагаемых строк — С НДС: {240 * (count // 4) + 110:.2f}' in paragraphs


def seed_result(app, root):
    from app import ui_trade
    result = match_batch(TradeCatalog(root / 'catalog.sqlite'), [{'query': '00120', 'qty': 2}])
    app.session_state['trade_state_workflow'] = {
        'result': result, 'version': 0, 'started': time.time(),
        'catalog_stamp': ui_trade._stamp(),
    }
    return result


def test_ui_rejects_stale_results_after_normal_import(tmp_path, monkeypatch, tmp_settings):
    import_catalog(tmp_path)
    app = ui_app(monkeypatch, tmp_path)
    seed_result(app, tmp_path)
    app.run()
    assert_ui_ok(app)
    assert app.get('download_button')
    updated = [list(row) for row in ROWS]
    updated[0][2] = '999'
    import_catalog(tmp_path, updated)
    app.run()
    assert_ui_ok(app)
    assert any('каталог изменился' in warning.value for warning in app.warning)
    assert not app.get('download_button')


def test_ui_invalidates_results_after_wal_only_catalog_update(tmp_path, monkeypatch, tmp_settings):
    catalog, _ = import_catalog(tmp_path)
    app = ui_app(monkeypatch, tmp_path)
    with closing(sqlite3.connect(catalog.path)) as keeper:
        keeper.execute('PRAGMA journal_mode=WAL')
        seed_result(app, tmp_path)
        app.run()
        assert_ui_ok(app)
        assert app.get('download_button')
        before = catalog.path.stat().st_mtime_ns
        updated = [list(row) for row in ROWS]
        updated[0][2] = '999'
        import_catalog(tmp_path, updated)
        assert catalog.path.stat().st_mtime_ns == before
        assert catalog.search('00120')[0]['price'] == 999
        app.run()
        assert_ui_ok(app)
        assert any('каталог изменился' in warning.value for warning in app.warning)
        assert not app.get('download_button')


def test_duplicate_request_lines_cannot_exceed_combined_stock(tmp_path):
    rows = [['00120', 'Oil pump', '120', '5', '20', '', '1 day']]
    catalog, _ = import_catalog(tmp_path, rows)
    positions = match_batch(catalog, text_items('00120 — 4 шт.\n00120 — 4 шт.'))
    assert [position['status'] for position in positions] == ['out_of_stock', 'out_of_stock']
    offered_qty = sum(p['qty'] for p in positions if p['status'] in ('exact', 'analog'))
    assert offered_qty <= 5, 'Two offered lines allocate eight units from a five-unit stock'


def test_letter_model_cannot_use_article_number_as_quantity(monkeypatch):
    text = 'Артикул 12345 — 2 шт.'
    monkeypatch.setattr(llm, 'chat', lambda *a, **kw: (json.dumps([
        {'query': '12345', 'qty': 12345, 'evidence': text}], ensure_ascii=False), {}))
    with pytest.raises(ValueError, match='Количество'):
        parse_letter_with_gigachat(text)


def test_letter_numeric_article_does_not_break_valid_quantity(monkeypatch):
    text = 'Артикул 123456789 — 2 шт.'
    monkeypatch.setattr(llm, 'chat', lambda *a, **kw: (json.dumps([
        {'query': '123456789', 'qty': 2, 'evidence': text}], ensure_ascii=False), {}))
    assert parse_letter_with_gigachat(text)[0]['qty'] == 2

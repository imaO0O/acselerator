import io

import pytest
from openpyxl import Workbook

from kbagent.trade_requests import drawing_fields, match_batch, quantity, read_file, text_items, validate


def test_quantity_does_not_damage_article_or_model():
    items = text_items('DEMO-AG-001 — 2 шт.\nФильтр на трактор ДЕМО-Т82 — 3 шт.\n52-001')
    assert [(v['query'], v['qty']) for v in items] == [('DEMO-AG-001', 2), ('Фильтр на трактор ДЕМО-Т82', 3), ('52-001', 1)]
    assert items[-1]['warning']
    for value in ['nan', 'inf', '-1', '0']:
        with pytest.raises(ValueError):
            quantity(value)


def test_excel_multiple_sheets_rows_and_csv_provenance():
    wb = Workbook()
    wb.active.title = 'Заявка'
    wb.active.append(['Артикул', 'Количество'])
    wb.active.append(['001-20', 2])
    ws = wb.create_sheet('Ещё')
    ws.append(['Описание', 'Кол-во'])
    ws.append(['Фильтр на ДЕМО-Т82', 4])
    data = io.BytesIO()
    wb.save(data)
    parsed = read_file('заявка.xlsx', data.getvalue())
    assert len(parsed['items']) == 2
    assert parsed['items'][0]['query'] == '001-20'
    assert parsed['items'][1]['source_row'] == 2
    assert 'Ещё' in parsed['items'][1]['source']
    csv = read_file('request.csv', 'Артикул;Количество\n001-20;2,5'.encode('utf-8-sig'))
    assert csv['items'][0]['qty'] == 2.5


def test_no_silent_truncation_or_invalid_blank_positions():
    assert len(text_items('\n'.join(f'PART-{i} — 1 шт.' for i in range(100)))) == 100
    with pytest.raises(ValueError, match='101'):
        text_items('\n'.join(f'PART-{i}' for i in range(101)))
    with pytest.raises(ValueError):
        validate([{'query': '', 'qty': 1}])
    with pytest.raises(ValueError, match='PDF'):
        read_file('part.dwg', b'demo')


def test_drawing_only_explicit_labeled_fields():
    fields = drawing_fields('Обозначение: DEMO-D-001\nНаименование: Втулка\nМатериал: Сталь ДЕМО\nМасса: 0,2 кг')
    assert fields == {'designation': 'DEMO-D-001', 'name': 'Втулка', 'material': 'Сталь ДЕМО', 'mass': '0,2 кг'}
    assert not any(drawing_fields('Просто фото неизвестной детали').values())


def test_batch_never_auto_approves_cross_or_fuzzy_and_checks_stock():
    class Catalog:
        def search(self, query, **kwargs):
            return {'one': [{'article': 'X', 'match_type': 'exact', 'stock': 1}],
                    'cross': [{'article': 'Y', 'match_type': 'cross', 'stock': 10}],
                    'ambiguous': [{'article': 'A', 'match_type': 'exact', 'stock': 10},
                                  {'article': 'A', 'match_type': 'exact', 'stock': 10}]}.get(query, [])
    items = [{'query': name, 'qty': 2} for name in ['one', 'cross', 'unknown', 'ambiguous']]
    out = match_batch(Catalog(), items)
    assert [p['status'] for p in out] == ['out_of_stock', 'review', 'not_found', 'review']
    assert out[1]['selected'] is None


def test_gigachat_extracts_only_verifiable_literal_spans(monkeypatch):
    from kbagent import llm
    from kbagent.trade_requests import parse_letter_with_gigachat
    monkeypatch.setattr(llm, 'chat', lambda *a, **kw: ('[{"query":"DEMO-01","qty":2,"evidence":"DEMO-01 — 2 шт."}]', {}))
    assert parse_letter_with_gigachat('Прошу DEMO-01 — 2 шт.')[0]['qty'] == 2
    with pytest.raises(ValueError, match='буквального'):
        parse_letter_with_gigachat('Другой запрос')
    monkeypatch.setattr(llm, 'chat', lambda *a, **kw: ('[{"query":"DEMO-01","qty":3,"evidence":"DEMO-01 — 2 шт."}]', {}))
    with pytest.raises(ValueError, match='Количество'):
        parse_letter_with_gigachat('DEMO-01 — 2 шт.')


def test_batch_stock_is_shared_across_repeated_positions_and_catalog_filter():
    class Catalog:
        def search(self, query, **kwargs):
            assert kwargs['catalog'] == 'Demo'
            return [{'article': 'ONE-01', 'match_type': 'exact', 'stock': 3}]
    out = match_batch(Catalog(), [{'query': 'ONE-01', 'qty': 2}] * 2, catalog='Demo')
    assert [p['status'] for p in out] == ['out_of_stock', 'out_of_stock']

"""Торговый подбор: табличные каталоги, несколько вложений и проверяемые предложения."""
import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui_common import ctx, inject_css
from kbagent.actions import log_action
from kbagent.config import ROOT, settings
from kbagent.trade_catalog import TradeCatalog, preview_table
from kbagent.trade_export import export_xlsx, proposal_docx
from kbagent.trade_pilot import PilotLog, RATINGS
from kbagent import trade_sources
from kbagent.privacy import mask
from kbagent.trade_requests import FILE_TYPES, check_stock, match_batch, parse_letter_with_gigachat, read_file, text_items, validate

DATA = ROOT / 'data' / 'trade'
LABELS = {'exact': 'Найдено точно', 'analog': 'Выбран сотрудником', 'review': 'Нужна проверка',
          'out_of_stock': 'Недостаточный остаток', 'not_found': 'Нет данных в каталоге'}
FIELDS = {'article': 'Артикул', 'name': 'Наименование', 'brand': 'Бренд', 'group': 'Группа',
          'price': 'Цена', 'stock': 'Остаток', 'lead_time': 'Срок', 'vat_rate': 'НДС, %',
          'cross_numbers': 'Кросс-номера', 'applicability': 'Применимость',
          'mark': 'Марка техники', 'model': 'Модель техники', 'year_from': 'Год с', 'year_to': 'Год по',
          'vin': 'VIN', 'price_level': 'Уровень цен'}


def _state():
    return st.session_state.setdefault('trade_state_' + ctx()['history_key'], {})


def _catalog():
    return TradeCatalog(DATA / 'catalog.sqlite')


def _stamp():
    stamps = []
    for name in ('catalog.sqlite', 'catalog.sqlite-wal'):
        try:
            info = (DATA / name).stat()
            stamps.append((info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            stamps.append((0, 0))
    return tuple(stamps)


def _pilot():
    return PilotLog(settings.log_dir / 'trade_pilot.sqlite')


def _user():
    return ctx()['login'] or ctx()['history_key']


def import_catalog():
    if not ctx()['is_admin']:
        st.info('Загружать и заменять общие каталоги может администратор.')
        return
    sync_panel()
    st.caption('Каталог хранится как таблица SQLite, а не фрагменты текста. Один каталог заменяется целиком; остальные сохраняются.')
    file = st.file_uploader('Каталог или прайс', type=['xlsx', 'csv'], key='trade-catalog-file')
    if file is None:
        return
    if file.size > 100 * 1024 * 1024:
        st.error('Каталог — до 100 МБ.')
        return
    data = file.getvalue()
    try:
        preview = preview_table(file.name, data)
        sheets = preview.get('sheets', [])
        sheet = st.selectbox('Лист каталога', sheets, key='trade-sheet') if sheets else None
        if sheet:
            preview = preview_table(file.name, data, sheet=sheet)
        st.dataframe(pd.DataFrame(preview['rows'], columns=preview['columns']), hide_index=True)
        with st.form('trade-mapping'):
            name = st.text_input('Название каталога', value='Основной')
            sync_path = st.text_input('Путь к обновляемой выгрузке на сервере (необязательно)',
                                      help='Для периодического чтения выгрузок CSV/XLSX из 1С. Укажите полный путь; исходная система не изменяется.')
            mapping = {}
            cols = st.columns(3)
            for i, (field, title) in enumerate(FIELDS.items()):
                options = ['—'] + preview['columns']
                synonyms = {'cross_numbers': ['аналоги', 'кросс-номера', 'кроссы'], 'vat_rate': ['ндс', 'ндс, %'],
                            'stock': ['остаток', 'наличие'], 'lead_time': ['срок', 'срок поставки']}
                expected = [field, title.lower()] + synonyms.get(field, [])
                default = next((j for j, c in enumerate(options) if c.lower() in expected), 0)
                chosen = cols[i % 3].selectbox(title, options, index=default, key='trade-map-' + field)
                if chosen != '—':
                    mapping[field] = chosen
            with st.expander('Колонки цен и скидок для клиентов'):
                for i in range(3):
                    tier, price_col, discount_col = st.columns(3)
                    level = tier.text_input('Уровень цен', key=f'trade-tier-{i}')
                    price = price_col.selectbox('Колонка цены', options, key=f'trade-tier-price-{i}')
                    discount = discount_col.selectbox('Колонка скидки, %', options, key=f'trade-tier-discount-{i}')
                    if level.strip():
                        if price != '—':
                            mapping[f'price:{level.strip()}'] = price
                        if discount != '—':
                            mapping[f'discount:{level.strip()}'] = discount
            checked = st.checkbox('Проверил колонки и подтверждаю замену этого каталога')
            submitted = st.form_submit_button('Загрузить каталог', type='primary')
        if submitted:
            if not checked or not name.strip():
                st.warning('Укажите название и подтвердите замену.')
                return
            if 'article' not in mapping or 'name' not in mapping:
                st.warning('Выберите колонки артикула и наименования.')
                return
            source = Path(sync_path.strip()) if sync_path.strip() else None
            if source and (not source.is_absolute() or source.suffix.lower() not in ('.csv', '.xlsx') or not source.is_file()):
                st.warning('Для обновления укажите полный путь к существующему CSV/XLSX на сервере.')
                return
            with st.spinner('Проверяю строки и строю поисковые индексы…'):
                report = _catalog().import_table(file.name, data, mapping, catalog=name.strip(), sheet=sheet)
                trade_sources.archive(DATA, data)
            if source:
                from kbagent.trade_sync import write_report
                conf = DATA / 'sync.json'
                existing = json.loads(conf.read_text(encoding='utf-8')) if conf.exists() else []
                existing = [x for x in existing if x.get('catalog') != name.strip()]
                existing.append({'path': str(source.resolve()), 'mapping': mapping, 'catalog': name.strip(), 'sheet': sheet})
                write_report(conf, existing)
            log_action('trade_import', _user(), name.strip(), str(report))
            st.success('Каталог загружен.')
            st.json(report)
    except (ValueError, OSError) as e:
        st.error(str(e))


def sync_panel():
    conf = DATA / 'sync.json'
    if conf.exists():
        with st.expander('Обновление цен и остатков из выгрузок'):
            st.caption('Настройки источников сохранены. Периодический запуск подключается при установке администратором; отсутствие обновлений не означает актуальный остаток.')
            if st.button('Обновить выгрузки сейчас'):
                from kbagent.trade_sync import sync
                st.json(sync(conf, DATA / 'catalog.sqlite'))
            report = conf.with_name('sync.last.json')
            if report.exists():
                st.json(json.loads(report.read_text(encoding='utf-8')))


def _source(p):
    sheet = f" / {p['sheet']}" if p.get('sheet') else ''
    return f"{p.get('source', '—')}{sheet}, строка {p.get('source_row', '—')}"


def _results(state):
    result = state.get('result')
    if not result:
        return
    check_stock(result)
    current_stamp = _stamp()
    if state.get('catalog_stamp', current_stamp) != current_stamp:
        st.warning('После подбора каталог изменился. Повторите подбор, чтобы выгрузить актуальные цены и остатки.')
        return
    st.subheader('Результат подбора')
    table = []
    for p in result:
        s = p.get('selected') or {}
        table.append({'№': p['line'], 'Запрошено': p['query'], 'Кол-во': p['qty'], 'Статус': LABELS[p['status']],
                      'Артикул': s.get('article', ''), 'Наименование': s.get('name', ''), 'Цена': s.get('price'),
                      'Остаток': s.get('stock'), 'Срок': s.get('lead_time', ''), 'Источник': _source(s) if s else ''})
    st.dataframe(pd.DataFrame(table), hide_index=True, width='stretch')
    st.caption(f"Поиск: {state.get('search_seconds', 0):.2f} с. Это время программы; время всей работы фиксируется после проверки ниже.")
    for p in result:
        candidates = p.get('candidates', [])
        if not candidates:
            continue
        with st.expander(f"Позиция {p['line']}: проверить источник / выбрать аналог"):
            st.dataframe(pd.DataFrame([{k: c.get(k) for k in ('article', 'name', 'brand', 'price', 'stock', 'lead_time',
                                                           'applicability', 'match_type', 'source', 'source_row')}
                                       for c in candidates]), hide_index=True)
            index = st.selectbox('Выбор сотрудника', list(range(-1, len(candidates))),
                                 format_func=lambda i, cs=candidates: '— не выбран —' if i < 0 else f"{cs[i]['article']} · {cs[i]['name']} · {_source(cs[i])}",
                                 key=f"trade-choice-{state['version']}-{p['line']}")
            source = candidates[index] if index >= 0 else p.get('selected') or candidates[0]
            digest = source.get('source_sha256')
            if digest and st.button('Открыть источник', key=f"trade-source-{p['line']}"):
                state['download_source'] = (p['line'], digest)
            if digest and state.get('download_source') == (p['line'], digest):
                try:
                    original = trade_sources.read(DATA, digest)
                    if original is not None:
                        st.download_button(_source(source), original, file_name=Path(source['source']).name,
                                           key=f"trade-source-download-{p['line']}", on_click='ignore')
                    else:
                        st.caption('Оригинал файла не сохранён. Название, лист и строка указаны в таблице.')
                except (ValueError, OSError) as e:
                    st.warning(str(e))
            if st.button('Подтвердить позицию', key=f"trade-confirm-{p['line']}", disabled=index < 0):
                selected = candidates[index]
                p['selected'] = selected
                p['status'] = ('out_of_stock' if selected.get('stock') is not None and selected['stock'] < p['qty']
                               else 'exact' if selected['match_type'] == 'exact' else 'analog')
                state.pop('pilot_id', None)
                state['version'] += 1
                st.rerun()
    customer = st.text_input('Заказчик для КП', key='trade-customer')
    include = st.checkbox('Цены каталога включают НДС', value=True, key='trade-vat-mode')
    st.caption('Пустые цена, остаток, НДС и срок означают «нет данных». Неподтверждённые кандидаты и нехватка остатка не включаются в сумму предложения. Черновик проверяет сотрудник.')
    left, right = st.columns(2)
    left.download_button('Подбор в Excel', export_xlsx(result, customer=customer, prices_include_vat=include),
                         file_name='podbor.xlsx', mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    right.download_button('Коммерческое предложение в Word', proposal_docx(result, customer=customer, prices_include_vat=include),
                          file_name='kommercheskoe_predlozhenie.docx')
    if state.get('demo'):
        st.caption('Демонстрационная заявка не учитывается в замерах пилота.')
        return
    with st.form('trade-finish'):
        rating = st.radio('Оценка результата сотрудником', RATINGS, index=None, horizontal=True)
        manual = st.number_input('Ручной замер на сопоставимой заявке, минут (необязательно)', min_value=0.0, value=None)
        comment = st.text_input('Комментарий к оценке')
        finished = st.form_submit_button('Завершить проверку и записать замер', disabled=bool(state.get('pilot_id')))
    if finished:
        if rating is None:
            st.warning('Выберите оценку результата.')
            return
        log = _pilot()
        request = '\n'.join(f"{p['query']} — {p['qty']}" for p in result)
        record = log.record(_user(), mask(request) if settings.log_mask_pii else request, len(result),
                            time.time() - state['started'], manual_seconds=None if manual is None else manual * 60)
        log.review(record, _user(), rating, comment=mask(comment) if settings.log_mask_pii else comment)
        state['pilot_id'] = record
        log_action('trade_review', _user(), record, rating)
        st.success('Оценка и полное время работы с агентом сохранены.')


def selection():
    state = _state()
    if ctx()['is_admin']:
        with st.expander('Готовые демо-кейсы без данных компании'):
            from kbagent.trade_demo import CASES, CATALOG_MAPPING, DEMO_ROOT
            case = st.selectbox('Сценарий', CASES, format_func=lambda k: {'agri': 'Сельхоззапчасти', 'auto': 'Автозапчасти', 'drawings': 'Текст чертежа и спецификация'}[k])
            st.caption('Вымышленные каталоги и применимость. Демо не подтверждает качество на реальных запчастях.')
            if st.button('Открыть демо-кейс'):
                folder = DEMO_ROOT / case
                catalog_name = 'ДЕМО / ' + case
                data = (folder / 'catalog.csv').read_bytes()
                _catalog().import_table('catalog.csv', data, CATALOG_MAPPING, catalog=catalog_name)
                trade_sources.archive(DATA, data)
                request = folder / ('drawing.pdf' if case == 'drawings' else 'request.csv')
                parsed = read_file(request.name, request.read_bytes())
                state.clear()
                fields = [{'file': request.name, **parsed['fields']}] if parsed['fields'] else []
                state.update(items=parsed['items'], started=time.time(), warnings=parsed['warnings'], fields=fields,
                             version=0, demo=True)
                st.session_state['trade-filter-catalog'] = catalog_name
                for field in ('brand', 'group'):
                    st.session_state[f'trade-filter-{field}'] = ''
                log_action('trade_import', _user(), catalog_name, 'Вымышленные демоданные')
    options = _catalog().options()
    cols = st.columns(3)
    filters = {key: cols[i].selectbox(title, [''] + options.get(key + 's', []), format_func=lambda v: v or 'Все',
                                    key='trade-filter-' + key)
               for i, (key, title) in enumerate([('catalog', 'Каталог'), ('brand', 'Бренд'), ('group', 'Группа товаров')])}
    with st.expander('Применимость и цена клиента'):
        a, b, c = st.columns(3)
        filters['model'] = a.text_input('Модель техники (только по данным каталога)')
        filters['year'] = b.number_input('Год выпуска', min_value=1900, max_value=2100, value=None)
        filters['vin'] = c.text_input('VIN (только явно указанный в каталоге)', max_chars=17)
        filters['price_level'] = a.text_input('Уровень прайса клиента')
        filters['discount'] = b.number_input('Согласованная скидка, %', min_value=0.0, max_value=100.0, value=0.0)
    st.caption('Перетащите несколько файлов в поле сообщения или вставьте список: одна позиция на строку, «артикул — количество шт.». После чтения проверьте список. Свободное письмо может требовать ручной правки.')
    sent = st.chat_input('Приложите заявку, спецификацию, фото или PDF-чертёж', accept_file='multiple', file_type=FILE_TYPES,
                         key='trade-chat')
    if sent:
        started = time.time()
        state.clear()
        state.update(started=started, version=0, raw_text=sent.text)
        try:
            try:
                items = text_items(sent.text) if sent.text.strip() else []
            except ValueError as e:
                if '200 символов' not in str(e):
                    raise
                items = []
            warnings, fields, structured = [], [], []
            for file in sent.files:
                parsed = read_file(file.name, file.getvalue())
                items.extend(parsed['items'])
                warnings.extend(parsed['warnings'])
                if any(parsed['fields'].values()):
                    fields.append({'file': file.name, **parsed['fields']})
                if not parsed['text'] or any(parsed['fields'].values()):
                    structured.extend(parsed['items'])
                else:
                    state['raw_text'] += '\n' + parsed['text']
            items = validate(items)
            if not items and not state['raw_text'].strip():
                raise ValueError('Не найдено позиций заявки')
            state.update(items=items, warnings=warnings, fields=fields, structured_items=structured,
                         demo=filters['catalog'].startswith('ДЕМО / '))
        except (ValueError, OSError) as e:
            st.error(str(e))
    if state.get('raw_text', '').strip():
        with st.expander('Текст письма / распознанного документа'):
            st.text(state['raw_text'][:30000])
            if st.button('Разобрать свободное письмо GigaChat'):
                try:
                    with st.spinner('GigaChat извлекает позиции из письма…'):
                        extracted = parse_letter_with_gigachat(state['raw_text'])
                        state['items'] = validate(state.get('structured_items', []) + extracted)
                    state.pop('result', None)
                    state.pop('pilot_id', None)
                    st.rerun()
                except (ValueError, RuntimeError) as e:
                    st.error(str(e))
    for warning in state.get('warnings', []):
        st.info(warning)
    if state.get('fields'):
        st.markdown('**Распознанная основная надпись — проверьте по чертежу**')
        st.dataframe(pd.DataFrame(state['fields']), hide_index=True)
    if state.get('items'):
        editor = st.data_editor(pd.DataFrame(state['items']), num_rows='dynamic', hide_index=True,
                                column_config={'query': 'Артикул / описание', 'qty': st.column_config.NumberColumn('Количество', min_value=0.001),
                                               'warning': 'Проверьте', 'source': 'Источник заявки', 'source_row': 'Строка'},
                                disabled=['line', 'source', 'source_row', 'warning'], key=f"trade-editor-{hashlib.sha256(str(state['items']).encode()).hexdigest()[:12]}")
        items = editor.fillna('').to_dict('records')
        criteria = (items, filters)
        if st.button('Подобрать все позиции', type='primary'):
            try:
                start = time.perf_counter()
                before = _stamp()
                result = match_batch(_catalog(), items, **filters)
                after = _stamp()
                if before != after:
                    state.pop('result', None)
                    raise ValueError('Каталог обновился во время подбора. Повторите запрос, чтобы все строки использовали актуальные данные.')
                state['result'] = result
                state['search_seconds'] = time.perf_counter() - start
                state['catalog_stamp'] = after
                state['criteria'] = criteria
                state['demo'] = filters['catalog'].startswith('ДЕМО / ') or any(
                    c.get('catalog', '').startswith('ДЕМО / ') for row in result for c in row.get('candidates', []))
                state['version'] += 1
                state.pop('pilot_id', None)
            except ValueError as e:
                st.error(str(e))
        if state.get('result') and state.get('criteria', criteria) != criteria:
            st.info('Заявка или фильтры изменились. Повторите подбор перед выгрузкой.')
            return
    _results(state)


def pilot():
    log = _pilot()
    totals = log.summary(_user(), is_admin=ctx()['is_admin'])
    a, b, c = st.columns(3)
    a.metric('Заявок в замере', totals['count'])
    b.metric('Оценено сотрудниками', totals['reviewed_count'])
    c.metric('Сокращение полного времени', 'Нет парных замеров' if totals['time_reduction_pct'] is None else f"{totals['time_reduction_pct']:.1f} %")
    st.caption('Сравнение только по заявкам с ручным замером; время с агентом включает проверку и выбор сотрудником. Это наблюдения пилота, а не автоматически доказанная экономия.')
    records = log.rows(_user(), is_admin=ctx()['is_admin'])
    if records:
        st.dataframe(pd.DataFrame(records).drop(columns=['request']), hide_index=True)
        st.download_button('Статистика пилота в CSV', log.export_csv(_user(), is_admin=ctx()['is_admin']), file_name='trade_pilot.csv')


def trade_page():
    inject_css()
    if ctx()['role'] not in settings.tkp_roles:
        st.error('Торговый подбор доступен отделу продаж и администратору.')
        return
    st.title('Подбор товаров и запчастей')
    tabs = st.tabs(['Заявка → подбор', 'Каталоги и прайсы', 'Замеры пилота'])
    for tab, show in zip(tabs, (selection, import_catalog, pilot)):
        with tab:
            show()

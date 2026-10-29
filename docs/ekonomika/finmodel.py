"""Финансовая модель проекта: допущения, экономика клиента, эффект для заказчика, план на три года, чувствительность.

Один источник цифр для книги Excel и документа «Экономика проекта» (build_finmodel.py) и автотеста: формулы Excel
повторяют функции этого модуля, тест пересчитывает книгу и сверяет результаты. У каждого допущения — статус:
«факт» (замерено продуктом, закон, реестр), «рынок» (внешний источник со ссылкой), «оценка» (проверить на пилоте).

Принципы:
- труд команды — в расходах на оплату труда; внедрение и поддержку несут сотрудники внедрения (себестоимость),
  разработку, продажи и управление — остальные (операционные расходы); двойного счёта труда нет;
- экономика одного клиента — маржинальная: что добавляет к прибыли ещё один клиент при той же команде;
- оплаченный пилот засчитывается во внедрение и отдельной строкой выручки не считается;
- подписка начинается с месяца подписания: в первый год — в среднем половина годовой;
- суммы — без НДС: УСН, выручка ниже порога НДС 20 млн ₽ (ст. 145 НК РФ в ред. 2026), а лицензии на ПО из
  реестра российского ПО от НДС освобождены (пп. 26 п. 2 ст. 149 НК РФ).
"""
from dataclasses import dataclass, field

YEARS = [2027, 2028, 2029]
SCENARIOS = ["консервативный", "базовый", "оптимистичный"]


@dataclass
class Item:
    key: str
    label: str
    value: object               # число; по годам — список из 3; по сценариям — словарь
    unit: str = ""
    status: str = "оценка"      # факт | рынок | оценка
    source: str = ""


GROUPS: dict[str, list[Item]] = {
    "Цены": [
        Item("impl_price", "Внедрение (разово)", 300_000, "₽", "оценка",
             "паспорт, п. 9: 150–500 тыс. ₽; у интеграторов RAG-ассистент — 350–800 тыс. ₽ (wehaveidea.ru)"),
        Item("sub_dept", "Подписка «Отдел» (до 50 пользователей)", 300_000, "₽ в год", "оценка", "паспорт, п. 9"),
        Item("sub_ent", "Подписка «Предприятие» (до 200 пользователей)", 600_000, "₽ в год", "оценка", "паспорт, п. 9"),
        Item("ent_share", "Доля клиентов на «Предприятии»", 0.3, "доля", "оценка", "проверить на продажах"),
        Item("module_tkp", "Модуль «ТКП и продажи» (реестр ТКП, бланк, почта, аналитика спроса)", 120_000, "₽ в год",
             "оценка", "паспорт: модули — от 100 тыс. ₽ в год"),
        Item("module_tkp_share", "Доля клиентов с модулем «ТКП и продажи»", 0.6, "доля", "оценка", ""),
        Item("module_1c", "Модуль «1С» (цены и остатки, учёт только на чтение)", 100_000, "₽ в год", "оценка", ""),
        Item("module_1c_share", "Доля клиентов с модулем «1С»", 0.3, "доля", "оценка", ""),
        Item("first_year_share", "Доля годовой подписки в год подписания", 0.5, "доля", "оценка",
             "подписание в среднем в середине года"),
        Item("price_index", "Индексация цен в год", 0.05, "доля", "оценка", "ниже инфляции — консервативно"),
        Item("services_price", "Услуги при внедрении: наполнение справочника параметров, интеграции, обучение",
             100_000, "₽ на клиента", "оценка", "слайд «Бизнес-модель»: настройка интеграций, новые источники, обучение"),
        Item("services_share", "Доля новых клиентов, покупающих услуги", 0.6, "доля", "оценка", ""),
        Item("services_days", "Работа по услугам", 5, "чел.-дн.", "оценка", "справочник 3–10 дн. (trudoemkost)"),
    ],
    "Клиент: затраты": [
        Item("impl_days", "Внедрение, чел.-дней", 9, "чел.-дн.", "оценка",
             "docs/ekonomika/trudoemkost.md: 7–11 чел.-дн."),
        Item("first_factor", "Коэффициент для клиентов первого года (процесс не отработан)", 1.4, "×", "оценка",
             "trudoemkost.md: +30–50 %"),
        Item("support_hours", "Сопровождение, ч в месяц", 4, "ч", "оценка", "trudoemkost.md: 2–8 ч"),
        Item("travel", "Выезды на одно внедрение", 20_000, "₽", "оценка", "концепция, 6.2: 10–30 тыс. ₽"),
        Item("presale_days", "Предпродажа на нового клиента (показ, встречи, ТКП)", 5, "чел.-дн.", "оценка", ""),
        Item("pilot_days", "Пилот на нового клиента", 10, "чел.-дн.", "оценка", "пилот 1–2 месяца, замер «до / после»"),
        Item("channel_share", "Доля клиентов через интеграторов", 0.5, "доля", "оценка",
             "партнёр — интегратор, канал к его клиентам"),
        Item("commission", "Комиссия интегратора с подписки", 0.25, "доля", "оценка", "концепция: 20–30 %"),
        Item("churn", "Отток подписок в год", {"консервативный": 0.15, "базовый": 0.10, "оптимистичный": 0.05},
             "доля", "оценка", ""),
    ],
    "Продажи": [
        Item("new_clients", "Новых клиентов в год",
             {"консервативный": [1, 3, 7], "базовый": [2, 5, 12], "оптимистичный": [3, 8, 18]}, "клиентов",
             "оценка", "базовый — план паспорта, п. 24: 2 + 5 + 12"),
        Item("pilots", "Пилотов в год (засчитываются во внедрение)",
             {"консервативный": [1, 2, 3], "базовый": [1, 3, 6], "оптимистичный": [2, 4, 8]}, "пилотов", "оценка",
             "половина пилотов переходит во внедрение"),
    ],
    "Команда": [
        Item("fte_dev", "Разработка и качество, ставок", [1.0, 1.25, 2.5], "ставок", "оценка", ""),
        Item("fte_impl", "Внедрение и поддержка, ставок", [0.25, 0.75, 1.5], "ставок", "оценка",
             "в 2027 г. внедряет вся команда: разработчики помогают"),
        Item("fte_sales", "Продажи и партнёры, ставок", [0.25, 0.5, 1.0], "ставок", "оценка", ""),
        Item("fte_admin", "Экономика, договоры, отчётность, ставок", [0.25, 0.5, 0.75], "ставок", "оценка", ""),
        Item("team_scale", "Штат 2028–2029 по сценарию (найм — вслед за клиентами)",
             {"консервативный": 0.7, "базовый": 1.0, "оптимистичный": 1.3}, "×", "оценка", ""),
        Item("sal_dev", "Оклад разработки", [60_000, 85_000, 100_000], "₽ в месяц", "рынок",
             "Рязань: средняя зарплата 80 тыс. ₽ (март 2026, Росстат), инженеры — 81–113 тыс. ₽ (вакансии)"),
        Item("sal_impl", "Оклад внедрения", [60_000, 70_000, 80_000], "₽ в месяц", "рынок", ""),
        Item("sal_sales", "Оклад продаж", [50_000, 65_000, 75_000], "₽ в месяц", "рынок", ""),
        Item("sal_admin", "Оклад экономики и договоров", [50_000, 55_000, 65_000], "₽ в месяц", "рынок", ""),
        Item("contrib", "Страховые взносы", [0.30, 0.15, 0.15], "доля", "факт",
             "30 % — до аккредитации; ИТ-аккредитация с 2026 г.: 15 % в пределах базы (ст. 427 НК РФ)"),
        Item("work_days", "Рабочих дней в году", 247, "дней", "факт", "производственный календарь"),
    ],
    "Прочие расходы": [
        Item("opex_accounting", "Бухгалтерия, банк", [60_000, 120_000, 150_000], "₽ в год", "оценка",
             "концепция, 6.2: 60–150 тыс. ₽"),
        Item("opex_legal", "Юрист, договоры, регистрация ПО (пошлина 5 000 ₽)", [35_000, 50_000, 80_000], "₽ в год",
             "оценка", "госпошлина — 389-ФЗ"),
        Item("opex_hardware", "Оборудование (ПК с видеокартой для показа и разработки)", [250_000, 200_000, 300_000],
             "₽ в год", "рынок", "ПК с видеокартой 12 ГБ — 150–250 тыс. ₽"),
        Item("opex_marketing", "Продвижение: выставки, поездки, материалы", [100_000, 300_000, 500_000], "₽ в год",
             "оценка", "концепция: 100–300 тыс. ₽"),
        Item("opex_office", "Офис (до 2029 — площадка акселератора и РГРТУ)", [0, 0, 360_000], "₽ в год", "оценка", ""),
        Item("opex_agpl", "PyMuPDF (AGPL): замена силами команды (8 чел.-дн. — в оплате труда)", [0, 0, 0], "₽",
             "оценка", "trudoemkost 2.1; коммерческая лицензия — риск, см. документ"),
        Item("opex_reserve", "Непредвиденные расходы", 0.10, "доля прочих", "оценка", ""),
    ],
    "Налоги и гранты": [
        Item("usn_rate", "УСН «доходы»", 0.06, "доля", "факт",
             "выручка ниже 20 млн ₽ — без НДС (ст. 145 НК РФ, порог 2026–2028)"),
        Item("grant", "Гранты", {"консервативный": [0, 0, 0], "базовый": [1_000_000, 0, 0],
                                 "оптимистичный": [1_000_000, 500_000, 0]}, "₽", "рынок",
             "ФСИ «Студенческий стартап» — 1 млн ₽; «УМНИК» — 500 тыс. ₽"),
    ],
}

# Эффект для заказчика: отдел техпродаж и сервиса (пилот) и предприятие целиком (пример паспорта)
CUSTOMER = {
    "отдел": {
        "label": "Отдел техпродаж и сервиса — как в пилоте",
        "people": 8, "answers_per_day": 5, "minutes_answer": 15, "saved_answer": 0.5,
        "tkp_per_month": 60, "minutes_tkp": 60, "saved_tkp": 0.5,
        "salary": 80_000, "subscription": 300_000, "modules": 120_000, "implementation": 300_000,
        "hardware": 200_000, "admin_hours": 3,
    },
    "предприятие": {
        "label": "Предприятие: 80 сотрудников работают с документами — пример паспорта",
        "people": 80, "answers_per_day": 0, "minutes_answer": 0, "saved_answer": 0,
        "search_minutes_day": 40, "saved_search": 0.3,
        "tkp_per_month": 60, "minutes_tkp": 60, "saved_tkp": 0.5,
        "salary": 80_000, "subscription": 600_000, "modules": 220_000, "implementation": 450_000,
        "hardware": 200_000, "admin_hours": 6,
    },
}
CUSTOMER_NOTES = {
    "minutes_answer": "оценка 15 мин — заменить хронометражем у партнёра (протокол docs/pilot/zamer_i_stoimost.md)",
    "saved_answer": "цель из карточки партнёра — минус 50 %; на пилоте замеряет вкладка «Эффект»",
    "minutes_tkp": "оценка 60 мин на ТКП — заменить замером у партнёра",
    "salary": "Рязанская область: 80 тыс. ₽ в месяц (Росстат, март 2026) — ниже, чем у инженеров (81–113 тыс. ₽)",
}
HOURS_MONTH = 165                    # норма часов в месяц
POWER_MONTH = 450                    # электроэнергия ПК, ₽ в месяц (docs/pilot/zamer_i_stoimost.md)


def values() -> dict:
    return {it.key: it.value for items in GROUPS.values() for it in items}


def pick(v, scenario: str):
    return v[scenario] if isinstance(v, dict) else v


# ---------- расчёт ----------

def arpu(a: dict, year_index: int) -> float:
    """Годовая выручка с клиента по подписке и модулям, с индексацией."""
    sub = a["sub_dept"] * (1 - a["ent_share"]) + a["sub_ent"] * a["ent_share"]
    modules = a["module_tkp"] * a["module_tkp_share"] + a["module_1c"] * a["module_1c_share"]
    return (sub + modules) * (1 + a["price_index"]) ** year_index


def day_cost(a: dict, year_index: int, role: str = "impl") -> float:
    return a[f"sal_{role}"][year_index] * 12 * (1 + a["contrib"][year_index]) / a["work_days"]


def plan(scenario: str = "базовый", a: dict | None = None) -> list[dict]:
    """План на 2027–2029 по годам: клиенты, выручка, расходы, EBITDA, налог, гранты, деньги."""
    a = a or values()
    churn = pick(a["churn"], scenario)
    new_list, grants = pick(a["new_clients"], scenario), pick(a["grant"], scenario)
    rows, active, cash = [], 0.0, 0.0
    for y, year in enumerate(YEARS):
        new = new_list[y]
        kept = active * (1 - churn)
        price = arpu(a, y)
        subscription = kept * price + new * price * a["first_year_share"]
        implementation = new * a["impl_price"] * (1 + a["price_index"]) ** y
        services = new * a["services_share"] * a["services_price"] * (1 + a["price_index"]) ** y
        revenue = subscription + implementation + services
        commission = subscription * a["channel_share"] * a["commission"]
        travel = new * a["travel"]

        scale = pick(a["team_scale"], scenario) if y > 0 else 1.0

        def payroll(role):
            return a[f"fte_{role}"][y] * scale * a[f"sal_{role}"][y] * 12 * (1 + a["contrib"][y])
        delivery_payroll = payroll("impl")
        cogs = commission + travel + delivery_payroll
        other = sum(a[k][y] for k in ("opex_accounting", "opex_legal", "opex_hardware", "opex_marketing",
                                      "opex_office", "opex_agpl"))
        other *= 1 + a["opex_reserve"]
        opex = payroll("dev") + payroll("sales") + payroll("admin") + other
        ebitda = revenue - cogs - opex
        contributions = sum(payroll(r) for r in ("dev", "impl", "sales", "admin")) * a["contrib"][y] / (1 + a["contrib"][y])
        tax = max(revenue * a["usn_rate"] - contributions, revenue * a["usn_rate"] / 2)   # вычет взносов — до 50 %
        cash += ebitda - tax + grants[y]
        active = kept + new
        # загрузка внедрения: нужные чел.-дни против ставок внедрения (80 % времени на клиентов)
        need_days = (new * a["impl_days"] * (a["first_factor"] if y == 0 else 1) + new * a["services_share"]
                     * a["services_days"] + active * a["support_hours"] * 12 / 8)
        capacity = a["fte_impl"][y] * scale * a["work_days"] * 0.8
        rows.append({"year": year, "new": new, "active": active, "revenue": revenue, "subscription": subscription,
                     "implementation": implementation, "services": services, "cogs": cogs, "gross": revenue - cogs, "opex": opex,
                     "ebitda": ebitda, "tax": tax, "grant": grants[y], "net": ebitda - tax + grants[y], "cash": cash,
                     "load": need_days / capacity if capacity else 0.0, "payroll": sum(payroll(r) for r in
                                                                                       ("dev", "impl", "sales", "admin"))})
    return rows


def unit(a: dict | None = None, scenario: str = "базовый") -> dict:
    """Экономика одного клиента (маржинальная, цены и ставки 2028 года — первого полного года)."""
    a = a or values()
    y = 1
    price, dc = arpu(a, y), day_cost(a, y)
    churn = pick(a["churn"], scenario)
    commission = a["channel_share"] * a["commission"]
    index = (1 + a["price_index"]) ** y
    rev1 = a["impl_price"] * index + price * a["first_year_share"] + a["services_share"] * a["services_price"] * index
    support = a["support_hours"] * 12 / 8 * dc
    cost1 = ((a["impl_days"] + a["services_share"] * a["services_days"]) * dc + a["travel"]
             + support * a["first_year_share"] + price * a["first_year_share"] * commission)
    margin1 = rev1 - cost1
    margin2 = price - support - price * commission
    marketing = a["opex_marketing"][y] / max(pick(a["new_clients"], scenario)[y], 1)
    cac = (a["presale_days"] + a["pilot_days"]) * dc + 2 * a["travel"] + marketing
    ltv = margin1 + sum(margin2 * (1 - churn) ** k for k in range(1, 5))          # 5 лет
    return {"arpu": price, "day_cost": dc, "rev1": rev1, "cost1": cost1, "margin1": margin1,
            "margin1_share": margin1 / rev1, "margin2": margin2, "margin2_share": margin2 / price, "cac": cac,
            "ltv": ltv, "ltv_cac": ltv / cac, "payback_months": cac / (margin2 / 12)}


def customer(name: str) -> dict:
    """Эффект для заказчика: часы и рубли в год, затраты, окупаемость, стоимость запроса."""
    c = CUSTOMER[name]
    hour = c["salary"] * 1.3 / HOURS_MONTH                              # час сотрудника с взносами
    days = 247
    answers_year = c["people"] * c["answers_per_day"] * days
    hours = (answers_year * c["minutes_answer"] * c["saved_answer"]
             + c["tkp_per_month"] * 12 * c["minutes_tkp"] * c["saved_tkp"]
             + c["people"] * c.get("search_minutes_day", 0) * c.get("saved_search", 0) * days) / 60
    effect = hours * hour
    yearly = c["subscription"] + c["modules"] + c["admin_hours"] * 12 * hour + POWER_MONTH * 12
    first = yearly + c["implementation"] + c["hardware"]
    requests = answers_year + c["tkp_per_month"] * 12
    saved_per_request = ((c["minutes_answer"] * c["saved_answer"] * answers_year
                          + c["minutes_tkp"] * c["saved_tkp"] * c["tkp_per_month"] * 12) / requests / 60 * hour
                         if requests else 0)
    return {"label": c["label"], "hour": hour, "hours": hours, "effect": effect, "first": first, "yearly": yearly,
            "payback_months": first / (effect / 12), "roi3": (3 * effect - first - 2 * yearly) / (first + 2 * yearly),
            "price_share": (c["subscription"] + c["modules"]) / effect, "requests": requests,
            "cost_per_request": yearly / requests if requests else 0, "saved_per_request": saved_per_request}


def sensitivity(scenario: str = "базовый") -> list[tuple[str, float, float, float]]:
    """Насколько меняется деньги на конец 2029 г. (накопленно) при изменении одного допущения."""
    base = plan(scenario)[-1]["cash"]
    cases = [("Новых клиентов ±30 %", "new_clients", 0.3), ("Подписка и модули ±20 %", "subscription", 0.2),
             ("Цена внедрения ±30 %", "impl_price", 0.3), ("Оплата труда ±20 %", "salary", 0.2),
             ("Отток 5 % / 20 %", "churn", None), ("Доля через интеграторов 25 % / 75 %", "channel_share", None)]
    out = []
    for label, key, d in cases:
        res = []
        for sign in (-1, 1):
            a = values()
            if key == "new_clients":
                a["new_clients"] = {s: [round(n * (1 + sign * d)) for n in v] for s, v in a["new_clients"].items()}
            elif key == "subscription":
                for k in ("sub_dept", "sub_ent", "module_tkp", "module_1c"):
                    a[k] *= 1 + sign * d
            elif key == "impl_price":
                a["impl_price"] *= 1 + sign * d
            elif key == "salary":
                for k in ("sal_dev", "sal_impl", "sal_sales", "sal_admin"):
                    a[k] = [s * (1 + sign * d) for s in a[k]]
            elif key == "churn":
                a["churn"] = {s: 0.05 if sign < 0 else 0.20 for s in a["churn"]}
            elif key == "channel_share":
                a["channel_share"] = 0.25 if sign < 0 else 0.75
            res.append(plan(scenario, a)[-1]["cash"])
        out.append((label, res[0], base, res[1]))
    return out


def breakeven_clients(a: dict | None = None) -> float:
    """Сколько активных клиентов покрывают постоянные расходы 2029 г. при марже подписки 2029 г."""
    a = a or values()
    rows = plan("базовый", a)
    y = 2
    price = arpu(a, y)
    margin = price * (1 - a["channel_share"] * a["commission"])
    fixed = rows[y]["opex"] + a["fte_impl"][y] * a["sal_impl"][y] * 12 * (1 + a["contrib"][y])
    return fixed / margin


# созданный продукт — затратный подход (нормативная трудоёмкость сделанного, чел.-дн.)
BUILT = [
    ("Прототип TRL 4 (03–14.10.2026): ядро, поиск, ответы с проверками, учёт, справочник, ТКП, интерфейс, поставка",
     167),
    ("Цены из прайса, ключ к моделям, 20 писем эталона и исправления разбора", 8),
    ("Выбор прибора менеджером, НДС, REST API /tkp", 6),
    ("Папка документов отдела и безопасная запись индекса двумя процессами", 7),
    ("Резервная копия и перенос в новую версию", 4),
    ("Реестр ТКП: номер, бланк компании, Excel, статусы, напоминания, редакции", 10),
    ("Почта отдела → входящие запросы", 6),
    ("Аналитика спроса, поиск по обозначениям, цены и остатки из 1С, отчёт о пилоте", 14),
    ("Проверка ветки, автотесты на Windows и Python 3.11, защита установки, пакет партнёру, регистрация ПО", 8),
]
RATES = {"ставки команды (концепция: 60 тыс. ₽ в месяц)": 60_000 * 1.3 / 20.6,
         "рыночная ставка разработчика в Рязани (≈ 120 тыс. ₽ в месяц)": 120_000 * 1.3 / 20.6}

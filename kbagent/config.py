"""Настройки агента. Любое значение можно переопределить переменной окружения."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return type(default)(value) if not isinstance(default, bool) else value.lower() in ("1", "true", "yes")


@dataclass
class Settings:
    # OpenAI-совместимые серверы (llama.cpp llama-server или Ollama)
    llm_url: str = _env("KB_LLM_URL", "http://127.0.0.1:8080/v1")
    llm_model: str = _env("KB_LLM_MODEL", "GigaChat3.1-10B-A1.8B")
    embed_url: str = _env("KB_EMBED_URL", "http://127.0.0.1:8081/v1")
    rerank_url: str = _env("KB_RERANK_URL", "http://127.0.0.1:8082/v1")
    use_reranker: bool = _env("KB_USE_RERANKER", True)
    # Ключ доступа к серверам моделей: создаёт scripts/start_servers при первом запуске и передаёт серверам
    # файлом; без ключа серверы отвечают 401. KB_MODEL_KEY — ключ внешнего сервера моделей предприятия.
    model_key_path: Path = ROOT / "data" / "model_api_key.txt"

    # данные
    raw_dir: Path = ROOT / "data" / "raw"
    index_dir: Path = ROOT / "data" / "index"
    db_path: Path = ROOT / "data" / "demo_db" / "uchet.sqlite"
    db_schema: Path = ROOT / "data" / "demo_db" / "schema.yaml"   # слой понятных названий базы
    users_path: Path = ROOT / "data" / "users.yaml"                # учётные записи (хэши паролей)
    tessdata_dir: Path = ROOT / "tools" / "tessdata"               # языки распознавания сканов
    log_dir: Path = ROOT / "logs"
    # вход по логину и паролю; KB_AUTH=0 — выбор роли из списка (для отладки)
    auth_required: bool = _env("KB_AUTH", True)

    # поиск
    candidates: int = _env("KB_CANDIDATES", 30)      # из каждого поиска (слова и смысл)
    context_chunks: int = _env("KB_CONTEXT_CHUNKS", 5)  # фрагментов в контекст модели
    # Порог отказа: если лучший фрагмент оценён переранжировщиком ниже порога,
    # агент отвечает «данных нет», не вызывая модель. Подбирается на эталонном наборе.
    rerank_threshold: float = _env("KB_RERANK_THRESHOLD", 0.03)
    # Ниже этой оценки — «зона сомнения»: найденное похоже на вопрос, но может быть
    # о другом предмете. Такой ответ дополнительно проверяется моделью (answer.verify).
    confident_threshold: float = _env("KB_CONFIDENT_THRESHOLD", 0.3)
    # Если найден фрагмент сильнее этой оценки, а ответ опирается только на более слабые — ответ «не о том»:
    # модель обошла найденное и взяла похожее словами (порядок поверки самого осциллографа на вопрос
    # «как отправить СИ на поверку»: сильнейший 0,26 не процитирован, процитирован 0,035). У верных ответов
    # эталона сильнейший процитированный фрагмент — от 0,3.
    cited_threshold: float = _env("KB_CITED_THRESHOLD", 0.1)
    # Запасной порог по косинусной близости, если переранжировщик выключен.
    dense_threshold: float = _env("KB_DENSE_THRESHOLD", 0.50)

    # генерация
    max_answer_tokens: int = _env("KB_MAX_TOKENS", 500)
    # одинаковый ответ на одинаковый вопрос (без кэша подсказок); на процессоре KB_DETERMINISTIC=0 быстрее
    deterministic: bool = _env("KB_DETERMINISTIC", True)
    # Персональные данные в журнале вопросов и оценках: маскировать (телефоны, почта, паспорт, СНИЛС, ФИО полностью)
    log_mask_pii: bool = _env("KB_LOG_MASK_PII", True)
    # Сколько дней хранить журнал вопросов (0 — без ограничения); журнал действий не чистится
    log_retention_days: int = _env("KB_LOG_RETENTION_DAYS", 180)
    request_timeout: int = _env("KB_TIMEOUT", 300)
    # НДС в итоге ТКП: ставка, % (0 — без НДС) и включён ли он в цены прайса. С 01.01.2026 общая ставка — 22 %;
    # ставку и режим налогообложения продавца сверить с бухгалтерией
    vat_rate: float = _env("KB_VAT_RATE", 22.0)
    prices_include_vat: bool = _env("KB_PRICES_WITH_VAT", True)

    # Роли и доступные им группы документов (разграничение прав, демо)
    roles: dict = field(default_factory=lambda: {
        "Инженер производства": {"all", "production"},
        "Метролог": {"all", "production", "metrology"},
        "Техпродажи и сервис": {"all"},
        "Администратор": {"all", "production", "metrology", "admin"},
    })

    @property
    def model_api_key(self) -> str:
        key = os.environ.get("KB_MODEL_KEY")
        if key is None and self.model_key_path.exists():
            key = self.model_key_path.read_text(encoding="utf-8")
        return (key or "").strip()


settings = Settings()

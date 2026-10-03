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

    # данные
    raw_dir: Path = ROOT / "data" / "raw"
    index_dir: Path = ROOT / "data" / "index"
    db_path: Path = ROOT / "data" / "demo_db" / "uchet.sqlite"
    log_dir: Path = ROOT / "logs"

    # поиск
    candidates: int = _env("KB_CANDIDATES", 30)      # из каждого поиска (слова и смысл)
    context_chunks: int = _env("KB_CONTEXT_CHUNKS", 5)  # фрагментов в контекст модели
    # Порог отказа: если лучший фрагмент оценён переранжировщиком ниже порога,
    # агент отвечает «данных нет», не вызывая модель. Подбирается на эталонном наборе.
    rerank_threshold: float = _env("KB_RERANK_THRESHOLD", 0.12)
    # Запасной порог по косинусной близости, если переранжировщик выключен.
    dense_threshold: float = _env("KB_DENSE_THRESHOLD", 0.50)

    # генерация
    max_answer_tokens: int = _env("KB_MAX_TOKENS", 500)
    request_timeout: int = _env("KB_TIMEOUT", 300)

    # Роли и доступные им группы документов (разграничение прав, демо)
    roles: dict = field(default_factory=lambda: {
        "Инженер производства": {"all", "production"},
        "Метролог": {"all", "production", "metrology"},
        "Администратор": {"all", "production", "metrology", "admin"},
    })


settings = Settings()

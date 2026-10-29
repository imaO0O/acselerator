"""Голосовой вопрос: речь распознаётся на этом компьютере, без интернета. Руки заняты, перчатки — вопрос голосом.

Модель — GigaAM v3 e2e (Сбер, лицензия MIT; вместе с GigaChat вся связка отечественная) в ONNX через onnx-asr:
сразу ставит знаки препинания и пишет числа цифрами, работает на процессоре. Файлы — models/asr/ (около 215 МБ,
scripts/setup.py --voice). Распознанный текст показывается как вопрос сотрудника — видно, как агент его понял.
Единицы измерения словами приводятся к обозначениям справочника («150 мегагерц» → «150 МГц»), иначе подбор по
параметрам их не узнаёт. Запись не сохраняется — только текст вопроса, как при вводе с клавиатуры.
"""
import io
import re
import wave
from functools import lru_cache
from pathlib import Path

from .config import ROOT

MODEL = "gigaam-v3-e2e-ctc"
DIR = ROOT / "models" / "asr" / MODEL
REPO = "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/main"
FILES = ["config.json", "v3_e2e_ctc.int8.onnx", "v3_e2e_ctc_vocab.txt", "LICENSE.txt"]
RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000)    # другие частоты модель приводит к 16 кГц сама
MAX_SECONDS = 30                         # дольше — модель без разбиения на фразы ошибается; вопрос короче
NUM = r"(\d+(?:[.,]\d+)?)"
UNITS = [                                # после числа: слово → обозначение, как в справочнике параметров
    (r"гигагерц\w*", "ГГц"), (r"мегагерц\w*", "МГц"), (r"килогерц\w*", "кГц"), (r"герц\w*", "Гц"),
    (r"гигасэмпл\w*|гигавыбор\w*", "Гвыб/с"), (r"мегасэмпл\w*|мегавыбор\w*", "Мвыб/с"),
    (r"киловольт\w*", "кВ"), (r"милливольт\w*", "мВ"), (r"вольт\w*", "В"),
    (r"миллиампер\w*", "мА"), (r"микроампер\w*", "мкА"), (r"ампер\w*", "А"),
    (r"мегаом\w*", "МОм"), (r"килоом\w*", "кОм"), (r"ом(?:а|ов)?", "Ом"),
    (r"киловатт\w*", "кВт"), (r"ватт\w*", "Вт"),
    (r"градус\w*(?:\s+цельси\w*)?", "°C"), (r"миллиметр\w*", "мм"), (r"сантиметр\w*", "см"),
]
UNIT_RES = [(re.compile(rf"{NUM}\s*(?:{words})(?![а-яё])", re.I), sign) for words, sign in UNITS]
# обозначение модели, сказанное голосом («мас 830 эл» → «МАС 830Л»), сверяется с моделями справочника по звучанию
TRANSLIT = str.maketrans({"А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "E", "Ж": "ZH", "З": "Z",
                          "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M", "Н": "N", "О": "O", "П": "P", "Р": "R",
                          "С": "S", "Т": "T", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "CH", "Ш": "SH",
                          "Щ": "SH", "Ы": "Y", "Ь": "", "Ъ": "", "Э": "E", "Ю": "YU", "Я": "YA"})


class VoiceError(Exception):
    """Запись не разобрать — фразой для сотрудника."""


def available() -> bool:
    if not all((DIR / f).exists() for f in FILES[:3]):
        return False
    try:
        import onnx_asr  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _model():
    import onnx_asr
    return onnx_asr.load_model(MODEL, DIR, quantization="int8", providers=["CPUExecutionProvider"])


def read_wav(data: bytes):
    """WAV из браузера → моно float32 и частота. Стерео сводится в моно."""
    import numpy as np
    try:
        with wave.open(io.BytesIO(data)) as w:
            rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise VoiceError("запись не читается — запишите вопрос ещё раз") from e
    if width != 2:
        raise VoiceError("неподдерживаемый формат записи — нужен WAV 16 бит")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def _key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper().translate(TRANSLIT))


def _close(a: str, b: str) -> bool:
    """Одна буква пропущена, лишняя или другая, цифры те же: «MS830L» ≈ «MAS830L», но «DT9208A» ≠ «DT9205A»."""
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b) or min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, longer = sorted((a, b), key=len)
    return any(longer[:i] + longer[i + 1:] == short for i in range(len(longer)))


def _match(key: str, keys: dict[str, str]) -> str | None:
    if key in keys:
        return keys[key]
    near = [m for k, m in keys.items() if _close(key, k)]
    return near[0] if len(near) == 1 else None          # похоже на две модели — не угадываем


def models_by_sound(text: str, models) -> str:
    """Отрезок из 1–4 слов, который по звучанию — модель справочника, заменяется её обозначением."""
    keys: dict[str, str] = {}
    for m in models:
        if re.search(r"\d", m) and len(k := _key(m)) >= 4:
            keys.setdefault(k, m)
    words = text.split(" ")
    i, out = 0, []
    while keys and i < len(words):
        found = None
        # предлог («у», «и», «в») обозначение не начинает: «у акип 4115 1В» — «у» остаётся
        if not re.fullmatch(r"[а-яё][.,]?", words[i], re.I):
            spans = [(n, " ".join(words[i:i + n])) for n in range(min(4, len(words) - i), 0, -1)]
            for exact in (True, False):           # сначала точное совпадение, потом — с одной буквой
                for n, span in spans:
                    tail = re.search(r"[.,;:!?»)]*$", span).group(0)
                    key = _key(span[:len(span) - len(tail)])
                    if len(key) >= 4 and re.search(r"\d", key) and (
                            model := keys.get(key) if exact else _match(key, keys)):
                        found = (n, model + tail)
                        break
                if found:
                    break
        if found:
            out.append(found[1])
            i += found[0]
        else:
            out.append(words[i])
            i += 1
    return " ".join(out) if keys else text


def normalize(text: str, models=()) -> str:
    """Текст распознавания → вопрос: слитные слова разделены, единицы — обозначениями, модели — как в справочнике."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([а-яё])([A-ZА-ЯЁ])", r"\1 \2", text)          # «мультиметромMAS830L» → два слова
    for rx, sign in UNIT_RES:
        text = rx.sub(lambda m: f"{m.group(1)} {sign}", text)
    text = models_by_sound(text, models)
    return text[:1].upper() + text[1:]


def transcribe(data: bytes, models=()) -> str:
    """models — обозначения из справочника параметров: сказанное голосом обозначение приводится к ним."""
    samples, rate = read_wav(data)
    seconds = len(samples) / rate if rate else 0
    if seconds < 0.3 or float(abs(samples).max(initial=0)) < 0.01:
        raise VoiceError("запись пустая — проверьте микрофон и скажите вопрос ещё раз")
    if seconds > MAX_SECONDS:
        raise VoiceError(f"запись длиннее {MAX_SECONDS} секунд — задайте вопрос короче или напишите его")
    if rate not in RATES:
        raise VoiceError("частота записи не поддерживается — обновите страницу")
    return normalize(str(_model().recognize(samples, sample_rate=rate)), models)


def setup_files() -> list[tuple[str, Path]]:
    """Что скачать для распознавания речи (scripts/setup.py --voice)."""
    return [(f"{REPO}/{name}", DIR / name) for name in FILES]

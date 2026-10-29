"""Голосовой вопрос: чтение записи, приведение распознанного текста к языку справочника, отказ на пустой записи."""
import io
import wave

import numpy as np
import pytest

from kbagent import spravochnik, voice

MODELS = {"MAS830L", "DT9205A", "DT9208A", "АКИП-4115/1В", "АКИП-4122/1(V)", "АКИП-4122/10(V)", "SBK936B",
          "Lukey 852D+"}


def _wav(samples: np.ndarray, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes((samples * 32767).astype("<i2").tobytes())
        else:
            w.writeframes(((samples + 1) * 127).astype("u1").tobytes())
    return buf.getvalue()


def test_units_and_glued_words():
    assert voice.normalize("подбери осциллограф от 150 мегагерц") == "Подбери осциллограф от 150 МГц"
    assert voice.normalize("ток 15 ампер и 220 вольт") == "Ток 15 А и 220 В"
    assert voice.normalize("пайка при 450 градусов цельсия") == "Пайка при 450 °C"
    assert voice.normalize("сопротивление 10 килоом,  предел 200 миллиампер") == "Сопротивление 10 кОм, предел 200 мА"
    assert voice.normalize("можно ли мультиметромMAS830L") == "Можно ли мультиметром MAS830L"


def test_models_said_aloud_become_designations():
    n = lambda t: voice.normalize(t, MODELS)  # noqa: E731
    assert n("можно ли мультиметром мас 830 л измерить ток 15 ампер?") == "Можно ли мультиметром MAS830L измерить ток 15 А?"
    assert n("Можно ли мультиметромMС 830Л измерить ток?") == "Можно ли мультиметром MAS830L измерить ток?"
    assert n("какая частота дискретизации у акип 4115 1В?") == "Какая частота дискретизации у АКИП-4115/1В?"
    assert n("сравни дт 9205 а и дт 9208 а") == "Сравни DT9205A и DT9208A"
    assert n("чем отличаются акип 4122 1 V и акип 4122 10 V") == "Чем отличаются АКИП-4122/1(V) и АКИП-4122/10(V)"
    # цифры другие — не та модель; одна буква «Б» без цифр — не обозначение
    assert n("а дт 9207 а есть?") == "А дт 9207 а есть?"
    assert n("отказ категории Б в ночную смену") == "Отказ категории Б в ночную смену"
    assert voice._close("MS830L", "MAS830L") and not voice._close("DT9208A", "DT9205A")


def test_spoken_wording_reaches_reference_search():
    q = spravochnik.understand(voice.normalize("Подбери ацеллограф с полосой не менее 150 мегагерц и четырьмя каналами"))
    assert q.kind == "Осциллограф" and {(r.param, r.value) for r in q.reqs} == {("Полоса пропускания", 150e6),
                                                                               ("Число каналов", 4)}
    q = spravochnik.understand("нужен осциллограф на два канала от 100 МГц")
    assert ("Число каналов", 2) in {(r.param, r.value) for r in q.reqs}


def test_recording_checks(monkeypatch):
    stereo = np.stack([np.full(16000, 0.3), np.full(16000, 0.1)], axis=1).reshape(-1)
    samples, rate = voice.read_wav(_wav(stereo, channels=2))
    assert rate == 16000 and len(samples) == 16000 and abs(samples[0] - 0.2) < 1e-3
    with pytest.raises(voice.VoiceError, match="16 бит"):
        voice.read_wav(_wav(np.zeros(1600), width=1))
    with pytest.raises(voice.VoiceError, match="не читается"):
        voice.read_wav(b"not a wav")
    with pytest.raises(voice.VoiceError, match="пустая"):
        voice.transcribe(_wav(np.zeros(16000)))
    with pytest.raises(voice.VoiceError, match="длиннее"):
        voice.transcribe(_wav(np.full(16000 * 31, 0.2)))

    class Fake:
        def recognize(self, samples, sample_rate):
            assert sample_rate == 16000
            return "можно ли мультиметром мас 830 л измерить ток 15 ампер"

    monkeypatch.setattr(voice, "_model", lambda: Fake())
    tone = 0.3 * np.sin(np.linspace(0, 2000, 16000 * 2))
    assert voice.transcribe(_wav(tone), MODELS) == "Можно ли мультиметром MAS830L измерить ток 15 А"

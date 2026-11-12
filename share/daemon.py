#!/usr/bin/env python3
"""Фоновая служба голосового ввода.

Держит модель Whisper загруженной в видеопамяти, чтобы распознавание
начиналось мгновенно. Команды принимает по unix-сокету: клиент `dictate`
шлёт "toggle".

Запись останавливается сама, когда человек договорил. Повторное нажатие
клавиши тоже останавливает — на случай, если надо оборвать раньше.

Текст вставляется через буфер обмена и синтетический Ctrl+V. Прямая
посимвольная печать не годится: uinput шлёт коды клавиш, а не символы,
и кириллица зависела бы от текущей раскладки.
"""

import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

MODEL_NAME = os.environ.get("DICTATE_MODEL", "large-v3-turbo")
LANGUAGE = os.environ.get("DICTATE_LANG", "ru") or None
RESTORE_CLIPBOARD = os.environ.get("DICTATE_RESTORE_CLIPBOARD", "1") == "1"
PASTE_DELAY = float(os.environ.get("DICTATE_PASTE_DELAY", "0.25"))

RATE = 16000
CHUNK_FRAMES = 1024                     # 64 мс
CHUNK_BYTES = CHUNK_FRAMES * 2
CHUNK_SEC = CHUNK_FRAMES / RATE

# сколько тишины считать концом фразы
SILENCE_SEC = float(os.environ.get("DICTATE_SILENCE_SEC", "1.5"))
# первые доли секунды уходят на замер фона
LEAD_IN_SEC = float(os.environ.get("DICTATE_LEAD_IN_SEC", "0.4"))
# если человек так и не заговорил — бросаем
NO_SPEECH_SEC = float(os.environ.get("DICTATE_NO_SPEECH_SEC", "8"))
# предохранитель от бесконечной записи
MAX_SEC = float(os.environ.get("DICTATE_MAX_SEC", "120"))
# порог не опускаем ниже этого, как бы тихо ни было вокруг
MIN_THRESHOLD = float(os.environ.get("DICTATE_MIN_THRESHOLD", "0.006"))
# Микрофон. Пусто — системный по умолчанию, то есть тот, что выбран
# в настройках GNOME. Можно закрепить конкретный: имя узла PipeWire
# (`wpctl status`, `pw-cli ls Node`) или его номер.
SOURCE = os.environ.get("DICTATE_SOURCE", "").strip()

RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "dictate"
SOCKET_PATH = RUNTIME / "daemon.sock"
STATE_PATH = RUNTIME / "state"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dictate"
SOURCE_PATH = CONFIG_DIR / "source"
SOUNDS_PATH = CONFIG_DIR / "sounds"
PREROLL_PATH = CONFIG_DIR / "preroll"
PROMPT_PATH = CONFIG_DIR / "prompt"

# Сколько звука держать «про запас». pw-record отдаёт первый отсчёт
# примерно через 170 мс после запуска, поэтому при записи по нажатию
# начало фразы теряется. С предзаписью поток открыт постоянно, и в дело
# идёт то, что прозвучало до нажатия.
PREROLL_SEC = float(os.environ.get("DICTATE_PREROLL_SEC", "0.6"))
SOUND_DIR = Path(__file__).resolve().parent / "sounds"

# звук старта попадает в запись, поэтому замер фона начинаем после него
BEEP_SKIP_SEC = float(os.environ.get("DICTATE_BEEP_SKIP_SEC", "0.25"))

IDLE, RECORDING, BUSY = "idle", "recording", "busy"

_model = None
_model_ready = threading.Event()
_state = IDLE
_state_lock = threading.Lock()
_stop_flag = threading.Event()
_uinput = None


def current_source():
    """Микрофон читаем перед каждой записью, а не при старте службы:
    значок в трее меняет его на лету, перезапуск для этого не нужен."""
    try:
        value = SOURCE_PATH.read_text().strip()
        if value:
            return value
    except Exception:
        pass
    return SOURCE


def initial_prompt():
    """Затравка для модели: список терминов в нужном написании.

    Жёстко заданный язык заставляет модель приводить к нему всё, включая
    английские термины: «pull request» превращается в «пул реквест» или
    «запрос на слияние». Затравка показывает модели, как эти слова
    выглядят на самом деле, и она перестаёт их переводить.
    """
    try:
        text = PROMPT_PATH.read_text().strip()
        if text:
            return text
    except Exception:
        pass
    return os.environ.get("DICTATE_PROMPT", "").strip() or None


def preroll_on():
    try:
        return PREROLL_PATH.read_text().strip() != "0"
    except Exception:
        # по умолчанию выключено: держать микрофон открытым — заметное
        # решение, его человек включает сам в меню значка
        return os.environ.get("DICTATE_PREROLL", "0") != "0"


def sounds_on():
    try:
        return SOUNDS_PATH.read_text().strip() != "0"
    except Exception:
        return os.environ.get("DICTATE_SOUNDS", "1") != "0"


def play(name):
    """Сигнал проигрываем в фоне: ждать его окончания значит задерживать
    начало записи, а это потерянные первые слоги."""
    if not sounds_on():
        return
    path = SOUND_DIR / f"{name}.wav"
    if not path.exists():
        return
    try:
        subprocess.Popen(["pw-play", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def set_state(value):
    """Состояние кладём в файл, чтобы значок в трее его видел."""
    global _state
    _state = value
    try:
        STATE_PATH.write_text(value)
    except Exception:
        pass


_notify_id = None
_notify_at = 0.0
NOTIFY_REUSE_SEC = 4.0


def notify(text, urgency="normal"):
    """Показать плашку, не оставляя следа в истории уведомлений.

    Флаг -e (transient) — главное здесь: без него GNOME складывает каждое
    уведомление в список, и после дня работы экран блокировки оказывается
    завален десятками «Диктовка». Подсказки x-canonical-private-synchronous
    для этого мало: она заменяет только всплывающую плашку.

    -r переиспользует номер предыдущего уведомления, чтобы плашка
    обновлялась на месте, а не выстраивалась стопкой. Но номер годится
    только пока плашка жива: если подсунуть GNOME номер давно погасшего
    уведомления, он обновит то, чего нет, и не покажет ничего. Поэтому
    переиспользуем номер лишь несколько секунд — ровно чтобы сменяли
    друг друга «Слушаю» и «Распознаю».
    """
    global _notify_id, _notify_at
    cmd = ["notify-send", "-a", "Диктовка", "-u", urgency, "-e", "-p"]
    if _notify_id and time.monotonic() - _notify_at < NOTIFY_REUSE_SEC:
        cmd += ["-r", str(_notify_id)]
    cmd += ["Диктовка", text]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        out = r.stdout.strip()
        if out.isdigit():
            _notify_id = int(out)
            _notify_at = time.monotonic()
    except Exception:
        pass


def load_model():
    global _model
    from faster_whisper import WhisperModel
    try:
        _model = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")
    except Exception as exc:
        print(f"CUDA недоступна ({exc}), перехожу на процессор", flush=True)
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    _model_ready.set()
    print(f"модель {MODEL_NAME} загружена", flush=True)


def make_uinput():
    """Виртуальная клавиатура создаётся один раз: системе нужно время,
    чтобы её заметить, и делать это на каждую вставку — терять полсекунды."""
    global _uinput
    from evdev import UInput, ecodes as e
    _uinput = UInput({e.EV_KEY: [e.KEY_LEFTCTRL, e.KEY_V]}, name="dictate-paste")
    time.sleep(0.5)


def press_ctrl_v():
    from evdev import ecodes as e
    _uinput.write(e.EV_KEY, e.KEY_LEFTCTRL, 1)
    _uinput.write(e.EV_KEY, e.KEY_V, 1)
    _uinput.syn()
    time.sleep(0.02)
    _uinput.write(e.EV_KEY, e.KEY_V, 0)
    _uinput.write(e.EV_KEY, e.KEY_LEFTCTRL, 0)
    _uinput.syn()


def clipboard_get():
    try:
        r = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=3)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def clipboard_set(data: bytes):
    subprocess.run(["wl-copy"], input=data, check=False, timeout=3)


def rms_of(chunk: bytes) -> float:
    """Громкость куска: без numpy, чтобы не дёргать его на каждые 64 мс."""
    import array
    a = array.array("h")
    a.frombytes(chunk)
    if not a:
        return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a)) / 32768.0



class Mic:
    """Один постоянный читатель микрофона.

    Когда предзапись включена, поток открыт всегда, а последние
    PREROLL_SEC секунд лежат в кольцевом буфере. По нажатию клавиши они
    становятся началом записи — поэтому не теряются ни 170 мс на открытие
    потока, ни первый слог, если человек заговорил чуть раньше кнопки.

    Когда выключена — поток открывается по нажатию, как раньше.
    """

    def __init__(self):
        self.proc = None
        self.ring = deque(maxlen=max(1, int(PREROLL_SEC / CHUNK_SEC)))
        self.queue = None
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    # --- поток -------------------------------------------------------
    def _open(self):
        cmd = ["pw-record", "--rate", str(RATE), "--channels", "1",
               "--format", "s16", "--raw", "--latency", "50ms"]
        source = current_source()
        if source:
            cmd += ["--target", source]
        cmd.append("-")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)

    def _close(self):
        if self.proc:
            self.proc.terminate()
            try:
                # полсекунды хватает с запасом; дольше ждать нечего,
                # иначе микрофон числится занятым уже без причины
                self.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                # без ожидания после kill остаётся зомби: процесса нет,
                # а в списке он виден, и кажется, что микрофон не отпущен
                self.proc.wait(timeout=0.5)
            self.proc = None
        self.ring.clear()

    def _loop(self):
        while True:
            wanted = preroll_on() or self.queue is not None
            if not wanted:
                if self.proc:
                    self._close()
                time.sleep(0.2)
                continue
            if not self.proc:
                self._open()
            chunk = self.proc.stdout.read(CHUNK_BYTES)
            if not chunk or len(chunk) < CHUNK_BYTES:
                self._close()      # поток оборвался — откроем заново
                continue
            with self.lock:
                self.ring.append(chunk)
                if self.queue is not None:
                    self.queue.put(chunk)

    # --- запись ------------------------------------------------------
    def begin(self):
        """Отдаёт очередь, уже наполненную предзаписью."""
        q = queue.Queue()
        with self.lock:
            for chunk in self.ring:
                q.put(chunk)
            preloaded = len(self.ring)
            self.queue = q
        return q, preloaded

    def end(self):
        with self.lock:
            self.queue = None


_mic = None

def record(auto_stop: bool):
    """Пишет с микрофона.

    auto_stop=True  — останавливаемся сами, когда человек домолчал
                      (режим «нажал / нажал ещё раз»).
    auto_stop=False — пишем, пока клавишу держат; конец задаёт отпускание
                      (режим «нажал и держу»). Тогда паузы в речи не
                      обрывают запись — человек сам решает, когда хватит.

    Порог громкости берём как нижний процентиль скользящего окна: у разных
    микрофонов и комнат фон отличается на порядок, а короткий громкий звук
    (сигнал старта, щелчок мыши) не должен его задирать.
    """
    q, preloaded = _mic.begin()
    if preloaded:
        print(f"предзапись: {preloaded * CHUNK_SEC:.2f} с", flush=True)

    buf = bytearray()
    levels = deque(maxlen=max(8, int(2.0 / CHUNK_SEC)))
    threshold = MIN_THRESHOLD
    speech = False
    silence = 0.0
    started = time.time()
    reason = "тишина"

    try:
        while True:
            if _stop_flag.is_set():
                reason = "нажатие"
                break
            try:
                chunk = q.get(timeout=1.0)
            except queue.Empty:
                reason = "микрофон молчит"
                break
            buf += chunk
            elapsed = len(buf) / 2 / RATE          # считаем по звуку, не по часам
            level = rms_of(chunk)
            levels.append(level)

            if len(levels) == levels.maxlen:
                ordered = sorted(levels)
                base = ordered[max(0, len(ordered) // 5 - 1)]
                threshold = max(base * 4.0, MIN_THRESHOLD)

            if level > threshold:
                speech = True
                silence = 0.0
            elif auto_stop and speech:
                silence += CHUNK_SEC
                if silence >= SILENCE_SEC:
                    break
            elif auto_stop and elapsed > BEEP_SKIP_SEC + NO_SPEECH_SEC:
                reason = "никто не говорил"
                break

            if elapsed > MAX_SEC:
                reason = "предел длительности"
                break
    finally:
        _mic.end()

    print(f"порог к концу {threshold:.5f}"
          + (f", фон {sorted(levels)[max(0, len(levels)//5 - 1)]:.5f}" if levels else ""),
          flush=True)
    return bytes(buf), speech, reason


def transcribe(pcm: bytes) -> str:
    """Отдаём модели готовый массив, минуя PyAV.

    faster-whisper объявляет зависимость av>=11 без верхней границы, а в
    свежих версиях PyAV убрали аргумент metadata_errors, который он всё ещё
    передаёт — распознавание на этом падает. Нам декодер и не нужен: звук
    уже в нужном формате.
    """
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, info = _model.transcribe(
        audio,
        language=LANGUAGE,
        task="transcribe",          # явно: никакого перевода
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=initial_prompt(),
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    # пишем только язык и уверенность, сам текст в журнал не попадает
    print(f"язык {info.language} ({info.language_probability:.2f})"
          f"{' — задан принудительно' if LANGUAGE else ' — определён сам'}",
          flush=True)
    return text


def paste(text: str):
    saved = clipboard_get() if RESTORE_CLIPBOARD else None
    clipboard_set(text.encode())
    time.sleep(PASTE_DELAY)
    press_ctrl_v()
    if saved is not None:
        time.sleep(1.2)                 # даём вставке состояться
        clipboard_set(saved)


def session(auto_stop: bool):
    """Один заход: запись → распознавание → вставка."""
    global _state
    try:
        play("start")
        notify("🎤 Говорите…" if auto_stop else "🎤 Говорите, держите клавишу…")
        pcm, speech, reason = record(auto_stop)

        with _state_lock:
            set_state(BUSY)

        if not speech or len(pcm) < RATE:        # меньше полусекунды звука
            play("error")
            notify(f"Ничего не услышал ({reason})", "low")
            return

        notify("⏳ Распознаю…")
        if not _model_ready.is_set():
            notify("⏳ Догружаю модель, это только в первый раз…")
            _model_ready.wait()

        t0 = time.time()
        try:
            text = transcribe(pcm)
        except Exception as exc:
            play("error")
            notify(f"Ошибка распознавания: {exc}", "critical")
            return
        secs = len(pcm) / 2 / RATE
        print(f"{secs:.1f} с звука → {time.time()-t0:.2f} с распознавания "
              f"[{reason}]", flush=True)

        if not text:
            play("error")
            notify("Речь не распознана", "low")
            return

        paste(text)
        play("done")
        notify("✓ " + (text if len(text) <= 60 else text[:57] + "…"))
    finally:
        with _state_lock:
            set_state(IDLE)
        _stop_flag.clear()


def begin(auto_stop: bool):
    with _state_lock:
        if _state != IDLE:
            return
        set_state(RECORDING)
        _stop_flag.clear()
        threading.Thread(target=session, args=(auto_stop,), daemon=True).start()


def end():
    with _state_lock:
        if _state == RECORDING:
            _stop_flag.set()


def toggle():
    """Режим «нажал / нажал ещё раз»."""
    with _state_lock:
        recording = _state == RECORDING
    if recording:
        end()
    else:
        begin(auto_stop=True)


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    global _mic
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    set_state(IDLE)
    _mic = Mic()
    make_uinput()
    threading.Thread(target=load_model, daemon=True).start()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET_PATH))
    srv.listen(4)
    print(f"служба слушает {SOCKET_PATH}", flush=True)
    print(f"режимы: start/stop (удержание), toggle (автостоп {SILENCE_SEC} с)", flush=True)
    print(f"микрофон: {current_source() or 'системный по умолчанию'}", flush=True)
    print(f"предзапись: {'включена, %.1f с' % PREROLL_SEC if preroll_on() else 'выключена'}",
          flush=True)

    while True:
        conn, _ = srv.accept()
        with conn:
            cmd = conn.recv(64).decode().strip()
        if cmd == "toggle":
            toggle()
        elif cmd == "start":          # клавишу нажали и держат
            begin(auto_stop=False)
        elif cmd == "stop":           # клавишу отпустили
            end()
        elif cmd == "quit":
            break


if __name__ == "__main__":
    sys.exit(main())

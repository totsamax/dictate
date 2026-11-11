#!/usr/bin/env python3
"""Генератор звуковых сигналов.

Тоны короткие, со сглаженными краями: без фейдов на стыке слышен щелчок.
"""
import math
import pathlib
import struct
import wave

RATE = 48000
OUT = pathlib.Path(__file__).resolve().parent.parent / "share" / "sounds"


def tone(path, notes, amp=0.18):
    frames = bytearray()
    for freq, ms in notes:
        n = int(RATE * ms / 1000)
        edge = int(RATE * 0.008)
        for i in range(n):
            env = 1.0
            if i < edge:
                env = i / edge
            elif i > n - edge:
                env = (n - i) / edge
            frames += struct.pack(
                "<h", int(32767 * amp * env * math.sin(2 * math.pi * freq * i / RATE)))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    tone(OUT / "start.wav", [(660, 55), (880, 55)])   # начало записи
    tone(OUT / "done.wav",  [(1320, 60)])             # текст вставлен
    tone(OUT / "error.wav", [(300, 90), (220, 140)])  # не получилось
    print("готово:", OUT)

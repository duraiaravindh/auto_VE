"""Writes an original soft Sunday-morning instrumental: tanpura drone, a flute line and a temple bell.

The melody is written here in the Mohanam scale. It is not taken from any song or film.
The same settings always give the same file.
Usage:  python engine/make_music.py <seconds> <out.wav> [festive]
"festive" moves up a tone, adds a soft hand drum that fades out as the story turns, and reorders the melody.
"""
import sys
import wave

import numpy as np

SR = 48000
FESTIVE = len(sys.argv) > 3 and sys.argv[3] == "festive"
BPM = 84 if FESTIVE else 72
BEAT = 60 / BPM
SHIFT = 2 if FESTIVE else 0
REST = None
# (MIDI note, beats). D is Sa: D E F# A B only.
A = [(62, 2), (64, 1), (66, 1), (69, 2), (66, 1), (64, 1), (66, 2), (69, 1), (71, 1), (69, 3), (REST, 1)]
B = [(74, 2), (71, 1), (69, 1), (71, 2), (69, 1), (66, 1), (64, 2), (66, 1), (64, 1), (62, 3), (REST, 1)]
C = [(69, 1), (71, 1), (74, 2), (76, 1), (74, 1), (71, 2), (69, 1), (66, 1), (69, 2), (66, 1), (64, 1), (62, 2)]
FORM = (C, A, B, A) if FESTIVE else (A, B, A, C)


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def flute(n, rng):
    """One continuous breath of flute that glides between notes, with vibrato and a little air."""
    notes, t, section = [], 0.0, 0
    while t * SR < n:
        for note, beats in FORM[section % len(FORM)]:
            notes.append((t, beats * BEAT, note))
            t += beats * BEAT
        section += 1
    pitch, amp = np.zeros(n), np.zeros(n)
    last = 62.0
    for start, length, note in notes:
        a, z = int(start * SR), min(int((start + length) * SR), n)
        if a >= n:
            break
        if note is None:
            pitch[a:z] = last
            continue
        size = z - a
        glide = min(int(0.07 * SR), size)
        note += SHIFT
        pitch[a:z] = note
        pitch[a:a + glide] = last + (note - last) * np.sin(np.linspace(0, np.pi / 2, glide)) ** 2
        env = np.ones(size)
        rise = min(int(0.05 * SR), size)
        fall = min(int(0.16 * SR), size)
        env[:rise] = np.linspace(0.55, 1, rise)
        env[-fall:] *= np.linspace(1, 0.35, fall)
        amp[a:z] = env
        last = note
    amp = np.convolve(amp, np.ones(960) / 960, mode="same")
    time = np.arange(n) / SR
    wobble = 1 + np.interp(time, [0, 2, n / SR], [0, 1, 1]) * 0.0035 * np.sin(2 * np.pi * 4.8 * time)
    phase = 2 * np.pi * np.cumsum(hz(pitch) * wobble) / SR
    tone = np.sin(phase) + 0.32 * np.sin(2 * phase) + 0.1 * np.sin(3 * phase) + 0.04 * np.sin(4 * phase)
    air = np.convolve(rng.standard_normal(n), np.ones(12) / 12, mode="same") * 0.11
    return (tone + air) * amp


def tanpura(n):
    """Slow plucked drone on Pa, Sa, Sa and low Sa, rich in overtones."""
    out = np.zeros(n)
    strings = (57, 62, 62, 50)
    step = int(BEAT * 1.5 * SR)
    length = int(5.5 * SR)
    t = np.arange(length) / SR
    for i, start in enumerate(range(0, n, step)):
        f = hz(strings[i % 4] - 12 + SHIFT)
        tone = sum((1 / k ** 0.9) * np.sin(2 * np.pi * f * k * t) * np.exp(-t * (0.55 + 0.12 * k)) for k in range(1, 11))
        tone = tone * np.minimum(t / 0.02, 1)
        size = min(length, n - start)
        out[start:start + size] += tone[:size]
    return out


def drum(n, until):
    """A soft hand drum on a simple festive pattern, fading out as the story turns."""
    out = np.zeros(n)
    size = int(0.4 * SR)
    t = np.arange(size) / SR
    low = np.sin(2 * np.pi * (88 * t + 26 * (1 - np.exp(-t * 26)) / 26)) * np.exp(-t * 11)
    high = np.sin(2 * np.pi * 240 * t) * np.exp(-t * 26)
    step = BEAT / 2
    for i, hit in enumerate((1, 0, 2, 2, 1, 2, 0, 2) * int(n / SR / (step * 8) + 1)):
        start = int(i * step * SR)
        if hit and start + size < n:
            out[start:start + size] += low if hit == 1 else 0.55 * high
    return out * np.clip((until - np.arange(n) / SR) / 4, 0, 1)


def bell(n, times):
    """Soft temple bell: detuned, inharmonic partials that ring for a long time."""
    out = np.zeros(n)
    length = int(5 * SR)
    t = np.arange(length) / SR
    tone = sum(g * np.sin(2 * np.pi * 587.33 * r * t) * np.exp(-t * d)
               for r, g, d in ((1, 1, 0.8), (2.01, 0.6, 1.1), (2.74, 0.4, 1.5), (3.76, 0.25, 2.2), (5.4, 0.12, 3)))
    tone *= np.minimum(t / 0.004, 1)
    for at in times:
        start = int(at * SR)
        size = min(length, n - start)
        if size > 0:
            out[start:start + size] += tone[:size]
    return out


def room(x, rng):
    """A short soft room, so the notes do not sound dry."""
    size = int(1.6 * SR)
    t = np.arange(size) / SR
    tail = rng.standard_normal(size) * np.exp(-t * 3.4)
    tail = np.convolve(tail, np.ones(16) / 16, mode="same")
    tail /= np.sqrt(np.sum(tail ** 2))
    return np.fft.irfft(np.fft.rfft(x, 2 * len(x)) * np.fft.rfft(tail, 2 * len(x)))[:len(x)]


def rms(x):
    return np.sqrt(np.mean(x ** 2))


def main():
    seconds, path = float(sys.argv[1]), sys.argv[2]
    n = int(seconds * SR)
    rng = np.random.default_rng(29)
    f, d = flute(n, rng), tanpura(n)
    b = bell(n, np.arange(0.0, seconds, 16 * BEAT * 2))
    f /= rms(f)
    d /= rms(d)
    b /= max(rms(b), 1e-9)
    # the flute sits a little to the right, the drone is centred, and the room adds width
    wet_l, wet_r = room(f, rng), room(f, rng)
    beat = drum(n, 24) if FESTIVE else np.zeros(n)
    if FESTIVE:
        beat /= rms(beat)
    left = 0.46 * f + 0.34 * wet_l + 0.62 * d + 0.13 * b + 0.3 * beat
    right = 0.56 * f + 0.34 * wet_r + 0.62 * d + 0.13 * b + 0.3 * beat
    data = np.stack([left, right], axis=1)
    data = data / np.max(np.abs(data)) * 0.89
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data * 32767).astype("<i2").tobytes())
    print(f"wrote {path}: {seconds:.1f}s, crest {20 * np.log10(np.max(np.abs(data)) / rms(data)):.1f} dB")


if __name__ == "__main__":
    main()

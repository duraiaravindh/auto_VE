"""Generates an original music-and-effects bed, so no third-party audio is involved.

Runs only when effects.json has a "sound" section. The same settings always give the same file.
All times are output time. The voice is never touched: the bed is mixed under it in the graph.
"""
import os
import wave

import numpy as np

import captions

SR = 48000
CHORDS = ([45, 52, 57, 60, 64], [41, 48, 53, 57, 60], [48, 55, 60, 64, 67], [43, 50, 55, 59, 62])


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def _db(x):
    return 10 ** (x / 20)


def _pad(n, bar):
    """Slow sustained chords, slightly detuned between left and right."""
    out, step = np.zeros((n, 2)), int(bar * SR)
    tail = int(1.4 * SR)
    for i, start in enumerate(range(0, n, step)):
        size = min(step + tail, n - start)
        t = np.arange(size) / SR
        env = np.minimum(t / 0.9, 1) * np.clip((step + tail - t * SR) / tail, 0, 1)
        env = env * env * (3 - 2 * env)
        for note in CHORDS[i % 4]:
            f = _hz(note)
            for ch, cents in ((0, -0.6), (1, 0.6)):
                out[start:start + size, ch] += env * (np.sin(2 * np.pi * (f + cents) * t) + 0.25 * np.sin(4 * np.pi * (f + cents) * t))
    return out


def _piano(n, beat, rng):
    """Sparse soft notes walking through each chord."""
    out, length = np.zeros((n, 2)), int(2.4 * SR)
    t = np.arange(length) / SR
    attack = np.minimum(t / 0.008, 1)
    for i in range(int(n / SR / beat)):
        if i % 4 == 3 and i % 8 != 7:   # leave some air
            continue
        chord = CHORDS[(i // 4) % 4]
        note = chord[2:][i % 3] + 12
        tone = sum(g * np.sin(2 * np.pi * _hz(note) * h * t) * np.exp(-t * h * 1.1)
                   for h, g in ((1, 1.0), (2, 0.35), (3, 0.12), (4, 0.05)))
        tone = tone * attack * (0.8 + 0.2 * rng.random())
        start = int(i * beat * SR)
        size = min(length, n - start)
        if size <= 0:
            break
        pan = 0.5 + 0.12 * np.sin(i * 0.9)
        out[start:start + size, 0] += tone[:size] * (1 - pan)
        out[start:start + size, 1] += tone[:size] * pan
    return out


def _whoosh(rng, seconds, low, high):
    """Noise through a band filter that sweeps up and back down."""
    n = int(seconds * SR)
    noise = rng.standard_normal(n)
    phase = np.arange(n) / n
    cutoff = low + (high - low) * np.sin(np.pi * phase) ** 2
    g = np.minimum(2 * np.sin(np.pi * cutoff / SR), 0.9)
    y, band, low_state = np.zeros(n), 0.0, 0.0
    for i in range(n):
        low_state += g[i] * band
        high_state = noise[i] - low_state - 0.7 * band
        band += g[i] * high_state
        y[i] = band
    y *= np.sin(np.pi * phase) ** 2
    return y / np.max(np.abs(y))


def _hit(rng):
    """A soft low thump with a short breath of noise on top."""
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    thump = np.sin(2 * np.pi * (46 * t + 14 * (1 - np.exp(-t * 18)) / 18)) * np.exp(-t * 7)
    air = rng.standard_normal(n) * np.exp(-t * 28)
    air = np.convolve(air, np.ones(24) / 24, mode="same")
    y = thump + 0.5 * air
    return y / np.max(np.abs(y))


def _crackle(rng, seconds=2.2):
    """A string of firecrackers: many short bright pops, thinning out at the end."""
    n = int(seconds * SR)
    y = np.zeros(n)
    pop = rng.standard_normal(int(0.03 * SR)) * np.exp(-np.arange(int(0.03 * SR)) / (0.006 * SR))
    pos = 0
    while pos < n - len(pop):
        y[pos:pos + len(pop)] += pop * (0.55 + 0.45 * rng.random())
        pos += int((0.025 + rng.random() * 0.075 * (1 + 2 * pos / n)) * SR)
    y *= np.minimum((n - np.arange(n)) / (0.3 * SR), 1)
    return y / np.max(np.abs(y))


def _boom():
    """A distant firework: a low thump with a slow airy tail."""
    n = int(1.6 * SR)
    t = np.arange(n) / SR
    thump = np.sin(2 * np.pi * (58 * t + 22 * (1 - np.exp(-t * 14)) / 14)) * np.exp(-t * 6)
    noise = np.convolve(np.random.default_rng(7).standard_normal(n), np.ones(90) / 90, mode="same") * np.exp(-t * 3.2) * 9
    y = thump + 0.35 * noise
    return y / np.max(np.abs(y))


def _whistle():
    """A rocket climbing: a thin rising tone."""
    n = int(0.9 * SR)
    t = np.arange(n) / SR
    f = 900 + 1900 * (t / t[-1]) ** 1.5
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.minimum(t / 0.08, 1) * np.minimum((t[-1] - t) / 0.05, 1)
    return y / np.max(np.abs(y))


def _shimmer(rng):
    """Soft bright chimes, for a tender or magical moment."""
    n = int(1.2 * SR)
    t = np.arange(n) / SR
    y = np.zeros(n)
    for i, f in enumerate((1760, 2093, 2349, 2637, 3136, 3520)):
        start = int(i * 0.07 * SR)
        y[start:] += np.sin(2 * np.pi * f * t[:n - start]) * np.exp(-t[:n - start] * 4.5) * (0.6 + 0.4 * rng.random())
    return y / np.max(np.abs(y))


def _type(rng):
    """Typewriter keys."""
    n = int(0.75 * SR)
    y = np.zeros(n)
    size = int(0.018 * SR)
    click = rng.standard_normal(size) * np.exp(-np.arange(size) / (0.003 * SR))
    pos = 0
    while pos < n - size:
        y[pos:pos + size] += click * (0.6 + 0.4 * rng.random())
        pos += int((0.05 + rng.random() * 0.045) * SR)
    return y / np.max(np.abs(y))


def _place(bed, sound, at, level_db):
    start = int(at * SR)
    if start < 0:
        sound, start = sound[-start:], 0
    size = min(len(sound), len(bed) - start)
    if size > 0:
        bed[start:start + size] += (sound[:size] * _db(level_db))[:, None]


def _sfx_times(fx, shots, total):
    """(time, kind) for each effect, taken from the real transitions and cutaways."""
    cuts, pos = [], 0.0
    for s in shots[:-1]:
        pos += s["out"] - s["in"]
        cuts.append(pos)
    reel = fx.get("reel_fx", {})
    half = reel.get("transitions_cfg", {}).get("swipe_ms", 267) / 2000
    items = []
    for x in reel.get("transitions", []):
        cut = min(cuts, key=lambda c: abs(c - x["at"]))
        items.append((cut, "whoosh") if x["type"] == "swipe" else (cut, "hit"))
    loud = [t for t, _ in items]
    for c in (fx.get("broll", {}).get("clips") or ()):
        if not c.get("cut_in") and all(abs(c["start"] - t) > 0.4 for t in loud):
            items.append((c["start"], "air"))
    items += [(x["at"], x["kind"]) for x in fx.get("sound", {}).get("cues", [])]
    return sorted(items), half


def build(fx, shots, total, folder, add):
    cfg = fx.get("sound")
    if not cfg:
        return None
    calm = [(captions.to_output_time(x["from"], shots), captions.to_output_time(x["to"], shots)) for x in fx["_protected"]]
    rng = np.random.default_rng(cfg["seed"])
    n = int((total + 0.1) * SR)
    beat = 60 / cfg["bpm"]

    t = np.arange(n) / SR
    if not cfg.get("music_file"):
        pad, piano = _pad(n, 4 * beat), _piano(n, beat, rng)
        pad /= np.sqrt(np.mean(pad ** 2))
        piano /= np.sqrt(np.mean(piano ** 2))

    def ramp(times, values):
        return np.interp(t, times, values)

    # piano drops out in the calm sections, so only the soft pad sits under those lines
    keys = [(0.0, 1.0)]
    for a, b in calm:
        keys += [(a - 1.0, 1.0), (a, 0.0), (b, 0.0), (b + 1.5, 1.0)]
    keys.append((total, 1.0))
    piano_gain = ramp([k[0] for k in keys], [k[1] for k in keys])
    if cfg.get("music_file"):
        # an instrumental made elsewhere replaces the pad and piano; levels below still apply
        with wave.open(cfg["music_file"], "rb") as w:
            music = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2) / 32768
        if len(music) < n:
            raise SystemExit("music_file is shorter than the Reel")
        music = music[:n]
    else:
        music = pad * cfg["pad_mix"] + piano * cfg["piano_mix"] * piano_gain[:, None]
    music /= np.sqrt(np.mean(music ** 2))

    keys = [(0.0, cfg["music_db"])]
    for a, b in calm:
        keys += [(a - 1.0, cfg["music_db"]), (a, cfg["music_db"] + cfg["calm_db"]), (b, cfg["music_db"] + cfg["calm_db"]), (b + 1.5, cfg["music_db"])]
    keys += [(total - 3.0, cfg["music_db"]), (total - 1.0, cfg["music_db"] + cfg["end_db"]), (total, cfg["music_db"] + cfg["end_db"])]
    gain = 10 ** (ramp([k[0] for k in keys], [k[1] for k in keys]) / 20)
    edge = np.minimum(t / cfg["fade_in"], 1) * np.minimum((total - t) / cfg["fade_out"], 1).clip(0, 1)
    bed = music * (gain * edge)[:, None]

    whoosh, air, hit = _whoosh(rng, 0.42, 500, 6500), _whoosh(rng, 0.5, 900, 3800), _hit(rng)
    items, _ = _sfx_times(fx, shots, total)
    extra = {}
    for kind, make, lead, seconds in (("crackle", lambda: _crackle(rng), 0.0, 2.2), ("boom", _boom, 0.0, 1.6), ("whistle", _whistle, 0.9, 0.9),
                                      ("shimmer", lambda: _shimmer(rng), 0.0, 1.2), ("type", lambda: _type(rng), 0.0, 0.75)):
        if any(k == kind for _, k in items):
            extra[kind] = (make(), lead, seconds)
    bad = []
    for at, kind in items:
        if kind in extra:
            sound, lead, length = extra[kind]
            start = at - lead
        else:
            sound, length = {"whoosh": (whoosh, 0.42), "air": (air, 0.5), "hit": (hit, 0.6)}[kind]
            start = at - length / 2 if kind == "whoosh" else at - 0.08 if kind == "air" else at
        if any(x < start + length and start < y for x, y in calm):
            bad.append(f"{kind} at {at:.2f}")
        _place(bed, sound, start, cfg[f"{kind}_db"])
    counts = {k: sum(1 for _, x in items if x == k) for k in ("whoosh", "hit", "air")}
    add("sound effects outside protected sections", not bad, bad or f"{counts}")

    path = os.path.join(folder, "soundbed.wav").replace(chr(92), "/")
    data = np.clip(bed, -1, 1)
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((data * 32767).astype("<i2").tobytes())
    return path

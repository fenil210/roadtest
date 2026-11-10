"""Make the launch video's soundtrack and put it on the video.

    node media/render.mjs media/scenes/launch.html media/out/launch-cues.json --cues
    .venv/bin/python media/sound.py media/out/launch-cues.json media/out/launch.mp4 docs/assets/launch.mp4

The scene exports a cue for everything that happens on screen: each key typed, each result line,
each scene change. Every sound is synthesised here from those cues: soft key clicks, a tick for
each passing scenario, a low muted note for each failure, a chime when CI turns green, and a quiet
pad that changes chord with the scenes. Nothing is sampled, so the repository carries no
third-party audio, and each sound lands on the frame it belongs to. It needs only the standard
library and ffmpeg.
"""

from __future__ import annotations

import cmath
import json
import math
import random
import struct
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

RATE = 48000
PAD_RATE = 16000  # the pad has nothing above 2 kHz, so it is made at a lower rate and resampled
TAU = 2 * math.pi
LOUDNESS = -16  # LUFS, a usual level for video on the web
PEAK = -1.5  # dBFS, the most any sample may reach after the limiter
rng = random.Random(7)  # the same soundtrack on every build

# The pad's chord under each part of the video, in D major. "ci-green" is when the check turns green.
CHORDS = [
    ("hook", "B2 F#3 A3 D4"),
    ("brand", "D3 A3 E4 F#4"),
    ("init", "G2 D3 B3 F#4"),
    ("agent", "E3 B3 D4 G4"),
    ("run", "B2 F#3 A3 D4"),
    ("report", "G2 D3 F#3 B3"),
    ("ci", "A2 E3 A3 D4"),
    ("ci-green", "D3 A3 D4 F#4"),
    ("any", "G2 D3 B3 A4"),
    ("end", "D3 A3 E4 F#4"),
]
STEP_NOTES = ["D5", "E5", "F#5", "A5", "B5"]
CHIP_NOTES = ["A4", "B4", "D5", "E5", "F#5", "D5", "E5", "F#5", "A5", "B5"]


def note(name: str) -> float:
    """"A4" -> 440.0"""
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    midi = names.index(name[:-1]) + 12 * (int(name[-1]) + 1)
    return 440.0 * 2 ** ((midi - 69) / 12)


# ── buses ───────────────────────────────────────────────────────────────────────────────────


class Bus:
    """A stereo track that sounds are added into."""

    def __init__(self, seconds: float, rate: int = RATE) -> None:
        self.rate = rate
        size = int(seconds * rate) + 1
        self.left = array("f", bytes(4 * size))
        self.right = array("f", bytes(4 * size))

    def add(self, samples: list[float], t: float, gain: float = 1.0, pan: float = 0.0) -> None:
        angle = (pan + 1) * math.pi / 4  # equal-power pan; centre keeps `gain` on both sides
        self.add_stereo(samples, samples, t, gain * math.cos(angle) * math.sqrt(2), gain * math.sin(angle) * math.sqrt(2))

    def add_stereo(self, left: list[float], right: list[float], t: float, gain_left: float, gain_right: float | None = None) -> None:
        gain_right = gain_left if gain_right is None else gain_right
        start = int(round(t * self.rate))
        out_left, out_right = self.left, self.right
        for index in range(max(0, start), min(len(out_left), start + len(left))):
            out_left[index] += left[index - start] * gain_left
            out_right[index] += right[index - start] * gain_right

    def write(self, path: Path) -> None:
        """A 32-bit float WAV, so nothing clips before the final level is set."""
        frames = array("f", bytes(8 * len(self.left)))
        frames[0::2] = self.left
        frames[1::2] = self.right
        data = frames.tobytes()
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 3, 2, self.rate, self.rate * 8, 8, 32, b"data", len(data),
        )
        path.write_bytes(header + data)


# ── building blocks ─────────────────────────────────────────────────────────────────────────


def biquad(samples: list[float], b0: float, b1: float, b2: float, a1: float, a2: float) -> list[float]:
    out = [0.0] * len(samples)
    x1 = x2 = y1 = y2 = 0.0
    for index, x in enumerate(samples):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
        out[index] = y
    return out


def bandpass(samples: list[float], freq: float, q: float) -> list[float]:
    w = TAU * freq / RATE
    alpha = math.sin(w) / (2 * q)
    a0 = 1 + alpha
    return biquad(samples, alpha / a0, 0.0, -alpha / a0, -2 * math.cos(w) / a0, (1 - alpha) / a0)


def lowpass(samples: list[float], freq: float, q: float = 0.707) -> list[float]:
    w = TAU * freq / RATE
    alpha, cos = math.sin(w) / (2 * q), math.cos(w)
    a0 = 1 + alpha
    b = (1 - cos) / 2 / a0
    return biquad(samples, b, 2 * b, b, -2 * cos / a0, (1 - alpha) / a0)


def noise(seconds: float) -> list[float]:
    return [rng.uniform(-1, 1) for _ in range(int(seconds * RATE))]


def envelope(samples: list[float], tau: float, attack: float = 0.0005) -> list[float]:
    """Fade in over `attack` seconds, then decay exponentially with time constant `tau`."""
    rise = max(1, int(attack * RATE))
    factor = math.exp(-1 / (tau * RATE))
    level, out = 1.0, []
    for index, sample in enumerate(samples):
        out.append(sample * level * min(1.0, index / rise))
        level *= factor
    return out


def tone(freq: float, seconds: float, tau: float, partials=((1, 1.0, 1.0),), attack: float = 0.003, glide: float = 0.0) -> list[float]:
    """A struck tone. Each partial is (frequency ratio, level, decay factor) and fades on its own.
    `glide` starts the pitch that fraction away from `freq` and slides into it over about 25 ms."""
    size = int(seconds * RATE)
    out = [0.0] * size
    for ratio, level, factor in partials:
        phase, amplitude = 0.0, level
        fall = math.exp(-1 / (tau * factor * RATE))
        step = TAU * freq * ratio / RATE
        slide = math.exp(-1 / (0.025 * RATE))
        offset = glide
        for index in range(size):
            phase += step * (1 - offset)
            offset *= slide
            out[index] += amplitude * math.sin(phase)
            amplitude *= fall
    rise = max(1, int(attack * RATE))
    for index in range(min(rise, size)):
        out[index] *= index / rise
    return out


def mix(*parts: tuple[list[float], float]) -> list[float]:
    size = max(len(samples) for samples, _ in parts)
    out = [0.0] * size
    for samples, gain in parts:
        for index, sample in enumerate(samples):
            out[index] += sample * gain
    return out


def normalized(samples: list[float]) -> list[float]:
    peak = max(abs(sample) for sample in samples) or 1.0
    return [sample / peak for sample in samples]


def strum(notes: list[str], timbre, spacing: float, levels: list[float] | None = None) -> list[float]:
    levels = levels or [1.0] * len(notes)
    shift = int(spacing * RATE)
    voices = [timbre(note(name)) for name in notes]
    out = [0.0] * (max(len(v) for v in voices) + shift * len(notes))
    for number, (voice, level) in enumerate(zip(voices, levels)):
        for index, sample in enumerate(voice):
            out[index + number * shift] += sample * level
    return normalized(out)


# ── the sounds ──────────────────────────────────────────────────────────────────────────────


def key_click(weight: float = 1.0) -> list[float]:
    """A keyboard key: a bright contact click, the plastic body, and a little low thock."""
    click = envelope(lowpass(bandpass(noise(0.012), rng.uniform(3200, 4400), 1.4), 9000), 0.0011)
    body = envelope(bandpass(noise(0.06), rng.uniform(900, 1500) / weight, 4.0), 0.006 * weight)
    thock = tone(rng.uniform(170, 240) / weight, 0.08, 0.012 * weight)
    return normalized(mix((normalized(click), 1.0), (normalized(body), 0.6), (normalized(thock), 0.35 * weight)))


def enter_key() -> list[float]:
    press, release = key_click(1.6), key_click(1.0)
    gap = int(0.085 * RATE)
    return normalized(mix((press, 1.0), ([0.0] * gap + release, 0.35)))


def pass_tick(pitch: float) -> list[float]:
    """A soft wooden tick for a passing scenario."""
    body = tone(pitch, 0.1, 0.016, partials=((1, 1.0, 1.0), (2.7, 0.22, 0.5)), attack=0.0008)
    click = envelope(bandpass(noise(0.01), 4200, 1.5), 0.0012)
    return normalized(mix((normalized(body), 1.0), (normalized(click), 0.25)))


def fail_note() -> list[float]:
    """A low, muted note that falls a little: something is wrong, said quietly."""
    body = tone(note("D3"), 0.6, 0.12, partials=((1, 1.0, 1.0), (2, 0.35, 0.55), (3, 0.1, 0.35)), attack=0.004, glide=-0.07)
    thud = envelope(lowpass(noise(0.08), 320), 0.02)
    return normalized(mix((normalized(body), 1.0), (normalized(thud), 0.3)))


def marimba(freq: float) -> list[float]:
    return tone(freq, 0.9, 0.3, partials=((1, 1.0, 1.0), (4.0, 0.15, 0.2), (10.0, 0.03, 0.05)), attack=0.002)


def electric_piano(freq: float, tau: float) -> list[float]:
    return tone(freq, tau * 2.6, tau, partials=((1, 1.0, 1.0), (2, 0.28, 0.45), (3, 0.07, 0.3), (7.1, 0.02, 0.06)), attack=0.006)


def bell(freq: float) -> list[float]:
    return tone(freq, 2.6, 1.0, partials=((1, 1.0, 1.0), (2, 0.3, 0.5), (3, 0.12, 0.33), (5.1, 0.05, 0.14)), attack=0.002)


def gate_failed() -> list[float]:
    """Two muted notes stepping down a minor third."""
    gap = int(0.17 * RATE)
    return normalized(mix((marimba(note("A3")), 1.0), ([0.0] * gap + marimba(note("F#3")), 0.9)))


def success_chime() -> list[float]:
    """Up a fifth, then the octave: the check turned green."""
    parts = [(bell(note("D5")), 1.0, 0.0), (bell(note("A5")), 0.85, 0.09), (bell(note("D6")), 0.4, 0.18)]
    return normalized(mix(*[([0.0] * int(delay * RATE) + samples, level) for samples, level, delay in parts]))


def pop(name: str, tau: float) -> list[float]:
    """A small rounded pop that slides up into its note."""
    return normalized(tone(note(name), tau * 4, tau, partials=((1, 1.0, 1.0), (2, 0.18, 0.5)), attack=0.002, glide=0.16))


def code_tick() -> list[float]:
    click = envelope(bandpass(noise(0.01), 5200, 2.0), 0.0014)
    return normalized(mix((normalized(click), 1.0), (normalized(tone(2400, 0.03, 0.004)), 0.4)))


def chord_bloom(notes: list[str], tau: float, sub: str | None) -> list[float]:
    chord = strum(notes, lambda freq: electric_piano(freq, tau), 0.022, [1.0 - 0.08 * i for i in range(len(notes))])
    parts = [(chord, 1.0)]
    if sub:
        parts.append((normalized(tone(note(sub), tau * 1.4, tau * 0.4, attack=0.02)), 0.3))
    return normalized(mix(*parts))


def whoosh(seconds: float = 0.8) -> tuple[list[float], list[float]]:
    """Air moving past: noise through a band-pass that sweeps up, panned from left to right."""
    source = noise(seconds)
    size = len(source)
    out = [0.0] * size
    x1 = x2 = y1 = y2 = 0.0
    for block in range(0, size, 32):
        progress = block / size
        freq = 350 * (2200 / 350) ** progress
        w = TAU * freq / RATE
        alpha = math.sin(w) / (2 * 0.9)
        a0 = 1 + alpha
        b0, b2, a1, a2 = alpha / a0, -alpha / a0, -2 * math.cos(w) / a0, (1 - alpha) / a0
        for index in range(block, min(size, block + 32)):
            x = source[index]
            y = b0 * x + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, x, y1, y
            out[index] = y
    out = lowpass(out, 4500)
    peak_at = 0.55
    shaped = []
    for index, sample in enumerate(out):
        p = index / size
        level = math.sin(math.pi / 2 * p / peak_at) ** 2 if p < peak_at else math.cos(math.pi / 2 * (p - peak_at) / (1 - peak_at)) ** 2
        shaped.append(sample * level)
    shaped = normalized(shaped)
    left = [s * math.cos((0.15 + 0.7 * i / size) * math.pi / 2) for i, s in enumerate(shaped)]
    right = [s * math.sin((0.15 + 0.7 * i / size) * math.pi / 2) for i, s in enumerate(shaped)]
    return left, right


def impulse_response(seconds: float = 1.8, rt60: float = 1.4) -> Bus:
    """A small, soft room for the reverb: decaying noise that gets darker as it fades."""
    room = Bus(seconds)
    tau = rt60 / 6.91
    for side in (room.left, room.right):
        state = 0.0
        for index in range(len(side)):
            t = index / RATE - 0.012  # pre-delay
            if t < 0:
                continue
            state += (0.6 - 0.45 * min(1.0, t / 1.2)) * (rng.gauss(0, 1) * math.exp(-t / tau) - state)
            side[index] = state
    return room


def pad(bus: Bus, segments: list[tuple[float, float, str]], duration: float) -> None:
    """A quiet bed of sustained chords. Each note is two slightly detuned sines, one per side."""
    fade = 1.6
    rate = bus.rate
    smooth = lambda x: 0.0 if x <= 0 else 1.0 if x >= 1 else x * x * (3 - 2 * x)  # noqa: E731
    for start, end, chord in segments:
        first, last = int(max(0.0, start - fade / 2) * rate), int(min(duration, end + fade / 2) * rate)
        shape = []
        for index in range(first, last):
            t = index / rate
            level = smooth((t - (start - fade / 2)) / fade) if start > 0 else smooth(t / 2.5)
            level *= 1 - smooth((t - (end - fade / 2)) / fade) if end < duration else 1 - smooth((t - (duration - 3.5)) / 3.5)
            shape.append(level)
        for voice, name in enumerate(chord.split()):
            gain = 0.014 * (1.0, 0.8, 0.65, 0.55)[voice]  # felt more than heard: the cues carry the track
            for side, detune in ((bus.left, 0.9988), (bus.right, 1.0012)):
                turn = cmath.exp(1j * TAU * note(name) * detune / rate)
                z = cmath.exp(1j * rng.uniform(0, TAU))
                for offset, index in enumerate(range(first, last)):
                    z *= turn
                    side[index] += gain * shape[offset] * (z.imag + 0.15 * (z * z).imag)


# ── the soundtrack ──────────────────────────────────────────────────────────────────────────


def soundtrack(cues: dict, folder: Path) -> None:
    """Write the stems for `mix_onto`: dry.wav, room.wav (gets reverb), pad.wav and ir.wav."""
    duration = cues["duration"]
    dry, room, bed = Bus(duration), Bus(duration), Bus(duration, PAD_RATE)
    keys = [key_click() for _ in range(16)]
    enter = enter_key()
    ticks = [pass_tick(1760 * rng.uniform(0.97, 1.03)) for _ in range(4)]
    soft_tick = pass_tick(2350)
    failed, gate, chime, tick = fail_note(), gate_failed(), success_chime(), code_tick()

    events = cues["cues"]
    for cue in events:
        t, kind = cue["t"], cue["kind"]
        if kind == "key":
            sample, jitter, gain = rng.choice(keys), rng.uniform(0, 0.006), rng.uniform(0.15, 0.25)
            dry.add(sample, t + jitter, gain, rng.uniform(-0.12, 0.12))
            room.add(sample, t + jitter, gain * 0.12)
        elif kind == "enter":
            dry.add(enter, t, 0.32)
            room.add(enter, t, 0.05)
        elif kind == "step":
            room.add(pop(STEP_NOTES[cue["index"] % len(STEP_NOTES)], 0.07), t, 0.2)
        elif kind == "code":
            dry.add(tick, t, 0.05, 0.2)
        elif kind == "chip":
            column = cue["index"] % 5
            room.add(pop(CHIP_NOTES[cue["index"] % len(CHIP_NOTES)], 0.05), t, 0.13, -0.4 + 0.2 * column)
        elif kind == "question":
            room.add(chord_bloom(["B2", "F#3", "D4"], 1.1, None), t, 0.26)
        elif kind == "brand":
            room.add(chord_bloom(["D3", "A3", "E4", "F#4", "A4"], 1.6, "D2"), t, 0.42)
        elif kind == "ci-red":
            room.add(failed, t, 0.26)
        elif kind == "ci-green":
            room.add(chime, t, 0.45)
        elif kind == "end":
            room.add(chord_bloom(["D3", "A3", "F#4", "A4", "E5"], 2.2, "D2"), t, 0.45)
        elif kind == "scene" and cue["scene"] not in ("brand", "end"):
            left, right = whoosh()
            room.add_stereo(left, right, t - 0.3, 0.07)

    # Result lines arrive in bursts. A burst that holds the summary gets one sound; otherwise
    # each line gets its own, spread out a little so a burst reads as a quick run of ticks.
    lines = [cue for cue in events if cue["kind"] == "line"]
    bursts: list[list[dict]] = []
    for cue in lines:
        if bursts and cue["t"] - bursts[-1][0]["t"] < 0.03:
            bursts[-1].append(cue)
        else:
            bursts.append([cue])
    for burst in bursts:
        t, texts = burst[0]["t"], [cue["text"] for cue in burst]
        if any("Gate failed" in text for text in texts):
            room.add(gate, t, 0.36)
            continue
        if any("Gate passed" in text for text in texts):
            room.add(chime, t, 0.4)
            continue
        position = 0
        for text in texts:
            at = t + position * 0.045
            if text.startswith("✓"):
                dry.add(rng.choice(ticks), at, 0.16, 0.05)
                room.add(ticks[0], at, 0.03)
            elif text.startswith("✗"):
                room.add(failed, at, 0.17 if "watch" in text else 0.28)
            elif text.startswith("created"):
                dry.add(soft_tick, at, 0.09, 0.05)
            else:
                continue
            position += 1

    segments, times = [], {"hook": 0.0, **{cue["scene"]: cue["t"] for cue in events if cue["kind"] == "scene"}}
    times.update({cue["kind"]: cue["t"] for cue in events if cue["kind"] == "ci-green"})
    starts = [(times[part], chord) for part, chord in CHORDS if part in times]
    for number, (start, chord) in enumerate(starts):
        end = starts[number + 1][0] if number + 1 < len(starts) else duration
        segments.append((start, end, chord))
    pad(bed, segments, duration)

    dry.write(folder / "dry.wav")
    room.write(folder / "room.wav")
    bed.write(folder / "pad.wav")
    impulse_response().write(folder / "ir.wav")


GRAPH = (
    "[3:a]aresample=48000[pad];"
    "[2:a][pad]amix=inputs=2:normalize=0,asplit=2[send][wet];"
    "[wet][4:a]afir[verb];"
    "[1:a][send][verb]amix=inputs=3:weights='1 1 0.45':normalize=0,highpass=f=30"
)


def ffmpeg(*arguments: str) -> str:
    done = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-y", *arguments], capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(done.stderr[-2000:])
    return done.stderr


def mix_onto(folder: Path, video: Path, output: Path, duration: float) -> None:
    """Mix the stems, bring them to LOUDNESS with one fixed gain, and put the result on the video.

    A fixed gain keeps the mix as it was made; the limiter only shaves the odd peak above PEAK.
    """
    stems = [str(folder / name) for name in ("dry.wav", "room.wav", "pad.wav", "ir.wav")]
    inputs = [item for path in stems for item in ("-i", path)]
    report = ffmpeg("-f", "lavfi", "-i", "anullsrc", *inputs, "-filter_complex", f"{GRAPH},ebur128=peak=true", "-f", "null", "-")
    summary = report[report.rindex("Summary:"):]
    loudness = float(summary.split("I:")[1].split("LUFS")[0])
    peak = float(summary.split("Peak:")[1].split("dBFS")[0])
    gain = min(LOUDNESS - loudness, PEAK - peak + 3)  # let the limiter take at most 3 dB off a peak
    level = (
        f"volume={gain:.2f}dB,alimiter=limit={10 ** (PEAK / 20):.3f}:level=0:latency=1,"
        f"afade=t=out:st={duration - 1.2:.2f}:d=1.2"
    )
    ffmpeg(
        "-i", str(video), *inputs, "-filter_complex", f"{GRAPH},{level}[out]",
        "-map", "0:v", "-map", "[out]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-shortest", "-movflags", "+faststart", str(output),
    )


def main() -> None:
    cue_file, video, output = (Path(argument) for argument in sys.argv[1:4])
    cues = json.loads(cue_file.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="roadtest-sound-") as folder:
        soundtrack(cues, Path(folder))
        mix_onto(Path(folder), video, output, cues["duration"])
    print(f"{output}: soundtrack added")


if __name__ == "__main__":
    main()

"""Pure construction of the AudioPostProfile FFmpeg graph (contract 4.1, 4.2).

Everything here is a function of typed, already-validated values. No caller string is ever
interpolated into the graph: stage parameters are numbers or members of a closed
enumeration (`orchestrator.domain.entities.AUDIO_STAGE_PARAMS`), and numbers are rendered
by `_number`, so the text is identical for identical inputs on every run.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from orchestrator.domain.entities import AudioPostProfile, AudioStage
from orchestrator.domain.seeds import derive_audio_seed

#: Everything runs at this rate until the encode.
PROCESSING_RATE = 22050
#: FR-08's delivery loudness, applied to every profile (loudness is not a profile field).
TARGET_LUFS = -18.0
TARGET_TRUE_PEAK = -1.0
TARGET_LRA = 11.0
#: AT-24: the encoded file's integrated loudness must land this close to the target.
LOUDNESS_TOLERANCE_LU = 0.5

NOISE_STAGE = "noise"

_LOUDNORM_TARGET = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TRUE_PEAK:g}:LRA={TARGET_LRA:g}"
#: Pass 1 only measures; its graph differs from pass 2's by this final filter.
LOUDNORM_MEASURE = f"{_LOUDNORM_TARGET}:print_format=json"


class LoudnessMeasurementError(ValueError):
    """FFmpeg's loudnorm report could not be read, or described a take it cannot normalise."""


@dataclass(frozen=True)
class LoudnormMeasurement:
    """The pass-1 values pass 2 is given. Kept as FFmpeg reported them, numerically."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    def as_record(self) -> dict[str, float]:
        return {"input_i": self.input_i, "input_tp": self.input_tp, "input_lra": self.input_lra,
                "input_thresh": self.input_thresh, "target_offset": self.target_offset}

    @property
    def filter_text(self) -> str:
        return (f"{_LOUDNORM_TARGET}:measured_I={_number(self.input_i)}"
                f":measured_TP={_number(self.input_tp)}:measured_LRA={_number(self.input_lra)}"
                f":measured_thresh={_number(self.input_thresh)}"
                f":offset={_number(self.target_offset)}:linear=true")


_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}")


def parse_loudnorm_report(stderr: str) -> LoudnormMeasurement:
    """Read the JSON block loudnorm prints to stderr when `print_format=json`."""
    blocks = _JSON_BLOCK.findall(stderr)
    if not blocks:
        raise LoudnessMeasurementError("loudnorm printed no measurement block")
    try:
        raw = json.loads(blocks[-1])
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        if any(isinstance(raw[key], bool) for key in keys):
            raise TypeError("a loudnorm measurement is a boolean, not a number")
        values = {key: float(raw[key]) for key in keys}
    except (KeyError, TypeError, ValueError) as exc:
        raise LoudnessMeasurementError(f"loudnorm measurement block is unreadable: {exc}") from exc
    if not all(math.isfinite(value) for value in values.values()):
        raise LoudnessMeasurementError(
            "loudnorm measured a non-finite value; the take is silent or unmeasurable")
    return LoudnormMeasurement(**values)


_INTEGRATED = re.compile(r"^\s*I:\s+(\S+)\s+LUFS\s*$", re.MULTILINE)


def parse_integrated_loudness(stderr: str) -> float:
    """The `I:` line of the final `ebur128` summary, in LUFS.

    Only the text after the last "Summary:" marker is read, and exactly one `I:` line must
    be there: an earlier valid reading can never stand in for a malformed final one.
    """
    marker = stderr.rfind("Summary:")
    if marker < 0:
        raise LoudnessMeasurementError("ebur128 printed no summary")
    found = _INTEGRATED.findall(stderr[marker:])
    if len(found) != 1:
        raise LoudnessMeasurementError(
            f"ebur128 summary has {len(found)} integrated-loudness lines; expected exactly one")
    try:
        value = float(found[0])
    except ValueError as exc:
        raise LoudnessMeasurementError(f"ebur128 integrated loudness is unreadable: {found[0]!r}") from exc
    if not math.isfinite(value):
        raise LoudnessMeasurementError(
            f"the encoded take is silent or unmeasurable: integrated loudness is {found[0]}")
    return value


def _number(value: float) -> str:
    """Shortest round-trip text of a finite number: stable, and never an unquoted surprise."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"cannot render non-finite number {value!r} into a filter graph")
    return repr(number)


def noise_seed(profile: AudioPostProfile, take_asset_id: str) -> int | None:
    """The seeded noise stage's seed, or None when the profile has no noise."""
    if profile.noise is None:
        return None
    return derive_audio_seed(profile.name, profile.semver, NOISE_STAGE, take_asset_id)


def stage_text(stage: AudioStage) -> str:
    """One validated stage as FFmpeg filter text."""
    p = stage.params
    match stage.filter:
        case "highpass" | "lowpass":
            return f"{stage.filter}=f={p['f']}:p={p['poles']}"
        case "volume":
            return f"volume={_number(p['gain_db'])}dB"
        case "asoftclip":
            return f"asoftclip=type={p['type']}:threshold={_number(p['threshold'])}"
        case "aecho":
            delays = "|".join(str(delay) for delay in p["delays_ms"])
            decays = "|".join(_number(decay) for decay in p["decays"])
            return f"aecho={_number(p['in_gain'])}:{_number(p['out_gain'])}:{delays}:{decays}"
        case "acompressor":
            return (f"acompressor=threshold={_number(p['threshold'])}:ratio={_number(p['ratio'])}"
                    f":attack={_number(p['attack'])}:release={_number(p['release'])}"
                    f":makeup={_number(p['makeup'])}")
        case "acrusher":
            return (f"acrusher=level_in={_number(p['level_in'])}:level_out={_number(p['level_out'])}"
                    f":bits={_number(p['bits'])}:mode={p['mode']}:aa={_number(p['aa'])}"
                    f":samples={_number(p['samples'])}")
        case "vibrato" | "tremolo":
            return f"{stage.filter}=f={_number(p['f'])}:d={_number(p['d'])}"
        case "aresample":
            return f"aresample={p['sample_rate']}"
    raise ValueError(f"filter {stage.filter!r} is not in the audio stage allowlist")


def _voice_chain(profile: AudioPostProfile) -> str:
    texts = [stage_text(stage) for stage in profile.voice_stages]
    rate = PROCESSING_RATE
    for stage in profile.voice_stages:
        if stage.filter == "aresample":
            rate = stage.params["sample_rate"]
    if rate != PROCESSING_RATE:
        # An aresample stage is an intermediate rate (for aliasing); everything after the
        # voice chain runs at the processing rate (contract 4.1), so return to it.
        texts.append(f"aresample={PROCESSING_RATE}")
    return ",".join(["aformat=channel_layouts=mono", f"aresample={PROCESSING_RATE}", *texts])


def build_filter_graph(profile: AudioPostProfile, *, seed: int | None, loudnorm: str) -> str:
    """The whole filter_complex, ending in `loudnorm` and labelled `[out]`.

    `seed` is the noise seed and must be given exactly when the profile has noise.
    """
    if (profile.noise is None) != (seed is None):
        raise ValueError("a noise seed is required exactly when the profile has noise")
    parts = [f"[0:a]{_voice_chain(profile)}[v]"]
    mix = ["[v]"]
    if profile.noise is not None:
        parts.append(f"anoisesrc=color={profile.noise.color}:amplitude={_number(profile.noise.amplitude)}"
                     f":seed={seed}:sample_rate={PROCESSING_RATE}[n]")
        mix.append("[n]")
    if profile.hum is not None:
        hum = profile.hum
        components = [(hum.frequency_hz, hum.amplitude)] + [
            (hum.frequency_hz * harmonic.multiple, hum.amplitude * harmonic.relative)
            for harmonic in hum.harmonics
        ]
        for index, (hertz, amplitude) in enumerate(components):
            parts.append(f"sine=f={hertz}:sample_rate={PROCESSING_RATE},volume={_number(amplitude)}[h{index}]")
            mix.append(f"[h{index}]")
    if len(mix) > 1:
        parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0,{loudnorm}[out]")
    else:
        parts.append(f"[v]{loudnorm}[out]")
    return ";".join(parts)


def stage_records(profile: AudioPostProfile) -> list[dict[str, Any]]:
    """The full stage list as recorded in a report."""
    return [stage.model_dump(mode="json") for stage in profile.voice_stages]

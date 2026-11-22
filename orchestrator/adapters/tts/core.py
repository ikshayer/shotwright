"""Shared request, result and filesystem model for the Piper and IndexTTS-2 adapters.

Nothing here starts a process or imports a model runtime. The two engines differ in how
they synthesise, not in what a run leaves behind, so the run layout, the WAV check and
the report plumbing are written once.
"""

from __future__ import annotations

import hashlib
import json
import os
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.errors import JobError, JobErrorKind

#: Fixed, repository-relative runtime layout. Deliberately not settings (contract 1).
WORK_DIRECTORY = "spikes/05_voice/_work"
PIPER_PYTHON = f"{WORK_DIRECTORY}/piper_env/Scripts/python.exe"
PIPER_SITE_PACKAGES = f"{WORK_DIRECTORY}/piper_env/Lib/site-packages"
INDEXTTS2_REPOSITORY = f"{WORK_DIRECTORY}/index-tts"
INDEXTTS2_PYTHON = f"{INDEXTTS2_REPOSITORY}/.venv/Scripts/python.exe"
INDEXTTS2_CHECKPOINTS = f"{INDEXTTS2_REPOSITORY}/checkpoints"
INDEXTTS2_CONFIG = f"{INDEXTTS2_CHECKPOINTS}/config.yaml"

INDEXTTS2_COMMIT = "d9e41aac89fd00b3d71497fddb287b7f24613712"
INDEXTTS2_CONFIG_SHA256 = "ea9c2815ecc3874577c7ac158b97248c027250e5b05972fbbb1216b6d6539081"

#: Auxiliary models IndexTTS-2 loads from `checkpoints/`; the run refuses rather than
#: download any that is absent (FR-16).
INDEXTTS2_AUXILIARY_FILES = (
    "hf_cache/w2v-bert-2.0/config.json",
    "hf_cache/w2v-bert-2.0/model.safetensors",
    "hf_cache/semantic_codec_model.safetensors",
    "hf_cache/campplus_cn_common.bin",
    "hf_cache/bigvgan/config.json",
    "hf_cache/bigvgan/bigvgan_generator.pt",
    "qwen0.6bemo4-merge/config.json",
)


class IndexTTS2WeightPin(BaseModel):
    """Immutable identity and expected bytes for one loaded checkpoint file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    size: StrictInt = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(min_length=1)


_INDEXTEAM_PIN_SOURCE = "IndexTeam/IndexTTS-2@740dcaff396282ffb241903d150ac011cd4b1ede"
_LOCAL_PIN_SOURCE = "local, size matches publisher (no published SHA-256)"

# Files loaded by IndexTTS2's constructor and the optional Qwen emotion-text path.
# Published SHA-256 values are used where available; local-only values are for small
# git-blob assets whose publisher does not publish a SHA-256.
INDEXTTS2_WEIGHT_PINS = (
    IndexTTS2WeightPin(
        path="gpt.pth", size=3484663079,
        sha256="baaaeb8b56328da81731dc540a85a7dee32eca9da28f174b05757cb651c602a4",
        source=f"{_INDEXTEAM_PIN_SOURCE}:gpt.pth",
    ),
    IndexTTS2WeightPin(
        path="s2mel.pth", size=1202198223,
        sha256="aae1bb12017cbb47e7a5ce537fc82f40b6b1deb71acdb9b8f25686f32714b636",
        source=f"{_INDEXTEAM_PIN_SOURCE}:s2mel.pth",
    ),
    IndexTTS2WeightPin(
        path="bpe.model", size=475997,
        sha256="b2a5ce8090d32da3642cc4f81fdc996376bc6dd3f4cd5e3d165f71120d9f2bc8",
        source=f"{_INDEXTEAM_PIN_SOURCE}:bpe.model",
    ),
    IndexTTS2WeightPin(
        path="feat1.pt", size=57170,
        sha256="f219cb447d80216ba615666da2ff8d63ac544eee26657f3a7b278692bf7a67c4",
        source=f"{_INDEXTEAM_PIN_SOURCE}:feat1.pt",
    ),
    IndexTTS2WeightPin(
        path="feat2.pt", size=374866,
        sha256="9c4292e96dee535aea9a6206e9a0c856dd578dde9212acdb16dd3ada4d12bf80",
        source=f"{_INDEXTEAM_PIN_SOURCE}:feat2.pt",
    ),
    IndexTTS2WeightPin(
        path="wav2vec2bert_stats.pt", size=9343,
        sha256="c9c176c2b8850ab2e3ba828bbfa969deaf4566ce55db5f2687b8430b87526ad2",
        source=f"{_INDEXTEAM_PIN_SOURCE}:wav2vec2bert_stats.pt",
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/model.safetensors", size=1192135096,
        sha256="11293257a8df593c154a8ecd5fc039f3076de35411e35f06d41b471e136f6641",
        source=f"{_INDEXTEAM_PIN_SOURCE}:qwen0.6bemo4-merge/model.safetensors",
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/tokenizer.json", size=11422654,
        sha256="aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
        source=f"{_INDEXTEAM_PIN_SOURCE}:qwen0.6bemo4-merge/tokenizer.json",
    ),
    IndexTTS2WeightPin(
        path="hf_cache/w2v-bert-2.0/model.safetensors", size=2322063736,
        sha256="eb890c9660ed6e3414b6812e27257b8ce5454365d5490d3ad581ea60b93be043",
        source="facebook/w2v-bert-2.0@da985ba0987f70aaeb84a80f2851cfac8c697a7b",
    ),
    IndexTTS2WeightPin(
        path="hf_cache/semantic_codec_model.safetensors", size=177183712,
        sha256="ec947271175d8cad75ec37e83aa487e27c97a0f72a303393772da5ffa84bddf2",
        source=("amphion/MaskGCT@265c6cef07625665d0c28d2faafb1415562379dc:"
                "semantic_codec/model.safetensors"),
    ),
    IndexTTS2WeightPin(
        path="hf_cache/campplus_cn_common.bin", size=28036335,
        sha256="3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8",
        source="funasr/campplus@e4b6ede7ce16997aff4ae69fbca1f0175e2afede",
    ),
    IndexTTS2WeightPin(
        path="hf_cache/bigvgan/bigvgan_generator.pt", size=449228171,
        sha256="e95ba25972d3de0628d99cd156e9315a9c018899bf739988959ebe3544080ced",
        source=("nvidia/bigvgan_v2_22khz_80band_256x@"
                "633ff708ed5b74903e86ff1298cf4a98e921c513"),
    ),
    IndexTTS2WeightPin(
        path="config.yaml", size=2882,
        sha256="ea9c2815ecc3874577c7ac158b97248c027250e5b05972fbbb1216b6d6539081",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="hf_cache/w2v-bert-2.0/config.json", size=1874,
        sha256="f5572bd5998b68182e9c328a43127ed21fed687f6910497136b91a4e3b0e3675",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="hf_cache/w2v-bert-2.0/preprocessor_config.json", size=275,
        sha256="8e6281aad64f97e40534135a59dcc5d33571efae376f2a25adf5551951897ab4",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="hf_cache/bigvgan/config.json", size=1405,
        sha256="88a1f47acf747db0b21e97a389d838566147f7a5464583ff5c8d819d870f03ee",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/config.json", size=727,
        sha256="2e04bc223dfb548dde52faebe2a819964523ff749cbe26467c49380aa371594f",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/generation_config.json", size=117,
        sha256="6453260cdb41552215d87400ab399a760f25509ec2860ceb07817b195651626d",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/tokenizer_config.json", size=5433,
        sha256="147bae7844ddfb8767c1f9cc0f505d4a26839bd6fbb0656d6d6bc17c82e9108a",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/added_tokens.json", size=707,
        sha256="c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/special_tokens_map.json", size=616,
        sha256="6676f091c8bc4d1b50146427cfde92073402866b87b6e39223227931b70083e9",
        source=_LOCAL_PIN_SOURCE,
    ),
    IndexTTS2WeightPin(
        path="qwen0.6bemo4-merge/chat_template.jinja", size=550,
        sha256="a13337ea164ac47b2c1e644b6338d5e7e91cdcb58218511899c083112472730c",
        source=_LOCAL_PIN_SOURCE,
    ),
)

INDEXTTS2_MODEL_LICENSE = (
    "bilibili Model Use License (IndexTTS-2; royalty-free below 100M MAU / RMB 1bn)"
)
#: An open creator decision (contract 5.3). Left unresolved on purpose: `credits.py`
#: reports any licence other than CC0-1.0 / CC-BY-4.0 as a publication blocker.
TAKE_LICENSE_PENDING = "Generated speech; rights classification pending creator decision"

MAX_TEXT_BYTES = 8192
MAX_EMOTION_DESCRIPTION_CHARS = 500

_ID = r"^[A-Za-z0-9_-]+$"
_CATALOG_PATH = Path(__file__).with_name("piper_voices.json")


def repository_root() -> ProjectRoot:
    return ProjectRoot(Path(__file__).resolve().parents[3])


class TtsRequestError(ValueError):
    """An invalid speech request. Mapped to 422 by `orchestrator/api/errors.py`."""


class TtsSetupError(RuntimeError):
    """A speech engine is disabled or its runtime is missing. Mapped to 503."""


# --- Piper voice catalog ----------------------------------------------------------------


class PiperVoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    voice_id: str = Field(pattern=_ID)
    gender: str = Field(min_length=1)
    license: str = Field(min_length=1)
    license_source: str = Field(min_length=1)
    #: Copied onto every take: a voice whose licence forbids commercial use is usable for
    #: internal evaluation only, and `credits.py` blocks publication of such takes.
    reuse_scope: str = Field(min_length=1)
    onnx_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    onnx_bytes: StrictInt = Field(gt=0)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_rate: StrictInt = Field(gt=0)

    @property
    def onnx_relative(self) -> str:
        return f"{WORK_DIRECTORY}/{self.voice_id}.onnx"

    @property
    def config_relative(self) -> str:
        return f"{WORK_DIRECTORY}/{self.voice_id}.onnx.json"


def load_piper_catalog(path: Path = _CATALOG_PATH) -> dict[str, PiperVoice]:
    """The committed catalog is the only source of allowed voices (contract 1.1)."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError(f"unsupported Piper voice catalog schema in {path}")
    voices = [PiperVoice.model_validate(entry) for entry in document["voices"]]
    catalog = {voice.voice_id: voice for voice in voices}
    if len(catalog) != len(voices):
        raise ValueError(f"duplicate voice_id in {path}")
    return catalog


# --- emotion steering (IndexTTS-2) ------------------------------------------------------

#: IndexTTS-2's `emo_vector` order. The model reads positions, not names.
EMOTION_VECTOR_ORDER = (
    "happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm",
)


class EmotionNone(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["none"]


class EmotionVector(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["vector"]
    happy: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    angry: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    sad: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    afraid: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    disgusted: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    melancholic: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    surprised: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    calm: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @field_validator(*EMOTION_VECTOR_ORDER, mode="before")
    @classmethod
    def _number_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("emotion weights must be numbers, not booleans")
        return value

    @model_validator(mode="after")
    def _some_emotion(self) -> EmotionVector:
        if not any(getattr(self, name) > 0 for name in EMOTION_VECTOR_ORDER):
            raise ValueError("an emotion vector needs at least one weight greater than 0; use mode 'none'")
        return self

    def as_list(self) -> list[float]:
        return [float(getattr(self, name)) for name in EMOTION_VECTOR_ORDER]


class EmotionText(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["text"]
    description: str = Field(max_length=MAX_EMOTION_DESCRIPTION_CHARS)

    @field_validator("description")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an emotion description must not be blank")
        return value


class EmotionReference(BaseModel):
    """Emotion taken from a recorded performance; the voice still comes from the speaker.

    IndexTTS-2's `emo_audio_prompt`: the clip may be a different person than the speaker
    reference, so a creator can perform the emotion without lending their voice. Only the
    first 15 s of the clip are used (IndexTTS-2 truncates there).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["reference"]
    emotion_asset_id: str = Field(pattern=_ID)
    #: IndexTTS-2's `emo_alpha`: how strongly the clip's emotion is applied.
    strength: float = Field(1.0, gt=0.0, le=1.0, allow_inf_nan=False)

    @field_validator("strength", mode="before")
    @classmethod
    def _number_not_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("strength must be a number, not a boolean")
        return value


Emotion = Annotated[EmotionNone | EmotionVector | EmotionText | EmotionReference,
                    Field(discriminator="mode")]


class VoiceReferenceBinding(BaseModel):
    """A registered voice clip as verified at submission, re-checked before use."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    asset_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: An emotion reference may also be a `licensed_recording`; a speaker never may.
    consent: Literal["synthetic", "creator_own_voice", "licensed_for_cloning", "licensed_recording"]
    consent_basis: str | None = None
    reuse_scope: str = Field(min_length=1)


# --- requests ---------------------------------------------------------------------------


def validate_speech_text(text: str) -> str:
    """The text is used byte-for-byte; this only decides whether it may be used at all."""
    if not any(not character.isspace() for character in text):
        raise TtsRequestError("text must contain a non-whitespace character")
    size = len(text.encode("utf-8"))
    if size > MAX_TEXT_BYTES:
        raise TtsRequestError(f"text is {size} UTF-8 bytes; the limit is {MAX_TEXT_BYTES}")
    return text


class _SpeechRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str = Field(pattern=_ID)
    shot_version: StrictInt = Field(ge=1)
    run_id: str = Field(pattern=_ID)
    text: str

    @field_validator("text")
    @classmethod
    def _usable_text(cls, value: str) -> str:
        try:
            return validate_speech_text(value)
        except TtsRequestError as exc:
            raise ValueError(str(exc)) from exc

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def run_directory(self) -> str:
        return f"generated/tts/{self.shot_id}/v{self.shot_version:04d}/{self.run_id}"


class PiperRequest(_SpeechRequest):
    voice_id: str = Field(pattern=_ID)
    length_scale: float | None = Field(None, ge=0.5, le=2.0, allow_inf_nan=False)
    sentence_silence_seconds: float | None = Field(None, ge=0.0, le=2.0, allow_inf_nan=False)

    @property
    def settings_record(self) -> dict[str, str | bool | int | float]:
        record: dict[str, str | bool | int | float] = {"voice_id": self.voice_id}
        if self.length_scale is not None:
            record["length_scale"] = self.length_scale
        if self.sentence_silence_seconds is not None:
            record["sentence_silence_seconds"] = self.sentence_silence_seconds
        return record


class IndexTts2Request(_SpeechRequest):
    speaker_asset_id: str = Field(min_length=1)
    speaker_path: str = Field(min_length=1)
    speaker_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    speaker_consent: Literal["synthetic", "creator_own_voice", "licensed_for_cloning"]
    speaker_consent_basis: str | None = None
    #: The reference's reuse scope; a take cloned from a restricted voice inherits it.
    #: None only for a request built before the field existed; registration then reads the
    #: reference's current scope rather than assuming a publishable one.
    speaker_reuse_scope: str | None = Field(None, min_length=1)
    emotion: Emotion
    #: The verified emotion clip; present exactly when `emotion.mode == "reference"`.
    emotion_reference: VoiceReferenceBinding | None = None

    @model_validator(mode="after")
    def _reference_bound_when_used(self) -> IndexTts2Request:
        uses_reference = isinstance(self.emotion, EmotionReference)
        if uses_reference != (self.emotion_reference is not None):
            raise ValueError("emotion_reference is required for, and only for, emotion mode 'reference'")
        if uses_reference and self.emotion_reference.asset_id != self.emotion.emotion_asset_id:
            raise ValueError("emotion_reference does not match emotion.emotion_asset_id")
        return self

    @property
    def settings_record(self) -> dict[str, str | bool | int | float]:
        record: dict[str, str | bool | int | float] = {
            "speaker_asset_id": self.speaker_asset_id, "emotion_mode": self.emotion.mode,
        }
        if isinstance(self.emotion, EmotionVector):
            for name in EMOTION_VECTOR_ORDER:
                record[f"emotion_{name}"] = getattr(self.emotion, name)
        elif isinstance(self.emotion, EmotionText):
            record["emotion_description"] = self.emotion.description
        elif isinstance(self.emotion, EmotionReference):
            record["emotion_asset_id"] = self.emotion.emotion_asset_id
            record["emotion_strength"] = self.emotion.strength
        return record


class TtsResult(BaseModel):
    """What a finished run left in the project. Paths are project-relative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: Literal["piper", "indextts2"]
    run_id: str
    tool_version: str
    take_path: str
    take_sha256: str
    text_path: str
    text_sha256: str
    report_path: str
    report_sha256: str
    log_path: str
    sample_rate: int
    channels: int
    duration_seconds: float


# --- run files --------------------------------------------------------------------------


class RunFiles:
    """The contract 5.1 layout for one run, written only through the ProjectRoot."""

    def __init__(self, root: ProjectRoot, request: _SpeechRequest) -> None:
        self.root = root
        self.directory = request.run_directory
        self.take = f"{self.directory}/take.wav"
        self.temporary_take = f"{self.directory}/take.partial.wav"
        self.text = f"{self.directory}/text.txt"
        self.report = f"{self.directory}/report.json"
        self.stderr = f"{self.directory}/logs/stderr.txt"
        self.stdout = f"{self.directory}/logs/stdout.txt"

    def prepare(self, text: str) -> None:
        if self.root.resolve(self.directory).exists():
            raise FileExistsError(f"speech run already exists: {self.directory}")
        self.root.make_dir(f"{self.directory}/logs")
        self.root.write_bytes(self.text, text.encode("utf-8"))

    def write_report(self, report: dict[str, Any]) -> str:
        """Write report.json and return its SHA-256."""
        data = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n")
        encoded = data.encode("utf-8")
        self.root.write_bytes(self.report, encoded)
        return hashlib.sha256(encoded).hexdigest()

    def discard_partial_take(self) -> None:
        self.root.resolve(self.temporary_take).unlink(missing_ok=True)

    def publish_take(self) -> None:
        os.replace(self.root.resolve(self.temporary_take), self.root.resolve(self.take))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class WavFacts(BaseModel):
    model_config = ConfigDict(frozen=True)
    sample_rate: int
    channels: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate


def inspect_wav(path: Path, *, job: Any = None) -> WavFacts:
    """Open with the stdlib `wave` module: PCM, 1 or 2 channels, a rate, and frames > 0."""
    try:
        with wave.open(str(path), "rb") as reader:
            facts = WavFacts(sample_rate=reader.getframerate(), channels=reader.getnchannels(),
                             frames=reader.getnframes())
    except (wave.Error, EOFError, OSError) as exc:
        raise _corrupt(f"synthesised audio is not a readable PCM WAV file: {exc}", job, exc) from exc
    if facts.channels not in (1, 2) or facts.sample_rate <= 0 or facts.frames <= 0:
        raise _corrupt(
            f"synthesised audio is unusable: {facts.channels} channel(s), "
            f"{facts.sample_rate} Hz, {facts.frames} frame(s)", job, None,
        )
    return facts


def _corrupt(message: str, job: Any, cause: BaseException | None) -> JobError:
    return JobError(JobErrorKind.CORRUPT_ARTIFACT, message,
                    job_id=getattr(job, "job_id", None), shot_id=getattr(job, "shot_id", None),
                    run_id=getattr(job, "run_id", None), cause=cause)


def job_error(kind: JobErrorKind, message: str, job: Any, cause: BaseException | None = None) -> JobError:
    return JobError(kind, message, job_id=getattr(job, "job_id", None),
                    shot_id=getattr(job, "shot_id", None), run_id=getattr(job, "run_id", None),
                    cause=cause)


def last_lines(data: bytes, count: int = 8) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    return "\n".join(text.splitlines()[-count:])


def require_cancel_clear(cancel: Callable[[], bool] | None, job: Any, message: str) -> None:
    if cancel is not None and cancel():
        raise job_error(JobErrorKind.CANCELLED, message, job)

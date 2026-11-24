"""Validated ``settings.toml`` (FR-23, AT-16).

Every configurable value in the PRD is a named field with a stated default. Loading
fails at startup with a ``SettingsError`` listing each offending key and what to do
about it. Secrets never live here: credentials are *names of environment variables*, and
unknown keys are rejected so a pasted ``api_key = "..."`` is an error, not a leak.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from orchestrator.platform.containment import UploadLimits

_ENV_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class BinariesSettings(_Section):
    blender_exe: Path = Field(Path(r"D:\Blender\blender.exe"), description="Blender executable.")
    blender_vulkan_device: str | None = Field(
        None,
        pattern=r"^[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}/[0-9]+$",
        description=(
            "Optional Blender Vulkan adapter selector as listed by Blender "
            "(hexadecimal vendor/device IDs and decimal index). Disables device fallback."
        ),
    )
    ffmpeg_exe: Path = Field(Path("ffmpeg"), description="FFmpeg executable; a bare name is looked up on PATH.")
    ntsc_pipe_exe: Path = Field(
        Path(__file__).resolve().parents[2] / "tools" / "bin" / "ntsc-pipe.exe",
        description=(
            "Pinned local ntsc-rs frame processor built by tools/build_ntsc_pipe.ps1; "
            "required only when CameraProfile.analog_artifacts is active."
        ),
    )
    comfyui_root: Path = Field(
        Path(r"D:\ComfyUI_0.44.0\ComfyUI_windows_portable"), description="ComfyUI portable install directory."
    )
    comfyui_port: int = Field(8188, ge=1, le=65535, description="Port the orchestrator launches ComfyUI on.")


class ModelsSettings(_Section):
    extra_model_paths_file: Path | None = Field(
        None, description="extra_model_paths.yaml that lets weights live on another volume."
    )
    model_roots: list[Path] = Field(default_factory=list, description="Additional directories holding model weights.")


#: Names of the pinned still-image model profiles. The profile contents live with the
#: adapter (`orchestrator.adapters.comfyui.profiles`); only the names belong to settings.
ProfileName = Literal["z_image_realgen", "z_image", "flux2_klein"]


class ImageModelSettings(_Section):
    weights_dir: Path = Field(Path(r"D:\Work\pipeline-models"), description="Directory containing pinned image model weights.")
    default_profile: ProfileName = Field("z_image_realgen", description="Pinned still-image model profile used when callers omit models.")

    @field_validator("weights_dir")
    @classmethod
    def _absolute_weights_dir(cls, value: Path) -> Path:
        # Model pins must be absolute (ModelPin); a relative root would only fail later,
        # at composition, instead of when the settings are loaded.
        if not value.is_absolute():
            raise ValueError("weights_dir must be an absolute path")
        return value


class ServerSettings(_Section):
    host: str = Field("127.0.0.1", description="API bind address; must be loopback (FR-25).")
    # UNMEASURED PLACEHOLDER: 8765 is an arbitrary default, not a measured or spec-derived value.
    port: int = Field(8765, ge=1, le=65535, description="API port.")

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, host: str) -> str:
        if host == "localhost":
            return host
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError(
                f"{host!r} is not a loopback address; the API must bind to 127.0.0.1 (or ::1) per FR-25"
            )
        return host


class BudgetSettings(_Section):
    """FR-26 ceilings. Exceeding any hard-stops the job."""

    # UNMEASURED PLACEHOLDERS: every default in this block is an invented number, not a measured figure.
    # Replace each with a value measured on this hardware before relying on it.
    gpu_minutes_per_shot: float = Field(20.0, gt=0)
    retries_per_shot: int = Field(3, ge=0)
    gpu_minutes_per_episode: float = Field(240.0, gt=0)
    disk_ceiling_gb: float = Field(40.0, gt=0, description="Project size ceiling.")
    min_free_disk_gb: float = Field(10.0, ge=0, description="Hard threshold below which jobs are refused.")
    per_shot_wall_clock_seconds: float = Field(900.0, gt=0, description="AT-15 final-render wall-clock budget.")
    # UNMEASURED PLACEHOLDER: no cut encode has been timed on this machine.
    cut_assembly_wall_clock_seconds: float = Field(900.0, gt=0)


class RetentionSettings(_Section):
    unapproved_versions_per_shot: int = Field(3, ge=0, description="Latest N unapproved versions kept per shot.")
    purge_previews: bool = Field(True, description="Previews are purgeable.")
    hosted_metadata_max_age_days: int = Field(
        30, ge=1, le=30, description="Hosted-platform metadata is refreshed or purged within 30 days (section 6.2)."
    )


class ResourceSettings(_Section):
    # UNMEASURED PLACEHOLDERS: the three defaults below are invented numbers, not measured figures.
    # Replace each with a value measured on this hardware before relying on it.
    vram_floor_mb: int = Field(1024, ge=0, description="Device memory used must fall below this after a lease release.")
    vram_release_timeout_seconds: float = Field(30.0, gt=0)
    foreign_vram_allowance_mb: int | None = Field(
        None, ge=0, description="Unset means: use the idle VRAM baseline recorded by preflight (FR-24)."
    )
    ram_floor_gb: float = Field(4.0, ge=0, description="Free system RAM that must remain after loading a checkpoint.")
    # UNMEASURED PLACEHOLDER: configure from observed progress rates on this hardware.
    throughput_floor_per_second: float = Field(
        0.001, ge=0, description="Minimum job progress per second before the GPU stall watchdog fires."
    )
    startup_allowance_seconds: float = Field(
        150.0,
        ge=0,
        description=(
            "Derived watchdog bound, not a measurement: 150 s is just over 2x the measured ~74 s cold "
            "Blender shader/kernel cache cost (PRD §8.4), with allowance through the first frame."
        ),
    )

    @field_validator("throughput_floor_per_second", "startup_allowance_seconds")
    @classmethod
    def _watchdog_value_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("watchdog thresholds must be finite")
        return value


class AssetProviderSettings(_Section):
    user_agent: str = "cctv-pipeline/0.1"
    sketchfab_token_file: Path | None = None
    request_timeout_seconds: float = Field(30.0, gt=0)
    max_attempts: int = Field(3, ge=1, le=5)
    max_search_pages: int = Field(3, ge=1, le=3)
    max_archive_files: int = Field(128, ge=1)

    @field_validator("user_agent")
    @classmethod
    def _valid_provider_agent(cls, value: str) -> str:
        if "cctv-pipeline" not in value.casefold() or "\r" in value or "\n" in value:
            raise ValueError("user_agent must identify cctv-pipeline and contain no line breaks")
        return value

    @field_validator("request_timeout_seconds")
    @classmethod
    def _finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("request_timeout_seconds must be finite")
        return value


class MinimaxH3Settings(_Section):
    enabled: bool = False
    required_vram_gb: float | None = None
    required_ram_gb: float | None = None
    required_disk_gb: float | None = None
    startup_allowance_seconds: float = Field(600.0, gt=0)
    execution_timeout_seconds: float = Field(1800.0, gt=0)
    wall_clock_seconds: float = Field(2400.0, gt=0)
    throughput_floor_per_second: float | None = None

    @field_validator("required_vram_gb", "required_ram_gb", "required_disk_gb", "throughput_floor_per_second", mode="before")
    @classmethod
    def _strict_numeric_when_set(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("H3 reservations and throughput floor must be numeric values, not booleans or strings")
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("H3 reservations and throughput floor must be finite positive values")
        return value

    @model_validator(mode="after")
    def _enabled_is_explicit(self) -> MinimaxH3Settings:
        if self.enabled and any(value is None for value in (
            self.required_vram_gb, self.required_ram_gb, self.required_disk_gb,
            self.throughput_floor_per_second,
        )):
            raise ValueError("enabled MiniMax H3 requires explicit VRAM/RAM/disk reservations and throughput floor")
        return self


def _strict_finite_number(value: object, label: str) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number, not a boolean or string")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


class PiperSettings(_Section):
    """Piper (CPU) text-to-speech for processed-source voices. Disabled by default."""

    enabled: bool = False
    timeout_seconds: float = Field(120.0, gt=0, description="Wall clock for one synthesis subprocess.")

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _strict_timeout(cls, value: object) -> object:
        return _strict_finite_number(value, "piper.timeout_seconds")


class Indextts2Settings(_Section):
    """IndexTTS-2 (GPU) diegetic voice synthesis. Disabled by default.

    The reservations are operator admission policy, not measured model minima: no VRAM
    figure for IndexTTS-2 has been measured (spike 05, Finding 5 measured host RAM only).
    """

    enabled: bool = False
    required_vram_gb: float | None = None
    required_ram_gb: float | None = None
    required_disk_gb: float | None = None
    startup_allowance_seconds: float = Field(300.0, gt=0)
    execution_timeout_seconds: float = Field(900.0, gt=0)
    use_fp16: bool = True

    @field_validator("required_vram_gb", "required_ram_gb", "required_disk_gb", mode="before")
    @classmethod
    def _strict_numeric_when_set(cls, value: object) -> object:
        if value is None:
            return value
        _strict_finite_number(value, "IndexTTS-2 reservations")
        if value <= 0:  # type: ignore[operator]
            raise ValueError("IndexTTS-2 reservations must be finite positive values")
        return value

    @field_validator("startup_allowance_seconds", "execution_timeout_seconds", mode="before")
    @classmethod
    def _strict_timeouts(cls, value: object) -> object:
        return _strict_finite_number(value, "IndexTTS-2 timeouts")

    @model_validator(mode="after")
    def _enabled_is_explicit(self) -> Indextts2Settings:
        if self.enabled and any(value is None for value in (
            self.required_vram_gb, self.required_ram_gb, self.required_disk_gb,
        )):
            raise ValueError("enabled IndexTTS-2 requires explicit VRAM/RAM/disk reservations")
        return self


class QwenReviewSettings(_Section):
    """Admission and watchdog policy for queued advisory Qwen reviews."""

    required_vram_gb: float = Field(8.0, gt=0, description="Measured helper admission guard.")
    required_ram_gb: float = Field(6.0, gt=0, description="Measured helper admission guard.")
    required_disk_gb: float = Field(8.0, gt=0, description="Measured helper admission guard.")
    startup_allowance_seconds: float = Field(
        960.0, gt=0, description="One native batch can run for up to its 900-second deadline."
    )
    wall_clock_seconds: float = Field(
        1800.0, gt=0,
        description="A1 unmeasured placeholder; the existing GPU-minute ceiling remains binding.",
    )
    max_interval_seconds: float = Field(
        120.0, gt=0, description="A1 limit: five 25-second windows in two native batches."
    )

    @field_validator(
        "required_vram_gb", "required_ram_gb", "required_disk_gb",
        "startup_allowance_seconds", "wall_clock_seconds", "max_interval_seconds",
        mode="before",
    )
    @classmethod
    def _strict_qwen_review_numbers(cls, value: object) -> object:
        _strict_finite_number(value, "qwen_review settings")
        if value <= 0:  # type: ignore[operator]
            raise ValueError("Qwen review settings must be finite positive values")
        return value


class ApprovalSettings(_Section):
    #: D1. When false (default), an agent-authored Review is an opinion and approves nothing.
    agent_may_approve: bool = False


class Settings(_Section):
    _runtime_secrets: set[str] = PrivateAttr(default_factory=set)
    project_root: Path = Field(Path("projects"), description="Active project root; all writes are contained here.")
    use_windows_share_delete_reads: bool = Field(
        False,
        description=(
            "Opt into slower Windows CreateFileW reads requesting FILE_SHARE_DELETE. "
            "The measured Amendment 2 replacement experiment disproved the original "
            "rationale; this option is retained for evaluation and is not known to fix anything."
        ),
    )
    hosted_video_tier: Literal[1, 2, 3] = Field(1, description="Section 6.2 access tier; 1 = annotate only.")
    binaries: BinariesSettings = BinariesSettings()
    models: ModelsSettings = ModelsSettings()
    image_models: ImageModelSettings = ImageModelSettings()
    server: ServerSettings = ServerSettings()
    upload: UploadLimits = UploadLimits()
    approvals: ApprovalSettings = ApprovalSettings()
    budget: BudgetSettings = BudgetSettings()
    retention: RetentionSettings = RetentionSettings()
    resources: ResourceSettings = ResourceSettings()
    asset_providers: AssetProviderSettings = AssetProviderSettings()
    minimax_h3: MinimaxH3Settings = MinimaxH3Settings()
    piper: PiperSettings = PiperSettings()
    indextts2: Indextts2Settings = Indextts2Settings()
    qwen_review: QwenReviewSettings = QwenReviewSettings()
    credentials: dict[str, str] = Field(
        default_factory=dict,
        description="provider name -> NAME of the environment variable holding its secret. Never the secret.",
    )

    @field_validator("credentials")
    @classmethod
    def _credentials_are_env_var_names(cls, credentials: dict[str, str]) -> dict[str, str]:
        for provider, env_name in credentials.items():
            if not _ENV_VAR_NAME.fullmatch(env_name):
                raise ValueError(
                    f"value for {provider!r} must be the NAME of an environment variable "
                    "(letters, digits, underscore), not the secret itself; settings files must not contain secrets"
                )
        return credentials

    @model_validator(mode="after")
    def _disk_thresholds_consistent(self) -> Settings:
        if self.budget.min_free_disk_gb >= self.budget.disk_ceiling_gb:
            raise ValueError(
                "budget.min_free_disk_gb must be smaller than budget.disk_ceiling_gb "
                f"(got {self.budget.min_free_disk_gb} >= {self.budget.disk_ceiling_gb})"
            )
        return self

    def resolve_credential(self, provider: str) -> str:
        """Read a provider's secret from the environment at the moment of use."""
        if provider not in self.credentials:
            raise KeyError(f"No credential configured for {provider!r}; add it under [credentials] in settings.toml")
        env_name = self.credentials[provider]
        value = os.environ.get(env_name)
        if not value:
            raise KeyError(f"Environment variable {env_name} (credential for {provider!r}) is not set")
        return value

    def register_runtime_secret(self, value: str) -> None:
        """Make an in-memory runtime token available to diagnostic redaction."""
        if value:
            self._runtime_secrets.add(value)

    def diagnostic_secrets(self) -> tuple[str, ...]:
        """Return configured credential values and runtime tokens for redaction."""
        values = {value for value in self._runtime_secrets if value}
        values.update(
            value for name in self.credentials.values() if (value := os.environ.get(name))
        )
        return tuple(sorted(values, key=len, reverse=True))


@dataclass(frozen=True)
class SettingIssue:
    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.key}: {self.message}"


class SettingsError(Exception):
    """Startup validation failure listing every offending key."""

    def __init__(self, source: str, issues: list[SettingIssue]) -> None:
        self.source = source
        self.issues = issues
        lines = "\n".join(f"  - {issue}" for issue in issues)
        super().__init__(f"Invalid settings in {source}:\n{lines}")


def _issues_from_validation_error(error: ValidationError) -> list[SettingIssue]:
    issues = []
    for item in error.errors():
        key = ".".join(str(part) for part in item["loc"]) or "(root)"
        if item["type"] == "extra_forbidden":
            message = "unknown key; remove it (secrets must not be stored in settings.toml)"
        else:
            message = item["msg"].removeprefix("Value error, ")
        issues.append(SettingIssue(key, message))
    return issues


def parse_settings(raw: dict[str, object], *, source: str = "settings") -> Settings:
    """Validate an already-parsed mapping. Pure: touches no filesystem."""
    try:
        return Settings.model_validate(raw)
    except ValidationError as error:
        raise SettingsError(source, _issues_from_validation_error(error)) from error


def check_paths_exist(settings: Settings) -> list[SettingIssue]:
    """Verify that configured executables and directories exist (startup, not mid-render)."""
    issues: list[SettingIssue] = []
    b = settings.binaries
    if not b.blender_exe.is_file():
        issues.append(SettingIssue("binaries.blender_exe", f"{b.blender_exe} does not exist; set the Blender path"))
    if not (b.ffmpeg_exe.is_file() or shutil.which(str(b.ffmpeg_exe))):
        issues.append(SettingIssue("binaries.ffmpeg_exe", f"{b.ffmpeg_exe} not found on disk or PATH; install FFmpeg or set the path"))
    if not b.comfyui_root.is_dir():
        issues.append(SettingIssue("binaries.comfyui_root", f"{b.comfyui_root} is not a directory; set the ComfyUI install path"))
    if not settings.image_models.weights_dir.is_dir():
        issues.append(SettingIssue("image_models.weights_dir", f"{settings.image_models.weights_dir} is not a directory; set the pinned image weights path"))
    if not settings.project_root.is_dir():
        issues.append(SettingIssue("project_root", f"{settings.project_root} is not a directory; create it or change the setting"))
    extra = settings.models.extra_model_paths_file
    if extra is not None and not extra.is_file():
        issues.append(SettingIssue("models.extra_model_paths_file", f"{extra} does not exist"))
    for index, root in enumerate(settings.models.model_roots):
        if not root.is_dir():
            issues.append(SettingIssue(f"models.model_roots.{index}", f"{root} is not a directory"))
    return issues


def load_settings(path: Path | str, *, verify_paths: bool = True) -> Settings:
    """Read and validate ``settings.toml``. Raises ``SettingsError`` naming each bad key."""
    path = Path(path)
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SettingsError(source, [SettingIssue("(file)", "settings file not found; create it or pass its path")]) from error
    except OSError as error:
        raise SettingsError(source, [SettingIssue("(file)", f"cannot read settings file: {error}")]) from error
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(source, [SettingIssue("(file)", f"not valid TOML: {error}")]) from error

    settings = parse_settings(raw, source=source)
    if verify_paths:
        issues = check_paths_exist(settings)
        if issues:
            raise SettingsError(source, issues)
    return settings

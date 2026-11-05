"""Validated ``settings.toml`` (FR-23, AT-16).

Every configurable value in the PRD is a named field with a stated default. Loading
fails at startup with a ``SettingsError`` listing each offending key and what to do
about it. Secrets never live here: credentials are *names of environment variables*, and
unknown keys are rejected so a pasted ``api_key = "..."`` is an error, not a leak.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orchestrator.platform.containment import UploadLimits

_ENV_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class BinariesSettings(_Section):
    blender_exe: Path = Field(Path(r"D:\Blender\blender.exe"), description="Blender executable.")
    ffmpeg_exe: Path = Field(Path("ffmpeg"), description="FFmpeg executable; a bare name is looked up on PATH.")
    comfyui_root: Path = Field(
        Path(r"D:\ComfyUI_3\ComfyUI_windows_portable"), description="ComfyUI portable install directory."
    )
    comfyui_port: int = Field(8188, ge=1, le=65535, description="Port the orchestrator launches ComfyUI on.")


class ModelsSettings(_Section):
    extra_model_paths_file: Path | None = Field(
        None, description="extra_model_paths.yaml that lets weights live on another volume."
    )
    model_roots: list[Path] = Field(default_factory=list, description="Additional directories holding model weights.")


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


class Settings(_Section):
    project_root: Path = Field(Path("projects"), description="Active project root; all writes are contained here.")
    hosted_video_tier: Literal[1, 2, 3] = Field(1, description="Section 6.2 access tier; 1 = annotate only.")
    binaries: BinariesSettings = BinariesSettings()
    models: ModelsSettings = ModelsSettings()
    server: ServerSettings = ServerSettings()
    upload: UploadLimits = UploadLimits()
    budget: BudgetSettings = BudgetSettings()
    retention: RetentionSettings = RetentionSettings()
    resources: ResourceSettings = ResourceSettings()
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

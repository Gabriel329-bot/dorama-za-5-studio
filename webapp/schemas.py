"""Строго валидируемые HTTP-контракты локального API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class DoramaRequest(StrictRequest):
    query: str = Field(min_length=3, max_length=180)
    limit: int = Field(default=10, ge=3, le=40)


class LicensedDoramaRequest(StrictRequest):
    query: str = Field(min_length=3, max_length=180)
    focus: str = Field(default="", max_length=300)
    limit: int = Field(default=10, ge=3, le=40)


class ClipRequest(StrictRequest):
    filename: str = Field(min_length=1, max_length=255)


class EpisodeRequest(StrictRequest):
    filename: str = Field(min_length=1, max_length=255)
    focus: str = Field(default="", max_length=300)
    rights_confirmed: bool


class PublishRequest(StrictRequest):
    privacy: Literal["private", "unlisted", "public"] = "private"


class CaptionRequest(StrictRequest):
    caption: str = Field(min_length=3, max_length=5000)


class ScheduleRequest(StrictRequest):
    enabled: bool
    post_times: list[str] = Field(min_length=1, max_length=6)
    privacy_status: Literal["private", "unlisted", "public"]


class PipelineSettingsRequest(StrictRequest):
    whisper_model: Literal["tiny", "base", "small", "medium", "large-v3"] = "medium"
    whisper_device: Literal["cuda", "cpu"] = "cuda"
    ollama_model: str = Field(
        min_length=3,
        max_length=60,
        pattern=r"^[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?$",
    )
    search_sources: list[
        Literal[
            "youtube",
            "bilibili",
            "dailymotion",
            "internet_archive",
            "wikimedia_commons",
        ]
    ] = Field(min_length=1, max_length=5)
    voice: Literal["ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural"]
    rate: str = Field(default="+12%", pattern=r"^[+-]\d{1,2}%$")
    pitch: str = Field(default="-2Hz", pattern=r"^[+-]\d{1,2}Hz$")
    target_duration_seconds: int = Field(default=300, ge=60, le=600)
    scene_count: int = Field(default=9, ge=3, le=20)
    original_audio_volume: float = Field(default=0.16, ge=0.0, le=1.0)
    target_script_words: int = Field(default=680, ge=200, le=2000)
    require_review: bool = True

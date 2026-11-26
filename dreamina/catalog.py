"""Model catalog, request validation, prompt references, and pricing."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ASPECT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
MODES = ("unified", "first_last")
_TOKEN = re.compile(r"@(image|audio)(\d+)")


class ValidationError(ValueError):
    code = "invalid_input"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    dreamina_key: str
    min_duration: int
    max_duration: int
    prices_per_second: dict[str, int]
    max_images: int
    max_audio: int
    max_prompt_chars: int
    max_audio_seconds: float
    # Dreamina commerce benefit type, formatted with the resolution.
    benefit_pattern: str = ""

    @property
    def resolutions(self) -> tuple[str, ...]:
        return tuple(self.prices_per_second)

    def benefit_type(self, resolution: str) -> str:
        return self.benefit_pattern.format(resolution=resolution)


MODELS: dict[str, ModelSpec] = {
    "seedance-2.5": ModelSpec(
        name="seedance-2.5",
        dreamina_key="dreamina_seedance_45_pro",
        min_duration=4,
        max_duration=30,
        prices_per_second={"480p": 39, "720p": 85, "1080p": 208, "4k": 387},
        max_images=30,
        max_audio=10,
        max_prompt_chars=15000,
        max_audio_seconds=30.2,
        benefit_pattern="seedance_25_{resolution}_no_input_video_output",
    ),
    "seedance-2.5-flash": ModelSpec(
        name="seedance-2.5-flash",
        dreamina_key="dreamina_seedance_45_flash",
        min_duration=4,
        max_duration=30,
        prices_per_second={"480p": 21, "720p": 44},
        max_images=10,
        max_audio=5,
        max_prompt_chars=15000,
        max_audio_seconds=30.2,
        benefit_pattern="seedance_25_flash_{resolution}_no_input_video",
    ),
    "seedance-2.0-fast": ModelSpec(
        name="seedance-2.0-fast",
        dreamina_key="dreamina_seedance_40",
        min_duration=4,
        max_duration=15,
        prices_per_second={"720p": 18, "1080p": 37, "2k": 45, "4k": 58},
        max_images=9,
        max_audio=3,
        max_prompt_chars=4000,
        max_audio_seconds=15.0,
        benefit_pattern="seedance_20_fast_{resolution}_output",
    ),
    "seedance-2.0-mini": ModelSpec(
        name="seedance-2.0-mini",
        dreamina_key="dreamina_seedance_40_mini",
        min_duration=4,
        max_duration=15,
        prices_per_second={"720p": 13, "1080p": 29, "2k": 37, "4k": 50},
        max_images=9,
        max_audio=3,
        max_prompt_chars=4000,
        max_audio_seconds=15.0,
        benefit_pattern="seedance_20_mini_{resolution}_output",
    ),
}


class PriceBook:
    """Per-second prices: live Dreamina metadata over the verified fallback table."""

    def __init__(self) -> None:
        self._live: dict[str, int] = {}
        self.updated_at: str | None = None
        self._lock = threading.Lock()

    def update(self, benefit_prices: dict[str, int]) -> int:
        """Store live prices for known benefit types; returns how many matched."""
        known = {
            model.benefit_type(resolution)
            for model in MODELS.values()
            for resolution in model.resolutions
        }
        live = {key: int(value) for key, value in benefit_prices.items() if key in known and int(value) > 0}
        with self._lock:
            if live:
                self._live = live
                self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return len(live)

    def per_second(self, model: ModelSpec, resolution: str) -> int:
        if resolution not in model.prices_per_second:
            raise ValidationError(f"unsupported resolution {resolution} for {model.name}")
        with self._lock:
            return self._live.get(model.benefit_type(resolution), model.prices_per_second[resolution])

    def table(self, model: ModelSpec) -> dict[str, int]:
        return {resolution: self.per_second(model, resolution) for resolution in model.resolutions}

    @property
    def source(self) -> str:
        return "live" if self._live else "fallback"


PRICES = PriceBook()


@dataclass(frozen=True)
class PromptToken:
    kind: str
    index: int


@dataclass
class GenerationSpec:
    model: ModelSpec
    prompt: str
    duration_seconds: int
    resolution: str
    aspect_ratio: str
    mode: str
    images: list[str] = field(default_factory=list)
    audio: list[str] = field(default_factory=list)
    first_frame: str | None = None
    last_frame: str | None = None

    @property
    def estimate_credits(self) -> int:
        return estimate_credits(self.model, self.duration_seconds, self.resolution)


def get_model(name: str) -> ModelSpec:
    try:
        return MODELS[name]
    except KeyError as exc:
        raise ValidationError(f"unknown model: {name}") from exc


def parse_prompt_tokens(prompt: str) -> list[PromptToken]:
    return [PromptToken(match.group(1), int(match.group(2))) for match in _TOKEN.finditer(prompt)]


def estimate_credits(model: ModelSpec, duration_seconds: int, resolution: str) -> int:
    return PRICES.per_second(model, resolution) * duration_seconds


def validate_generation(
    *,
    model_name: str,
    prompt: str,
    duration_seconds: int,
    resolution: str,
    aspect_ratio: str | None,
    images: list[str] | None = None,
    audio: list[str] | None = None,
    first_frame: str | None = None,
    last_frame: str | None = None,
) -> GenerationSpec:
    model = get_model(model_name)
    images = list(images or [])
    audio = list(audio or [])
    image_count = len(images)
    audio_count = len(audio)
    has_first_frame = bool(first_frame)
    has_last_frame = bool(last_frame)
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValidationError("prompt is required")
    if len(prompt) > model.max_prompt_chars:
        raise ValidationError(f"prompt exceeds {model.max_prompt_chars} characters for {model.name}")
    if not isinstance(duration_seconds, int) or isinstance(duration_seconds, bool):
        raise ValidationError("duration_seconds must be an integer")
    if not model.min_duration <= duration_seconds <= model.max_duration:
        raise ValidationError(
            f"duration_seconds must be between {model.min_duration} and {model.max_duration} for {model.name}"
        )
    if resolution not in model.prices_per_second:
        raise ValidationError(
            f"resolution must be one of {', '.join(model.resolutions)} for {model.name}"
        )
    if has_last_frame and not has_first_frame:
        raise ValidationError("last_frame requires first_frame")

    frame_mode = has_first_frame or has_last_frame
    if frame_mode and (image_count or audio_count):
        raise ValidationError("first/last-frame mode cannot be combined with unified image/audio references")
    if not frame_mode and not aspect_ratio:
        raise ValidationError("aspect_ratio is required in unified-reference mode")
    if not frame_mode and aspect_ratio not in ASPECT_RATIOS:
        raise ValidationError(f"aspect_ratio must be one of {', '.join(ASPECT_RATIOS)}")

    if image_count > model.max_images:
        raise ValidationError(f"{model.name} accepts at most {model.max_images} images")
    if audio_count > model.max_audio:
        raise ValidationError(f"{model.name} accepts at most {model.max_audio} audio files")

    tokens = parse_prompt_tokens(prompt)
    for token in tokens:
        limit = image_count if token.kind == "image" else audio_count
        if token.index < 1 or token.index > limit:
            raise ValidationError(f"prompt references @{token.kind}{token.index}, but only {limit} provided")

    return GenerationSpec(
        model=model,
        prompt=prompt,
        duration_seconds=duration_seconds,
        resolution=resolution,
        aspect_ratio="adaptive" if frame_mode else str(aspect_ratio),
        mode="first_last" if frame_mode else "unified",
        images=images,
        audio=audio,
        first_frame=first_frame,
        last_frame=last_frame,
    )


def public_catalog() -> list[dict[str, Any]]:
    catalog = []
    for model in MODELS.values():
        catalog.append(
            {
                "name": model.name,
                "dreamina_model_key": model.dreamina_key,
                "duration_seconds": {"min": model.min_duration, "max": model.max_duration},
                "resolutions": list(model.resolutions),
                "aspect_ratios": list(ASPECT_RATIOS),
                "pricing_credits_per_second": PRICES.table(model),
                "pricing_source": PRICES.source,
                "pricing_updated_at": PRICES.updated_at,
                "reference_limits": {
                    "images": model.max_images,
                    "audio": model.max_audio,
                    "audio_seconds": model.max_audio_seconds,
                    "prompt_chars": model.max_prompt_chars,
                },
            }
        )
    return catalog

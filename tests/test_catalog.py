import pytest

from dreamina.catalog import (
    MODELS,
    PRICES,
    PriceBook,
    ValidationError,
    estimate_credits,
    parse_prompt_tokens,
    public_catalog,
    validate_generation,
)


def valid(**overrides):
    args = dict(
        model_name="seedance-2.5-flash",
        prompt="a paper boat",
        duration_seconds=4,
        resolution="480p",
        aspect_ratio="16:9",
    )
    args.update(overrides)
    return validate_generation(**args)


def test_text_only_is_unified():
    spec = valid()
    assert spec.mode == "unified"
    assert spec.estimate_credits == 84


@pytest.mark.parametrize(
    "model,low,high",
    [("seedance-2.5", 4, 30), ("seedance-2.5-flash", 4, 30), ("seedance-2.0-fast", 4, 15), ("seedance-2.0-mini", 4, 15)],
)
def test_duration_boundaries(model, low, high):
    resolution = MODELS[model].resolutions[0]
    valid(model_name=model, resolution=resolution, duration_seconds=low)
    valid(model_name=model, resolution=resolution, duration_seconds=high)
    for bad in (low - 1, high + 1):
        with pytest.raises(ValidationError):
            valid(model_name=model, resolution=resolution, duration_seconds=bad)


def test_rejects_unknown_model_resolution_and_aspect():
    with pytest.raises(ValidationError):
        valid(model_name="seedance-9")
    with pytest.raises(ValidationError):
        valid(resolution="1080p")  # flash is 480p/720p only
    with pytest.raises(ValidationError):
        valid(aspect_ratio="2:1")
    with pytest.raises(ValidationError):
        valid(aspect_ratio=None)


def test_prompt_limits_per_model():
    valid(model_name="seedance-2.5", prompt="x" * 15000)
    with pytest.raises(ValidationError):
        valid(model_name="seedance-2.0-mini", resolution="720p", prompt="x" * 4001)
    with pytest.raises(ValidationError):
        valid(prompt="   ")


def test_reference_counts_per_model():
    valid(images=["i"] * 10, audio=["a"] * 5)
    with pytest.raises(ValidationError):
        valid(images=["i"] * 11)
    with pytest.raises(ValidationError):
        valid(model_name="seedance-2.0-fast", resolution="720p", audio=["a"] * 4)


def test_tokens_must_point_at_files():
    assert [(t.kind, t.index) for t in parse_prompt_tokens("@image2 and @audio1")] == [("image", 2), ("audio", 1)]
    valid(prompt="@image1 sings @audio1", images=["i"], audio=["a"])
    with pytest.raises(ValidationError):
        valid(prompt="@image2 sings", images=["i"])
    with pytest.raises(ValidationError):
        valid(prompt="@audio1", images=["i"])


def test_first_last_mode():
    spec = valid(first_frame="f.png", last_frame="l.png", aspect_ratio=None)
    assert spec.mode == "first_last"
    assert spec.aspect_ratio == "adaptive"
    with pytest.raises(ValidationError):
        valid(last_frame="l.png")
    with pytest.raises(ValidationError):
        valid(first_frame="f.png", images=["i"])


def test_estimates_match_verified_charges():
    assert estimate_credits(MODELS["seedance-2.5"], 4, "480p") == 156
    assert estimate_credits(MODELS["seedance-2.0-fast"], 4, "720p") == 72
    assert estimate_credits(MODELS["seedance-2.0-mini"], 4, "720p") == 52


def test_price_book_prefers_live_prices_for_known_benefits():
    book = PriceBook()
    flash = MODELS["seedance-2.5-flash"]
    assert book.per_second(flash, "480p") == 21 and book.source == "fallback"
    assert book.update({"seedance_25_flash_480p_no_input_video": 25, "unrelated": 1}) == 1
    assert book.per_second(flash, "480p") == 25
    assert book.per_second(flash, "720p") == 44
    assert book.source == "live"


def test_public_catalog_lists_all_models():
    names = {model["name"] for model in public_catalog()}
    assert names == set(MODELS)
    assert PRICES.source in {"live", "fallback"}

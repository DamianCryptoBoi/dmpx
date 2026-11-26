import json
import re
from pathlib import Path

import pytest

from dreamina import browser
from dreamina.browser import (
    BrowserError,
    BrowserManager,
    GenerationRejected,
    _GenerateCapture,
    prompt_plan,
    validate_captured_request,
)
from dreamina.catalog import validate_generation
from dreamina.envstore import EnvStore

GENERATE_URL = "https://mweb-api-sg.capcut.com/mweb/v1/aigc_draft/generate?aid=513641"


def spec(**overrides):
    args = dict(
        model_name="seedance-2.5-flash",
        prompt="a boat",
        duration_seconds=4,
        resolution="480p",
        aspect_ratio="16:9",
    )
    args.update(overrides)
    return validate_generation(**args)


def draft(model="dreamina_seedance_45_flash", duration_ms=4000, resolution="480p", materials=(), metas=()):
    video = {
        "duration_ms": duration_ms,
        "resolution": resolution,
        "unified_edit_input": {
            "material_list": [{"material_type": kind} for kind in materials],
            "meta_list": [{"meta_type": kind} for kind in metas],
        },
    }
    params = {"model_req_key": model, "video_gen_inputs": [video]}
    content = {"component_list": [{"abilities": {"gen_video": {"text_to_video_params": params}}}]}
    return {"submit_id": "req-submit", "draft_content": json.dumps(content)}


# prompt construction -------------------------------------------------------------
def test_prompt_plan_tags_every_reference_in_order():
    plan = prompt_plan(spec(prompt="@image2 then @audio1\nend", images=["a", "b"], audio=["c"]))
    assert plan == [
        ("chip", ("image", 1)),
        ("text", " "),
        ("chip", ("image", 2)),
        ("text", " then "),
        ("chip", ("audio", 1)),
        ("text", " end"),
    ]


def test_prompt_plan_text_only_never_types_newlines():
    assert prompt_plan(spec(prompt="line one\r\nline two")) == [("text", "line one line two")]
    frames = spec(prompt="a\nb", first_frame="f.png", aspect_ratio=None)
    assert prompt_plan(frames) == [("text", "a b")]


# captured request validation ------------------------------------------------------
def test_captured_request_matches():
    request = draft(materials=["image", "audio"], metas=["text", "image", "audio"])
    assert validate_captured_request(request, spec(images=["a"], audio=["b"])) == []


def test_captured_request_mismatches_are_reported():
    request = draft(model="other", duration_ms=5000, resolution="720p", materials=["image", "audio"], metas=["text"])
    warnings = validate_captured_request(request, spec(images=["a"], audio=["b"]))
    assert any("model" in w for w in warnings)
    assert any("duration" in w for w in warnings)
    assert any("resolution" in w for w in warnings)
    assert any("image references" in w for w in warnings)
    assert any("audio references" in w for w in warnings)
    assert validate_captured_request({"draft_content": "not json"}, spec()) == [
        "captured request did not contain video generation parameters"
    ]


# submit guard -------------------------------------------------------------------------
class FakeRequest:
    def __init__(self, body):
        self.url = GENERATE_URL
        self.method = "POST"
        self.post_data = json.dumps(body)


class FakeResponse:
    def __init__(self, request, payload):
        self.url = GENERATE_URL
        self.request = request
        self._payload = payload

    async def json(self):
        return self._payload


class FakeSubmit:
    def __init__(self, page):
        self.page = page

    @property
    def last(self):
        return self

    async def is_disabled(self):
        return False

    async def click(self):
        self.page.clicks += 1
        self.page.script.pop(0)(self.page)


class FakePage:
    def __init__(self, script):
        self.url = "https://dreamina.capcut.com/ai-tool/generate"
        self.script = list(script)
        self.clicks = 0
        self.handlers = {"request": [], "response": []}

    def on(self, event, handler):
        self.handlers[event].append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    def locator(self, selector):
        return FakeSubmit(self)

    def send(self, body, payload=None):
        request = FakeRequest(body)
        for handler in self.handlers["request"]:
            handler(request)
        if payload is not None:
            for handler in self.handlers["response"]:
                handler(FakeResponse(request, payload))


OK = {"ret": "0", "data": {"aigc_data": {"submit_id": "resp-submit"}}}


@pytest.fixture
def manager(settings):
    manager = BrowserManager(settings, EnvStore(settings.env_path))
    manager.submit_timeout = 0.05
    return manager


async def submit(manager, page, generation=None):
    capture = _GenerateCapture(page)
    try:
        return await manager._submit(page, generation or spec(), capture)
    finally:
        capture.detach()


async def test_single_click_submission(manager):
    page = FakePage([lambda p: p.send(draft(), OK)])
    outcome = await submit(manager, page)
    assert outcome.submit_id == "resp-submit" and not outcome.uncertain
    assert page.clicks == 1 and outcome.warnings == []


async def test_second_click_only_after_workspace_init_without_request(manager):
    def init_workspace(page):
        page.url += "&workspace=123"

    page = FakePage([init_workspace, lambda p: p.send(draft(), OK)])
    outcome = await submit(manager, page)
    assert page.clicks == 2 and outcome.submit_id == "resp-submit"


async def test_request_without_response_is_never_clicked_again(manager):
    page = FakePage([lambda p: p.send(draft())])
    page.url += "?workspace=1"
    outcome = await submit(manager, page)
    assert page.clicks == 1
    assert outcome.uncertain and outcome.submit_id == "req-submit"


async def test_no_request_and_no_workspace_fails_without_retry(manager):
    page = FakePage([lambda p: None])
    with pytest.raises(BrowserError, match="nothing was charged"):
        await submit(manager, page)
    assert page.clicks == 1


async def test_early_submission_is_adopted_not_repeated(manager):
    page = FakePage([])
    capture = _GenerateCapture(page)
    page.send(draft(), OK)
    try:
        outcome = await manager._submit(page, spec(), capture)
    finally:
        capture.detach()
    assert page.clicks == 0 and outcome.submit_id == "resp-submit"
    assert "before submit was clicked" in outcome.warnings[0]


async def test_rejected_generation(manager):
    page = FakePage([lambda p: p.send(draft(), {"ret": "-6", "errmsg": "shark not pass reject"})])
    with pytest.raises(GenerationRejected) as excinfo:
        await submit(manager, page)
    assert excinfo.value.shark and excinfo.value.code == "upstream_rejected"


# localization ---------------------------------------------------------------------------
BANNED = [
    r"get_by_text",
    r"get_by_label",
    r"get_by_placeholder",
    r"get_by_title",
    r"get_by_role\([^)]*name=",
    r"has_text",
    r":has-text\(",
    r":text\(",
    r"['\"]text=",
    r"inner_text",
    r"text_content",
    r"innerText",
    r"textContent",
    r"aria-label",
    r"placeholder=",
]


def test_browser_driver_never_reads_visible_text():
    # The one allowed read-back compares the editor with our own prompt and is
    # marked on its line.
    lines = [line for line in Path(browser.__file__).read_text().splitlines() if "own-text-ok" not in line]
    source = "\n".join(lines)
    found = [pattern for pattern in BANNED if re.search(pattern, source)]
    assert found == [], f"localized-text selectors in browser.py: {found}"


# review regressions -----------------------------------------------------------------
def test_expected_text_keeps_the_space_typed_before_each_chip():
    plan = prompt_plan(spec(prompt="参考@image1的风格", images=["a"]))
    assert browser.plan_text(plan) == "参考 的风格"
    plan = prompt_plan(spec(prompt="(@image1)", images=["a"]))
    assert browser.plan_text(plan) == "( )"


async def test_existing_workspace_url_never_gets_a_second_click(manager):
    page = FakePage([lambda p: None])
    page.url += "?workspace=1"
    with pytest.raises(browser.NotSubmitted):
        await submit(manager, page)
    assert page.clicks == 1


async def test_error_after_request_returns_tracked_outcome(manager):
    def send_then_break(page):
        page.send(draft())
        raise RuntimeError("navigation interrupted")

    recorded = []
    page = FakePage([send_then_break])
    capture = _GenerateCapture(page, recorded.append)
    try:
        outcome = await manager._submit(page, spec(), capture)
    finally:
        capture.detach()
    assert recorded == ["req-submit"]
    assert outcome.uncertain and outcome.submit_id == "req-submit"
    assert page.clicks == 1


async def test_click_error_without_request_is_uncertain_not_rejected(manager):
    def broken_click(page):
        raise RuntimeError("click failed mid-flight")

    page = FakePage([broken_click])
    with pytest.raises(browser.SubmitUncertain) as excinfo:
        await submit(manager, page)
    assert excinfo.value.code == "upstream_timeout"


def test_only_the_exact_generate_endpoint_counts_as_submission():
    base = "https://mweb-api-sg.capcut.com/mweb/v1/aigc_draft/"
    assert browser._is_generate(base + "generate?aid=513641", "POST")
    assert not browser._is_generate(base + "generate_accelerate?aid=513641", "POST")
    assert not browser._is_generate(base + "generate", "GET")


async def test_browser_without_devtools_endpoint_fails_clearly(manager):
    stopped = []

    class Launched:
        cdp_url = None

        def stop(self):
            stopped.append(True)

    class SDK:
        def create_profile(self, platform):
            return type("Profile", (), {"id": "p1"})()

        def launch(self, profile, **kwargs):
            return Launched()

    manager._sdk = SDK()
    with pytest.raises(BrowserError) as excinfo:
        await manager.open("alpha", None)
    assert "--no-sandbox" in str(excinfo.value) and stopped == [True]
    assert not manager.is_open("alpha")

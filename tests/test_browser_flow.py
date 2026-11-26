import json
import re
from pathlib import Path

import pytest

from dreamina import browser
from dreamina.browser import (
    COMPLIANCE_AGREE,
    POPUP,
    apply_language,
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


class FakeAgree:
    """The acknowledgment's Agree button: present only while page.compliance is set."""

    def __init__(self, page):
        self.page = page

    def filter(self, **_kwargs):
        return self

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.page.compliance else 0

    async def click(self, **_kwargs):
        self.page.agreed += 1
        step, self.page.compliance = self.page.compliance, None
        step(self.page)


class FakePage:
    agreed = 0

    def __init__(self, script):
        self.url = "https://dreamina.capcut.com/ai-tool/generate"
        self.script = list(script)
        self.clicks = 0
        self.handlers = {"request": [], "response": []}

    def on(self, event, handler):
        self.handlers[event].append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    compliance = None  # set to a script step: the dialog is up until Agree runs it

    def locator(self, selector):
        if selector == COMPLIANCE_AGREE:
            return FakeAgree(self)
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

    saved, created = [], []

    class SDK:
        def create_profile(self, platform):
            return type("Profile", (), {"id": "p1", "config": {"navigator": {"language": "auto"}, "timezone": "auto"}})()

        def save_profile(self, profile):
            saved.append(profile.config)

        def launch(self, profile, **kwargs):
            return Launched()

    manager._sdk = SDK()
    manager.on_profile_created = lambda account_id, profile_id: created.append((account_id, profile_id))
    with pytest.raises(BrowserError) as excinfo:
        await manager.open("alpha", None)
    assert "--no-sandbox" in str(excinfo.value) and stopped == [True]
    assert not manager.is_open("alpha")
    # The new profile is reported before anything can fail, and pinned to English.
    assert created == [("alpha", "p1")]
    assert saved[0]["navigator"]["language"] == "en-US" and saved[0]["timezone"] == "auto"


def test_apply_language_pins_english_and_keeps_auto_timezone():
    config = {"navigator": {"language": "auto"}, "timezone": "auto"}
    assert apply_language(config, "en-US") is True
    assert config["navigator"] == {"language": "en-US", "accept_language": "en-US,en;q=0.9", "languages": ["en-US", "en"]}
    assert config["icu_locale"] == "en-US" and config["timezone"] == "auto"
    assert apply_language(config, "en-US") is False  # already pinned: no rewrite
    untouched = {"navigator": {"language": "auto"}}
    assert apply_language(untouched, "") is False and untouched == {"navigator": {"language": "auto"}}


async def test_open_browsers_stay_within_the_concurrency_limit(manager):
    closed = []

    class Part:
        def __init__(self, account_id):
            self.account_id = account_id

        async def close(self):
            closed.append(self.account_id)

        async def stop(self):
            pass

    class Launched:
        def stop(self):
            pass

    def session(account_id, last_used):
        return type(
            "Session",
            (),
            {"account_id": account_id, "last_used": last_used, "browser": Part(account_id), "playwright": Part(account_id), "launched": Launched()},
        )()

    assert manager.settings.max_concurrent_jobs == 2
    manager._sessions = {"old": session("old", 1.0), "new": session("new", 2.0)}
    await manager._make_room("third")
    assert closed == ["old"] and set(manager._sessions) == {"new"}

    # A browser in use (its account lock held) is never closed for room.
    manager._sessions = {"busy": session("busy", 1.0), "idle": session("idle", 2.0)}
    closed.clear()
    async with manager._account_lock("busy"):
        await manager._make_room("third")
    assert closed == ["idle"] and set(manager._sessions) == {"busy"}


async def test_first_generation_acknowledgment_is_agreed_then_submitted(manager):
    page = FakePage([lambda p: None])  # the click only opens the acknowledgment
    page.compliance = lambda p: p.send(draft(), OK)  # agreeing sends the request
    manager.submit_timeout = 0.3
    outcome = await submit(manager, page)
    assert outcome.submit_id == "resp-submit" and page.clicks == 1 and page.agreed == 1


async def test_no_acknowledgment_means_nothing_is_agreed(manager):
    page = FakePage([lambda p: p.send(draft(), OK)])
    await submit(manager, page)
    assert page.agreed == 0


def test_promo_closer_never_targets_the_acknowledgment():
    assert ':not(:has([class*="aigc-compliance-confirmation"]))' in POPUP


class StackPage:
    """The omni reference stack: every dropped file is added, but the collapsed
    stack draws only its first and newest cards."""

    def __init__(self, busy_polls=0):
        self.files = 0
        self.busy_polls = busy_polls
        self.polls = 0

    async def evaluate(self, script, args):
        self.files += len(args["files"])
        return "dispatched"

    async def wait_for_timeout(self, ms):
        self.polls += 1
        if self.polls > 20:
            raise AssertionError("still waiting for references that are never drawn")

    def locator(self, selector):
        page = self

        class Count:
            async def count(self):
                if selector == browser.FIRST_SLOT:
                    return 1
                if selector == browser.REFERENCE_BODY:
                    return min(page.files, 2)
                if "aria-busy" in selector:
                    return 1 if page.polls < page.busy_polls else 0
                raise AssertionError(selector)

        return Count()


@pytest.mark.parametrize("count", [1, 2, 3, 5])
async def test_reference_upload_finishes_with_more_files_than_the_stack_draws(manager, tmp_path, count):
    files = []
    for index in range(count):
        path = tmp_path / f"image{index + 1}.png"
        path.write_bytes(b"png")
        files.append(str(path))
    page = StackPage(busy_polls=2)
    await manager._upload_references(page, files)
    assert page.files == count
    assert page.polls >= 2  # waited while the slot was busy


def test_render_failures_explain_the_reason():
    from dreamina.http_client import parse_history
    from dreamina.jobs import render_failure

    state = parse_history({"status": 30, "fail_code": "2052", "fail_msg": "OutputAudioCopyright"})
    assert state.failed and state.fail_msg == "OutputAudioCopyright"
    message = render_failure(state.fail_code, state.fail_msg)
    assert "code 2052" in message and "copyrighted music" in message and "nothing was charged" in message
    assert render_failure("7", None) == "the generation failed while rendering (code 7)"

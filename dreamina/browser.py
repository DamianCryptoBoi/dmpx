"""ShardBrowser login and language-neutral Dreamina UI generation.

Control flow never reads localized text. Selectors use stable data attributes,
input types/values, option indices, and DOM relationships only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from patchright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from shardx import ShardX

from .catalog import GenerationSpec, parse_prompt_tokens
from .config import Settings
from .envstore import EnvStore
from .http_client import save_cookie_file

log = logging.getLogger(__name__)

EDITOR = 'div.tiptap.ProseMirror[contenteditable="true"]'
# First/last-frame mode uses a plain textarea instead of the rich editor.
TEXTAREA = '[data-testid="dreamina.generator"] textarea'
PROMPT_FIELD = f"{EDITOR}, {TEXTAREA}"
SUBMIT = '[data-testid="dreamina.generator.submit"]'
GUEST_SHELL = '[data-main-frame-2026-guest-top-actions-ready="true"]'
COMBOBOX = '[role="combobox"]'
MODEL_OPTION = '[data-selector-option="model"][data-model-key="{key}"]'
MODEL_PANEL_OPTIONS = '[data-selector-panel="true"] [data-selector-option="model"]'
MODEL_LABEL = '[class*="model-label"]'
MENU_OPTIONS = '.lv-select-popup li[role="option"]'
# Settings popovers (aspect/resolution, duration). Toggles such as the
# director-studio effects button open no popover and must never be clicked.
SETTINGS_BUTTON = (
    'button[class*="toolbar-button-"]'
    ':not([data-director-studio-trigger]):not([class*="toggle-"])'
)
VISIBLE_POPOVER = '.lv-popover-inner'
CHIP = 'span.node-referenceMentionTag'
REFERENCE_BODY = '[data-native-reference-body]'
FIRST_SLOT = '[id$="reference-upload-0"]'
SECOND_SLOT = '[id$="reference-upload-1"]'
GENERATE_PATH = "/mweb/v1/aigc_draft/generate"
_TOKEN = re.compile(r"@(image|audio)(\d+)")


class BrowserError(Exception):
    code = "ui_changed"

    def __init__(self, message: str, *, screenshot: Path | None = None):
        super().__init__(message)
        self.message = message
        self.screenshot = screenshot


class LoginAttention(BrowserError):
    code = "attention"


class LoginFailed(BrowserError):
    code = "session_expired"


class NotSubmitted(BrowserError):
    """No generate request left the page, so nothing was charged."""


class SubmitUncertain(BrowserError):
    """Submit was clicked but the outcome is unknown and no submit_id was seen."""

    code = "upstream_timeout"


class GenerationRejected(BrowserError):
    """Dreamina answered the generate request with ret != 0; nothing was charged."""

    code = "upstream_rejected"

    def __init__(self, message: str, *, ret: str, screenshot: Path | None = None):
        super().__init__(message, screenshot=screenshot)
        self.ret = ret

    @property
    def shark(self) -> bool:
        return self.ret == "-6" or "shark" in self.message.lower()


@dataclass
class LoginResult:
    cookies_path: Path
    user_agent: str
    profile_id: str


@dataclass
class GenerationOutcome:
    submit_id: str
    profile_id: str
    request: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # True when the generate request was sent but its response was not seen.
    uncertain: bool = False


@dataclass
class AccountBrowser:
    account_id: str
    profile_id: str
    launched: Any
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    last_used: float = field(default_factory=time.time)


def prompt_plan(spec: GenerationSpec) -> list[tuple[str, Any]]:
    """Ordered ("text", str) / ("chip", (kind, index)) steps for the editor.

    References the prompt never mentions are inserted first so every uploaded
    file is tagged in the request metadata. Newlines become spaces because the
    editor may treat Enter as submit.
    """
    prompt = re.sub(r"[\r\n]+", " ", spec.prompt)
    if spec.mode != "unified" or not (spec.images or spec.audio):
        return [("text", prompt)]
    referenced = {(token.kind, token.index) for token in parse_prompt_tokens(prompt)}
    steps: list[tuple[str, Any]] = []
    for kind, items in (("image", spec.images), ("audio", spec.audio)):
        for index in range(1, len(items) + 1):
            if (kind, index) not in referenced:
                steps.append(("chip", (kind, index)))
                steps.append(("text", " "))
    position = 0
    for match in _TOKEN.finditer(prompt):
        if match.start() > position:
            steps.append(("text", prompt[position:match.start()]))
        steps.append(("chip", (match.group(1), int(match.group(2)))))
        position = match.end()
    if position < len(prompt):
        steps.append(("text", prompt[position:]))
    return steps


def plan_text(plan: list[tuple[str, Any]]) -> str:
    """Editor text expected after running a plan, ignoring chip labels.

    _insert_chip types " @" and the chip replaces only the "@", so each chip
    leaves one space behind.
    """
    return _normalize_space("".join(value if kind == "text" else " " for kind, value in plan))


def _is_generate(url: str, method: str) -> bool:
    # Exact path: Dreamina also POSTs /aigc_draft/generate_accelerate (a
    # prefetch when the mode changes), which must not count as a submission.
    return method == "POST" and urlsplit(url).path == GENERATE_PATH


def _request_body(request) -> dict[str, Any]:
    try:
        body = json.loads(request.post_data or "{}")
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


class _GenerateCapture:
    """Records every generate request/response the page sends.

    `on_submit` receives the submit_id the moment a generate request leaves
    the page, so the caller can persist it before anything else can fail.
    """

    def __init__(self, page: Page, on_submit: Callable[[str], None] | None = None):
        self.page = page
        self.on_submit = on_submit
        self.requests: list[Any] = []
        self.responses: list[Any] = []
        self.submit_id: str | None = None
        self._event = asyncio.Event()
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _on_request(self, request) -> None:
        if not _is_generate(request.url, request.method):
            return
        self.requests.append(request)
        submit_id = _request_body(request).get("submit_id")
        if submit_id and self.submit_id is None:
            self.submit_id = str(submit_id)
            if self.on_submit:
                try:
                    self.on_submit(self.submit_id)
                except Exception:
                    log.exception("could not record submit_id %s", self.submit_id)

    def _on_response(self, response) -> None:
        if _is_generate(response.url, response.request.method):
            self.responses.append(response)
            self._event.set()

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def detach(self) -> None:
        for event, handler in (("request", self._on_request), ("response", self._on_response)):
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass


class BrowserManager:
    submit_timeout = 60.0

    def __init__(self, settings: Settings, env: EnvStore):
        self.settings = settings
        self.env = env
        self._sessions: dict[str, AccountBrowser] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sdk: ShardX | None = None
        self._closed = False

    @property
    def sdk(self) -> ShardX:
        if self._sdk is None:
            self._sdk = ShardX(
                cache_dir=str(self.settings.shardx_cache_dir),
                profiles_dir=str(self.settings.profiles_dir),
            )
        return self._sdk

    def _account_lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def is_open(self, account_id: str) -> bool:
        return account_id in self._sessions

    async def open(self, account_id: str, profile_id: str | None) -> AccountBrowser:
        session = self._sessions.get(account_id)
        if session is not None:
            if session.browser.is_connected():
                session.last_used = time.time()
                return session
            await self.close(account_id)
        if self._closed:
            raise BrowserError("browser manager is closed")

        profile = await asyncio.to_thread(self._open_or_create_profile, account_id, profile_id)
        launched = await asyncio.to_thread(
            self.sdk.launch, profile, cdp=True, headless=self.settings.headless
        )
        playwright = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.connect_over_cdp(launched.cdp_url)
            if not browser.contexts:
                raise BrowserError("browser exposed no context")
            context = browser.contexts[0]
            page = self._pick_page(context) or await context.new_page()
            session = AccountBrowser(
                account_id=account_id,
                profile_id=profile.id,
                launched=launched,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )
            self._sessions[account_id] = session
            return session
        except BaseException:
            stops = [lambda: asyncio.to_thread(launched.stop)]
            if playwright is not None:
                stops.insert(0, playwright.stop)
            for stop in stops:
                try:
                    await stop()
                except Exception:
                    pass
            raise

    def _open_or_create_profile(self, account_id: str, profile_id: str | None):
        if profile_id:
            try:
                return self.sdk.open_profile(profile_id)
            except Exception:
                log.warning("profile %s missing; creating a replacement for %s", profile_id, account_id)
        return self.sdk.create_profile(platform=self.settings.shard_platform)

    def delete_profile(self, profile_id: str) -> None:
        try:
            self.sdk.delete_profile(profile_id)
        except Exception:
            log.warning("could not delete Shard profile %s", profile_id)

    @staticmethod
    def _pick_page(context: BrowserContext) -> Page | None:
        for page in context.pages:
            if "dreamina.capcut.com" in page.url:
                return page
        return context.pages[0] if context.pages else None

    async def close(self, account_id: str) -> None:
        session = self._sessions.pop(account_id, None)
        if not session:
            return
        for stop in (
            session.browser.close,
            session.playwright.stop,
            lambda: asyncio.to_thread(session.launched.stop),
        ):
            try:
                await stop()
            except Exception:
                pass

    async def close_all(self) -> None:
        self._closed = True
        for account_id in list(self._sessions):
            await self.close(account_id)

    async def close_idle(self) -> None:
        cutoff = time.time() - self.settings.browser_idle_seconds
        for account_id, session in list(self._sessions.items()):
            lock = self._account_lock(account_id)
            if session.last_used < cutoff and not lock.locked():
                async with lock:
                    if self._sessions.get(account_id) is session:
                        await self.close(account_id)

    login_timeout = 300.0

    async def login(self, account_id: str, email: str, password: str, profile_id: str | None) -> LoginResult:
        async with self._account_lock(account_id):
            session = await self.open(account_id, profile_id)

            async def run() -> LoginResult:
                await self._navigate(session.page)
                await self._dismiss_overlays(session.page)
                if await self._is_logged_out(session.page):
                    await self._login_flow(session.page, email, password)
                path, user_agent = await self._export_cookies(session)
                return LoginResult(cookies_path=path, user_agent=user_agent, profile_id=session.profile_id)

            try:
                try:
                    return await asyncio.wait_for(run(), self.login_timeout)
                except asyncio.TimeoutError:
                    raise LoginFailed("login timed out") from None
            except BrowserError as exc:
                if exc.screenshot is None:
                    exc.screenshot = await self._screenshot(session.page, self.settings.data_dir / "accounts" / account_id)
                raise
            finally:
                session.last_used = time.time()

    async def generate(
        self,
        account_id: str,
        profile_id: str | None,
        spec: GenerationSpec,
        job_id: str,
        *,
        dry_run: bool = False,
        on_submit: Callable[[str], None] | None = None,
    ) -> GenerationOutcome:
        """Prepare and submit one generation. With dry_run, stop before submit.

        `on_submit` is called with the submit_id as soon as a generate request
        is sent. Once that has happened this method returns an outcome (marked
        uncertain if needed) instead of raising, so the job is polled, never
        resubmitted.
        """
        async with self._account_lock(account_id):
            session = await self.open(account_id, profile_id)
            page = session.page
            # Listen from the start so an unexpected early submission is caught
            # instead of being followed by a second, double-charged click.
            capture = _GenerateCapture(page, on_submit)
            try:
                try:
                    await asyncio.wait_for(self._prepare(page, account_id, spec), self.settings.generation_timeout_seconds)
                except asyncio.TimeoutError:
                    if not capture.requests:
                        raise NotSubmitted("preparing the generation timed out; nothing was submitted") from None
                if dry_run and not capture.requests:
                    if await page.locator(SUBMIT).last.is_disabled():
                        raise BrowserError("submit button is disabled")
                    await self._screenshot(page, self.settings.data_dir / "dry-run", "prepared.png")
                    return GenerationOutcome(submit_id="", profile_id=session.profile_id, warnings=["dry run: not submitted"])
                outcome = await self._submit(page, spec, capture)
                outcome.profile_id = session.profile_id
                return outcome
            except (GenerationRejected, NotSubmitted, SubmitUncertain, LoginAttention, LoginFailed) as exc:
                if exc.screenshot is None:
                    exc.screenshot = await self._screenshot(page, self.settings.jobs_dir / job_id)
                raise
            except Exception as exc:
                if capture.submit_id:
                    return self._uncertain(capture, f"generation errored after submitting ({type(exc).__name__})")
                screenshot = await self._screenshot(page, self.settings.jobs_dir / job_id)
                if isinstance(exc, BrowserError):
                    exc.screenshot = exc.screenshot or screenshot
                    raise
                # First line only: Playwright appends call logs that can echo typed values.
                raise BrowserError(f"{type(exc).__name__}: {_first_line(exc)}", screenshot=screenshot) from None
            finally:
                capture.detach()
                session.last_used = time.time()

    async def export_cookies(self, account_id: str) -> Path | None:
        """Save the account's current cookies for the read-only HTTP client."""
        session = self._sessions.get(account_id)
        if session is None:
            return None
        async with self._account_lock(account_id):
            path, _ = await asyncio.wait_for(self._export_cookies(session), 20)
            return path

    async def _prepare(self, page: Page, account_id: str, spec: GenerationSpec) -> None:
        await self._navigate(page)
        await self._dismiss_overlays(page)
        if await self._is_logged_out(page):
            email, password = self.env.account_credentials(account_id)
            if not email or not password:
                raise LoginFailed("account credentials are missing from .env")
            await self._login_flow(page, email, password)
        model_index = await self._find_model_combo(page)
        await self._select_mode(page, spec, model_index)
        await self._select_model(page, spec.model.dreamina_key, model_index)
        if spec.mode == "unified":
            await self._upload_references(page, spec.images + spec.audio)
            await self._set_radio(page, spec.aspect_ratio)
        else:
            await self._upload_frames(page, spec.first_frame, spec.last_frame)
        await self._set_radio(page, spec.resolution)
        await self._set_duration(page, spec.duration_seconds)
        await self._assert_model_selected(page, spec.model.dreamina_key, model_index)
        await self._enter_prompt(page, spec)

    async def _export_cookies(self, session: AccountBrowser) -> tuple[Path, str]:
        cookies = await session.context.cookies("https://dreamina.capcut.com")
        user_agent = await session.page.evaluate("navigator.userAgent")
        path = self.settings.data_dir / "accounts" / session.account_id / "cookies.json"
        save_cookie_file(path, cookies, user_agent)
        return path, user_agent

    # navigation and login ---------------------------------------------
    async def _navigate(self, page: Page) -> None:
        await page.goto(self.settings.workspace_url, wait_until="domcontentloaded", timeout=90_000)
        try:
            await page.locator(PROMPT_FIELD).first.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError:
            await page.reload(wait_until="domcontentloaded", timeout=90_000)
            await page.locator(PROMPT_FIELD).first.wait_for(state="visible", timeout=45_000)
        await page.wait_for_timeout(800)

    async def _is_logged_out(self, page: Page) -> bool:
        shell = page.locator(GUEST_SHELL)
        if await shell.count():
            return await shell.first.is_visible()
        return False

    async def _login_flow(self, page: Page, email: str, password: str) -> None:
        shell = page.locator(GUEST_SHELL).first
        if not await shell.count():
            raise LoginFailed("guest login control not found")
        await shell.evaluate(
            """el => { const t = el.querySelector('[role="button"], [class*="login-button"]'); (t || el).click(); }"""
        )
        email_input = page.locator('input[type="email"][name="username"]').first
        try:
            await email_input.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise LoginFailed("email login form did not appear") from exc
        try:
            await email_input.fill(email)
        except Exception:
            # Playwright error text echoes the filled value; never surface it.
            raise LoginFailed("could not enter the account email") from None
        await page.locator('button[class*="continue-button"]').first.click()

        password_input = page.locator('input[type="password"][name="password"]').first
        try:
            await password_input.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError as exc:
            if await self._captcha_visible(page):
                raise LoginAttention("verification challenge appeared during login") from exc
            if not await self._is_logged_out(page):
                return
            raise LoginAttention("password step was not offered; account may require verification") from exc

        try:
            await password_input.fill(password)
        except Exception:
            raise LoginFailed("could not enter the account password") from None
        await page.locator('button[class*="continue-button"]').first.click()
        deadline = time.time() + 60
        while time.time() < deadline:
            if await self._captcha_visible(page):
                raise LoginAttention("verification challenge appeared during login")
            if not await self._is_logged_out(page):
                try:
                    await page.locator(EDITOR).first.wait_for(state="visible", timeout=5_000)
                except PlaywrightTimeoutError:
                    pass
                await page.wait_for_timeout(1_500)
                return
            await page.wait_for_timeout(1_000)
        raise LoginFailed("login did not complete before the timeout")

    async def _captcha_visible(self, page: Page) -> bool:
        for selector in ('iframe[src*="captcha"]', '[class*="captcha"]', '[id*="captcha"]'):
            locator = page.locator(selector)
            for index in range(min(await locator.count(), 5)):
                if await locator.nth(index).is_visible():
                    return True
        return False

    async def _dismiss_overlays(self, page: Page) -> None:
        for _ in range(6):
            acted = False
            for selector in ('[data-slot="CommerceDrawer_closeButton"]', 'div.lv-modal-wrapper .lv-modal-close-icon'):
                locator = page.locator(selector)
                for index in range(await locator.count() - 1, -1, -1):
                    element = locator.nth(index)
                    try:
                        if await element.is_visible():
                            try:
                                await element.click(timeout=2_000)
                            except PlaywrightTimeoutError:
                                await element.click(timeout=2_000, force=True)
                            acted = True
                            await page.wait_for_timeout(400)
                    except PlaywrightTimeoutError:
                        pass
            if not acted:
                break
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)

    # generation --------------------------------------------------------
    async def _open_combo(self, page: Page, index: int) -> bool:
        combo = page.locator(COMBOBOX).nth(index)
        if await combo.get_attribute("aria-disabled") == "true":
            return False
        try:
            await combo.click(timeout=3_000)
        except PlaywrightTimeoutError:
            return False
        return True

    async def _close_menu(self, page: Page) -> None:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)

    async def _find_model_combo(self, page: Page) -> int:
        """Index of the combobox whose panel lists models by data-model-key.

        The combobox holding a model label is tried first (stable class
        prefix); every candidate is still confirmed by opening its panel,
        which can take several seconds on a cold page.
        """
        combos = page.locator(COMBOBOX)
        # A promo dialog can appear seconds after load and swallow clicks, so
        # dismiss overlays and scan once more before giving up.
        for attempt in range(2):
            if attempt:
                await self._dismiss_overlays(page)
            count = await combos.count()
            hinted = [i for i in range(count) if await combos.nth(i).locator(MODEL_LABEL).count()]
            for index in hinted + [i for i in range(count) if i not in hinted]:
                if not await self._open_combo(page, index):
                    continue
                try:
                    await page.locator(MODEL_PANEL_OPTIONS).first.wait_for(state="visible", timeout=8_000)
                    await self._close_menu(page)
                    return index
                except PlaywrightTimeoutError:
                    await self._close_menu(page)
        raise BrowserError("model selector not found")

    async def _open_model_panel(self, page: Page, model_index: int) -> None:
        if not await self._open_combo(page, model_index):
            raise BrowserError("model selector is disabled")
        try:
            await page.locator(MODEL_PANEL_OPTIONS).first.wait_for(state="visible", timeout=8_000)
        except PlaywrightTimeoutError as exc:
            raise BrowserError("model dropdown did not open") from exc

    async def _select_model(self, page: Page, model_key: str, model_index: int) -> None:
        await self._open_model_panel(page, model_index)
        option = page.locator(MODEL_OPTION.format(key=model_key)).first
        if not await option.count():
            await self._close_menu(page)
            raise BrowserError(f"model {model_key} is not offered by the UI")
        await option.scroll_into_view_if_needed()
        await option.click()
        await page.wait_for_timeout(900)
        await self._dismiss_overlays(page)

    async def _assert_model_selected(self, page: Page, model_key: str, model_index: int) -> None:
        await self._open_model_panel(page, model_index)
        pressed = await page.locator(MODEL_OPTION.format(key=model_key)).first.get_attribute("aria-pressed")
        await self._close_menu(page)
        if pressed != "true":
            raise BrowserError(f"model {model_key} is not selected")

    async def _menu_options(self, page: Page, combo_index: int):
        """Options of the listbox owned by a combobox, via aria-controls."""
        popup_id = await page.locator(COMBOBOX).nth(combo_index).get_attribute("aria-controls")
        if not popup_id:
            return None
        options = page.locator(f'[id="{popup_id}"] li[role="option"]')
        try:
            await options.first.wait_for(state="visible", timeout=2_500)
        except PlaywrightTimeoutError:
            return None
        return options

    async def _select_mode(self, page: Page, spec: GenerationSpec, model_index: int) -> None:
        if spec.mode == "first_last":
            target = self.settings.mode_index_first_last
        else:
            target = self.settings.mode_index_unified
        # The reference-mode selector is the first listbox combobox after the
        # model selector.
        combos = page.locator(COMBOBOX)
        for combo_index in range(model_index + 1, await combos.count()):
            if not await self._open_combo(page, combo_index):
                continue
            options = await self._menu_options(page, combo_index)
            if options is None or await page.locator(MODEL_PANEL_OPTIONS).count():
                await self._close_menu(page)
                continue
            count = await options.count()
            if target >= count:
                await self._close_menu(page)
                raise BrowserError(f"reference mode index {target} is outside the {count} available options")
            await options.nth(target).click()
            await page.wait_for_timeout(900)
            await self._dismiss_overlays(page)
            if not await self._open_combo(page, combo_index):
                raise BrowserError("reference mode selector became disabled")
            options = await self._menu_options(page, combo_index)
            selected = options is not None and await options.nth(target).get_attribute("aria-selected") == "true"
            await self._close_menu(page)
            if not selected:
                raise BrowserError(f"reference mode index {target} was not applied")
            return
        raise BrowserError("reference mode selector not found")

    async def _upload_references(self, page: Page, files: list[str]) -> None:
        if not files:
            return
        if not await page.locator(FIRST_SLOT).count():
            raise BrowserError("reference upload slot not found")
        await self._drop(page, FIRST_SLOT, files)
        deadline = time.time() + 300
        while time.time() < deadline:
            bodies = await page.locator(REFERENCE_BODY).count()
            busy = await page.locator('[id*="reference-upload"][aria-busy="true"]').count()
            if bodies >= len(files) and not busy:
                return
            await page.wait_for_timeout(500)
        raise BrowserError("reference upload did not finish")

    async def _upload_frames(self, page: Page, first: str | None, last: str | None) -> None:
        if not first:
            raise BrowserError("first frame is required")
        await self._drop_and_wait(page, FIRST_SLOT, first)
        if last:
            try:
                await page.locator(SECOND_SLOT).first.wait_for(state="attached", timeout=30_000)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("last-frame upload slot did not appear") from exc
            await self._drop_and_wait(page, SECOND_SLOT, last)

    async def _drop_and_wait(self, page: Page, selector: str, path: str) -> None:
        if not await page.locator(selector).count():
            raise BrowserError(f"upload slot {selector} not found")
        await self._drop(page, selector, [path])
        # An accepted frame replaces its empty slot.
        try:
            await page.locator(selector).first.wait_for(state="detached", timeout=90_000)
        except PlaywrightTimeoutError as exc:
            raise BrowserError("frame upload was not accepted") from exc

    async def _drop(self, page: Page, selector: str, files: list[str]) -> None:
        payload = [
            {
                "base64": base64.b64encode(Path(path).read_bytes()).decode("ascii"),
                "name": Path(path).name,
                "mime": _mime_for(Path(path)),
            }
            for path in files
        ]
        result = await page.evaluate(
            """({files, selector}) => {
                const el = document.querySelector(selector);
                if (!el) return 'missing';
                const dt = new DataTransfer();
                for (const file of files) {
                    const bytes = Uint8Array.from(atob(file.base64), c => c.charCodeAt(0));
                    dt.items.add(new File([bytes], file.name, {type: file.mime}));
                }
                for (const type of ['dragenter', 'dragover', 'drop']) {
                    el.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt}));
                }
                return 'dispatched';
            }""",
            {"files": payload, "selector": selector},
        )
        if result != "dispatched":
            raise BrowserError(f"upload drop failed: {result}")

    async def _open_settings_button(self, page: Page, button) -> bool:
        """Open one settings popover. The toolbar scrolls horizontally and a
        clipped trigger does not open its popover, so center it first."""
        if not await button.is_visible():
            return False
        await button.evaluate("e => e.scrollIntoView({block: 'nearest', inline: 'center'})")
        await page.wait_for_timeout(250)
        try:
            await button.click(timeout=2_500)
        except PlaywrightTimeoutError:
            return False
        await page.wait_for_timeout(400)
        return True

    async def _set_radio(self, page: Page, value: str) -> None:
        if not value or value == "adaptive":
            return
        if await self._click_radio_in_visible_popover(page, value) is True:
            await self._close_menu(page)
            return
        buttons = page.locator(SETTINGS_BUTTON)
        for index in range(await buttons.count()):
            if not await self._open_settings_button(page, buttons.nth(index)):
                continue
            selected = await self._click_radio_in_visible_popover(page, value)
            await self._close_menu(page)
            if selected is True:
                return
        raise BrowserError(f"could not select setting {value}")

    @staticmethod
    async def _click_radio_in_visible_popover(page: Page, value: str) -> bool | None:
        return await page.evaluate(
            """(value) => {
                const pops = Array.from(document.querySelectorAll('.lv-popover-inner'))
                    .filter(e => e.offsetWidth || e.offsetHeight);
                // Values are language-neutral but not consistently cased (2K, 4k).
                const wanted = value.toLowerCase();
                for (const pop of pops) {
                    const input = Array.from(pop.querySelectorAll('input[type="radio"]'))
                        .find(i => i.value.toLowerCase() === wanted);
                    if (!input) continue;
                    if (input.disabled) return input.checked;
                    input.click();
                    return input.checked;
                }
                return null;
            }""",
            value,
        )

    async def _set_duration(self, page: Page, seconds: int) -> None:
        buttons = page.locator(SETTINGS_BUTTON)
        for index in range(await buttons.count()):
            if not await self._open_settings_button(page, buttons.nth(index)):
                continue
            inputs = page.locator(f'{VISIBLE_POPOVER}:visible input[role="spinbutton"]')
            if await inputs.count():
                spin = inputs.first
                await spin.fill(str(seconds))
                await spin.press("Enter")
                accepted = await spin.input_value() == str(seconds)
                await self._close_menu(page)
                if not accepted:
                    raise BrowserError("duration input did not accept the requested value")
                return
            await self._close_menu(page)
        raise BrowserError("duration selector not found")

    async def _caret_to_end(self, page: Page) -> None:
        await page.locator(EDITOR).first.evaluate(
            """el => {
                el.focus();
                const range = document.createRange();
                range.selectNodeContents(el.lastElementChild || el);
                range.collapse(false);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
            }"""
        )
        await page.wait_for_timeout(50)

    async def _enter_prompt(self, page: Page, spec: GenerationSpec) -> None:
        editor = page.locator(EDITOR).first
        if not (await editor.count() and await editor.is_visible()):
            textarea = page.locator(TEXTAREA).first
            if spec.mode == "unified" or not await textarea.count():
                raise BrowserError("prompt field not found")
            await self._enter_plain_prompt(page, textarea, spec)
            return
        prompt = editor
        await prompt.click()
        await prompt.press("ControlOrMeta+A")
        await prompt.press("Backspace")
        plan = prompt_plan(spec)
        for kind, value in plan:
            await self._caret_to_end(page)
            if kind == "text":
                # One native insertText input event per segment. Per-key typing
                # drops characters once mention chips (React node views) exist.
                await page.keyboard.insert_text(value)
                await page.wait_for_timeout(150)
            else:
                await self._insert_chip(page, *value)
        await page.wait_for_timeout(400)
        if spec.mode == "unified":
            chips = await prompt.locator(CHIP).count()
            expected = len(spec.images) + len(spec.audio)
            if chips < expected:
                raise BrowserError(f"expected {expected} reference chips, found {chips}")
        wanted = plan_text(plan)
        entered = _normalize_space(await self._editor_plain_text(page))
        if entered != wanted:
            raise BrowserError("prompt text in the editor does not match the request")

    async def _enter_plain_prompt(self, page: Page, textarea, spec: GenerationSpec) -> None:
        (_kind, text), = prompt_plan(spec)
        await textarea.click()
        await textarea.press("ControlOrMeta+A")
        await textarea.press("Backspace")
        await page.keyboard.insert_text(text)
        await page.wait_for_timeout(300)
        if _normalize_space(await textarea.input_value()) != _normalize_space(text):
            raise BrowserError("prompt text in the editor does not match the request")

    @staticmethod
    async def _editor_plain_text(page: Page) -> str:
        """The editor's text without mention chips: our own prompt read back,
        never UI copy, so it is safe to compare in any locale."""
        return await page.locator(EDITOR).first.evaluate(
            """(el, chip) => {
                const copy = el.cloneNode(true);
                copy.querySelectorAll(chip).forEach(node => node.remove());
                return copy.textContent;  // own-text-ok: reads back the requested prompt
            }""",
            CHIP,
        )

    async def _insert_chip(self, page: Page, kind: str, index: int) -> None:
        prompt = page.locator(EDITOR).first
        before = await prompt.locator(CHIP).count()
        await page.keyboard.type(" @", delay=30)
        try:
            await page.locator(MENU_OPTIONS).first.wait_for(state="visible", timeout=8_000)
        except PlaywrightTimeoutError as exc:
            raise BrowserError("reference mention menu did not open") from exc
        if kind == "image":
            options = page.locator(f"{MENU_OPTIONS}:has(img)")
        else:
            options = page.locator(f"{MENU_OPTIONS}:has(svg):not(:has(img))")
        if index < 1 or index > await options.count():
            raise BrowserError(f"reference {kind}{index} is not present in the mention menu")
        await options.nth(index - 1).click()
        deadline = time.time() + 10
        while time.time() < deadline:
            if await prompt.locator(CHIP).count() > before:
                return
            await page.wait_for_timeout(200)
        raise BrowserError(f"reference {kind}{index} chip was not inserted")

    async def _submit(self, page: Page, spec: GenerationSpec, capture: _GenerateCapture) -> GenerationOutcome:
        """Click submit, observing exactly one generate request.

        A second click happens only when the first sent no generate request at
        all and opened a fresh workspace instead (verified Dreamina behavior).
        Once any generate request is seen, nothing is clicked again, and any
        later error yields an uncertain outcome tracked by submit_id.
        """
        submit = page.locator(SUBMIT).last
        warnings: list[str] = []
        clicked = False
        try:
            if capture.requests:
                warnings.append("a generate request was sent before submit was clicked")
            else:
                if await submit.is_disabled():
                    raise NotSubmitted("submit button is disabled")
                for attempt in range(2):
                    url_before = page.url
                    clicked = True
                    await submit.click()
                    if await capture.wait(self.submit_timeout) or capture.requests:
                        break
                    # The first click on a fresh page can create a workspace
                    # without submitting; only then is a second click safe.
                    if attempt == 0 and "workspace=" in page.url and "workspace=" not in url_before:
                        continue
                    raise NotSubmitted("no generate request was sent; nothing was charged")
            if not capture.responses:
                await capture.wait(self.submit_timeout)
            return await self._read_outcome(capture, spec, warnings)
        except (GenerationRejected, NotSubmitted):
            raise
        except Exception as exc:
            if capture.submit_id:
                return self._uncertain(capture, f"generate outcome unreadable ({type(exc).__name__})", warnings)
            if clicked or capture.requests:
                raise SubmitUncertain(
                    "submit was clicked but its outcome is unknown; check Dreamina history before retrying"
                ) from None
            raise

    def _uncertain(self, capture: _GenerateCapture, reason: str, warnings: list[str] | None = None) -> GenerationOutcome:
        request = _request_body(capture.requests[0]) if capture.requests else {}
        return GenerationOutcome(
            submit_id=str(capture.submit_id),
            profile_id="",
            request=request,
            warnings=[*(warnings or []), f"{reason}; tracking by submit_id"],
            uncertain=True,
        )

    async def _read_outcome(
        self, capture: _GenerateCapture, spec: GenerationSpec, warnings: list[str]
    ) -> GenerationOutcome:
        if len(capture.requests) > 1:
            warnings.append(f"{len(capture.requests)} generate requests were observed")
        if not capture.responses:
            if not capture.submit_id:
                raise SubmitUncertain("generate request had no submit_id and no response arrived") from None
            return self._uncertain(capture, "generate response was not observed", warnings)
        response = capture.responses[0]
        request_body = _request_body(capture.requests[0] if capture.requests else response.request)
        try:
            payload = await response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            if not capture.submit_id:
                raise SubmitUncertain("generate response was unreadable and the request had no submit_id") from None
            return self._uncertain(capture, "generate response was unreadable", warnings)
        ret = str(payload.get("ret"))
        if ret != "0":
            message = str(payload.get("errmsg") or "Dreamina rejected the generation")
            raise GenerationRejected(f"Dreamina rejected the generation (ret={ret}): {message}"[:300], ret=ret)
        submit_id = (
            ((payload.get("data") or {}).get("aigc_data") or {}).get("submit_id")
            or capture.submit_id
            or request_body.get("submit_id")
        )
        if not submit_id:
            raise SubmitUncertain("generate response did not contain a submit_id") from None
        warnings.extend(validate_captured_request(request_body, spec))
        return GenerationOutcome(submit_id=str(submit_id), profile_id="", request=request_body, warnings=warnings)

    async def _screenshot(self, page: Page, directory: Path, name: str = "failure.png") -> Path | None:
        path = directory / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path))
            return path
        except Exception:
            return None


def validate_captured_request(request_body: dict[str, Any], spec: GenerationSpec) -> list[str]:
    """Compare a captured generate request with what was asked for."""
    warnings: list[str] = []
    try:
        draft = request_body.get("draft_content")
        if isinstance(draft, str):
            draft = json.loads(draft)
        params = draft["component_list"][0]["abilities"]["gen_video"]["text_to_video_params"]
        video = params["video_gen_inputs"][0]
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError):
        return ["captured request did not contain video generation parameters"]

    if params.get("model_req_key") != spec.model.dreamina_key:
        warnings.append("captured model key did not match the request")
    if int(video.get("duration_ms") or 0) != spec.duration_seconds * 1000:
        warnings.append("captured duration did not match the request")
    if str(video.get("resolution") or "").lower() != spec.resolution.lower():
        warnings.append("captured resolution did not match the request")

    if spec.mode == "first_last":
        if not video.get("first_frame_image"):
            warnings.append("captured request is missing the first frame")
        if spec.last_frame and not video.get("end_frame_image"):
            warnings.append("captured request is missing the last frame")
    else:
        unified = video.get("unified_edit_input") or {}
        material_types = [item.get("material_type") for item in unified.get("material_list") or []]
        meta_types = [item.get("meta_type") for item in unified.get("meta_list") or []]
        if material_types.count("image") != len(spec.images):
            warnings.append("captured image material count did not match the request")
        if material_types.count("audio") != len(spec.audio):
            warnings.append("captured audio material count did not match the request")
        if spec.images and "image" not in meta_types:
            warnings.append("captured metadata is missing image references")
        if spec.audio and "audio" not in meta_types:
            warnings.append("captured metadata is missing audio references")
    return warnings


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:300]


def _normalize_space(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split())


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".wav":
        return "audio/wav"
    return "audio/mpeg"

"""Signed read-only Dreamina HTTP calls (credits, history, prices) and result links."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
COMMERCE_HOST = "commerce-api-sg.capcut.com"
MWEB_HOST = "mweb-api-sg.capcut.com"
# Each account is served by its region's cluster (the store-idc cookie): a US
# account's session is "login error" on the Singapore hosts. Hosts seen in the
# browser's own traffic are saved with the session and win; this table covers
# sessions saved without them.
REGION_HOSTS = {
    "alisg": (COMMERCE_HOST, MWEB_HOST),
    "useast5": ("commerce-us-ttp2.us.capcut.com", "dreamina-api-us-ttp2.us.capcut.com"),
}


@dataclass(frozen=True)
class ApiHosts:
    commerce: str = COMMERCE_HOST
    mweb: str = MWEB_HOST


def _upstream_host(value: Any) -> str | None:
    return value if isinstance(value, str) and value.endswith(".capcut.com") and "/" not in value else None


def resolve_hosts(cookies: list[dict[str, Any]], saved: dict[str, Any] | None = None) -> ApiHosts:
    """API hosts for a session: saved from its traffic, else by region, else Singapore."""
    commerce, mweb = REGION_HOSTS.get(_cookie_value(cookies, ("store-idc",)) or "", (COMMERCE_HOST, MWEB_HOST))
    saved = saved if isinstance(saved, dict) else {}
    return ApiHosts(
        commerce=_upstream_host(saved.get("commerce")) or commerce,
        mweb=_upstream_host(saved.get("mweb")) or mweb,
    )


class UpstreamError(Exception):
    code = "upstream_error"

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status


class SessionExpired(UpstreamError):
    code = "session_expired"


class UpstreamRejected(UpstreamError):
    code = "upstream_rejected"


class UpstreamTimeout(UpstreamError):
    code = "upstream_timeout"


class NoWebId(UpstreamError):
    code = "session_expired"


HISTORY_SUCCEEDED = 50
HISTORY_FAILED = 30


@dataclass(frozen=True)
class HistoryState:
    status: int | None
    video_url: str | None
    credits_used: int | None
    fail_code: str | None
    fail_msg: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == HISTORY_SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.status == HISTORY_FAILED


def parse_history(record: dict[str, Any]) -> HistoryState:
    status = record.get("status")
    credits = find_value(record, "credits_consume")
    fail_code = record.get("fail_code")
    return HistoryState(
        status=int(status) if isinstance(status, (int, str)) and str(status).lstrip("-").isdigit() else None,
        video_url=find_value(record, "video_url") or None,
        credits_used=int(credits) if isinstance(credits, (int, float)) else None,
        fail_code=str(fail_code) if fail_code not in (None, "", 0, "0") else None,
        fail_msg=str(record.get("fail_msg") or "") or None,
    )


@dataclass(frozen=True)
class Credits:
    total: int
    vip: int = 0
    gift: int = 0
    purchase: int = 0


def _read_cookie_file(path: Path) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        cookies = data.get("cookies", [])
        user_agent = data.get("user_agent") or DEFAULT_UA
        hosts = data.get("hosts") or {}
    else:
        cookies, user_agent, hosts = data, DEFAULT_UA, {}
    if not isinstance(cookies, list):
        raise ValueError("cookie file must contain a list")
    return cookies, user_agent, hosts


def load_cookie_file(path: Path) -> tuple[list[dict[str, Any]], str]:
    cookies, user_agent, _hosts = _read_cookie_file(path)
    return cookies, user_agent


def save_cookie_file(
    path: Path, cookies: list[dict[str, Any]], user_agent: str, hosts: dict[str, str] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    data: dict[str, Any] = {"cookies": cookies, "user_agent": user_agent}
    if hosts:
        data["hosts"] = hosts
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _cookie_value(cookies: list[dict[str, Any]], names: tuple[str, ...]) -> str | None:
    for name in names:
        for cookie in cookies:
            if cookie.get("name") == name and cookie.get("value"):
                return str(cookie["value"])
    return None


def find_value(value: Any, key: str) -> Any:
    """First non-empty value for key anywhere in nested dicts/lists."""
    if isinstance(value, dict):
        if value.get(key) not in (None, ""):
            return value[key]
        for item in value.values():
            found = find_value(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_value(item, key)
            if found is not None:
                return found
    return None


class DreaminaHTTP:
    def __init__(
        self,
        cookies: list[dict[str, Any]],
        user_agent: str = DEFAULT_UA,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        hosts: ApiHosts | None = None,
    ):
        self.cookies = cookies
        self.user_agent = user_agent or DEFAULT_UA
        self.hosts = hosts or resolve_hosts(cookies)
        self.region = (
            _cookie_value(cookies, ("store-country-code", "loc", "region")) or "vn"
        ).upper()
        web_id = _cookie_value(cookies, ("_tea_web_id", "_v2_spipe_web_id", "webId"))
        if not web_id or not web_id.isdigit():
            raise NoWebId("no numeric web id cookie found")
        self.web_id = web_id
        # Match the platform the browser profile claims.
        ua = self.user_agent
        self.os_name = "windows" if "Windows" in ua else "linux" if "Linux" in ua and "Android" not in ua else "mac"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=20.0, read=timeout, write=timeout, pool=timeout),
            follow_redirects=True,
            transport=transport,
        )
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            self._client.cookies.set(
                name,
                str(value),
                domain=cookie.get("domain") or "dreamina.capcut.com",
                path=cookie.get("path") or "/",
            )

    @classmethod
    def from_cookie_file(
        cls, path: Path, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> "DreaminaHTTP":
        cookies, user_agent, hosts = _read_cookie_file(path)
        return cls(cookies, user_agent, transport=transport, hosts=resolve_hosts(cookies, hosts))

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DreaminaHTTP":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _headers(self, device_time: str, path: str) -> dict[str, str]:
        sign_source = f"9e2c|{path[-7:]}|7|8.4.0|{device_time}||11ac"
        return {
            "content-type": "application/json",
            "appid": "513641",
            "pf": "7",
            "appvr": "8.4.0",
            "app-sdk-version": "48.0.0",
            "device-time": device_time,
            "sign": hashlib.md5(sign_source.encode()).hexdigest(),
            "sign-ver": "1",
            "did": self.web_id,
            "lan": "en",
            "loc": self.region,
            "store-country-code": self.region.lower(),
            "store-country-code-src": "uid",
            "origin": "https://dreamina.capcut.com",
            "referer": "https://dreamina.capcut.com/ai-tool/generate",
            "user-agent": self.user_agent,
        }

    def _query(self) -> str:
        return urlencode(
            {
                "aid": "513641",
                "device_platform": "web",
                "region": self.region,
                "webId": self.web_id,
                "os": self.os_name,
                "web_component_open_flag": "1",
                "web_version": "7.5.0",
                "da_version": "3.3.40",
                "aigc_features": "app_lip_sync",
            }
        )

    async def post(
        self, host: str, path: str, body: dict[str, Any], *, retries: int = 3
    ) -> tuple[int, dict[str, Any]]:
        url = f"https://{host}{path}?{self._query()}"
        last_error: Exception | None = None
        for attempt in range(retries):
            device_time = str(int(time.time()))
            try:
                response = await self._client.post(
                    url, headers=self._headers(device_time, path), json=body
                )
            except httpx.TransportError as exc:
                last_error = UpstreamTimeout(f"transport error: {type(exc).__name__}", retryable=True)
            else:
                if response.status_code >= 500:
                    last_error = UpstreamError(
                        f"upstream HTTP {response.status_code}", retryable=True, status=response.status_code
                    )
                else:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise UpstreamError(
                            "upstream returned invalid JSON", status=response.status_code
                        ) from exc
                    if not isinstance(payload, dict):
                        raise UpstreamError("upstream returned an unexpected payload")
                    return response.status_code, payload
            if attempt < retries - 1:
                await asyncio.sleep((1.5 ** attempt) + random.uniform(0, 0.4))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _raise_for_ret(payload: dict[str, Any], status: int) -> None:
        ret = str(payload.get("ret", ""))
        if ret in {"0", ""}:
            return
        message = str(payload.get("errmsg") or "upstream rejected request")
        lowered = message.lower()
        if "shark" in lowered or ret == "-6":
            raise UpstreamRejected(message, status=status)
        if "login" in lowered or "session" in lowered or "auth" in lowered or status == 401:
            raise SessionExpired(message, status=status)
        raise UpstreamRejected(message, status=status)

    async def credits(self) -> Credits:
        status, payload = await self.post(self.hosts.commerce, "/commerce/v1/benefits/user_credit", {})
        self._raise_for_ret(payload, status)
        raw = payload.get("response") or "{}"
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        credit = (parsed or {}).get("credit") or {}
        vip = int(credit.get("vip_credit") or 0)
        gift = int(credit.get("gift_credit") or 0)
        purchase = int(credit.get("purchase_credit") or 0)
        return Credits(total=vip + gift + purchase, vip=vip, gift=gift, purchase=purchase)

    async def benefit_prices(self) -> dict[str, int]:
        """Per-unit credit prices for every video benefit type."""
        status, payload = await self.post(
            self.hosts.commerce,
            "/commerce/v3/resource/benefit_metadata",
            {"query_list": [{"resource_type": "aigc", "resource_id": "generate_video", "benefit_type_list": []}]},
        )
        self._raise_for_ret(payload, status)
        raw = payload.get("response") or "{}"
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        prices: dict[str, int] = {}
        for meta in (parsed or {}).get("metadata_list") or []:
            for benefit in meta.get("benefits_pay_strategy") or []:
                info = (benefit.get("credit_strategy") or {}).get("credit_pricing_info") or {}
                price = info.get("credit_unit_price")
                if benefit.get("benefit_type") and isinstance(price, int) and benefit.get("unit") == "second":
                    prices[benefit["benefit_type"]] = price
        return prices

    async def history_by_ids(self, submit_ids: list[str]) -> dict[str, Any]:
        status, payload = await self.post(
            self.hosts.mweb, "/mweb/v1/get_history_by_ids", {"submit_ids": submit_ids}
        )
        self._raise_for_ret(payload, status)
        data = payload.get("data") or {}
        if isinstance(data, dict):
            return data
        return {}

    async def history_record(self, submit_id: str) -> dict[str, Any] | None:
        data = await self.history_by_ids([submit_id])
        record = data.get(submit_id)
        if record is None and isinstance(data, dict):
            record = next(
                (
                    value
                    for value in data.values()
                    if isinstance(value, dict) and value.get("submit_id") == submit_id
                ),
                None,
            )
        return record

    async def recent_history(self, count: int = 20) -> list[dict[str, Any]]:
        status, payload = await self.post(
            self.hosts.mweb,
            "/mweb/v1/get_history",
            {"count": count, "history_type_list": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]},
        )
        self._raise_for_ret(payload, status)
        data = payload.get("data") or {}
        return list(data.get("records_list") or [])


# result links ---------------------------------------------------------------------
def link_expiry(url: str | None) -> datetime | None:
    """When a pre-signed result link stops working.

    The CDN puts the expiry in the path as hex Unix seconds
    (https://v16-cc.capcut.com/<signature>/<expiry-hex>/video/...); links
    last about six hours from when history returns them.
    """
    if not url:
        return None
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    candidates = segments[1:2] if len(segments) > 1 else []
    for raw in candidates:
        if re.fullmatch(r"[0-9a-f]{8}", raw):
            seconds = int(raw, 16)
            if 1_600_000_000 < seconds < 4_102_444_800:  # 2020..2100
                return datetime.fromtimestamp(seconds, timezone.utc)
    for key, value in parse_qsl(parts.query):
        if key.lower() in {"x-expires", "expires"} and value.isdigit():
            return datetime.fromtimestamp(int(value), timezone.utc)
    return None

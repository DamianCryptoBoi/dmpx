"""Accounts are served by their region's API cluster."""

import json
from collections import Counter

import httpx

from dreamina.browser import AccountBrowser
from dreamina.http_client import (
    COMMERCE_HOST,
    MWEB_HOST,
    DreaminaHTTP,
    load_cookie_file,
    resolve_hosts,
    save_cookie_file,
)

US = ("commerce-us-ttp2.us.capcut.com", "dreamina-api-us-ttp2.us.capcut.com")


def cookies(idc=None):
    jar = [{"name": "_tea_web_id", "value": "1234567890123456789"}]
    if idc:
        jar.append({"name": "store-idc", "value": idc})
    return jar


def test_hosts_follow_saved_traffic_then_region_then_singapore():
    assert resolve_hosts(cookies()) == resolve_hosts(cookies("alisg"))
    assert (resolve_hosts(cookies()).commerce, resolve_hosts(cookies()).mweb) == (COMMERCE_HOST, MWEB_HOST)
    us = resolve_hosts(cookies("useast5"))
    assert (us.commerce, us.mweb) == US
    saved = resolve_hosts(cookies("useast5"), {"commerce": "commerce-eu.capcut.com", "mweb": "evil.example.com/x"})
    assert saved.commerce == "commerce-eu.capcut.com" and saved.mweb == US[1]  # only *.capcut.com hosts are trusted


def test_cookie_file_round_trips_hosts(tmp_path):
    path = tmp_path / "cookies.json"
    save_cookie_file(path, cookies("useast5"), "ua", {"commerce": US[0], "mweb": US[1]})
    assert json.loads(path.read_text())["hosts"] == {"commerce": US[0], "mweb": US[1]}
    assert load_cookie_file(path) == (cookies("useast5"), "ua")  # older callers unchanged
    client = DreaminaHTTP.from_cookie_file(path)
    assert (client.hosts.commerce, client.hosts.mweb) == US


async def test_us_account_reads_credits_from_the_us_cluster(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, json={"ret": "0", "response": json.dumps({"credit": {"gift_credit": 5}})})

    path = tmp_path / "cookies.json"
    save_cookie_file(path, cookies("useast5"), "ua")
    async with DreaminaHTTP.from_cookie_file(path, transport=httpx.MockTransport(handler)) as client:
        credits = await client.credits()
    assert credits.total == 5 and seen == [US[0]]


def test_browser_session_learns_the_busiest_host_of_each_kind():
    session = AccountBrowser.__new__(AccountBrowser)
    session.api_hosts = {"commerce": Counter(), "mweb": Counter()}
    for url in [
        f"https://{US[0]}/commerce/v1/benefits/user_credit",
        f"https://{US[1]}/mweb/v1/get_user_info",
        f"https://{US[1]}/mweb/v1/feed",
        "https://dreamina-api.us.capcut.com/mweb/v1/get_enable_list",
        "https://mon.capcutapi.us/monitor_browser/collect",
        "https://evil.example.com/commerce/v1/x",
    ]:
        AccountBrowser.note_request(session, url)
    assert AccountBrowser.hosts(session) == {"commerce": US[0], "mweb": US[1]}

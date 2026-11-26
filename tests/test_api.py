import dataclasses
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, API_KEY, MP3, PNG, make_ready
from dreamina.app import create_app

AUTH = {"Authorization": f"Bearer {API_KEY}"}
SUCCESS = {"status": 50, "video_url": "https://cdn/x.mp4", "credits_consume": 84}
FORM = {
    "model": "seedance-2.5-flash",
    "prompt": "@image1 dances to @audio1",
    "duration_seconds": "4",
    "resolution": "480p",
    "aspect_ratio": "16:9",
}


@pytest.fixture
def client(app):
    with TestClient(app, follow_redirects=False) as client:
        yield client


def files():
    return [("images", ("a.png", PNG, "image/png")), ("audio", ("a.mp3", MP3, "audio/mpeg"))]


def post(client, key="key-1", data=None, upload=None):
    headers = dict(AUTH)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/v1/generations", data=data or FORM, files=upload if upload is not None else files(), headers=headers)


def wait_status(client, job_id, wanted, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/generations/{job_id}", headers=AUTH).json()
        if body["status"] == wanted and (wanted != "succeeded" or body["media"] == "ready"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {wanted}: {body}")


def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_v1_requires_api_key(client):
    response = client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert client.get("/v1/models", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_v1_disabled_without_configured_key(settings, services):
    services.settings = dataclasses.replace(settings, api_key="")
    with TestClient(create_app(settings, services=services)) as client:
        response = client.get("/v1/models", headers=AUTH)
    assert response.status_code == 503 and response.json()["error"]["code"] == "api_disabled"


def test_models(client):
    body = client.get("/v1/models", headers=AUTH).json()
    flash = next(model for model in body["models"] if model["name"] == "seedance-2.5-flash")
    assert flash["pricing_credits_per_second"] == {"480p": 21, "720p": 44}
    assert flash["reference_limits"]["images"] == 10


def test_generation_lifecycle_and_idempotency(client, services, dreamina):
    make_ready(services, "alpha")
    response = post(client)
    assert response.status_code == 202
    job = response.json()
    assert response.headers["location"] == f"/v1/generations/{job['id']}"
    assert job["status"] == "queued" and job["credits_estimate"] == 84
    assert job["references"] == {"images": 1, "audio": 1, "first_frame": False, "last_frame": False}

    replay = post(client)
    assert replay.status_code == 200 and replay.json()["id"] == job["id"]
    conflict = post(client, data={**FORM, "prompt": "@image1 something else @audio1"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"

    dreamina.history["submit-1"] = SUCCESS
    done = wait_status(client, job["id"], "succeeded")
    assert done["credits_used"] == 84
    content = client.get(done["content_url"], headers=AUTH)
    assert content.status_code == 200 and content.headers["content-type"] == "video/mp4"
    assert client.get(done["content_url"]).status_code == 401

    [(account_id, spec)] = services.browsers.generate_calls
    assert account_id == "alpha" and len(spec.images) == 1 and len(spec.audio) == 1
    listing = client.get("/v1/generations?limit=5", headers=AUTH).json()
    assert listing["total"] == 1 and listing["data"][0]["id"] == job["id"]


@pytest.mark.parametrize(
    "data,upload,message",
    [
        ({**FORM, "duration_seconds": "four"}, None, "integer"),
        ({**FORM, "duration_seconds": "31"}, None, "between"),
        ({**FORM, "prompt": "@image2"}, None, "@image2"),
        (FORM, [("images", ("a.png", b"not an image", "image/png")), ("audio", ("a.mp3", MP3, "audio/mpeg"))], "supported image"),
        (FORM, [("images", ("a.gif", b"GIF89a", "image/gif")), ("audio", ("a.mp3", MP3, "audio/mpeg"))], "content type"),
        ({**FORM, "prompt": "boat"}, files() + [("first_frame", ("f.png", PNG, "image/png"))], "cannot be combined"),
    ],
)
def test_generation_validation(client, services, data, upload, message):
    make_ready(services, "alpha")
    response = post(client, data=data, upload=upload)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_input"
    assert message in response.json()["error"]["message"]
    assert not any(services.settings.jobs_dir.iterdir())


def test_idempotency_key_is_required(client, services):
    make_ready(services, "alpha")
    response = post(client, key=None)
    assert response.status_code == 400 and "Idempotency-Key" in response.json()["error"]["message"]


def test_first_last_frame_request(client, services):
    make_ready(services, "alpha")
    data = {"model": "seedance-2.0-mini", "prompt": "boat drifts", "duration_seconds": "4", "resolution": "720p"}
    upload = [("first_frame", ("f.png", PNG, "image/png")), ("last_frame", ("l.png", PNG, "image/png"))]
    response = post(client, data=data, upload=upload)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == "first_last" and body["aspect_ratio"] == "adaptive"
    assert body["references"]["first_frame"] and body["references"]["last_frame"]


def test_capacity_errors(client, services):
    response = post(client, key="a")
    assert response.status_code == 503 and response.json()["error"]["code"] == "no_account_available"
    make_ready(services, "alpha", credits=10)
    response = post(client, key="b")
    assert response.status_code == 402 and response.json()["error"]["code"] == "insufficient_credits"


def test_content_before_completion(client, services):
    make_ready(services, "alpha")
    services.browsers.next_submit_id = "never-finishes"
    job = post(client).json()
    response = client.get(f"/v1/generations/{job['id']}/content", headers=AUTH)
    assert response.status_code == 409 and response.json()["error"]["code"] == "not_ready"
    assert client.get("/v1/generations/missing", headers=AUTH).status_code == 404


def test_credits_hide_emails(client, services, dreamina):
    dreamina.credits["alpha"] = 500
    make_ready(services, "alpha", credits=500)
    response = client.get("/v1/credits", headers=AUTH)
    assert response.json()["total_available"] == 500
    assert "@example.com" not in response.text


# dashboard -------------------------------------------------------------------
def login(client, password=ADMIN_PASSWORD):
    return client.post("/admin/login", data={"password": password})


def csrf_token(client, path="/admin/accounts"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_admin_requires_login(client):
    response = client.get("/admin")
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert client.get("/admin/status.json").status_code == 401


def test_admin_login_sets_strict_cookie(client):
    assert login(client, "wrong").status_code == 401
    response = login(client)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/admin" in cookie
    assert client.get("/admin").status_code == 200
    for page in ("/admin/accounts", "/admin/jobs", "/admin/models"):
        assert client.get(page).status_code == 200, page


def test_admin_mutations_require_csrf(client, services):
    login(client)
    response = client.post("/admin/accounts/alpha/toggle", data={"csrf": "forged"})
    assert response.status_code == 403
    assert services.db.get_account("alpha")["enabled"] == 1
    response = client.post("/admin/accounts/alpha/toggle", data={"csrf": csrf_token(client)})
    assert response.status_code == 303
    assert services.db.get_account("alpha")["enabled"] == 0


def test_admin_login_throttle(client):
    for _ in range(5):
        login(client, "wrong")
    response = login(client)
    assert response.status_code == 401 and "Too many" in response.text


def test_admin_add_account_keeps_password_out_of_database(client, services, env_file):
    login(client)
    response = client.post(
        "/admin/accounts",
        data={
            "csrf": csrf_token(client),
            "account_id": "gamma",
            "label": "Gamma",
            "email": "gamma@example.com",
            "password": "gamma-secret-pw",
        },
    )
    assert response.status_code == 303
    assert "DREAMINA_ACCOUNT_GAMMA_PASSWORD=gamma-secret-pw" in env_file.read_text()
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    rows = json.dumps(services.db.list_accounts())
    assert "gamma@example.com" in rows and "gamma-secret-pw" not in rows
    deadline = time.time() + 5
    while services.db.get_account("gamma")["session_state"] != "ready" and time.time() < deadline:
        time.sleep(0.05)
    assert services.db.get_account("gamma")["session_state"] == "ready"
    assert "gamma-secret-pw" not in client.get("/admin/accounts").text


def test_admin_remove_requires_typed_confirmation(client, services, env_file):
    login(client)
    client.post("/admin/accounts/beta/remove", data={"csrf": csrf_token(client), "confirm": "nope"})
    assert services.db.get_account("beta") is not None
    client.post("/admin/accounts/beta/remove", data={"csrf": csrf_token(client), "confirm": "beta"})
    assert services.db.get_account("beta") is None
    assert "DREAMINA_ACCOUNT_BETA" not in env_file.read_text()


def test_admin_job_creation_uses_shared_validation(client, services):
    make_ready(services, "alpha")
    login(client)
    token = csrf_token(client, "/admin/jobs")
    data = {**FORM, "prompt": "a boat", "csrf": token, "idempotency_key": "dash-1"}
    response = client.post("/admin/jobs", data=data)
    assert response.status_code == 303 and response.headers["location"].startswith("/admin/jobs/")
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200 and "a boat" in detail.text


def test_non_ascii_credentials_are_rejected_not_crashed(client):
    response = client.get("/v1/models", headers={"Authorization": "Bearer clé-ünïcode".encode("utf-8")})
    assert response.status_code == 401
    response = client.get("/admin", headers={"Cookie": "dreamina_admin=bödy.sig".encode("utf-8")})
    assert response.status_code == 303


def test_admin_login_refuses_file_parts(client):
    response = client.post("/admin/login", files={"password": ("p.txt", b"x" * 10, "text/plain")})
    assert response.status_code == 400

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.settings import Settings

ADMIN = {"X-Requested-With": "kotodama"}
MAC = "907069f466b0"


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, max_recording_bytes=1000)
    with TestClient(create_app(settings)) as c:
        yield c


def login(c):
    r = c.post("/admin/login", data={"password": "pw"}, follow_redirects=False)
    assert r.status_code == 303


def request_adoption(c, mac="90:70:69:F4:66:B0"):
    r = c.post("/api/v1/adoption/request", json={"device_id": mac, "hardware": "test", "firmware": "0.0.1"})
    assert r.status_code == 200, r.text
    return r.json()


def adopt(c, name="Kitchen"):
    req = request_adoption(c)
    login(c)
    r = c.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": req["code"], "name": name},
               headers=ADMIN)
    assert r.status_code == 200, r.text
    status = c.get(f"/api/v1/adoption/{req['request_id']}").json()
    assert status["state"] == "approved"
    return status["token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_ping(client):
    body = client.get("/api/v1/ping").json()
    assert body["ok"] and body["server"] == "kotodama"


def test_adoption_flow_hands_out_token_once(client):
    token = adopt(client)
    r = client.get("/api/v1/device/config", headers=bearer(token))
    assert r.status_code == 200
    config = r.json()
    assert config["device"] == {"id": MAC, "name": "Kitchen"}
    assert config["version"] == 1
    assert config["layout"]["id"] == "character_clock"
    assert set(config["character"]["states"]) == {"idle", "listening", "thinking", "speaking", "sleeping", "error"}
    assert client.get(f"/api/v1/device/config", headers={**bearer(token), "If-None-Match": '"1"'}).status_code == 304


def test_token_is_only_returned_once(client):
    req = request_adoption(client)
    login(client)
    client.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": req["code"]}, headers=ADMIN)
    assert "token" in client.get(f"/api/v1/adoption/{req['request_id']}").json()
    assert client.get(f"/api/v1/adoption/{req['request_id']}").json() == {"state": "collected"}


def test_repeat_request_returns_same_code(client):
    a = request_adoption(client)
    b = request_adoption(client, mac=MAC)
    assert a["request_id"] == b["request_id"] and a["code"] == b["code"]


def test_admin_never_sees_code(client):
    req = request_adoption(client)
    login(client)
    overview = client.get("/admin/api/overview").text
    assert req["request_id"] in overview
    assert f'"code"' not in overview


def test_wrong_codes_reject_request(client):
    req = request_adoption(client)
    login(client)
    wrong = f"{(int(req['code']) + 1) % 1000:03d}"
    for _ in range(4):
        r = client.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": wrong}, headers=ADMIN)
        assert r.status_code == 403
    r = client.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": wrong}, headers=ADMIN)
    assert "rejected" in r.json()["detail"]
    assert client.get(f"/api/v1/adoption/{req['request_id']}").json() == {"state": "rejected"}
    r = client.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": req["code"]}, headers=ADMIN)
    assert r.status_code == 410


def test_admin_api_needs_login_and_csrf_header(client):
    assert client.get("/admin/api/overview").status_code == 401
    login(client)
    assert client.get("/admin/api/overview").status_code == 200
    assert client.put("/admin/api/defaults", json={"config": {}}).status_code == 403
    assert client.put("/admin/api/defaults", json={"config": {}}, headers=ADMIN).status_code == 200


def test_admin_pages_render(client):
    assert client.get("/admin", follow_redirects=False).status_code == 303
    adopt(client)
    for path in ("/admin", f"/admin/devices/{MAC}", "/admin/defaults", "/admin/characters"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_bad_token_rejected(client):
    assert client.get("/api/v1/device/config", headers=bearer("nope")).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/device/ws", headers=bearer("nope")) as ws:
            ws.receive_json()


def test_websocket_gets_config_and_live_updates(client):
    token = adopt(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        first = ws.receive_json()
        assert first["type"] == "config" and first["version"] == 1

        config = first["config"]
        config["audio"]["volume"] = 33
        config["display"]["theme"] = "ocean"
        config["display"]["colors"]["accent"] = "#123456"
        r = client.put(f"/admin/api/devices/{MAC}", json={"name": "Den", "config": config}, headers=ADMIN)
        assert r.json() == {"version": 2, "pushed": True}

        pushed = ws.receive_json()
        assert pushed["version"] == 2
        assert pushed["device"]["name"] == "Den"
        assert pushed["config"]["audio"]["volume"] == 33
        assert pushed["theme"]["accent"] == "#123456"
        assert pushed["theme"]["primary"] == "#4fd1c5"

        ws.send_json({"type": "status", "status": {"state": "idle", "rssi": -61, "battery": 95, "config_version": 2}})
        ws.send_json({"type": "log", "lines": [{"level": "I", "tag": "main", "message": "hello"}]})
        ws.send_json({"type": "config_request"})
        assert ws.receive_json()["version"] == 2  # all earlier messages have been handled by now

        r = client.post(f"/admin/api/devices/{MAC}/command", json={"command": "identify"}, headers=ADMIN)
        assert r.status_code == 200
        assert ws.receive_json() == {"type": "command", "command": "identify", "args": {}}

    device = client.get(f"/admin/api/devices/{MAC}").json()
    assert device["status"]["rssi"] == -61 and device["status"]["state"] == "idle"
    assert device["online"] is False
    logs = client.get(f"/admin/api/devices/{MAC}/logs").json()
    assert logs[-1]["message"] == "hello"


def test_command_to_offline_device(client):
    adopt(client)
    r = client.post(f"/admin/api/devices/{MAC}/command", json={"command": "reboot"}, headers=ADMIN)
    assert r.status_code == 409


def test_wifi_passwords_masked_and_preserved(client):
    token = adopt(client)
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["wifi"] = [{"ssid": "Thing Tank", "password": "supersecret"}]
    assert client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": config}, headers=ADMIN).status_code == 200

    seen = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    assert seen["wifi"] == [{"ssid": "Thing Tank", "password": "********"}]
    assert "supersecret" not in client.get(f"/admin/devices/{MAC}").text

    seen["audio"]["volume"] = 10
    assert client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": seen}, headers=ADMIN).status_code == 200
    device_view = client.get("/api/v1/device/config", headers=bearer(token)).json()
    assert device_view["config"]["wifi"] == [{"ssid": "Thing Tank", "password": "supersecret"}]

    seen["wifi"].append({"ssid": "Other", "password": "********"})
    r = client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": seen}, headers=ADMIN)
    assert r.status_code == 422


def test_invalid_config_rejected(client):
    adopt(client)
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    for path, value in ((("display", "layout"), "nope"), (("display", "brightness"), 500),
                        (("character",), "missing-character")):
        bad = {**config, "display": dict(config["display"])}
        target = bad
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        r = client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": bad}, headers=ADMIN)
        assert r.status_code == 422, path


def test_character_assets_are_sized(client):
    token = adopt(client)
    config = client.get("/api/v1/device/config", headers=bearer(token)).json()
    url = config["character"]["states"]["idle"]["urls"]["144"]
    r = client.get(url.replace("http://testserver", ""))
    assert r.status_code == 200 and r.headers["content-type"] == "image/gif"
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (144, 144) and im.n_frames > 1
    # speakers get opaque GIFs flattened onto their theme background (midnight = #0b1020)
    assert "&bg=0b1020" in url
    with Image.open(io.BytesIO(r.content)) as im:
        assert "transparency" not in im.info
        assert im.convert("RGB").getpixel((0, 0)) == (0x0b, 0x10, 0x20)
    assert client.get("/assets/characters/default/idle.gif?size=999").status_code == 404
    assert client.get("/assets/characters/default/idle.gif?size=144&bg=nothex").status_code == 422
    assert client.get("/assets/characters/../idle.gif").status_code == 404


def test_upload_character_state(client):
    login(client)
    assert client.post("/admin/api/characters", json={"id": "robot", "name": "Robot"}, headers=ADMIN).status_code == 201
    img = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (25, 0, 75, 50))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = client.post("/admin/api/characters/robot/states/idle", files={"file": ("idle.png", buf.getvalue(), "image/png")},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    gif = client.get("/assets/characters/robot/idle.gif").content
    with Image.open(io.BytesIO(gif)) as im:
        assert im.size == (240, 240)
        rgba = im.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((120, 120))[:3] == (255, 0, 0)

    r = client.post("/admin/api/characters/robot/states/idle", files={"file": ("x.png", b"not an image", "image/png")},
                    headers=ADMIN)
    assert r.status_code == 422
    assert client.delete("/admin/api/characters/default", headers=ADMIN).status_code == 400
    assert client.delete("/admin/api/characters/robot", headers=ADMIN).status_code == 204


def test_recordings(client):
    token = adopt(client)
    r = client.post("/api/v1/device/recordings?kind=utterance", content=b"RIFF" + b"\0" * 100,
                    headers={**bearer(token), "Content-Type": "audio/wav"})
    assert r.status_code == 201
    recs = client.get(f"/admin/api/devices/{MAC}/recordings").json()
    assert len(recs) == 1 and recs[0]["size"] == 104
    assert client.get(f"/admin/recordings/{recs[0]['id']}").content.startswith(b"RIFF")
    r = client.post("/api/v1/device/recordings", content=b"\0" * 2000,
                    headers={**bearer(token), "Content-Type": "audio/wav"})
    assert r.status_code == 413
    r = client.post("/api/v1/device/recordings", content=b"x", headers={**bearer(token), "Content-Type": "text/plain"})
    assert r.status_code == 415


@pytest.fixture
def fresh_client(tmp_path):
    settings = Settings(data_dir=tmp_path, public_url="http://testserver", secure_cookies=False)
    with TestClient(create_app(settings)) as c:
        yield c


def test_first_run_setup_then_login(fresh_client):
    c = fresh_client
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/admin/setup"
    assert c.get("/admin/login", follow_redirects=False).headers["location"] == "/admin/setup"
    r = c.post("/admin/setup", data={"password": "short", "confirm": "short"}, follow_redirects=False)
    assert r.status_code == 400
    r = c.post("/admin/setup", data={"password": "correct horse", "confirm": "correct horse"}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/admin/api/overview").status_code == 200

    c.post("/admin/logout")
    # setup is closed once a password exists
    r = c.post("/admin/setup", data={"password": "attacker pw!", "confirm": "attacker pw!"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/login"
    assert c.post("/admin/login", data={"password": "attacker pw!"}, follow_redirects=False).status_code == 401
    assert c.post("/admin/login", data={"password": "correct horse"}, follow_redirects=False).status_code == 303


def test_change_password(client):
    login(client)
    r = client.post("/admin/password", data={"current": "nope", "password": "new password 1",
                                             "confirm": "new password 1"})
    assert r.status_code == 400
    r = client.post("/admin/password", data={"current": "pw", "password": "new password 1",
                                             "confirm": "new password 1"})
    assert r.status_code == 200 and "Password changed" in r.text
    client.post("/admin/logout")
    # the UI password now replaces the bootstrap one from the environment
    assert client.post("/admin/login", data={"password": "pw"}, follow_redirects=False).status_code == 401
    assert client.post("/admin/login", data={"password": "new password 1"},
                       follow_redirects=False).status_code == 303


def test_session_secret_persists(tmp_path):
    settings = Settings(data_dir=tmp_path, public_url="http://testserver", secure_cookies=False,
                        admin_password="pw")
    with TestClient(create_app(settings)) as c:
        login(c)
        cookie = c.cookies.get("kotodama_session")
    with TestClient(create_app(settings)) as c:
        c.cookies.set("kotodama_session", cookie)
        assert c.get("/admin/api/overview").status_code == 200


def test_forget_revokes_token(client):
    token = adopt(client)
    assert client.delete(f"/admin/api/devices/{MAC}", headers=ADMIN).status_code == 204
    assert client.get("/api/v1/device/config", headers=bearer(token)).status_code == 401


def test_readoption_replaces_token_and_keeps_config(client):
    old = adopt(client, name="Kitchen")
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["audio"]["volume"] = 12
    client.put(f"/admin/api/devices/{MAC}", json={"name": "Kitchen", "config": config}, headers=ADMIN)
    new = adopt(client, name="")
    assert client.get("/api/v1/device/config", headers=bearer(old)).status_code == 401
    view = client.get("/api/v1/device/config", headers=bearer(new)).json()
    assert view["device"]["name"] == "Kitchen" and view["config"]["audio"]["volume"] == 12

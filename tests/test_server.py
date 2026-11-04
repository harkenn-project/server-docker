import json
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from app import kani
from app.assets import STATES
from app.main import create_app
from app.settings import Settings
from app.starter import BUILTIN_VERSION, build
from app.voice.vad import SpeechEnd

ADMIN = {"X-Requested-With": "harkenn"}
MAC = "907069f466b0"


@pytest.fixture(scope="session")
def prebuilt(tmp_path_factory):
    """Draw the built-in characters once for the whole test run."""
    path = tmp_path_factory.mktemp("prebuilt")
    build(path)
    # In the container the prebuilt set is read-only to the app: make sure nothing tries to write there.
    (path / ".cache").write_text("not a directory")
    return path


@pytest.fixture
def client(tmp_path, prebuilt):
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, max_recording_bytes=1000,
                        prebuilt_characters=prebuilt)
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
    assert body["ok"] and body["server"] == "harkenn"


def test_adoption_flow_hands_out_token_once(client):
    token = adopt(client)
    r = client.get("/api/v1/device/config", headers=bearer(token))
    assert r.status_code == 200
    config = r.json()
    assert config["device"] == {"id": MAC, "name": "Kitchen"}
    assert config["version"] == 1 and config["server_url"] == "http://testserver"
    assert config["layout"]["id"] == "character_clock"
    assert set(config["character"]["states"]) == set(STATES)
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
    body = {"name": "Home", "config": {}}
    assert client.put("/admin/api/groups/home", json=body).status_code == 403
    assert client.put("/admin/api/groups/home", json=body, headers=ADMIN).status_code == 200


def test_admin_pages_render(client):
    assert client.get("/admin", follow_redirects=False).status_code == 303
    adopt(client)
    for path in ("/admin", f"/admin/devices/{MAC}", "/admin/groups", "/admin/groups/home", "/admin/characters",
                 "/admin/sounds", "/admin/people", "/admin/voice", "/admin/screen", "/admin/firmware", "/admin/agents",
                 "/admin/server", "/admin/about", "/admin/password"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'class="active"' in r.text or path == "/admin/password" or path == "/admin/about", path
    assert client.get("/admin/defaults", follow_redirects=False).headers["location"] == "/admin/groups"


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


def test_volume_set_on_speaker_is_stored_without_a_push(client):
    token = adopt(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        assert ws.receive_json()["version"] == 1
        ws.send_json({"type": "settings", "volume": 42})
        ws.send_json({"type": "settings", "volume": 400})  # out of range: ignored
        ws.send_json({"type": "status", "status": {"volume": 42, "muted": True}})
        ws.send_json({"type": "config_request"})
        reply = ws.receive_json()  # the next message is the requested config, not a push
        assert reply["version"] == 1 and reply["config"]["audio"]["volume"] == 42
    device = client.get(f"/admin/api/devices/{MAC}").json()
    assert device["config"]["audio"]["volume"] == 42 and device["config_version"] == 1
    assert device["status"]["muted"] is True


def test_every_state_has_an_animation(client):
    token = adopt(client)
    states = client.get("/api/v1/device/config", headers=bearer(token)).json()["character"]["states"]
    assert states["muted"]["source"] == "muted"


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
    dhcp = {"ip": "", "gateway": "", "subnet": "", "dns": ""}
    assert seen["wifi"] == [{"ssid": "Thing Tank", "password": "********", **dhcp}]
    assert "supersecret" not in client.get(f"/admin/devices/{MAC}").text

    seen["audio"]["volume"] = 10
    assert client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": seen}, headers=ADMIN).status_code == 200
    device_view = client.get("/api/v1/device/config", headers=bearer(token)).json()
    assert device_view["config"]["wifi"] == [{"ssid": "Thing Tank", "password": "supersecret", **dhcp}]

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
    # speakers get KANI animations flattened onto their theme background (midnight = #0b1020)
    assert ".kani?" in url and "bg=0b1020" in url
    r = client.get(url.replace("http://testserver", ""))
    assert r.status_code == 200
    assert r.content[:4] == b"KANI"
    frames = kani.decode(r.content)
    assert len(frames) > 1 and all(len(f) == 144 * 144 for f in frames)
    midnight = (0x0b >> 3) << 11 | (0x10 >> 2) << 5 | (0x20 >> 3)
    assert frames[0][0] == midnight  # corner pixel is the background
    # admin previews are transparent GIFs
    with Image.open(io.BytesIO(client.get("/assets/characters/default/idle.gif?size=144").content)) as im:
        assert im.size == (144, 144) and im.n_frames > 1
    assert client.get("/assets/characters/default/idle.gif?size=999").status_code == 404
    assert client.get("/assets/characters/default/idle.kani?size=144&bg=nothex").status_code == 422
    assert client.get("/assets/characters/default/idle.kani?size=144").status_code == 422
    assert client.get("/assets/characters/../idle.gif").status_code == 404


def test_builtin_characters(client):
    login(client)
    chars = client.get("/admin/api/catalog").json()["characters"]
    assert [c["id"] for c in chars] == ["default", "obake", "neko", "robo"]
    assert all(len(c["states"]) == len(STATES) for c in chars)


def test_kani_roundtrip():
    rng = np.random.default_rng(1)
    frames = []
    base = Image.new("RGBA", (40, 30), (10, 20, 30, 255))
    for k in range(5):
        f = base.copy()
        f.paste((200, 100, 50, 255), (k * 3, 5, k * 3 + 10, 20))
        noise = Image.fromarray(rng.integers(0, 255, (3, 7, 4), dtype=np.uint8))
        f.paste(noise, (30, 2))
        frames.append(f)
    data = kani.encode(frames, [50] * 5, "000000")
    for decoded, f in zip(kani.decode(data), frames):
        flat = Image.alpha_composite(Image.new("RGBA", f.size, (0, 0, 0, 255)), f)
        assert np.array_equal(decoded, kani.to_rgb565(flat))


def test_customized_builtin_is_not_redrawn(tmp_path, prebuilt):
    from app.assets import CharacterStore
    from app.starter import ensure_builtin_characters
    store = CharacterStore(tmp_path / "c", tmp_path / "cache")
    ensure_builtin_characters(store, prebuilt)
    v1 = store.get("neko")["version"]
    store.update_meta("neko", builtin=BUILTIN_VERSION - 1)  # pretend a newer drawing shipped
    ensure_builtin_characters(store, prebuilt)
    assert store.get("neko")["version"] > v1  # upgraded, with a fresh version number
    store.update_meta("neko", builtin=BUILTIN_VERSION - 1, customized=True)
    v2 = store.get("neko")["version"]
    ensure_builtin_characters(store, prebuilt)
    assert store.get("neko")["version"] == v2  # customized: left alone


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
def fresh_client(tmp_path, prebuilt):
    settings = Settings(data_dir=tmp_path, public_url="http://testserver", secure_cookies=False,
                        prebuilt_characters=prebuilt)
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


def test_session_secret_persists(tmp_path, prebuilt):
    settings = Settings(data_dir=tmp_path, public_url="http://testserver", secure_cookies=False,
                        admin_password="pw", prebuilt_characters=prebuilt)
    with TestClient(create_app(settings)) as c:
        login(c)
        cookie = c.cookies.get("harkenn_session")
    with TestClient(create_app(settings)) as c:
        c.cookies.set("harkenn_session", cookie)
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


def fake_firmware(version="0.4.0", hardware="test", marker="HARKENN-FW"):
    return bytes([0xE9]) + bytes(2000) + f"{marker}|{version}|{hardware}|".encode() + bytes(500)


def upload_firmware(c, data):
    return c.post("/admin/api/firmware", files={"file": ("firmware.bin", data, "application/octet-stream")},
                  headers=ADMIN)


def test_firmware_upload_and_update(client):
    token = adopt(client)
    r = upload_firmware(client, fake_firmware())
    assert r.status_code == 201, r.text
    fw = r.json()
    assert fw["version"] == "0.4.0" and fw["hardware"] == "test"
    assert upload_firmware(client, fake_firmware()).json()["id"] == fw["id"]  # same file: no duplicate
    assert [b["id"] for b in client.get("/admin/api/firmware").json()] == [fw["id"]]
    assert client.get("/admin/firmware").status_code == 200
    assert fw["version"] in client.get(f"/admin/devices/{MAC}").text

    # offline speaker
    r = client.post(f"/admin/api/devices/{MAC}/update", json={"firmware": fw["id"]}, headers=ADMIN)
    assert r.status_code == 409

    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        r = client.post(f"/admin/api/devices/{MAC}/update", json={"firmware": fw["id"]}, headers=ADMIN)
        assert r.status_code == 200, r.text
        cmd = ws.receive_json()
    assert cmd["command"] == "update_firmware"
    args = cmd["args"]
    assert args["version"] == "0.4.0" and args["size"] == fw["size"] and args["sha256"] == fw["sha256"]

    path = args["url"].removeprefix("http://testserver")
    assert client.get(path).status_code == 401  # speakers only
    got = client.get(path, headers=bearer(token))
    assert got.status_code == 200 and got.content == fake_firmware()

    assert client.delete(f"/admin/api/firmware/{fw['id']}", headers=ADMIN).status_code == 204
    assert client.get(path, headers=bearer(token)).status_code == 404


def test_firmware_rejects_other_files_and_hardware(client):
    adopt(client)
    assert upload_firmware(client, b"\x00" * 4000).status_code == 422  # not an ESP32 image
    assert upload_firmware(client, bytes([0xE9]) + bytes(4000)).status_code == 422  # no Harkenn marker
    fw = upload_firmware(client, fake_firmware(hardware="other-board")).json()
    r = client.post(f"/admin/api/devices/{MAC}/update", json={"firmware": fw["id"]}, headers=ADMIN)
    assert r.status_code == 422 and "other-board" in r.text

@pytest.fixture
def git_client(tmp_path, prebuilt):
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        update_url="https://releases.example/fw")
    with TestClient(create_app(settings)) as c:
        yield c


def fake_releases(c, version="0.5.0", calls=None, sha=None):
    import hashlib
    updates = c.app.state.ctx.updates
    calls = calls if calls is not None else []
    data = fake_firmware(version)

    def manifest(channel="stable"):
        if channel != "stable":
            return None
        calls.append("manifest")
        return {"version": version, "file": f"v{version}/firmware.bin",
                "sha256": sha or hashlib.sha256(data).hexdigest()}

    updates.fetch_manifest = manifest
    updates.fetch_file = lambda path: calls.append(path) or data
    return calls

def test_boot_update_check_offers_newer_release(git_client):
    c = git_client
    token = adopt(c)  # adopted with firmware 0.0.1, hardware "test"
    calls = fake_releases(c)
    with c.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "update_check"})
        offer = ws.receive_json()
    assert offer["type"] == "update_available"
    assert offer["firmware"]["version"] == "0.5.0" and offer["firmware"]["url"].endswith(".bin")
    state = c.post("/admin/api/firmware/check", headers=ADMIN).json()
    assert state["release"] == "v0.5.0" and state["latest"]["version"] == "0.5.0" and state["error"] is None
    assert calls.count("v0.5.0/firmware.bin") == 1  # the same release isn't downloaded again
    assert "v0.5.0" in c.get("/admin/firmware").text


def test_boot_update_check_is_quiet_when_up_to_date(git_client):
    c = git_client
    token = adopt(c)
    calls = fake_releases(c, version="0.0.1")  # same as the speaker
    with c.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "update_check"})
        ws.send_json({"type": "update_check"})  # a second speaker booting soon after: cached
        ws.send_json({"type": "config_request"})
        assert ws.receive_json()["type"] == "config"  # no offer came first
    assert calls.count("manifest") == 1


def test_update_check_failure_is_reported(git_client):
    c = git_client
    login(c)
    updates = c.app.state.ctx.updates

    def broken(channel="stable"):
        raise OSError("releases unreachable")

    updates.fetch_manifest = broken
    state = c.post("/admin/api/firmware/check", headers=ADMIN).json()
    assert "unreachable" in state["error"] and state["latest"] is None


def test_version_order():
    from app.updates import is_newer
    assert is_newer("0.4.10", "0.4.9") and is_newer("1.0.0", "0.9.9") and is_newer("0.5.0", None)
    assert not is_newer("0.4.1", "0.4.1") and not is_newer("v0.4.0", "0.4.1")

def test_update_check_rejects_a_bad_checksum(git_client):
    c = git_client
    login(c)
    fake_releases(c, sha="0" * 64)
    state = c.post("/admin/api/firmware/check", headers=ADMIN).json()
    assert "sha256" in state["error"] and state["latest"] is None

def fake_voice(c, heard="what time is it"):
    voice = c.app.state.ctx.voice
    got = {}

    def transcribe(pcm, **kw):
        got["pcm"] = pcm
        got["stt"] = kw
        return heard

    def synthesize(text, **kw):
        got["tts"] = (text, kw)
        return b"\x01\x00" * 4000  # 0.25 s at 16 kHz

    voice.stt.transcribe = transcribe
    voice.tts.synthesize = synthesize
    voice.speech_end = lambda silence_s: SpeechEnd(silence_s, probability=loud)  # loud is speech, quiet isn't
    return got


def loud(window):
    return 1.0 if np.abs(window).max() > 0.1 else 0.0


SPEECH = np.full(1600, 12000, dtype="<i2").tobytes()  # 100 ms that the fake detector calls speech
QUIET = bytes(3200)                           # 100 ms of silence


def receive_speech(ws):
    start = ws.receive_json()
    assert start["type"] == "speak_start" and start["rate"] == 16000
    audio = b""
    while len(audio) < start["bytes"]:
        audio += ws.receive_bytes()
    assert ws.receive_json()["type"] == "speak_end"
    return start, audio


def test_voice_round_trip(client):
    token = adopt(client)
    got = fake_voice(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "s1", "local_time": "2026-10-31T15:42:00"})
        for _ in range(10):
            ws.send_bytes(b"\x00\x01" * 1600)  # 1 s in 100 ms frames
        ws.send_json({"type": "voice_end", "session": "s1"})
        result = ws.receive_json()
        assert result == {"type": "voice_result", "session": "s1", "heard": "what time is it",
                          "reply": "It's 3:42 PM."}  # no assistant configured: built-in answer, speaker's local time
        start, audio = receive_speech(ws)
    assert start["text"] == "It's 3:42 PM." and len(audio) == 8000
    assert len(got["pcm"]) == 32000 and got["stt"]["model"] == "base.en"
    assert got["tts"][1]["voice"] == "en_US-ljspeech-high"
    logs = [line["message"] for line in client.get(f"/admin/api/devices/{MAC}/logs").json()]
    assert any(m.startswith('heard "what time is it"') for m in logs)
    assert client.get(f"/admin/api/devices/{MAC}/recordings").json() == []  # not kept by default


def test_voice_silence_and_recordings(client):
    token = adopt(client)
    fake_voice(client, heard="")
    login(client)
    s = client.get("/admin/api/voice").json()
    s["keep_recordings"] = True
    assert client.put("/admin/api/voice", json=s, headers=ADMIN).status_code == 200
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "s2"})
        ws.send_bytes(b"\x00\x00" * 16000)
        ws.send_json({"type": "voice_end", "session": "s2"})
        assert ws.receive_json() == {"type": "voice_result", "session": "s2", "heard": "", "reply": ""}
    recs = client.get(f"/admin/api/devices/{MAC}/recordings").json()
    assert len(recs) == 1 and recs[0]["content_type"] == "audio/wav"


def test_voice_settings_and_say(client):
    token = adopt(client)
    got = fake_voice(client)
    s = client.get("/admin/api/voice").json()
    s.update(assistant_url="http://ollama:11434/v1", assistant_key="secret-key", tts_voice="en_US-amy-medium")
    out = client.put("/admin/api/voice", json=s, headers=ADMIN).json()
    assert out["assistant_key"] == "********"
    out["system_prompt"] = "Be brief."
    client.put("/admin/api/voice", json=out, headers=ADMIN)  # the mask keeps the stored key
    assert client.app.state.ctx.voice.settings().assistant_key == "secret-key"
    bad = dict(out, stt_engine="cloud")
    assert client.put("/admin/api/voice", json=bad, headers=ADMIN).status_code == 422
    page = client.get("/admin/voice").text
    assert "<datalist" not in page  # plain dropdowns: a datalist hides the other choices once one is picked
    for name in ("stt_model", "tts_voice", "language", "voice"):
        assert f'<select name="{name}">' in page
    assert "Amy · US English · female" in page

    assert client.post(f"/admin/api/devices/{MAC}/say", json={"text": "hi"}, headers=ADMIN).status_code == 409
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        r = client.post(f"/admin/api/devices/{MAC}/say", json={"text": "Hi there!"}, headers=ADMIN)
        assert r.status_code == 200, r.text
        start, _ = receive_speech(ws)
    assert start["text"] == "Hi there!" and got["tts"][1]["voice"] == "en_US-amy-medium"


def test_builtin_replies_and_cleanup():
    from datetime import datetime
    from app.voice.brain import builtin_reply, speakable
    now = datetime(2026, 10, 31, 9, 5)
    assert builtin_reply("What time is it?", now) == "It's 9:05 AM."
    assert builtin_reply("what's the date", now) == "It's Saturday, October 31."
    assert builtin_reply("Hello there", now) == "Hi there!"
    assert speakable("<think>hmm</think>**Sure!** See https://x.y") == "Sure! See"


def test_resample_keeps_duration():
    from app.voice.engines import resample
    pcm = (np.sin(np.arange(22050) / 10) * 8000).astype("<i2").tobytes()
    out = resample(pcm, 22050)
    assert abs(len(out) / 2 - 16000) <= 1


def test_wyoming_address():
    from app.voice.wyoming import parse_address
    assert parse_address("whisper:10300", 1) == ("whisper", 10300)
    assert parse_address("tcp://piper.local:10200/", 1) == ("piper.local", 10200)
    assert parse_address("piper", 10200) == ("piper", 10200)


def test_only_harkenn_firmware_is_accepted(client):
    login(client)
    assert upload_firmware(client, fake_firmware(marker="OTHER-FW")).status_code == 422


def test_settings_from_environment(tmp_path, monkeypatch):
    from app.settings import load_settings
    monkeypatch.setenv("HARKENN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HARKENN_PUBLIC_URL", "https://harkenn.example/")
    s = load_settings()
    assert s.data_dir == tmp_path and s.public_url == "https://harkenn.example" and s.secure_cookies
    assert s.update_url == "https://raw.githubusercontent.com/harkenn-project/speaker-firmware/releases"
    assert s.db_path == tmp_path / "harkenn.db"


def test_public_url_from_admin_and_links_follow_the_speaker(client):
    token = adopt(client)
    assert client.get("/admin/api/server").json()["effective_public_url"] == "http://testserver"
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        first = ws.receive_json()
        assert first["server_url"] == "http://testserver"
        r = client.put("/admin/api/server", json={"public_url": " https://harkenn.example/ "}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["effective_public_url"] == "https://harkenn.example"
        pushed = ws.receive_json()  # every online speaker hears about it
    assert pushed["server_url"] == "https://harkenn.example"
    # but links for the speaker still use the address it connected through
    assert pushed["character"]["states"]["idle"]["urls"]["144"].startswith("http://testserver/assets/")
    assert client.get("/api/v1/device/config", headers={**bearer(token), "Host": "harkenn"}).json()[
        "character"]["states"]["idle"]["urls"]["144"].startswith("http://harkenn/assets/")
    assert client.put("/admin/api/server", json={"public_url": "harkenn.example"}, headers=ADMIN).status_code == 422
    client.put("/admin/api/server", json={"public_url": ""}, headers=ADMIN)  # back to the default
    assert client.get("/admin/api/server").json()["effective_public_url"] == "http://testserver"
    assert client.get("/admin/server").status_code == 200

def test_base_url_drops_default_port():
    from starlette.requests import Request

    from app.context import base_url

    def req(scheme, host):
        return Request({"type": "http", "scheme": scheme, "path": "/", "query_string": b"",
                        "headers": [(b"host", host.encode())], "server": ("x", 0)})

    assert base_url(req("https", "harkenn.example:443")) == "https://harkenn.example"
    assert base_url(req("http", "harkenn:80")) == "http://harkenn"
    assert base_url(req("http", "harkenn:8000")) == "http://harkenn:8000"
    assert base_url(req("https", "harkenn.example")) == "https://harkenn.example"


def test_speech_end_waits_for_speech_then_silence():
    end = SpeechEnd(1.0, probability=loud)
    assert not any(end.feed(QUIET) for _ in range(30))  # silence alone never ends it: the speaker's timeout does
    assert not end.feed(SPEECH[:1000])                   # a click is not enough speech
    assert not any(end.feed(QUIET) for _ in range(15))
    assert not any(end.feed(SPEECH) for _ in range(5))   # speech...
    quiet = next(n for n in range(1, 20) if end.feed(QUIET))  # ...then quiet, in 100 ms frames
    assert 10 <= quiet <= 11                                  # ends after 1 s (give or take a 32 ms window)
    assert not end.feed(QUIET)                                # and only once


def test_server_ends_listening_after_speech(client):
    token = adopt(client)
    fake_voice(client)
    db = client.app.state.ctx.db
    device = db.get_device(MAC)
    device["config"]["audio"]["silence_s"] = 1  # the speaker's "stop after" setting is what the server waits for
    db.update_device(MAC, name=device["name"], config=device["config"])
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "s3"})
        for frame in [QUIET] * 5 + [SPEECH] * 8 + [QUIET] * 12:
            ws.send_bytes(frame)
        assert ws.receive_json() == {"type": "listen_stop", "session": "s3"}
        ws.send_json({"type": "voice_end", "session": "s3"})
        assert ws.receive_json()["type"] == "voice_result"


def test_silero_ignores_silence_and_noise():
    end = SpeechEnd(0.5)  # the real model, as bundled with faster-whisper
    rng = np.random.default_rng(1)
    noise = (rng.normal(0, 3000, 16000 * 3)).astype("<i2").tobytes()
    assert not any(end.feed(bytes(3200)) for _ in range(20))
    assert not any(end.feed(noise[i:i + 3200]) for i in range(0, len(noise), 3200))
    assert not end.heard_speech


def said(message):
    """What the person said, without the news in parentheses Harkenn may put first (the time, weather, timers)."""
    return message.split(")\n", 1)[1] if message.startswith("(") and ")\n" in message else message


def mcp_call(c, token, method, params=None, rid=1):
    r = c.post("/mcp", headers={**bearer(token), "Accept": "application/json, text/event-stream",
                                "MCP-Protocol-Version": "2025-06-18"},
               json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    return body["result"]


def mcp_tool(c, token, name, **args):
    result = mcp_call(c, token, "tools/call", {"name": name, "arguments": args})
    assert not result.get("isError"), result
    return result["structuredContent"]


def mcp_token(c):
    login(c)
    r = c.post("/admin/api/agents/tokens", json={"name": "Hermes"}, headers=ADMIN)
    assert r.status_code == 201
    return r.json()["token"]


def test_mcp_needs_a_token(client):
    token = mcp_token(client)
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/mcp", json={}, headers=bearer("hk_wrong")).status_code == 401
    init = mcp_call(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "test", "version": "1"}})
    assert init["serverInfo"]["name"] == "harkenn" and "presume no one heard" in init["instructions"].lower()
    tools = {t["name"]: t for t in mcp_call(client, token, "tools/list")["tools"]}
    assert set(tools) == {"list_speakers", "speak", "ring", "get_weather", "show_weather", "list_timers", "set_timer",
                          "set_alarm", "cancel_timer", "notify", "household_items", "household_change",
                          "calendar_events", "news_headlines", "morning_briefing", "media_library", "play_media",
                          "control_media"}
    assert "freezing_rain" in tools["show_weather"]["description"]
    assert "no one answers" in tools["ring"]["description"]
    agents = client.get("/admin/api/agents", headers=ADMIN).json()
    assert agents["mcp_url"] == "http://testserver/mcp" and agents["tokens"][0]["last_used"]
    assert "hash" not in agents["tokens"][0]
    assert client.delete(f"/admin/api/agents/tokens/{agents['tokens'][0]['id']}", headers=ADMIN).status_code == 204
    assert client.post("/mcp", json={}, headers=bearer(token)).status_code == 401


def test_mcp_speak_says_no_one_may_have_heard(client):
    device_token = adopt(client)
    got = fake_voice(client)
    token = mcp_token(client)
    assert mcp_tool(client, token, "list_speakers")["speakers"][0] == {
        "id": MAC, "name": "Kitchen", "online": False, "state": "unknown", "mic_muted": False,
        "agents": "ring_and_speak"}
    assert mcp_tool(client, token, "speak", text="Dinner is ready")["spoken_on"] == []  # offline
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        out = mcp_tool(client, token, "speak", text="Dinner is ready", speaker="kitchen")
        assert out["spoken_on"] == ["Kitchen"] and "Presume no one heard" in out["note"]
        start, _ = receive_speech(ws)
        assert start["text"] == "Dinner is ready" and "listen" not in start
    assert got["tts"][0] == "Dinner is ready"
    assert "No speaker called" in mcp_tool(client, token, "speak", text="hi", speaker="garage")["note"]


def test_mcp_ring_answer_and_reply(client):
    import threading

    device_token = adopt(client)
    fake_voice(client, heard="yes please")
    token = mcp_token(client)
    result = {}
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        call = threading.Thread(target=lambda: result.update(mcp_tool(
            client, token, "ring", seconds=10, message="Do you want tea?", wait_for_reply=True)))
        call.start()
        ring = ws.receive_json()
        assert ring["type"] == "ring" and ring["seconds"] == 10
        ws.send_json({"type": "ring_answer", "ring": ring["ring"]})
        assert ws.receive_json() == {"type": "ring_stop", "ring": ring["ring"]}
        start, _ = receive_speech(ws)
        assert start["text"] == "Do you want tea?" and start["listen"] is True
        ws.send_json({"type": "voice_start", "session": "r1"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "r1"})
        assert ws.receive_json()["reply"] == "Thanks, I'll pass that on."  # the reply goes to the agent, not the brain
        receive_speech(ws)
        call.join(10)
    assert result["answered"] is True and result["answered_on"] == "Kitchen" and result["reply"] == "yes please"


def test_mcp_ring_unanswered(client, monkeypatch):
    import app.voice.calls as calls

    monkeypatch.setattr(calls, "MIN_RING_S", 0.2)
    import app.mcp_server as m
    monkeypatch.setattr(m, "MIN_RING_S", 0.2)
    device_token = adopt(client)
    token = mcp_token(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        out = mcp_tool(client, token, "ring", speaker="all", seconds=0)
        assert ws.receive_json()["type"] == "ring"
        assert ws.receive_json()["type"] == "ring_stop"
    assert out["answered"] is False and "Presume no one is there" in out["note"]


def test_assistant_apis(monkeypatch):
    from datetime import datetime

    from app.voice import brain

    seen = {}

    def post(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers)
        if url.endswith("/messages"):
            return {"content": [{"type": "text", "text": "Hello from Claude"}]}
        return {"choices": [{"message": {"content": "**Hi** there"}}]}

    monkeypatch.setattr(brain, "post", post)
    conv = brain.Conversations()
    common = dict(device_id="d1", speaker="Kitchen", character="Janet", now=datetime(2026, 10, 31, 9),
                  conversations=conv)
    s = {"assistant_api": "hermes", "assistant_url": "http://hermes:8642/v1/", "assistant_key": "k"}
    assert brain.ask("hi", settings=s, **common) == "Hi there"
    assert seen["url"] == "http://hermes:8642/v1/chat/completions"  # stateless: Harkenn sends the history
    assert seen["headers"] == {"Authorization": "Bearer k", "X-Hermes-Session-Key": "harkenn-d1"}
    system = seen["body"]["messages"][0]["content"]
    assert "Kitchen" in system and "Janet" not in system  # Hermes has its own persona
    assert brain.ask("hi", settings={"assistant_api": "anthropic", "assistant_key": "a"}, **common) == "Hello from Claude"
    assert seen["url"] == "https://api.anthropic.com/v1/messages" and seen["headers"]["x-api-key"] == "a"
    instructions = seen["body"]["system"][0]  # cached: the same in every request
    assert instructions["text"].startswith("You are Janet") and instructions["cache_control"] == {"type": "ephemeral"}
    assert len(seen["body"]["messages"]) == 3
    assert seen["body"]["messages"][1]["content"][0]["cache_control"]  # the history so far, cached too
    s = {"assistant_api": "openai", "assistant_url": "https://api.deepseek.com/v1", "assistant_model": "deepseek-v4-flash"}
    assert brain.ask("and you?", settings=s, **common) == "Hi there"
    messages = seen["body"]["messages"]
    assert seen["body"]["model"] == "deepseek-v4-flash" and messages[-1]["content"] == "and you?"  # nothing new
    assert len(messages) == 6  # system, two earlier exchanges, and this one
    # the instructions and the history don't change between requests, so providers can cache them
    assert "It is" not in messages[0]["content"] and [m["content"] for m in messages[1:5]] == [
        "hi", "Hi there", "hi", "Hello from Claude"]


def test_context_is_the_last_conversations_in_the_last_minutes(monkeypatch):
    from app.voice import brain

    clock = [1000.0]
    monkeypatch.setattr(brain.time, "time", lambda: clock[0])
    conv = brain.Conversations()
    for k in range(7):  # seven conversations, ten minutes apart; the last has a follow-up
        conv.start("d")
        conv.add("d", f"q{k}", f"a{k}")
        clock[0] += 600
    conv.add("d", "q6b", "a6b")  # a reply in the same conversation
    said = lambda h: [t["content"] for t in h if t["role"] == "user"]
    assert said(conv.history("d", 5, 60)) == ["q2", "q3", "q4", "q5", "q6", "q6b"]
    assert said(conv.history("d", 5, 25)) == ["q5", "q6", "q6b"]  # only the ones active in the last 25 minutes
    assert said(conv.history("d", 1, 60)) == ["q6", "q6b"]
    assert conv.history("d", 0, 60) == []
    conv.start("d")
    conv.start("d")  # nothing was said: no empty conversations pile up
    assert said(conv.history("d", 2, 60)) == ["q5", "q6", "q6b"]


def test_follow_up_continues_the_conversation(client, monkeypatch):
    from app.voice import brain, service

    token = adopt(client)
    fake_voice(client)
    voice = client.app.state.ctx.voice
    seen = {}

    def post(url, body, headers, timeout):
        seen["system"] = body["messages"][0]["content"]
        return {"choices": [{"message": {"content": "Sure. Anything else? [[listen]]"}}]}

    monkeypatch.setattr(brain, "post", post)
    monkeypatch.setattr(service.VoiceService, "effective",
                        lambda self: self.settings().model_copy(update={"assistant_url": "http://llm/v1"}))
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()

        def ask(session, follow_up=False):
            ws.send_json({"type": "voice_start", "session": session, "follow_up": follow_up})
            ws.send_bytes(SPEECH * 5)
            ws.send_json({"type": "voice_end", "session": session})
            assert ws.receive_json()["type"] == "voice_result"
            return receive_speech(ws)[0]

        start = ask("c1")
        assert start["listen"] is True and start["follow_up"] is True  # the assistant asked to hear an answer
        assert start["text"] == "Sure. Anything else?" and "[[listen]]" in seen["system"]
        ask("c2", follow_up=True)
        ask("c3")  # a tap: a new conversation
    convs = voice.conversations._convs[MAC]
    assert [len(c["turns"]) for c in convs] == [4, 2]
    login(client)
    s = client.get("/admin/api/voice", headers=ADMIN).json()
    assert s["follow_up"] == "assistant"  # the default
    s["follow_up"] = "questions"
    assert client.put("/admin/api/voice", json=s, headers=ADMIN).status_code == 200
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "c4"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "c4"})
        ws.receive_json()
        assert receive_speech(ws)[0]["listen"] is True  # "Anything else?" is a question
    monkeypatch.setattr(brain, "post", lambda *a: {"choices": [{"message": {"content": "Done."}}]})
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "c5"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "c5"})
        ws.receive_json()
        assert "listen" not in receive_speech(ws)[0]  # not a question: no listening


def test_assistant_from_environment(tmp_path, prebuilt):
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        assistant_api="hermes", assistant_url="http://hermes:8642/v1", assistant_key="from-secret")
    with TestClient(create_app(settings)) as c:
        voice = c.app.state.ctx.voice
        s = voice.effective()
        assert (s.assistant_api, s.assistant_url, s.assistant_key) == ("hermes", "http://hermes:8642/v1", "from-secret")
        assert voice.settings().assistant_key == ""  # not copied into the database
        login(c)
        out = c.get("/admin/api/voice", headers=ADMIN).json()
        assert out["from_environment"]["assistant_key"] is True and out["assistant_key"] == ""


def set_agents(c, **agents):
    db = c.app.state.ctx.db
    device = db.get_device(MAC)
    device["config"]["agents"] = {"access": "ring_and_speak", "quiet_from": "", "quiet_to": "", **agents}
    db.update_device(MAC, name=device["name"], config=device["config"])


def test_mcp_respects_do_not_disturb(client, monkeypatch):
    import app.mcp_server as m

    device_token = adopt(client)
    fake_voice(client)
    token = mcp_token(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        set_agents(client, access="do_not_disturb")
        assert mcp_tool(client, token, "list_speakers")["speakers"][0]["agents"] == "do_not_disturb"
        out = mcp_tool(client, token, "speak", text="hello", speaker="Kitchen")
        assert out["spoken_on"] == [] and "do not disturb" in out["skipped"][0] and "Respect it" in out["note"]
        out = mcp_tool(client, token, "ring", speaker="all")
        assert out["answered"] is False and "Respect it" in out["note"]
        set_agents(client, access="speak_only")
        out = mcp_tool(client, token, "ring")
        assert out["answered"] is False and "speak only" in out["note"]
        assert mcp_tool(client, token, "speak", text="hello")["spoken_on"] == ["Kitchen"]
        receive_speech(ws)
        set_agents(client, quiet_from="00:00", quiet_to="23:59")  # quiet hours now
        assert mcp_tool(client, token, "list_speakers")["speakers"][0]["agents"] == "do_not_disturb"
        set_agents(client)
        monkeypatch.setattr(m, "MAX_RINGS_PER_HOUR", 0)
        assert "the limit" in mcp_tool(client, token, "ring")["note"]  # nothing was sent to the speaker
    # the setting is the server's alone, but it's part of the speaker's saved config
    login(client)
    assert client.get(f"/admin/api/devices/{MAC}", headers=ADMIN).json()["config"]["agents"]["access"] == "ring_and_speak"


def test_quiet_hours():
    from datetime import datetime

    from app.mcp_server import in_quiet_hours

    at = lambda h, mi=0: datetime(2026, 11, 1, h, mi)
    assert in_quiet_hours("22:00", "07:00", at(23)) and in_quiet_hours("22:00", "07:00", at(6, 59))
    assert not in_quiet_hours("22:00", "07:00", at(7)) and not in_quiet_hours("22:00", "07:00", at(12))
    assert in_quiet_hours("13:00", "14:00", at(13, 30)) and not in_quiet_hours("13:00", "14:00", at(14))
    assert not in_quiet_hours("", "07:00", at(3))


def test_builtin_sound_packs_render_every_slot(tmp_path):
    from app.sounds import MAX_CLIP_S, SLOTS, SYNTH_PACKS, SoundStore

    store = SoundStore(tmp_path / "sounds", tmp_path / "cache")
    assert [p["id"] for p in store.list()] == list(SYNTH_PACKS) and len(SYNTH_PACKS) == 7
    for pid in SYNTH_PACKS:
        for slot in SLOTS:
            pcm = store.clip(pid, slot)
            seconds = len(pcm) / 32000
            assert 0.1 < seconds <= MAX_CLIP_S, (pid, slot, seconds)
            peak = np.abs(np.frombuffer(pcm, dtype="<i2")).max()
            assert 8000 < peak < 32767, (pid, slot, peak)  # audible, not clipped
    assert store.clip("classic", "boot") == store.clip("classic", "boot")  # cached, deterministic
    assert store.speaker_manifest("classic", "http://x") is None  # the speaker's own chimes


def tone_wav(seconds=0.5, hz=440):
    import wave

    t = np.arange(int(22050 * seconds)) / 22050
    pcm = (np.sin(2 * np.pi * hz * t) * 12000).astype("<i2")
    pcm = np.concatenate([np.zeros(4000, "<i2"), pcm, np.zeros(4000, "<i2")])  # silence to trim
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_sound_packs_voiced_custom_and_speakers(client):
    device_token = adopt(client)
    got = fake_voice(client)
    tts = client.app.state.ctx.voice.tts
    quiet = tts.synthesize
    tts.synthesize = lambda text, **kw: (quiet(text, **kw), tone_wav()[44:])[1]  # audible, so it isn't trimmed away
    login(client)
    packs = client.get("/admin/api/sounds", headers=ADMIN).json()
    assert [p["id"] for p in packs["packs"]][:2] == ["classic", "marimba"] and "wake" in packs["slots"]

    r = client.post("/admin/api/sounds", headers=ADMIN, json={
        "name": "Janet", "kind": "voiced", "voice": "en_US-hfc_female-medium",
        "lines": {"boot": "Hello!", "wake": "Hi there!", "done": "", "error": "Oops!", "shutdown": "Bye!"}})
    assert r.status_code == 201, r.text
    janet = r.json()
    assert janet["id"] == "janet" and janet["slots"] == {"boot": True, "wake": True, "done": False, "error": True,
                                                         "shutdown": True}
    assert got["tts"][1]["voice"] == "en_US-hfc_female-medium"
    r = client.put("/admin/api/sounds/janet", headers=ADMIN, json={"lines": {"done": "Got it!"}})
    assert r.json()["slots"]["done"] and got["tts"][0] == "Got it!"

    custom = client.post("/admin/api/sounds", headers=ADMIN,
                         json={"name": "My sounds", "kind": "custom", "copy_from": "glass"}).json()
    assert custom["id"] == "my-sounds" and all(custom["slots"].values())
    r = client.put("/admin/api/sounds/my-sounds/wake", headers=ADMIN, files={"file": ("ding.wav", tone_wav())})
    assert r.status_code == 200, r.text
    wav_bytes = client.get("/assets/sounds/my-sounds/wake.wav").content
    assert wav_bytes[:4] == b"RIFF" and 0.45 < (len(wav_bytes) - 44) / 32000 < 0.62  # resampled, trimmed
    bad = client.put("/admin/api/sounds/my-sounds/wake", headers=ADMIN, files={"file": ("x.wav", b"not audio")})
    assert bad.status_code == 422
    assert client.delete("/admin/api/sounds/my-sounds/error", headers=ADMIN).json()["slots"]["error"] is False
    assert client.get("/assets/sounds/my-sounds/error.pcm").status_code == 404  # silent
    assert client.put("/admin/api/sounds/glass/wake", headers=ADMIN,
                      files={"file": ("d.wav", tone_wav())}).status_code == 404  # built in: read-only

    # a speaker using the custom pack gets its clips, with silence as an empty URL
    device = client.get(f"/admin/api/devices/{MAC}", headers=ADMIN).json()
    device["config"]["audio"]["sound_pack"] = "my-sounds"
    assert client.put(f"/admin/api/devices/{MAC}", headers=ADMIN,
                      json={"name": device["name"], "config": device["config"]}).status_code == 200
    sounds = client.get("/api/v1/device/config", headers=bearer(device_token)).json()["sounds"]
    assert sounds["pack"] == "my-sounds" and sounds["slots"]["error"] == {"url": "", "bytes": 0}
    url = sounds["slots"]["wake"]["url"]
    assert url.startswith("http://testserver/assets/sounds/my-sounds/wake.pcm?v=")
    assert len(client.get(url).content) == sounds["slots"]["wake"]["bytes"]
    assert client.delete("/admin/api/sounds/my-sounds", headers=ADMIN).status_code == 409  # in use
    device["config"]["audio"]["sound_pack"] = "nope"
    assert client.put(f"/admin/api/devices/{MAC}", headers=ADMIN,
                      json={"name": device["name"], "config": device["config"]}).status_code == 422

    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        assert client.post(f"/admin/api/devices/{MAC}/sound", headers=ADMIN,
                           json={"pack": "retro", "slot": "boot"}).json() == {"played": True}
        start, audio = receive_speech(ws)
        assert start["text"] == "" and len(audio) == len(client.get("/assets/sounds/retro/boot.pcm").content)


def test_the_assistant_decides_when_to_listen(monkeypatch):
    from datetime import datetime

    from app.voice import brain

    answers = iter(["Do you want the lights on? [[listen]]", "Done.", "OK [[ Listen ]] then.", "Which room?"])
    monkeypatch.setattr(brain, "post", lambda *a: {"choices": [{"message": {"content": next(answers)}}]})
    conv = brain.Conversations()
    kw = dict(device_id="d", speaker="Office", character="Janet", now=datetime(2026, 11, 1, 9), conversations=conv,
              settings={"assistant_url": "http://llm/v1", "follow_up": "assistant"})
    r = brain.ask("it's dark", **kw)
    assert r == "Do you want the lights on?" and r.listen is True
    assert conv.history("d")[-1]["content"].endswith("[[listen]]")  # kept in history, so it keeps using it
    r = brain.ask("yes", **kw)
    assert r == "Done." and r.listen is False
    r = brain.ask("hm", **kw)
    assert r == "OK then." and r.listen is True  # anywhere, any case, never spoken
    r = brain.ask("lights", **kw)
    assert r == "Which room?" and r.listen is True  # a question without the marker still listens


def test_version_order_is_semver():
    from app.updates import is_newer, version_key

    order = ["0.5.0", "0.6.0-alpha.1", "0.6.0-alpha.2", "0.6.0-beta.2", "0.6.0-beta.10", "0.6.0-development.3",
             "0.6.0", "0.6.1", "0.10.0"]
    assert sorted(order, key=version_key) == ["0.5.0", "0.6.0-alpha.1", "0.6.0-alpha.2", "0.6.0-beta.2",
                                              "0.6.0-beta.10", "0.6.0-development.3", "0.6.0", "0.6.1", "0.10.0"]
    assert is_newer("0.6.0", "0.6.0-beta.10") and not is_newer("0.6.0-beta.1", "0.6.0")
    assert version_key("v0.6.0+build.7") == version_key("0.6.0")


def test_release_channels(git_client):
    import hashlib

    c = git_client
    login(c)
    updates = c.app.state.ctx.updates
    builds = {"stable": "0.5.0", "beta": "0.6.0-beta.2", "alpha": "0.6.0-alpha.4", "development": "0.7.0-development.1"}
    images = {v: fake_firmware(v) for v in builds.values()}

    def manifest(channel="stable"):
        v = builds.get(channel)
        return {"version": v, "file": f"v{v}/firmware.bin", "sha256": hashlib.sha256(images[v]).hexdigest()} if v else None

    updates.fetch_manifest = manifest
    updates.fetch_file = lambda path: images[path.split("/")[0][1:]]
    assert c.post("/admin/api/firmware/check", headers=ADMIN).json()["release"] == "v0.5.0"  # stable by default
    st = c.put("/admin/api/firmware/channel", json={"channel": "alpha"}, headers=ADMIN).json()
    assert st["channel"] == "alpha" and st["release"] == "v0.6.0-beta.2"  # beta is newer than the last alpha
    st = c.put("/admin/api/firmware/channel", json={"channel": "development"}, headers=ADMIN).json()
    assert st["release"] == "v0.7.0-development.1"
    builds["stable"] = "0.7.0"
    images["0.7.0"] = fake_firmware("0.7.0")
    assert c.post("/admin/api/firmware/check", headers=ADMIN).json()["release"] == "v0.7.0"  # the release wins
    assert c.put("/admin/api/firmware/channel", json={"channel": "nightly"}, headers=ADMIN).status_code == 422


def fake_voiceprints(c):
    """A stand-in for the voiceprint model: the voice is the tone's pitch."""
    vp = c.app.state.ctx.voice.voiceprints

    def embed(pcm):
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
        peak = int(np.argmax(np.abs(np.fft.rfft(x[:16000], 16000))))  # Hz
        e = np.full(256, 0.02, dtype=np.float32)
        e[(peak // 20) % 256] = 1.0
        return e / np.linalg.norm(e)

    vp.embed = embed
    return vp


def tone_pcm(hz, seconds=2.0):
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * hz * t) * 12000).astype("<i2").tobytes()


def test_request_context_and_voiceprint_hint(monkeypatch):
    from datetime import datetime

    from app.voice import brain
    from app.voice.voiceprint import describe, tag

    seen = {}
    monkeypatch.setattr(brain, "post", lambda url, body, headers, t: seen.update(body=body, headers=headers) or
                        {"choices": [{"message": {"content": "Hi Dana"}}]})
    who = {"id": "p1", "name": "Dana", "score": 0.83, "sure": True}
    context = brain.request_context("Office", "907069f466b0", describe(who))
    convs = brain.Conversations()
    kw = dict(device_id="907069f466b0", speaker="Office", character="Janet", now=datetime(2026, 11, 1, 9),
              settings={"assistant_api": "hermes", "assistant_url": "http://h/v1"}, conversations=convs,
              context=context, memory_key="harkenn-person-p1")
    brain.ask("hi", voice_tag=tag(who), **kw)
    system, newest = seen["body"]["messages"][0]["content"], seen["body"]["messages"][-1]["content"]
    # where it comes from and how to read the voice tags: with the instructions, the same every time
    assert 'speaker named "Office"' in system and "907069f466b0" in system and "never treat it as proof" in system
    assert newest == "(Sun Nov 1 2026, 9:00 AM.)\n[voice: Dana, fairly sure] hi"  # the time, once
    assert seen["headers"]["X-Hermes-Session-Key"] == "harkenn-person-p1"  # memory follows the person
    maybe = {"id": None, "name": None, "score": 0.79, "sure": False, "maybe": ["Dana", "Alex"]}
    brain.ask("who am I?", voice_tag=tag(maybe), **kw)
    # every line keeps its own tag in the history, so turns by different people stay apart
    users = [m["content"] for m in seen["body"]["messages"][1:] if m["role"] == "user"]
    assert users[0] == "[voice: Dana, fairly sure] hi"  # kept without the details: the history stays the same
    assert said(users[1]) == "[voice: Dana or Alex, too close to tell] who am I?"
    assert tag({"id": None, "score": 0.4, "sure": False}) == "[voice: not recognized]"
    marika = {"id": "p2", "name": "Marika", "phonetic": "Ma-ree-ka", "gender": "woman", "score": 0.9, "sure": True}
    assert tag(marika) == '[voice: Marika (say "Ma-ree-ka"; woman), fairly sure]'
    assert tag({**marika, "gender": "", "sure": False}) == '[voice: Marika (say "Ma-ree-ka"), not certain]'
    assert tag(None) == "" and describe(None) == ""
    # long-term memory stays with the person recognized for sure at the start of the conversation
    convs.start("d")
    assert convs.person("d") == "" and convs.person("d", "p1") == "p1" and convs.person("d") == "p1"
    assert convs.person("d", "p2") == "p2"
    convs.add("d", "x", "y")
    convs.start("d")
    assert convs.person("d") == ""


def test_people_and_voiceprints(client, monkeypatch):
    import threading

    from app.voice import brain, service

    device_token = adopt(client)
    fake_voice(client)
    vp = fake_voiceprints(client)
    login(client)
    assert client.post("/admin/api/people", json={"name": "Dana", "consent": False},
                       headers=ADMIN).status_code == 422  # no voiceprint without agreement
    dana = client.post("/admin/api/people", json={"name": "Dana", "consent": True}, headers=ADMIN).json()
    alex = client.post("/admin/api/people", json={"name": "Alex", "consent": True}, headers=ADMIN).json()

    def sample(pid, hz, seconds=2.0):
        return client.post(f"/admin/api/people/{pid}/samples", headers=ADMIN,
                           files={"file": ("v.wav", sounds_wav(tone_pcm(hz, seconds)))})

    assert sample(dana["id"], 440).json()["samples"] == 1
    assert sample(dana["id"], 445).json()["samples"] == 2
    assert sample(alex["id"], 900).json()["samples"] == 1
    assert sample(alex["id"], 900, 0.5).status_code == 422  # too short to learn from
    out = client.post("/admin/api/people/identify", headers=ADMIN,
                      files={"file": ("v.wav", sounds_wav(tone_pcm(441)))}).json()["who"]
    assert out["name"] == "Dana" and out["sure"]
    out = client.post("/admin/api/people/identify", headers=ADMIN,
                      files={"file": ("v.wav", sounds_wav(tone_pcm(3000)))}).json()["who"]
    assert out["id"] is None  # nobody's voice
    people = client.get("/admin/api/people", headers=ADMIN).json()
    assert [(p["name"], p["samples"]) for p in people["people"]] == [("Dana", 2), ("Alex", 1)]

    # recording on the speaker: it asks, listens, and the recording becomes a sample (not a question)
    result = {}
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        rec = threading.Thread(target=lambda: result.update(client.post(
            f"/admin/api/people/{alex['id']}/record/{MAC}", headers=ADMIN).json()))
        rec.start()
        start, _ = receive_speech(ws)
        assert "Alex" in start["text"] and start["listen"] is True and "follow_up" not in start
        ws.send_json({"type": "voice_start", "session": "e1"})
        ws.send_bytes(tone_pcm(905))
        ws.send_json({"type": "voice_end", "session": "e1"})
        assert ws.receive_json()["reply"] == "Thanks!"
        receive_speech(ws)
        rec.join(10)
    assert result["samples"] == 2

    # a question: the assistant hears which speaker, and who's probably talking
    seen = {}
    monkeypatch.setattr(brain, "post", lambda url, body, headers, t: seen.update(body=body, headers=headers) or
                        {"choices": [{"message": {"content": "Sure."}}]})
    monkeypatch.setattr(service.VoiceService, "effective", lambda self: self.settings().model_copy(
        update={"assistant_url": "http://h/v1", "assistant_api": "hermes"}))
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "q1"})
        ws.send_bytes(tone_pcm(440))
        ws.send_json({"type": "voice_end", "session": "q1"})
        ws.receive_json()
        receive_speech(ws)
    system, newest = seen["body"]["messages"][0]["content"], seen["body"]["messages"][-1]["content"]
    assert 'speaker named "Kitchen"' in system and "Voice recognition" in system
    assert said(newest).startswith("[voice: Dana")
    assert seen["headers"]["X-Hermes-Session-Key"] == f"harkenn-person-{dana['id']}"
    assert client.delete(f"/admin/api/people/{dana['id']}", headers=ADMIN).status_code == 204
    assert [p["name"] for p in vp.people()] == ["Alex"]
    out = client.patch(f"/admin/api/people/{alex['id']}", headers=ADMIN,
                       json={"name": "Alix", "phonetic": "AH-leeks", "gender": "non-binary"}).json()
    assert [(p["name"], p["phonetic"], p["gender"]) for p in out["people"]] == [("Alix", "AH-leeks", "non-binary")]
    out = client.patch(f"/admin/api/people/{alex['id']}", headers=ADMIN, json={"gender": ""}).json()
    assert out["people"][0]["gender"] == "" and out["people"][0]["phonetic"] == "AH-leeks"  # only what's given
    assert client.patch("/admin/api/people/nobody", headers=ADMIN, json={"name": "x"}).status_code == 404


def sounds_wav(pcm):
    from app.sounds import wav

    return wav(pcm)


def test_wake_word_in_the_speaker_config(tmp_path, prebuilt):
    import json as jsonlib

    bundled = tmp_path / "wakewords"
    bundled.mkdir()
    (bundled / "hey_jarvis.tflite").write_bytes(b"TFL3" + bytes(100))
    (bundled / "hey_jarvis.json").write_text(jsonlib.dumps({"micro": {
        "probability_cutoff": 0.97, "sliding_window_size": 5, "tensor_arena_size": 22860}}))
    settings = Settings(data_dir=tmp_path / "data", admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        wakewords_dir=bundled)
    with TestClient(create_app(settings)) as c:
        token = adopt(c)
        config = c.get("/api/v1/device/config", headers=bearer(token)).json()
        assert config["wake_word"] is None  # none by default
        device = c.get(f"/admin/api/devices/{MAC}", headers=ADMIN).json()
        device["config"]["audio"].update(wake_word="hey_jarvis", wake_sensitivity="high")
        assert c.put(f"/admin/api/devices/{MAC}", headers=ADMIN,
                     json={"name": device["name"], "config": device["config"]}).status_code == 200
        wake = c.get("/api/v1/device/config", headers=bearer(token)).json()["wake_word"]
        assert wake["id"] == "hey_jarvis" and wake["name"] == "Hey Jarvis" and wake["bytes"] == 104
        assert wake["cutoff"] == 0.92 and wake["window"] == 5 and wake["arena"] == 22860  # high: 0.97 - 0.05
        assert wake["url"].startswith("http://testserver/assets/wakewords/hey_jarvis.tflite?v=")
        assert c.get(wake["url"]).content == b"TFL3" + bytes(100)
        assert c.get("/assets/wakewords/nope.tflite").status_code == 404
        device["config"]["audio"]["wake_word"] = "ok_nabu"  # an old, unknown id: no wake word, not an error
        c.put(f"/admin/api/devices/{MAC}", headers=ADMIN, json={"name": device["name"], "config": device["config"]})
        assert c.get("/api/v1/device/config", headers=bearer(token)).json()["wake_word"] is None
        assert "hey_jarvis" in c.get("/admin/api/catalog", headers=ADMIN).json()["wake_words"]


def test_custom_wake_word(client):
    import json as jsonlib

    token = adopt(client)
    login(client)
    model = b"\x1c\x00\x00\x00TFL3" + bytes(200)
    manifest = jsonlib.dumps({"wake_word": "Hi Janet", "micro": {"probability_cutoff": 0.9, "sliding_window_size": 4,
                                                                  "tensor_arena_size": 28000, "other": 1}})
    assert client.post("/admin/api/wakewords", headers=ADMIN, files={"model": ("x.tflite", b"not a model")},
                       data={"name": "Bad"}).status_code == 422
    r = client.post("/admin/api/wakewords", headers=ADMIN,
                    files={"model": ("hi_janet.tflite", model), "manifest": ("hi_janet.json", manifest)})
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "hi_janet"
    listed = {w["id"]: w for w in client.get("/admin/api/wakewords", headers=ADMIN).json()["wake_words"]}
    assert listed["hi_janet"]["custom"] and not listed["hey_jarvis"]["custom"]
    device = client.get(f"/admin/api/devices/{MAC}", headers=ADMIN).json()
    device["config"]["audio"]["wake_word"] = "hi_janet"
    assert client.put(f"/admin/api/devices/{MAC}", headers=ADMIN,
                      json={"name": device["name"], "config": device["config"]}).status_code == 200
    wake = client.get("/api/v1/device/config", headers=bearer(token)).json()["wake_word"]
    assert wake["name"] == "Hi Janet" and wake["cutoff"] == 0.9 and wake["window"] == 4 and wake["arena"] == 28000
    assert client.get(wake["url"]).content == model
    assert client.delete("/admin/api/wakewords/hi_janet", headers=ADMIN).status_code == 409  # in use
    device["config"]["audio"]["wake_word"] = ""
    client.put(f"/admin/api/devices/{MAC}", headers=ADMIN, json={"name": device["name"], "config": device["config"]})
    assert client.delete("/admin/api/wakewords/hi_janet", headers=ADMIN).status_code == 204
    assert client.delete("/admin/api/wakewords/hey_jarvis", headers=ADMIN).status_code == 404  # built in


def png_data_url(size=(64, 48), color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    import base64
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def struct_durations(data):
    import struct
    n = struct.unpack_from("<H", data, 10)[0]
    return [struct.unpack_from("<H", data, 12 + 10 * i)[0] for i in range(n)]


def test_screen_marker_is_taken_out_of_the_reply():
    from app.screens import split_marker

    text, spec, err = split_marker('Here it is. [[screen {"overlays": [{"type": "button", "label": "OK"}]}]] Enjoy!')
    assert text == "Here it is.  Enjoy!" and spec == {"overlays": [{"type": "button", "label": "OK"}]} and not err
    text, spec, err = split_marker('Look [[screen {"image": "<svg>[[x]]</svg>"}]]')  # brackets inside strings
    assert text == "Look " and spec == {"image": "<svg>[[x]]</svg>"}
    text, spec, err = split_marker("Oops [[screen {not json}]] after")
    assert text == "Oops  after" and spec is None and "invalid" in err
    assert split_marker("No screen here.") == ("No screen here.", None, "")


def test_screen_rendering_buttons_areas_and_pictures():
    from app import screens

    theme = {"background": "#0b1020", "surface": "#1a2240", "primary": "#7aa2ff", "accent": "#ff7ab6",
             "text": "#e8ecff", "muted": "#8a93b8"}
    screen, warnings = screens.render({
        "image": png_data_url(),
        "overlays": [{"type": "text", "text": "Lights?", "y": 40, "h": 30},
                     {"type": "button", "label": "Yes"}, {"type": "button", "label": "No", "style": "secondary"},
                     {"type": "area", "label": "Lamp", "x": 100, "y": 60, "w": 40, "h": 40},
                     {"type": "button"}, {"type": "sparkles"}],
        "seconds": 9999, "dismiss": "none"}, theme)
    assert [r["label"] for r in screen.regions] == ["Yes", "No", "Lamp"]
    yes, no = screen.regions[:2]
    assert yes["y"] == no["y"] and yes["x"] < no["x"]  # laid out side by side
    assert len(warnings) == 2  # the button without a label, the unknown overlay
    assert screen.seconds == screens.MAX_SECONDS and screen.dismiss == "none"
    frame = kani.decode(screen.kani)[0].reshape(240, 240)
    assert frame[120, 5] >> 11 > 20  # the red picture covers the screen
    # an animated GIF stays animated
    gif = io.BytesIO()
    frames = [Image.new("RGB", (40, 40), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    import base64
    screen, _ = screens.render({"image": "data:image/gif;base64," + base64.b64encode(gif.getvalue()).decode()},
                               theme)
    assert len(kani.decode(screen.kani)) == 2
    # a long animation keeps MAX_FRAMES frames and its length
    gif = io.BytesIO()
    many = [Image.new("RGB", (40, 40), (i * 2, 0, 0)) for i in range(100)]
    many[0].save(gif, "GIF", save_all=True, append_images=many[1:], duration=50, loop=0)
    frames, durations = screens.decode_picture(gif.getvalue())
    assert len(frames) == screens.MAX_FRAMES and abs(sum(durations) - 5000) <= 100
    # frames as a list (SVG animation isn't played)
    screen, warnings = screens.render({"image": [png_data_url(color=(255, 0, 0)), png_data_url(color=(0, 0, 255))],
                                       "frame_ms": 300}, theme)
    assert len(kani.decode(screen.kani)) == 2 and struct_durations(screen.kani) == [300, 300] and not warnings
    assert screens.thin(list("abcdef"), [10] * 6, 3) == (["a", "c", "e"], [20, 20, 20])
    assert screens.bare_picture("Here: https://x.org/dog.GIF?x=1. Enjoy") == {"image": "https://x.org/dog.GIF?x=1"}
    assert screens.bare_picture("See https://x.org/page") is None
    # text is wrapped by its real width and shrinks to fit its box rather than losing words
    f = screens.font(28)
    lines = screens.wrap("Turn on the porch light?", f, 172)
    assert " ".join(lines) == "Turn on the porch light?" and all(f.getlength(x) <= 172 for x in lines)
    small = screens.font(18)
    parts = screens.wrap("Supercalifragilistic", small, 50)
    assert "".join(parts) == "Supercalifragilistic" and len(parts) > 1 and all(small.getlength(x) <= 50 for x in parts)
    layer = Image.new("RGBA", (240, 240))
    screens.draw_text(layer, "Turn on the porch light, please, right now?", (30, 40, 180, 70), 28,
                      (255, 255, 255, 255), None, "center")
    assert layer.getbbox()
    with pytest.raises(screens.ScreenError):
        screens.render({"overlays": [{"type": "nope"}]}, theme)  # nothing usable
    assert 'href=""' in screens.safe_svg("<svg><image href='file:///etc/passwd'/></svg>")
    assert "data:" in screens.safe_svg("<svg><image href='data:image/png;base64,AA'/></svg>")


def test_screen_from_the_assistant_and_taps(client, monkeypatch):
    from app.voice import brain, service

    token = adopt(client)
    fake_voice(client)
    seen = []
    answers = iter([
        'Which one? [[screen {"overlays": [{"type": "button", "label": "Red"}, {"type": "button", "label": "Blue"}]}]]',
        "Blue it is.",
        '[[screen {"overlays": [{"type": "text", "text": "Only a screen"}]}]]',
    ])

    def post(url, body, headers, timeout):
        seen.append(body)
        return {"choices": [{"message": {"content": next(answers)}}]}

    monkeypatch.setattr(brain, "post", post)
    monkeypatch.setattr(service.VoiceService, "effective",
                        lambda self: self.settings().model_copy(update={"assistant_url": "http://llm/v1"}))
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "s1"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "s1"})
        shown = ws.receive_json()
        assert shown["type"] == "screen" and [r["label"] for r in shown["regions"]] == ["Red", "Blue"]
        assert shown["dismiss"] == "tap" and shown["seconds"] == 60
        picture = b""
        while len(picture) < shown["bytes"]:  # the picture follows on the socket
            picture += ws.receive_bytes()
        assert picture[:4] == b"KANI" and len(picture) == shown["bytes"]
        assert ws.receive_json()["reply"] == "Which one?"  # voice_result
        start, _ = receive_speech(ws)
        assert start["text"] == "Which one?"
        assert "[[screen" in seen[0]["messages"][0]["content"] and seen[0]["max_tokens"] > 300
        r = client.get(shown["url"].replace("http://testserver", ""))
        assert r.status_code == 200 and r.content[:4] == b"KANI"
        # a label that isn't on the screen: nothing happens
        ws.send_json({"type": "screen_tap", "session": "t0", "screen": shown["screen"], "label": "Green"})
        assert ws.receive_json() == {"type": "voice_result", "session": "t0", "heard": "", "reply": ""}
        ws.send_json({"type": "screen_tap", "session": "t1", "screen": shown["screen"], "label": "Blue"})
        result = ws.receive_json()
        assert result["session"] == "t1" and result["reply"] == "Blue it is."
        assert said(seen[1]["messages"][-1]["content"]) == '(tapped "Blue" on the screen)'
        assert seen[1]["messages"][-2]["content"] == "Which one? [[listen]]"  # same conversation, no screen marker
        receive_speech(ws)
        ws.send_json({"type": "voice_start", "session": "s2"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "s2"})
        only = ws.receive_json()
        assert only["type"] == "screen"
        got = 0
        while got < only["bytes"]:
            got += len(ws.receive_bytes())
        assert ws.receive_json() == {"type": "voice_result", "session": "s2", "heard": "what time is it",
                                     "reply": ""}  # nothing to say: the speaker is done
    assert client.get("/api/v1/screens/not-a-screen.kani").status_code == 404


def test_screen_admin_preview_and_custom_overlays(client):
    login(client)
    assert client.get("/admin/screen").status_code == 200
    spec = {"image": png_data_url(), "overlays": [{"type": "button", "label": "OK"}]}
    out = client.post("/admin/api/screen/preview", json={"spec": spec}, headers=ADMIN).json()
    assert out["regions"][0]["label"] == "OK" and not out["warnings"]
    assert Image.open(io.BytesIO(__import__("base64").b64decode(out["png"]))).size == (240, 240)
    r = client.put("/admin/api/screen/overlays/weather", headers=ADMIN, json={
        "description": "Today's weather", "svg": "<svg viewBox='0 0 100 50'><text>{{temp}} {{ sky }}</text></svg>",
        "x": 20, "y": 20, "w": 200, "h": 100})
    assert r.status_code == 200, r.text
    assert r.json()["overlays"]["weather"]["params"] == ["temp", "sky"]
    assert '"type": "weather", "temp", "sky": ...' in r.json()["prompt"]
    assert client.put("/admin/api/screen/overlays/button", headers=ADMIN,
                      json={"svg": "<svg></svg>"}).status_code == 422  # a built-in name
    assert client.put("/admin/api/screen/overlays/x", headers=ADMIN, json={"svg": "not svg"}).status_code == 422
    assert client.post(f"/admin/api/devices/{MAC}/screen", json={"spec": spec}, headers=ADMIN).status_code == 404
    assert client.delete("/admin/api/screen/overlays/weather", headers=ADMIN).status_code == 204
    from app.screens import fill_template
    assert fill_template("<text>{{a}}</text>", {"a": "<b>&"}) == "<text>&lt;b&gt;&amp;</text>"


class FakeBroker:
    """Just enough of an MQTT broker for the Home Assistant bridge: accepts one client at a time, records what it
    publishes, and can send it messages."""

    def __init__(self):
        import socket
        import threading
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.published = []  # (topic, payload, retain)
        self.subscribed = []
        self.conn = None
        self.connect_flags = None
        self.lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _serve(self):
        import struct
        while True:
            try:
                self.conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                while True:
                    first = self._read(1)[0]
                    length, shift = 0, 0
                    while True:
                        b = self._read(1)[0]
                        length |= (b & 0x7F) << shift
                        shift += 7
                        if not b & 0x80:
                            break
                    body = self._read(length) if length else b""
                    kind = first >> 4
                    if kind == 1:
                        self.connect_flags = body[7]
                        self.conn.sendall(b"\x20\x02\x00\x00")
                    elif kind == 3:
                        n = struct.unpack_from("!H", body)[0]
                        with self.lock:
                            self.published.append((body[2:2 + n].decode(), body[2 + n:], bool(first & 1)))
                    elif kind == 8:
                        pid = body[:2]
                        i, topics = 2, []
                        while i < len(body):
                            n = struct.unpack_from("!H", body, i)[0]
                            topics.append(body[i + 2:i + 2 + n].decode())
                            i += 3 + n
                        self.subscribed += topics
                        self.conn.sendall(b"\x90" + bytes([2 + len(topics)]) + pid + b"\x00" * len(topics))
                    elif kind == 12:
                        self.conn.sendall(b"\xd0\x00")
            except (ConnectionError, OSError):
                continue

    def send(self, topic, payload):
        from app.mqtt import publish_packet
        self.conn.sendall(publish_packet(topic, payload.encode()))

    def wait(self, check, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            with self.lock:
                hit = [p for p in self.published if check(p)]
            if hit:
                return hit
            _t.sleep(0.02)
        raise AssertionError("nothing matching was published")

    def last(self, topic):
        import json
        with self.lock:
            payload = [p for t, p, _ in self.published if t == topic][-1]
        return json.loads(payload)


def test_mqtt_packets_and_broker_addresses():
    from app.mqtt import Broker, encode_length

    assert encode_length(0) == b"\x00" and encode_length(127) == b"\x7f" and encode_length(321) == b"\xc1\x02"
    assert Broker.parse("mqtt://u%40x:p@ha.local") == Broker("ha.local", 1883, "u@x", "p", False)
    assert Broker.parse("mqtts://h:9999").tls and Broker.parse("mqtts://h:9999").port == 9999
    with pytest.raises(ValueError):
        Broker.parse("http://h")


def test_home_assistant_bridge(tmp_path, prebuilt):
    broker = FakeBroker()
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        mqtt_url=f"mqtt://127.0.0.1:{broker.port}")
    with TestClient(create_app(settings)) as c:
        token = adopt(c)
        fake_voice(c)
        state = f"harkenn/{MAC}/state"
        broker.wait(lambda p: p[0] == f"homeassistant/sensor/harkenn_{MAC}/activity/config" and p[2])
        assert broker.connect_flags & 0x24 == 0x24  # a retained last will (harkenn/status offline)
        assert ("harkenn/status", b"online", True) in broker.published
        assert "harkenn/+/+/set" in broker.subscribed and "homeassistant/status" in broker.subscribed
        cfg = broker.last(f"homeassistant/number/harkenn_{MAC}/volume/config")
        assert cfg["command_topic"] == f"harkenn/{MAC}/volume/set" and cfg["device"]["name"] == "Kitchen"
        assert broker.last(state)["online"] is False
        with c.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
            ws.receive_json()
            broker.wait(lambda p: p[0] == state and b'"online": true' in p[1])
            ws.send_json({"type": "voice_start", "session": "h1"})
            broker.wait(lambda p: p[0] == state and b'"activity": "listening"' in p[1])
            broker.send(f"harkenn/{MAC}/volume/set", "40")
            pushed = ws.receive_json()
            assert pushed["type"] == "config" and pushed["config"]["audio"]["volume"] == 40
            broker.wait(lambda p: p[0] == state and b'"volume": 40' in p[1])
            ws.send_json({"type": "voice_cancel", "session": "h1"})
            broker.send(f"harkenn/{MAC}/say/set", "Dinner is ready")
            start, _ = receive_speech(ws)
            assert start["text"] == "Dinner is ready"
            broker.wait(lambda p: p[0] == state and b'"activity": "speaking"' in p[1])
            broker.send(f"harkenn/{MAC}/agents/set", "Do not disturb")
            broker.wait(lambda p: p[0] == state and b'"agents": "Do not disturb"' in p[1])
        assert c.app.state.ctx.db.get_device(MAC)["config"]["agents"]["access"] == "do_not_disturb"
        # Home Assistant restarted: it gets the discovery messages again
        before = len([p for p in broker.published if p[0].endswith("/activity/config")])
        broker.send("homeassistant/status", "online")
        broker.wait(lambda p: len([q for q in broker.published if q[0].endswith("/activity/config")]) > before)
        # the admin UI: the password never reaches the browser, and an unchanged one is kept
        login(c)
        out = c.get("/admin/api/homeassistant", headers=ADMIN).json()
        assert out["status"] == "connected" and out["from_environment"] is True
        r = c.put("/admin/api/homeassistant", headers=ADMIN, json={
            "enabled": True, "broker": f"mqtt://ha:secret@127.0.0.1:{broker.port}", "discovery_prefix": "homeassistant"})
        assert r.status_code == 200 and "secret" not in r.text and "ha:********@" in r.json()["broker"]
        r = c.put("/admin/api/homeassistant", headers=ADMIN, json={
            "enabled": True, "broker": r.json()["broker"], "discovery_prefix": "homeassistant"})
        assert c.app.state.ctx.homeassistant.settings().broker == f"mqtt://ha:secret@127.0.0.1:{broker.port}"
        assert c.put("/admin/api/homeassistant", headers=ADMIN,
                     json={"enabled": True, "broker": "http://nope"}).status_code == 422
        # forgetting a speaker removes it from Home Assistant
        assert c.delete(f"/admin/api/devices/{MAC}", headers=ADMIN).status_code == 204
        broker.wait(lambda p: p[0] == f"homeassistant/sensor/harkenn_{MAC}/activity/config" and p[1] == b"")
    broker.sock.close()


def test_about_page_shows_links_support_and_credits(client):
    from app.about import markdown

    r = client.get("/admin/about")  # open to everyone: no sign-in
    assert r.status_code == 200
    assert "github.com/harkenn-project/server-docker" in r.text and "Buy us a coffee" in r.text
    assert "<td><a href=\"https://github.com/SYSTRAN/faster-whisper\"" in r.text  # the credits, as a table
    assert "Master Magic" in r.text and 'href="/admin/about"' in r.text
    out = str(markdown("# Title\n\nSee [docs](docs/X.md) and **<b>** `x`.\n\n- one\n  more\n- two\n\n| A | B |\n|---|---|\n| 1 | 2 |",
                       "https://g/r"))
    assert "Title" not in out and 'href="https://g/r/blob/main/docs/X.md"' in out and "<strong>&lt;b&gt;</strong>" in out
    assert "<li>one more</li><li>two</li>" in out and "<td>1</td><td>2</td>" in out


# the speaker's own web page (local_access.py)

def adopt_with_password(c, name="Kitchen"):
    req = request_adoption(c)
    login(c)
    r = c.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": req["code"], "name": name},
               headers=ADMIN)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v1/adoption/{req['request_id']}").json()["token"], r.json()["speaker_password"]


def test_wpa_key_matches_the_standard():
    from app.local_access import wpa_key
    # IEEE 802.11i-2004, H.4.3
    assert wpa_key("password", "IEEE") == "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"


def test_adoption_makes_a_speaker_password_kept_only_as_hashes(client):
    from app.auth import verify_password
    from app.local_access import SPEAKER_ITERATIONS, wpa_key
    token, password = adopt_with_password(client)
    assert len(password) == 14 and password.count("-") == 2
    access = client.get("/api/v1/device/config", headers=bearer(token)).json()["local_access"]
    assert verify_password(password, access["speaker_hash"]) and not verify_password("nope", access["speaker_hash"])
    assert access["speaker_hash"].split("$")[1] == str(SPEAKER_ITERATIONS)
    assert access["hotspot_psk"] == wpa_key(password, "Harkenn-66B0")
    assert verify_password("pw", access["admin_hash"])  # logging in sent the admin password, hashed for speakers
    assert access["unlock"] == 0
    device = client.get(f"/admin/api/devices/{MAC}").json()
    assert device["local_access"] == {"speaker_password": True, "viewable": True, "admin_password": True}
    assert password not in json.dumps(client.get("/admin/api/overview").json())
    # whoever can log in here can see it again; it's stored encrypted, not in the clear
    assert client.get(f"/admin/api/devices/{MAC}/speaker-password").json() == {"password": password}
    assert password not in json.dumps(client.get("/admin/api/devices/" + MAC).json())
    from app.local_access import DEVICES_KEY
    assert password not in json.dumps(client.app.state.ctx.db.get_setting(DEVICES_KEY))
    client.post("/admin/logout")
    assert client.get(f"/admin/api/devices/{MAC}/speaker-password").status_code == 401


def test_set_speaker_password_and_unlock(client):
    from app.auth import verify_password
    token = adopt(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        r = client.post(f"/admin/api/devices/{MAC}/speaker-password", json={"password": "short"}, headers=ADMIN)
        assert r.status_code == 422
        r = client.post(f"/admin/api/devices/{MAC}/speaker-password", json={"password": "my own words"},
                        headers=ADMIN)
        assert r.json() == {"password": None, "pushed": True}
        assert verify_password("my own words", ws.receive_json()["local_access"]["speaker_hash"])
        r = client.post(f"/admin/api/devices/{MAC}/speaker-password", json={}, headers=ADMIN).json()
        assert verify_password(r["password"], ws.receive_json()["local_access"]["speaker_hash"])
        assert client.get(f"/admin/api/devices/{MAC}/speaker-password").json() == {"password": r["password"]}
        assert client.post(f"/admin/api/devices/{MAC}/unlock", headers=ADMIN).json() == {"pushed": True}
        assert ws.receive_json()["local_access"]["unlock"] == 1


def test_admin_password_change_reaches_speakers(client):
    from app.auth import verify_password
    token = adopt(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        assert verify_password("pw", ws.receive_json()["local_access"]["admin_hash"])
        r = client.post("/admin/password", data={"current": "pw", "password": "new password 1",
                                                 "confirm": "new password 1"})
        assert r.status_code == 200
        assert verify_password("new password 1", ws.receive_json()["local_access"]["admin_hash"])
        client.post("/admin/logout")
        login_again = client.post("/admin/login", data={"password": "new password 1"}, follow_redirects=False)
        assert login_again.status_code == 303
        ws.send_json({"type": "config_request"})  # logging in with the same password pushes nothing new
        assert verify_password("new password 1", ws.receive_json()["local_access"]["admin_hash"])


def test_factory_reset_from_admin(client):
    token = adopt(client)
    assert client.post(f"/admin/api/devices/{MAC}/factory-reset", headers=ADMIN).status_code == 409
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        assert client.post(f"/admin/api/devices/{MAC}/factory-reset", headers=ADMIN).json() == {"sent": True}
        assert ws.receive_json() == {"type": "command", "command": "factory_reset", "args": {}}
    assert client.get(f"/admin/api/devices/{MAC}").status_code == 404
    assert client.get("/api/v1/device/config", headers=bearer(token)).status_code == 401
    assert client.get("/api/v1/device/config", headers=bearer(token)).status_code == 401


def test_changes_made_on_the_speaker_are_kept(client):
    token = adopt(client)
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["wifi"] = [{"ssid": "Old", "password": "oldpassword"}, {"ssid": "Kept", "password": ""}]
    client.put(f"/admin/api/devices/{MAC}", json={"name": "Kitchen", "config": config}, headers=ADMIN)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        assert ws.receive_json()["version"] == 2
        ws.send_json({"type": "local_changes", "id": 7, "changes": {
            "wifi": [{"ssid": "Cabin", "password": "cabin pass", "ip": "192.168.5.20", "subnet": "255.255.255.0",
                      "gateway": "192.168.5.1", "dns": ""},
                     {"ssid": "Bad", "password": "short"}],
            "wifi_removed": ["Old"], "log_level": "debug", "brightness": 40, "server_url": "http://10.0.0.5:8000"}})
        assert ws.receive_json() == {"type": "local_changes_ack", "id": 7}
        pushed = ws.receive_json()
        assert pushed["version"] == 3
        assert [n["ssid"] for n in pushed["config"]["wifi"]] == ["Cabin", "Kept"]
        assert pushed["config"]["wifi"][0]["ip"] == "192.168.5.20"
        assert pushed["config"]["log_level"] == "debug" and pushed["config"]["display"]["brightness"] == 40
        ws.send_json({"type": "local_changes", "id": 8, "changes": {}})
        assert ws.receive_json() == {"type": "local_changes_ack", "id": 8}  # nothing changed: no push
    message = client.get(f"/admin/api/devices/{MAC}/logs").json()[-1]["message"]
    assert "Wi-Fi Cabin (static 192.168.5.20)" in message and "ignored Wi-Fi 'Bad'" in message
    assert "removed Wi-Fi Old" in message and "server address http://10.0.0.5:8000" in message


def test_static_ip_needs_gateway_and_subnet(client):
    adopt(client)
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["wifi"] = [{"ssid": "Cabin", "password": "", "ip": "192.168.5.20"}]
    r = client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": config}, headers=ADMIN)
    assert r.status_code == 422 and "gateway" in r.text
    config["wifi"][0] |= {"ip": "192.168.5.300", "gateway": "192.168.5.1", "subnet": "255.255.255.0"}
    assert client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": config},
                      headers=ADMIN).status_code == 422


def test_static_links_change_with_their_content(client):
    import hashlib
    from pathlib import Path
    login(client)
    page = client.get("/admin").text
    js = (Path("app/static/admin.js")).read_bytes()
    assert f'/static/admin.js?v={hashlib.sha256(js).hexdigest()[:10]}"' in page and "/static/admin.css?v=" in page


def test_recordings_and_firmware_are_capped(tmp_path, prebuilt):
    from app.firmware import FirmwareStore
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        recordings_per_device=3)
    with TestClient(create_app(settings)) as c:
        token = adopt(c)
        for i in range(5):
            r = c.post("/api/v1/device/recordings", content=b"RIFF" + bytes([i]) * 10,
                       headers={**bearer(token), "Content-Type": "audio/wav"})
            assert r.status_code == 201
        recs = c.get(f"/admin/api/devices/{MAC}/recordings").json()
        assert len(recs) == 3 and len(list((tmp_path / "recordings" / MAC).iterdir())) == 3
        assert [c.get(f"/admin/recordings/{r['id']}").content[4] for r in recs] == [4, 3, 2]  # the newest

    store = FirmwareStore(tmp_path / "fw", keep=2)
    ids = [store.add(fake_firmware(f"0.4.{i}"))["id"] for i in range(4)]
    assert {m["id"] for m in store.list()} == set(ids[2:])
    assert len(list((tmp_path / "fw").glob("*.bin"))) == 2


# remote speakers through the relay (relay.py)

class FakeRelay:
    """The relay's two ends in memory: Harkenn's websocket and the speaker's."""

    def __init__(self):
        import asyncio
        self.to_speaker, self.to_harkenn = asyncio.Queue(), asyncio.Queue()

    # Harkenn's side, as a websockets client connection
    async def send(self, data):
        await self.to_speaker.put(data)

    async def recv(self):
        frame = await self.to_harkenn.get()
        if frame is None:
            raise ConnectionError("closed")
        return frame

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except ConnectionError:
            raise StopAsyncIteration

    async def close(self, code=1000, reason=""):
        await self.to_speaker.put(None)


class SpeakerEnd:
    """What the firmware does at the relay (docs/RELAY.md), in Python."""

    def __init__(self, relay, key):
        self.relay, self.key = relay, key
        self.sent = self.received = 0

    async def handshake(self):
        import secrets
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from app.relay import ANSWER, HELLO, OK, _mac
        frame = await self.relay.to_speaker.get()
        assert frame[0] == HELLO and len(frame) == 17
        hn, sn = frame[1:], secrets.token_bytes(16)
        await self.relay.to_harkenn.put(bytes([ANSWER]) + sn + _mac(self.key, b"harkenn-relay speaker", hn, sn)[:16])
        ok = await self.relay.to_speaker.get()
        assert ok == bytes([OK]) + _mac(self.key, b"harkenn-relay harkenn", hn, sn)[:16]
        self.send_key = AESGCM(_mac(self.key, b"harkenn-relay s2h", hn, sn))
        self.recv_key = AESGCM(_mac(self.key, b"harkenn-relay h2s", hn, sn))

    async def send(self, kind, data):
        import struct
        self.sent += 1
        counter = struct.pack(">Q", self.sent)
        await self.relay.to_harkenn.put(b"\x10" + counter + self.send_key.encrypt(b"\0" * 4 + counter, kind + data, None))

    async def recv(self):
        import struct
        frame = await self.relay.to_speaker.get()
        assert frame[0] == 0x10
        (counter,) = struct.unpack(">Q", frame[1:9])
        assert counter > self.received
        self.received = counter
        plain = self.recv_key.decrypt(b"\0" * 4 + frame[1:9], frame[9:], None)
        return plain[:1], plain[1:]

    async def recv_text(self):
        while True:
            kind, data = await self.recv()
            if kind == b"T":
                return json.loads(data)


@pytest.fixture
def relay_client(tmp_path, prebuilt):
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        relay_url="wss://relay.example", relay_server_url="ws://relay.internal")
    app = create_app(settings)
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def remote_speaker(c):
    token = adopt(c)
    config = c.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["remote"] = True
    c.put(f"/admin/api/devices/{MAC}", json={"name": "Kitchen", "config": config}, headers=ADMIN)
    return token, c.get("/api/v1/device/config", headers=bearer(token)).json()["remote"]


def test_remote_speaker_through_the_relay(relay_client, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from app import relay as relay_mod
    c = relay_client
    token, remote = remote_speaker(c)
    assert remote["url"] == "wss://relay.example" and len(remote["id"]) == 32 and len(remote["key"]) == 64

    ctx = c.app_ref.state.ctx
    manager = relay_mod.RelayManager(ctx, c.app_ref)
    manager.check()
    assert manager.tasks == {}  # not yet: the speaker hasn't said it has the credentials
    with c.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "status", "status": {"remote": remote["id"]}})
        ws.send_json({"type": "config_request"})
        ws.receive_json()
    assert relay_mod._all(ctx)[MAC]["ready"] is True
    assert c.get(f"/admin/api/devices/{MAC}").json()["relay_ready"] is True

    fake = FakeRelay()
    urls = []

    @asynccontextmanager
    async def fake_connect(url, **kw):
        urls.append(url)
        yield fake

    monkeypatch.setattr(relay_mod, "connect", fake_connect)

    async def scenario():
        meet = asyncio.create_task(manager._meet(MAC))
        await fake.to_harkenn.put('{"type":"paired"}')
        speaker = SpeakerEnd(fake, bytes.fromhex(remote["key"]))
        await speaker.handshake()
        await speaker.send(b"T", json.dumps({"type": "hello", "token": token}).encode())
        first = await speaker.recv_text()
        assert first["type"] == "config" and first["remote"]["id"] == remote["id"]
        assert ctx.hub.via(MAC) == "relay"
        url = first["character"]["states"]["idle"]["urls"]["144"]
        await speaker.send(b"T", json.dumps({"type": "fetch", "id": 7, "url": url}).encode())
        result = await speaker.recv_text()
        assert result["type"] == "fetch_result" and result["id"] == 7 and result["status"] == 200
        got = b""
        while len(got) < result["size"]:
            kind, data = await speaker.recv()
            assert kind == b"F" and data[:4] == (7).to_bytes(4, "big")
            got += data[4:]
        assert got[:4] == b"KANI"
        await speaker.send(b"T", json.dumps({"type": "fetch", "id": 8, "url": "http://x/admin/api/overview"}).encode())
        assert (await speaker.recv_text())["status"] == 403  # only what a speaker downloads
        await fake.to_harkenn.put(None)  # the speaker goes away
        for _ in range(100):
            if not ctx.hub.online(MAC):
                break
            await asyncio.sleep(0.05)
        assert not ctx.hub.online(MAC)
        meet.cancel()

    c.portal.call(scenario)
    assert urls[0] == f"ws://relay.internal/relay/harkenn/{remote['id']}"


def test_relay_rejects_a_wrong_key_and_tampering(relay_client):
    import asyncio
    from cryptography.exceptions import InvalidTag
    from app import relay as relay_mod
    c = relay_client
    token, remote = remote_speaker(c)

    async def handshake_with(key):
        fake = FakeRelay()
        task = asyncio.create_task(relay_mod.RelayManager._handshake(fake, bytes.fromhex(remote["key"])))
        speaker = SpeakerEnd(fake, key)
        speaker_task = asyncio.create_task(speaker.handshake())
        result = await asyncio.gather(task, return_exceptions=True)
        speaker_task.cancel()
        return result

    [result] = c.portal.call(handshake_with, b"\0" * 32)
    assert isinstance(result, ConnectionError)  # the speaker didn't have the key

    channel = relay_mod.Channel(b"k" * 32, b"h" * 16, b"s" * 16)
    mirror = relay_mod.Channel(b"k" * 32, b"h" * 16, b"s" * 16)
    mirror.recv_key, mirror.send_key = mirror.send_key, mirror.recv_key  # the speaker's side
    frame = mirror.seal(b"T", b"hi")
    assert channel.open(frame) == (b"T", b"hi")
    with pytest.raises(ValueError):
        channel.open(frame)  # played again
    tampered = bytearray(mirror.seal(b"T", b"hi"))
    tampered[-1] ^= 1
    with pytest.raises(InvalidTag):
        channel.open(bytes(tampered))


def test_remote_needs_a_relay(client):
    token = adopt(client)
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["remote"] = True
    client.put(f"/admin/api/devices/{MAC}", json={"name": "Kitchen", "config": config}, headers=ADMIN)
    assert client.get("/api/v1/device/config", headers=bearer(token)).json()["remote"] is None


def test_sleep_display_and_wake_arbitration_settings(client):
    token = adopt(client)
    config = client.get("/api/v1/device/config", headers=bearer(token)).json()["config"]
    assert config["display"]["sleep_display"] == "character" and config["audio"]["wake_arbitration"] is True
    config["display"]["sleep_display"] = "clock"
    config["audio"]["wake_arbitration"] = False
    assert client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": config},
                      headers=ADMIN).status_code == 200
    seen = client.get("/api/v1/device/config", headers=bearer(token)).json()["config"]
    assert seen["display"]["sleep_display"] == "clock" and seen["audio"]["wake_arbitration"] is False
    config["display"]["sleep_display"] = "dark"
    assert client.put(f"/admin/api/devices/{MAC}", json={"name": "K", "config": config},
                      headers=ADMIN).status_code == 422


# the weather screen (weather.py)

OPEN_METEO = {
    "current": {"temperature_2m": -4.2, "apparent_temperature": -11.0, "relative_humidity_2m": 81, "is_day": 1,
                "precipitation": 0.4, "weather_code": 73, "wind_speed_10m": 17.0, "wind_direction_10m": 300,
                "wind_gusts_10m": 31.0, "pressure_msl": 1012.0},
    "daily": {"time": ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"], "weather_code": [73, 3, 0, 61],
              "temperature_2m_max": [-1.0, 2.0, 5.0, 7.0], "temperature_2m_min": [-9.0, -6.0, -3.0, 1.0],
              "precipitation_probability_max": [80, 20, 0, 60], "uv_index_max": [1.2, 2.0, 3.0, 2.0]},
}


def fake_open_meteo(monkeypatch, air=None):
    from app import weather
    calls = []

    def get(url):
        calls.append(url)
        if "geocoding" in url:
            return {"results": [{"name": "Calgary", "admin1": "Alberta", "latitude": 51.05, "longitude": -114.07}]}
        if "air-quality" in url:
            return air or {"current": {"us_aqi": 30, "pm2_5": 5}}
        return OPEN_METEO

    monkeypatch.setattr(weather, "_get", get)
    return calls


def test_weather_screen_for_every_condition_stays_small():
    from app import kani, weather
    for condition in weather.CONDITIONS:
        frames, durations, background = weather.render(
            {"condition": condition, "temp": -14, "high": 3, "low": -6, "wind": 15, "wind_dir": 300,
             "location": "Calgary, Alberta", "precip_chance": 60})
        assert len(frames) == weather.FRAMES and frames[0].size == (240, 240) and len(background) == 6
        assert len(kani.encode(frames, durations, background)) < 400 * 1024, condition
        assert frames[0].tobytes() != frames[weather.FRAMES // 4].tobytes(), condition  # it moves
    # nothing known at all still draws (a clear sky); a forecast fits four days
    weather.render({})
    weather.render({"temp": 12, "forecast": [{"label": "Mon", "condition": "rain", "high": 9, "low": 1}] * 4})
    assert weather.deg(-4.2) == "-4°" and weather.deg(2.46) == "2°" and weather.number(2.46) == "2.5" and weather.compass(300) == "NW"
    assert weather.normalize("Sunny") == "clear" and weather.normalize("light snow") == "light_snow"
    assert weather.normalize("volcano") == ""


def test_weather_marker():
    from app import weather
    assert weather.split_marker("It's cold. [[weather]]") == ("It's cold. ", {}, "")
    text, spec, err = weather.split_marker('Banff: [[weather {"location": "Banff", "temp": -9}]] brr.')
    assert text == "Banff:  brr." and spec == {"location": "Banff", "temp": -9} and not err
    text, spec, err = weather.split_marker("[[weather {nope}]] ok")
    assert text == " ok" and spec == {} and "invalid" in err
    assert weather.split_marker("no marker") == ("no marker", None, "")


def test_live_weather_and_merging(client, monkeypatch):
    from app import weather
    calls = fake_open_meteo(monkeypatch)
    ctx = client.app.state.ctx
    assert ctx.weather.current() is None and ctx.weather.summary() == ""
    assert ctx.weather.set_place("Calgary")["name"] == "Calgary, Alberta"
    w = ctx.weather.current()
    assert w["condition"] == "snow" and w["temp"] == -4.2 and w["snow"] == 0.4 and w["aqi"] == 30
    assert w["high"] == -1.0 and [d["label"] for d in w["daily"]][:2] == ["Today", "Tmrw"]
    ctx.weather.current()
    assert sum("forecast?" in u for u in calls) == 1  # cached
    summary = ctx.weather.summary()
    assert "Calgary, Alberta" in summary and "snow" in summary and "-4" in summary and "Tmrw" in summary
    # asked-for fields win; another place takes nothing from here; "forecast" uses the coming days
    assert weather.merge({"temp": 3}, w)["temp"] == 3 and weather.merge({"temp": 3}, w)["wind"] == 17.0
    assert "wind" not in weather.merge({"location": "Banff", "temp": -9}, w)
    assert len(weather.merge({"view": "forecast"}, w)["forecast"]) == 4
    # wildfire smoke: a clear sky with bad air shows as smoke
    smoky = weather.from_open_meteo({"current": {"weather_code": 0}}, {"current": {"us_aqi": 180, "pm2_5": 90}},
                                    "X", "metric")
    assert smoky["condition"] == "smoke"


def test_weather_admin_settings(client, monkeypatch):
    fake_open_meteo(monkeypatch)
    login(client)
    assert client.get("/admin/api/weather").json()["place"] == ""
    r = client.put("/admin/api/weather", json={"place": "Calgary", "units": "metric"}, headers=ADMIN).json()
    assert r["place"] == "Calgary, Alberta" and "snow" in r["now"]
    from app import weather
    monkeypatch.setattr(weather, "_get", lambda url: {"results": []})
    assert client.put("/admin/api/weather", json={"place": "Nowhere"}, headers=ADMIN).status_code == 422
    assert client.put("/admin/api/weather", json={"place": ""}, headers=ADMIN).json()["place"] == ""


def test_weather_from_the_assistant(client, monkeypatch):
    from app.voice import brain, service
    fake_open_meteo(monkeypatch)
    client.app.state.ctx.weather.set_place("Calgary")
    token = adopt(client)
    fake_voice(client, heard="what's the weather")
    seen = []

    def post(url, body, headers, timeout):
        seen.append(body)
        return {"choices": [{"message": {"content": "Snowing, minus four. [[weather]]"}}]}

    monkeypatch.setattr(brain, "post", post)
    monkeypatch.setattr(service.VoiceService, "effective",
                        lambda self: self.settings().model_copy(update={"assistant_url": "http://llm/v1"}))
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "voice_start", "session": "s1"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "s1"})
        shown = ws.receive_json()
        assert shown["type"] == "screen" and shown["regions"] == [] and shown["bytes"] < 700 * 1024
        got = b""
        while len(got) < shown["bytes"]:
            got += ws.receive_bytes()
        assert got[:4] == b"KANI"
        assert ws.receive_json()["reply"] == "Snowing, minus four."
    system, newest = seen[0]["messages"][0]["content"], seen[0]["messages"][-1]["content"]
    assert "[[weather" in system and "The weather in Calgary, Alberta now: snow" in newest
    assert "Calgary" not in system  # the instructions stay the same; the weather goes on the message


def test_mcp_weather(client, monkeypatch):
    fake_open_meteo(monkeypatch)
    device_token = adopt(client)
    token = mcp_token(client)
    assert mcp_tool(client, token, "get_weather")["weather"] == {}  # no place yet
    client.app.state.ctx.weather.set_place("Calgary")
    assert mcp_tool(client, token, "get_weather")["weather"]["condition"] == "snow"
    assert mcp_tool(client, token, "show_weather")["skipped"] == ["Kitchen (offline)"]
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        out = mcp_tool(client, token, "show_weather", speaker="kitchen", location="Banff", condition="blizzard",
                       temp=-18, wind=40, seconds=30)
        assert out["shown_on"] == ["Kitchen"] and not out["problems"]
        shown = ws.receive_json()
        assert shown["type"] == "screen" and shown["seconds"] == 30
        got = 0
        while got < shown["bytes"]:
            got += len(ws.receive_bytes())


# timers, alarms and notifications (timers.py)

def test_timer_markers_and_messages():
    from app import timers
    assert timers.duration_s("9m") == 540 and timers.duration_s("1h30m") == 5400 and timers.duration_s(90) == 90
    assert timers.duration_s("90 seconds") == 90 and timers.duration_s("1.5 hours") == 5400
    assert timers.duration_s("1:30") == 90 and timers.duration_s("soon") == 0
    text, actions, problems = timers.split_markers(
        'Done. [[timer {"start": "9m", "label": "pasta"}]] And [[timer {"alarm": "6:45 am", "days": "weekdays"}]]!')
    assert text == "Done.  And !" and len(actions) == 2 and not problems
    msg, _ = timers.message(actions[0])
    assert msg == {"type": "timer", "action": "set", "kind": "timer", "seconds": 540, "label": "pasta"}
    msg, _ = timers.message(actions[1])
    assert msg["kind"] == "alarm" and msg["time"] == "06:45" and msg["days"] == 0x3E
    assert timers.message({"alarm": "7pm", "days": "mon, wed"})[0]["time"] == "19:00"
    assert timers.message({"alarm": "7pm", "days": "mon, wed"})[0]["days"] == 0b1010
    assert timers.message({"alarm": "12 am"})[0]["time"] == "00:00"
    assert timers.message({"alarm": "07:30", "date": "2026-12-24"})[0]["date"] == "2026-12-24"
    assert timers.message({"alarm": "25:00"})[0] is None and timers.message({"alarm": "7", "date": "soon"})[0] is None
    assert timers.message({"start": "forever"})[0] is None
    assert timers.message({"cancel": "pasta"})[0] == {"type": "timer", "action": "cancel", "what": "pasta"}
    assert timers.message({"show": True})[0]["action"] == "show"
    assert timers.message({"stop": True})[0]["action"] == "stop"
    assert timers.message({"nothing": 1})[0] is None
    assert timers.split_markers("[[timer oops]] hi")[2] == ["a timer marker without an action"]


def test_timer_board_summary():
    import time
    from app import timers
    b = timers.Board()
    assert b.summary("d") == ""
    b.report("d", {"items": [{"id": 1, "kind": "timer", "label": "pasta", "remaining_s": 272, "duration": 540},
                             {"id": 2, "kind": "alarm", "label": "", "remaining_s": 30000, "time": "07:30",
                              "days": 0x3E}]})
    b.event("d", "timer_missed", {"kind": "timer", "label": "tea"})
    b.notify("d", "Package", "It's on the porch")
    s = b.summary("d")
    assert 'timer "pasta" (id 1) 4 min 3' in s and "of 9 min 0 s" in s
    assert "alarm (id 2) at 07:30 weekdays" in s
    assert 'timer "tea" rang unanswered' in s and '"Package: It\'s on the porch"' in s
    b.seen("d")
    assert "Unread" not in b.summary("d")
    b.report("d", {"items": []})
    assert "Timers and alarms on this speaker: none." in b.summary("d")


def test_timer_from_the_assistant(client, monkeypatch):
    from app.voice import brain, service
    token = adopt(client)
    fake_voice(client, heard="set a pasta timer for nine minutes")
    seen = []

    def post(url, body, headers, timeout):
        seen.append(body)
        return {"choices": [{"message": {"content": 'Pasta timer, nine minutes. [[timer {"start": "9m", "label": "pasta"}]]'}}]}

    monkeypatch.setattr(brain, "post", post)
    monkeypatch.setattr(service.VoiceService, "effective",
                        lambda self: self.settings().model_copy(update={"assistant_url": "http://llm/v1"}))
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "timers", "items": [{"id": 4, "kind": "alarm", "label": "work", "remaining_s": 3600,
                                                   "time": "07:30", "days": 62}]})
        ws.send_json({"type": "voice_start", "session": "s1"})
        ws.send_bytes(SPEECH * 5)
        ws.send_json({"type": "voice_end", "session": "s1"})
        msg = ws.receive_json()
        assert msg == {"type": "timer", "action": "set", "kind": "timer", "seconds": 540, "label": "pasta"}
        assert ws.receive_json()["reply"] == "Pasta timer, nine minutes."
    assert "[[timer" in seen[0]["messages"][0]["content"]  # the instructions
    assert 'alarm "work" (id 4) at 07:30 weekdays' in seen[0]["messages"][-1]["content"]  # what's set, on the message


def test_mcp_timers_and_notifications(client):
    device_token = adopt(client)
    token = mcp_token(client)
    tools = {t["name"] for t in mcp_call(client, token, "tools/list")["tools"]}
    assert {"list_timers", "set_timer", "set_alarm", "cancel_timer", "notify"} <= tools
    assert mcp_tool(client, token, "set_timer", duration="5m")["note"] == "Kitchen is offline."
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        out = mcp_tool(client, token, "set_alarm", time_of_day="6:30 am", days="weekends", label="hike")
        assert out["done"] and out["speaker"] == "Kitchen"
        assert ws.receive_json() == {"type": "timer", "action": "set", "kind": "alarm", "time": "06:30",
                                     "label": "hike", "days": 0x41}
        assert mcp_tool(client, token, "set_timer", duration="soon")["done"] is False
        assert mcp_tool(client, token, "cancel_timer", which="hike")["done"]
        assert ws.receive_json() == {"type": "timer", "action": "cancel", "what": "hike"}
        out = mcp_tool(client, token, "notify", text="The dryer is done", title="Laundry")
        assert out["shown_on"] == ["Kitchen"]
        assert ws.receive_json() == {"type": "notify", "title": "Laundry", "text": "The dryer is done", "seconds": 12}
        ws.send_json({"type": "timers", "items": []})
        ws.send_json({"type": "config_request"})
        ws.receive_json()
        listed = mcp_tool(client, token, "list_timers")["speakers"][0]
        assert listed["timers"] == [] and listed["unread_notifications"] == ["The dryer is done"]
        ws.send_json({"type": "notifications_seen"})
        ws.send_json({"type": "config_request"})
        ws.receive_json()
        assert mcp_tool(client, token, "list_timers")["speakers"][0]["unread_notifications"] == []
    # unread notifications come back, quietly, when a speaker connects again
    client.app.state.ctx.board.notify(MAC, "", "Call Mom")
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        assert ws.receive_json()["type"] == "config"
        assert ws.receive_json() == {"type": "notify", "title": "", "text": "Call Mom", "quiet": True}


def test_timers_in_the_admin_ui(client):
    device_token = adopt(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "timers", "items": [{"id": 7, "kind": "timer", "label": "tea", "remaining_s": 60,
                                                   "duration": 180}]})
        ws.send_json({"type": "config_request"})
        ws.receive_json()
        d = client.get(f"/admin/api/devices/{MAC}").json()
        assert d["timers"][0]["label"] == "tea"
        r = client.post(f"/admin/api/devices/{MAC}/timers/cancel", json={"what": "7"}, headers=ADMIN)
        assert r.json() == {"sent": True}
        assert ws.receive_json() == {"type": "timer", "action": "cancel", "what": "7"}
        client.app.state.ctx.board.notify(MAC, "", "hello")
        assert client.post(f"/admin/api/devices/{MAC}/notifications/clear", headers=ADMIN).status_code == 204
        assert ws.receive_json() == {"type": "notifications_clear"}
    assert client.get(f"/admin/api/devices/{MAC}").json()["notifications"] == []


def test_sd_card_report(client):
    device_token = adopt(client)
    assert client.get(f"/admin/api/devices/{MAC}").json()["card"] is None  # never reported (older firmware)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "card", "card": {
            "present": True, "total_mb": 30000, "free_mb": 29000,
            "music": [{"name": "Study", "tracks": 12}, {"name": "", "tracks": 3}, "junk"],
            "soundscapes": ["Rain", "White Noise", ""], "meditations": ["Breathing"],
            "photos": [{"name": "Sample", "pictures": 8}]}})
        ws.send_json({"type": "config_request"})
        ws.receive_json()
        card = client.get(f"/admin/api/devices/{MAC}").json()["card"]
        assert card["music"] == [{"name": "Study", "tracks": 12}]
        assert card["soundscapes"] == ["Rain", "White Noise"] and card["photos"][0]["pictures"] == 8
        ws.send_json({"type": "card", "card": {"present": False, "unreadable": True}})  # exFAT, say
        ws.send_json({"type": "config_request"})
        ws.receive_json()
        assert client.get(f"/admin/api/devices/{MAC}").json()["card"] == {"present": False, "unreadable": True}
        login(client)
        r = client.post(f"/admin/api/devices/{MAC}/command", json={"command": "card_format"}, headers=ADMIN)
        assert r.json() == {"sent": True}
        assert ws.receive_json() == {"type": "command", "command": "card_format", "args": {}}
        ws.send_json({"type": "card", "card": {"present": False}})  # taken out: works as before
        ws.send_json({"type": "config_request"})
        ws.receive_json()
    assert client.get(f"/admin/api/devices/{MAC}").json()["card"] == {"present": False}


CARD = {"present": True, "total_mb": 30000, "free_mb": 29000, "music": [{"name": "Background", "tracks": 10}],
        "soundscapes": ["City Rain", "Thunderstorm", "White Noise", "Campfire"],
        "meditations": ["Breathing", "Body Scan"], "photos": []}


def test_media_requests_are_recognized():
    from app import media
    playing = {"state": "playing", "kind": "music", "album": "Background", "title": "Clair de lune"}
    assert media.parse("Play rain sounds", CARD, {})["album"] == "City Rain"
    req = media.parse("Hey, play white noise for an hour please", CARD, {})
    assert (req["kind"], req["album"], req["minutes"]) == ("soundscape", "White Noise", 60)
    assert media.parse("play some relaxing music", CARD, {})["album"] == "Background"
    assert media.parse("play the breathing meditation", CARD, {})["reply"] == "Starting the Breathing meditation."
    assert media.parse("pause", CARD, playing)["action"] == "pause"
    assert media.parse("pause", CARD, {})  is None  # nothing playing: not ours
    assert media.parse("stop", CARD, {}) is None
    assert media.parse("stop in half an hour", CARD, playing) == {"action": "sleep", "minutes": 30,
                                                                  "reply": "OK, I'll stop in half an hour."}
    assert media.parse("what's playing", CARD, playing)["reply"] == "This is Clair de lune."
    assert media.parse("play Despacito", CARD, {}) is None  # not on the card: the assistant may know better
    assert media.parse("what's the weather", CARD, playing) is None
    assert "memory card" in media.parse("play some music", {"present": False}, {})["reply"]
    assert media.parse("play the news", {"present": False}, {}) is None
    with pytest.raises(ValueError):
        media.command("play", kind="video", album="x")


def test_media_by_voice(client):
    token = adopt(client)
    fake_voice(client, heard="play the rain for 20 minutes")
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "card", "card": CARD})
        ws.send_json({"type": "voice_start", "session": "m1"})
        ws.send_bytes(b"\x00\x01" * 16000)
        ws.send_json({"type": "voice_end", "session": "m1"})
        assert ws.receive_json() == {"type": "media", "action": "play", "kind": "soundscape", "album": "City Rain",
                                     "track": 0, "shuffle": False, "minutes": 20}
        assert ws.receive_json()["reply"] == "Playing City Rain for 20 minutes."
        receive_speech(ws)
        ws.send_json({"type": "media_state", "media": {"state": "playing", "kind": "soundscape",
                                                       "album": "City Rain", "title": "City Rain", "track": 0,
                                                       "tracks": 1, "repeat": "one", "sleep_s": 1199}})
        fake_voice(client, heard="Pause.")
        ws.send_json({"type": "voice_start", "session": "m2"})
        ws.send_bytes(b"\x00\x01" * 16000)
        ws.send_json({"type": "voice_end", "session": "m2"})
        assert ws.receive_json() == {"type": "media", "action": "pause"}
        assert ws.receive_json() == {"type": "voice_result", "session": "m2", "heard": "Pause.", "reply": ""}
        ws.send_json({"type": "config_request"})
        ws.receive_json()
    d = client.get(f"/admin/api/devices/{MAC}").json()
    assert d["media"]["album"] == "City Rain" and d["media"]["sleep_s"] == 1199


def test_media_through_mcp(client):
    device_token = adopt(client)
    token = mcp_token(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(device_token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "card", "card": CARD})
        ws.send_json({"type": "config_request"})
        ws.receive_json()
        lib = mcp_tool(client, token, "media_library")
        assert lib["speakers"][0]["soundscapes"][0] == "City Rain" and lib["speakers"][0]["has_card"]
        r = mcp_tool(client, token, "play_media", what="body scan")
        assert r["done"] and r["kind"] == "meditation"
        assert ws.receive_json()["album"] == "Body Scan"
        assert not mcp_tool(client, token, "play_media", what="Jazz")["done"]
        assert mcp_tool(client, token, "control_media", action="sleep", minutes=15)["done"]
        assert ws.receive_json() == {"type": "media", "action": "sleep", "minutes": 15}
        assert not mcp_tool(client, token, "control_media", action="dance")["done"]


def test_stop_model_for_speakers(tmp_path, prebuilt):
    words = tmp_path / "words"
    words.mkdir()
    (words / "stop.tflite").write_bytes(b"TFL3" + bytes(100))
    (words / "stop.json").write_text('{"micro": {"probability_cutoff": 0.5, "sliding_window_size": 5, '
                                     '"tensor_arena_size": 21000}}')
    settings = Settings(data_dir=tmp_path, admin_password="pw", session_secret="s" * 32,
                        public_url="http://testserver", secure_cookies=False, prebuilt_characters=prebuilt,
                        wakewords_dir=words)
    with TestClient(create_app(settings)) as c:
        token = adopt(c)
        stop = c.get("/api/v1/device/config", headers=bearer(token)).json()["stop_word"]
        assert stop["cutoff"] == 0.5 and stop["arena"] == 21000 and "/assets/wakewords/stop.tflite" in stop["url"]
        assert c.get(stop["url"].replace("http://testserver", "")).content[:4] == b"TFL3"


def test_news_only_once_per_conversation():
    """Within a conversation, the time, weather and timers go to the assistant only when they're news."""
    from datetime import datetime, timedelta
    from app.voice import brain
    convs = brain.Conversations()
    t = datetime(2026, 11, 3, 9, 0)
    first = convs.news("d", t, {"weather": "Snow, -4.", "timers": ""})
    assert first == "Tue Nov 3 2026, 9:00 AM. Snow, -4."
    assert convs.news("d", t + timedelta(minutes=1), {"weather": "Snow, -4.", "timers": ""}) == ""
    assert convs.news("d", t + timedelta(minutes=2), {"weather": "Snow, -4.", "timers": "Timer pasta."}) == \
        "Timer pasta."
    assert convs.news("d", t + timedelta(minutes=3), {"weather": "Snow, -4.", "timers": ""}) == \
        "No timers or alarms now."
    assert convs.news("d", t + timedelta(minutes=9), {"weather": "Snow, -5.", "timers": ""}) == \
        "Tue Nov 3 2026, 9:09 AM. Snow, -5."
    convs.add("d", "hi", "hello")
    convs.start("d")  # a new conversation hears it all again
    assert convs.news("d", t + timedelta(minutes=10), {"weather": "Snow, -5.", "timers": ""}).startswith("Tue")


# speaker groups (groups.py)

def test_groups_share_settings_and_speakers_keep_their_own(client):
    token = adopt(client)
    home = client.get("/admin/api/groups/home").json()
    assert home["default"] and [m["id"] for m in home["members"]] == [MAC]
    config = home["config"]
    config["audio"]["volume"] = 40
    config["display"]["theme"] = "ocean"
    config["wifi"] = [{"ssid": "Home", "password": "hunter2hunter2"}]
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        r = client.put("/admin/api/groups/home", json={"name": "Home", "config": config}, headers=ADMIN)
        assert r.json() == {"saved": True, "speakers": 1, "pushed": 1}
        pushed = ws.receive_json()
        assert pushed["config"]["audio"]["volume"] == 40 and pushed["config"]["wifi"][0]["ssid"] == "Home"

        # the speaker's own: brightness, and its own network (with a fixed IP), on top of the group's
        device = client.get(f"/admin/api/devices/{MAC}").json()
        mine = device["config"]
        mine["display"]["brightness"] = 30
        mine["wifi"] = [{"ssid": "Cabin", "password": "", "ip": "10.0.0.9", "subnet": "255.255.255.0",
                         "gateway": "10.0.0.1", "dns": ""}]
        r = client.put(f"/admin/api/devices/{MAC}", headers=ADMIN,
                       json={"name": "Kitchen", "config": mine, "overrides": ["display.brightness"]})
        assert r.status_code == 200, r.text
        pushed = ws.receive_json()["config"]
        assert pushed["display"]["brightness"] == 30
        assert [n["ssid"] for n in pushed["wifi"]] == ["Cabin", "Home"]
        assert pushed["wifi"][1]["password"] == "hunter2hunter2"  # the group's, not the mask
        assert client.get(f"/admin/api/devices/{MAC}").json()["overrides"] == ["display.brightness", "wifi:Cabin"]

        # the group changes: the speaker follows, except for its own
        config = client.get("/admin/api/groups/home").json()["config"]
        config["display"]["brightness"] = 90
        config["audio"]["volume"] = 55
        client.put("/admin/api/groups/home", json={"name": "Home", "config": config}, headers=ADMIN)
        pushed = ws.receive_json()["config"]
        assert pushed["display"]["brightness"] == 30 and pushed["audio"]["volume"] == 55

        # another group: it keeps its own, takes the rest from there
        cabin = client.post("/admin/api/groups", json={"name": "Cabin"}, headers=ADMIN).json()
        assert cabin["id"] == "cabin" and cabin["config"]["audio"]["volume"] == 55  # a copy of the default group
        config = cabin["config"]
        config["audio"]["volume"] = 20
        config["wifi"] = []
        client.put("/admin/api/groups/cabin", json={"name": "Cabin", "config": config}, headers=ADMIN)
        r = client.post("/admin/api/groups/cabin/members", json={"devices": [MAC]}, headers=ADMIN)
        assert r.json() == {"moved": 1, "pushed": 1}
        pushed = ws.receive_json()["config"]
        assert pushed["audio"]["volume"] == 20 and pushed["display"]["brightness"] == 30
        assert [n["ssid"] for n in pushed["wifi"]] == ["Cabin"]

        # deleting a group moves its speakers to the default group; the default group can't go
        assert client.delete("/admin/api/groups/home", headers=ADMIN).status_code == 409
        assert client.delete("/admin/api/groups/cabin", headers=ADMIN).status_code == 204
        pushed = ws.receive_json()["config"]
        assert pushed["audio"]["volume"] == 55 and pushed["display"]["brightness"] == 30
    d = client.get(f"/admin/api/devices/{MAC}").json()
    assert d["group"] == {"id": "home", "name": "Home"}


def test_settings_changed_elsewhere_become_the_speakers_own(client):
    from app import groups
    token = adopt(client)
    db = client.app.state.ctx.db
    db.note_device_setting(MAC, "audio", "volume", 12)  # from its touch screen
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        ws.send_json({"type": "local_changes", "changes": {"log_level": "debug"}})  # from its own page
        ws.send_json({"type": "config_request"})
        ws.receive_json()
    assert set(db.get_device(MAC)["overrides"]) == {"audio.volume", "log_level"}
    config = groups.get(db, "home")["config"]
    config["audio"]["volume"] = 99
    config["log_level"] = "warn"
    config["display"]["brightness"] = 44
    groups.save(db, "home", config=config)
    after = db.get_device(MAC)["config"]
    assert after["audio"]["volume"] == 12 and after["log_level"] == "debug" and after["display"]["brightness"] == 44


def test_old_defaults_become_the_first_group(tmp_path, prebuilt):
    from app import groups
    from app.db import Database
    from app.models import DeviceConfig
    db = Database(tmp_path / "harkenn.db")
    defaults = DeviceConfig().model_dump()
    defaults["audio"]["volume"] = 61
    db.set_setting("default_config", defaults)
    mine = DeviceConfig().model_dump()
    mine["audio"]["volume"] = 61
    mine["display"]["brightness"] = 20
    db.adopt_device(device_id=MAC, name="Office", hardware="", firmware="", token_hash="x", config=mine, now=1)
    groups.ensure(db)
    assert groups.get(db, "home")["config"]["audio"]["volume"] == 61
    device = db.get_device(MAC)
    assert device["group_id"] == "home" and device["overrides"] == ["display.brightness"]
    assert device["config"] == mine and device["config_version"] == 1  # nothing changes for the speaker


def test_time_zone_by_name(client):
    from app import timezones
    token = adopt(client)
    rule = timezones.posix("Europe/Paris")
    assert rule.startswith("CET-1CEST")
    assert timezones.posix("../../etc/passwd") == "" and timezones.posix("Nowhere/Town") == ""
    config = client.get(f"/admin/api/devices/{MAC}").json()["config"]
    config["timezone_name"] = "Europe/Paris"
    r = client.put(f"/admin/api/devices/{MAC}", headers=ADMIN,
                   json={"name": "Kitchen", "config": config, "overrides": ["timezone", "timezone_name"]})
    assert r.status_code == 200, r.text
    sent = client.get("/api/v1/device/config", headers=bearer(token)).json()["config"]
    assert sent["timezone"] == rule and sent["timezone_name"] == "Europe/Paris"
    assert "Europe/Paris" in client.get("/admin/api/catalog").json()["timezones"]


def test_adopt_into_a_group(client):
    login(client)
    client.post("/admin/api/groups", json={"name": "Kids rooms"}, headers=ADMIN)
    req = request_adoption(client)
    r = client.post(f"/admin/api/adoptions/{req['request_id']}/approve", headers=ADMIN,
                    json={"code": req["code"], "name": "Nursery", "group": "kids-rooms"})
    assert r.status_code == 200, r.text
    d = client.get(f"/admin/api/devices/{MAC}").json()
    assert d["group"]["id"] == "kids-rooms" and d["overrides"] == [] and d["config_version"] == 1


# household modules (household/)

def _household(client, *modules):
    login(client)
    hh = client.app.state.ctx.household
    body = {"settings": {**hh.settings().model_dump(), "enabled": {m: True for m in modules}}}
    r = client.put("/admin/api/household", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return hh


def test_household_modules_are_off_except_memories(client):
    login(client)
    out = client.get("/admin/api/household").json()
    assert [m["name"] for m in out["modules"] if m["enabled"]] == ["memories"]
    page = client.get("/admin/modules").text
    assert "Modules" in page and '/admin/household/memories"' in page and '/admin/household/todo"' not in page
    hh = client.app.state.ctx.household
    assert "[[memory" in hh.prompt() and "[[todo" not in hh.prompt()
    _household(client, "memories", "todo", "shopping")
    assert '/admin/household/todo"' in client.get("/admin/modules").text
    for m in ("memories", "todo", "shopping", "reminders", "calendar", "messages", "chores"):
        assert client.get(f"/admin/household/{m}").status_code == 200, m
    assert client.get("/admin/household/nope").status_code == 404


def test_the_assistant_keeps_household_things(client, monkeypatch):
    import asyncio
    from datetime import datetime

    from app.voice import brain

    hh = _household(client, "memories", "todo", "shopping", "reminders", "calendar", "messages", "chores")
    marika = client.app.state.ctx.voice.voiceprints.add_person("Marika")["id"]
    client.app.state.ctx.voice.voiceprints.add_person("Jules")
    seen = []
    answers = iter([
        'Got it. [[memory {"remember": "my sister is Anna", "for": "me"}]] [[shopping {"add": ["milk", "2 lemons"]}]]',
        '[[todo {"add": "call the plumber", "due": "2026-11-05"}]] [[reminder {"text": "bins", "at": "19:00", '
        '"repeat": "weekly"}]] [[calendar {"add": "Dentist", "start": "2026-11-06 14:30", "end": "15:30", '
        '"for": "me"}]] [[message {"to": "jules", "text": "dinner is ready"}]] Done, all of it.',
    ])

    def post(url, body, headers, timeout):
        seen.append(body)
        return {"choices": [{"message": {"content": next(answers)}}]}

    monkeypatch.setattr(brain, "post", post)
    conv = brain.Conversations()
    now = datetime(2026, 11, 3, 9)
    kw = dict(device_id="d", speaker="Kitchen", character="Janet", now=now, conversations=conv,
              settings={"assistant_url": "http://llm/v1"}, household=hh, person=marika)
    for heard in ("remember my sister is Anna, and we need milk and two lemons", "and the rest"):
        r = brain.ask(heard, **kw)
        for module, body in r.household:
            asyncio.run(hh.apply(module, body, "d", marika))
    assert r == "Done, all of it." and not r.household_problems
    assert "[[shopping" in seen[0]["messages"][0]["content"]  # the instructions
    assert [i["data"]["text"] for i in hh.items("shopping")] == ["milk", "2 lemons"]
    memory = hh.items("memories")[0]
    assert memory["owner"] == marika and memory["data"]["text"] == "my sister is Anna"
    assert hh.items("todo")[0]["data"]["due"] == "2026-11-05"
    reminder = hh.items("reminders")[0]["data"]
    assert reminder["repeat"] == "weekly" and reminder["due"].endswith("T19:00") and reminder["speaker"] == "d"
    event = hh.items("calendar")[0]
    assert event["owner"] == marika and event["data"]["end"] == "2026-11-06T15:30"
    assert hh.items("messages")[0]["data"] == {"to": "Jules", "from": "Marika", "text": "dinner is ready",
                                               "delivered_at": None}

    # what the assistant is told: hers, the household's, but not someone else's
    notes = hh.notes("d", marika, now)
    assert "Remembered for Marika: my sister is Anna." in notes["memories"]
    assert "milk, 2 lemons" in notes["shopping"] and "call the plumber (due Thursday)" in notes["todo"]
    assert "Dentist" not in hh.notes("d", "", now)["calendar"]  # hers
    assert "Dentist at" not in notes["calendar"]  # Nov 6 isn't today or tomorrow
    assert "Messages waiting for: Jules" in notes["messages"]
    jules = hh.people()[1]["id"]
    assert 'from Marika: "dinner is ready"' in hh.notes("d", jules, now)["messages"]
    assert hh.items("messages")[0]["data"]["delivered_at"]  # passed on

    # in the admin pages
    page = client.get("/admin/api/household/shopping").json()
    assert [i["view"]["text"] for i in page["items"]] == ["milk", "2 lemons"]
    milk = page["items"][0]["id"]
    page = client.patch(f"/admin/api/household/shopping/{milk}", json={"data": {"got": True}}, headers=ADMIN).json()
    assert page["items"][-1]["data"]["got"] is True  # got it: at the bottom
    assert "already got: milk" in hh.notes("d", "", now)["shopping"]
    r = client.post("/admin/api/household/chores", headers=ADMIN,
                    json={"data": {"name": "Dishes", "people": "Marika, Jules", "every": "daily"}})
    assert r.status_code == 201 and r.json()["items"][0]["view"]["turn"] == "Marika"
    asyncio.run(hh.apply("chores", {"done": "dishes", "by": "Marika"}, "d", marika))
    assert "Dishes: Jules's turn" in hh.notes("d", "", now)["chores"]
    assert client.post("/admin/api/household/reminders", json={"data": {"text": "x", "due": "whenever"}},
                       headers=ADMIN).status_code == 422
    # markers for modules that are off do nothing
    _household(client, "memories")
    text, actions, problems = hh.split('Sure. [[shopping {"add": "eggs"}]]')
    assert text == "Sure." and actions == [] and problems == ["shopping is turned off"]


def test_household_lookups_take_a_second_request(client, monkeypatch):
    from datetime import datetime

    from app import household

    from app.voice import brain

    hh = _household(client, "news", "calendar", "briefing")
    hh.save_settings(hh.settings().model_copy(update={"news_feeds": [household.Link(name="CBC",
                                                                                    url="https://example.com/rss")]}))
    rss = b"""<?xml version="1.0"?><rss><channel><item><title>Oilers win in overtime</title>
      <description>&lt;p&gt;A late goal.&lt;/p&gt;</description></item>
      <item><title>Snow on the way</title></item></channel></rss>"""
    monkeypatch.setattr("app.household.feeds.fetch", lambda url: rss)
    hh.add("calendar", {"title": "Hockey", "start": "2026-11-10 19:00"})
    hh.add("calendar", {"title": "Mum's birthday", "start": "1960-11-12", "repeat": "yearly"})
    seen = []
    answers = iter(["[[news]]", "The Oilers won in overtime.", '[[calendar {"find": "2026-11-10", "to": "2026-11-12"}]]',
                    "Hockey on Tuesday, and your mum's birthday on Thursday."])

    def post(url, body, headers, timeout):
        seen.append(body["messages"])
        return {"choices": [{"message": {"content": next(answers)}}]}

    monkeypatch.setattr(brain, "post", post)
    conv = brain.Conversations()
    kw = dict(device_id="d", speaker="Kitchen", character="Janet", now=datetime(2026, 11, 3, 9), conversations=conv,
              settings={"assistant_url": "http://llm/v1"}, household=hh)
    assert brain.ask("what's in the news?", **kw) == "The Oilers won in overtime."
    found = seen[1][-1]["content"]
    assert "[CBC] Oilers win in overtime - A late goal." in found and "Snow on the way" in found
    assert seen[1][-2] == {"role": "assistant", "content": "[[news]]"}
    r = brain.ask("what's on next week?", **kw)
    assert r.startswith("Hockey") and not r.household
    found = seen[3][-1]["content"]
    assert "19:00: Hockey" in found and "Mum's birthday" in found
    assert "Morning briefing" in hh.briefing("", "", datetime(2026, 11, 10, 7))
    assert "Hockey" in hh.briefing("", "", datetime(2026, 11, 10, 7))


def test_ics_subscriptions_and_events(client, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import household

    hh = _household(client, "calendar")
    ics = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Swimming\r\n\
DTSTART:20261102T230000Z\r\nDTEND:20261103T000000Z\r\nRRULE:FREQ=WEEKLY\r\nLOCATION:Pool\r\nEND:VEVENT\r\n\
END:VCALENDAR\r\n"""
    monkeypatch.setattr("app.household.feeds.fetch", lambda url: ics)
    hh.save_settings(hh.settings().model_copy(update={"calendars": [household.Link(
        name="Family", url="webcal://example.com/family.ics")]}))
    tz = hh.tz()
    events = hh.events(datetime(2026, 11, 1), datetime(2026, 11, 20))
    assert [e["title"] for e in events] == ["Swimming"] * 3 and events[0]["where"] == "Pool"
    assert events[0]["start"] == datetime(2026, 11, 2, 23).replace(tzinfo=ZoneInfo("UTC")).astimezone(tz) \
        .replace(tzinfo=None)
    upcoming = client.get("/admin/api/household/calendar").json()["upcoming"]
    assert upcoming and upcoming[0]["calendar"] == "Family"


def test_reminders_ring_on_time(client):
    from datetime import datetime, timedelta

    token = adopt(client)
    hh = _household(client, "reminders", "chores")
    now = hh.now()
    hh.add("reminders", {"text": "take the pills", "due": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                         "speaker": MAC, "alarm": True, "repeat": "daily"})
    hh.add("reminders", {"text": "later", "due": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")})
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        client.portal.call(hh.tick)
        card = ws.receive_json()
        assert card == {"type": "notify", "title": "Reminder", "text": "take the pills", "seconds": 20}
        ring = ws.receive_json()
        assert ring["type"] == "timer" and ring["seconds"] == 1 and ring["label"] == "take the pills"
    pills, later = sorted(hh.items("reminders"), key=lambda i: i["data"]["text"])[::-1]
    assert pills["data"]["due"] > now.strftime("%Y-%m-%dT%H:%M") and not pills["data"]["done"]  # tomorrow
    assert not later["data"]["fired_at"]


def test_agents_use_the_household_modules_through_mcp(client):
    token = mcp_token(client)
    off = mcp_tool(client, token, "household_change", module="shopping", action={"add": ["eggs"]})
    assert "turned off" in off["problem"]
    _household(client, "shopping", "calendar")
    client.app.state.ctx.voice.voiceprints.add_person("Marika")
    assert mcp_tool(client, token, "household_change", module="shopping", action={"add": ["eggs", "flour"]}) ==         {"done": "added 2 to the shopping list"}
    items = mcp_tool(client, token, "household_items", module="shopping")["items"]
    assert [i["text"] for i in items] == ["eggs", "flour"]
    done = mcp_tool(client, token, "household_change", module="calendar", person="Marika",
                    action={"add": "Yoga", "start": "2026-11-05 18:00", "for": "me"})
    assert done["done"].startswith("added event Yoga")
    events = mcp_tool(client, token, "calendar_events", start="2026-11-05", person="Marika")["events"]
    assert events[0]["title"] == "Yoga" and events[0]["for"] == "Marika"
    assert mcp_tool(client, token, "calendar_events", start="2026-11-05")["events"] == []  # hers


# broadcast (broadcast.py)

def adopt_another(c, mac, name):
    req = request_adoption(c, mac)
    r = c.post(f"/admin/api/adoptions/{req['request_id']}/approve", json={"code": req["code"], "name": name},
               headers=ADMIN)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v1/adoption/{req['request_id']}").json()["token"]


def test_broadcast_requests_are_recognized(client):
    from app import broadcast

    adopt(client)
    login(client)
    client.post("/admin/api/groups", json={"name": "Bedrooms"}, headers=ADMIN)
    db = client.app.state.ctx.db
    assert broadcast.parse("Broadcast that dinner's ready.", db) == {"to": "all", "message": "Dinner's ready",
                                                                     "skip": 2}
    assert broadcast.parse("Okay, announce to the bedrooms that it's bedtime", db) == {
        "to": "bedrooms", "message": "It's bedtime", "skip": 6}
    assert broadcast.parse("broadcast to the kitchen speaker come down please", db)["to"] == MAC
    assert broadcast.parse("Broadcast to everyone, the car's here", db)["message"] == "The car's here"
    assert broadcast.parse("Broadcast.", db) == {"to": "all", "message": "", "skip": 1}
    assert broadcast.parse("what's a broadcast?", db) is None
    words = [(" Broadcast", 0.1, 0.6), (" that", 0.6, 0.8), (" dinner's", 1.0, 1.4), (" ready.", 1.4, 1.9)]
    assert broadcast.message_span(words, 2, "Dinner's ready") == (1.0 - broadcast.LEAD_S, 1.9 + broadcast.TAIL_S)


def test_broadcast_plays_the_recording_on_the_other_speakers(client):
    from app import broadcast

    kitchen = adopt(client)
    office = adopt_another(client, "90:70:69:F4:66:B1", "Office")
    got = fake_voice(client, heard="Broadcast that dinner's ready")
    voice = client.app.state.ctx.voice
    voice.stt.words = lambda pcm, **kw: [(" Broadcast", 0.0, 0.5), (" that", 0.5, 0.7), (" dinner's", 1.0, 1.3),
                                         (" ready", 1.3, 1.5)]
    spoken = np.full(16000 * 2, 3000, dtype="<i2").tobytes()  # 2 s, quiet
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(kitchen)) as k, \
            client.websocket_connect("/api/v1/device/ws", headers=bearer(office)) as o:
        k.receive_json()
        o.receive_json()
        k.send_json({"type": "voice_start", "session": "b1"})
        k.send_bytes(spoken)
        k.send_json({"type": "voice_end", "session": "b1"})
        assert o.receive_json() == {"type": "notify", "title": "From Kitchen", "text": "Dinner's ready", "seconds": 20}
        start, audio = receive_speech(o)
        assert start["text"] == "Announcement from Kitchen. Dinner's ready"
        intro = 8000  # the fake voice: 0.25 s
        clip = audio[intro + len(broadcast.GAP):]
        span = (1.0 - broadcast.LEAD_S, 1.5 + broadcast.TAIL_S)
        assert len(clip) == (int(span[1] * 16000) - int(span[0] * 16000)) * 2  # from the message on
        assert np.frombuffer(clip, "<i2").max() == 18000  # brought up (6x at most)
        result = k.receive_json()
        assert result["reply"] == "Sent to the Office." and result["heard"] == "Broadcast that dinner's ready"
        receive_speech(k)

        # "Broadcast" alone: it asks, and sends the next thing heard, whole
        got_heard = iter(["Broadcast to the office", "Come and help me carry the shopping"])
        voice.stt.transcribe = lambda pcm, **kw: next(got_heard)
        k.send_json({"type": "voice_start", "session": "b2"})
        k.send_bytes(spoken)
        k.send_json({"type": "voice_end", "session": "b2"})
        assert k.receive_json()["reply"] == "What's the message?"
        start, _ = receive_speech(k)
        assert start["listen"] and start["follow_up"]
        k.send_json({"type": "voice_start", "session": "b3", "follow_up": True})
        k.send_bytes(spoken)
        k.send_json({"type": "voice_end", "session": "b3"})
        assert o.receive_json()["text"] == "Come and help me carry the shopping"
        _, audio = receive_speech(o)
        assert len(audio) == intro + len(broadcast.GAP) + len(spoken)
    history = client.get("/admin/api/broadcasts").json()["history"]
    assert [(b["text"], b["heard_on"], b["voice"]) for b in history] == [
        ("Come and help me carry the shopping", ["Office"], True), ("Dinner's ready", ["Office"], True)]
    assert "Broadcast" in client.get("/admin/broadcast").text


def test_broadcast_from_the_admin_page(client):
    token = adopt(client)
    got = fake_voice(client)
    with client.websocket_connect("/api/v1/device/ws", headers=bearer(token)) as ws:
        ws.receive_json()
        r = client.post("/admin/api/broadcasts", json={"text": "Lunch!", "to": "home", "sender": "Mum"},
                        headers=ADMIN)
        assert r.json()["sent"]["heard_on"] == ["Kitchen"]
        assert ws.receive_json()["title"] == "From Mum"
        start, _ = receive_speech(ws)
    assert got["tts"][0] == "Announcement from Mum. Lunch!"
    assert client.post("/admin/api/broadcasts", json={"text": "hi", "to": "garage"}, headers=ADMIN).status_code == 422
    assert client.post("/admin/api/broadcasts", json={"text": "", "to": "all"}, headers=ADMIN).status_code == 422

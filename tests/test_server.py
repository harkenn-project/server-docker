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
                        update_repo="harkenn/harkenn-speakerfirmware")
    with TestClient(create_app(settings)) as c:
        yield c


def fake_gitlab(c, version="0.5.0", calls=None, sha=None):
    import hashlib
    updates = c.app.state.ctx.updates
    calls = calls if calls is not None else []
    data = fake_firmware(version)

    def manifest():
        calls.append("manifest")
        return {"version": version, "file": f"v{version}/firmware.bin",
                "sha256": sha or hashlib.sha256(data).hexdigest()}

    updates.fetch_manifest = manifest
    updates.fetch_file = lambda path: calls.append(path) or data
    return calls

def test_boot_update_check_offers_newer_release(git_client):
    c = git_client
    token = adopt(c)  # adopted with firmware 0.0.1, hardware "test"
    calls = fake_gitlab(c)
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
    calls = fake_gitlab(c, version="0.0.1")  # same as the speaker
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

    def broken():
        raise OSError("gitlab unreachable")

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
    fake_gitlab(c, sha="0" * 64)
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
    return got


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
    assert s.update_repo == "harkenn/harkenn-speakerfirmware" and s.update_branch == "releases"
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
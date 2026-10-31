"""Pretend to be a speaker, for testing the server without firmware.

Waits for the server, gets adopted (it prints the 3-digit code), saves the token, then stays connected over the
WebSocket. It sends status every 10 s, a few log lines, and answers pings and commands.

    python tools/simulate_device.py --url http://localhost:8000
    python tools/simulate_device.py --url https://kotodama.mylife.ninja --id 02aabbccddee
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx
import websockets

FIRMWARE = "sim-0.1"


async def wait_for_server(client: httpx.AsyncClient, url: str) -> None:
    while True:
        try:
            r = await client.get(f"{url}/api/v1/ping")
            if r.status_code == 200:
                print(f"server is up: {r.json()}")
                return
            print(f"ping returned {r.status_code}")
        except httpx.HTTPError as e:
            print(f"server not reachable ({e.__class__.__name__}), retrying")
        await asyncio.sleep(3)


async def adopt(client: httpx.AsyncClient, url: str, device_id: str) -> str:
    while True:
        r = await client.post(f"{url}/api/v1/adoption/request",
                              json={"device_id": device_id, "hardware": "simulator", "firmware": FIRMWARE})
        r.raise_for_status()
        req = r.json()
        print(f"\n    *** adoption code: {req['code']} ***   (expires in {req['expires_in']} s)\n")
        while True:
            await asyncio.sleep(req["poll_interval_s"])
            r = await client.get(f"{url}/api/v1/adoption/{req['request_id']}")
            state = r.json().get("state") if r.status_code == 200 else "expired"
            if state == "approved":
                print("adopted")
                return r.json()["token"]
            if state != "pending":
                print(f"adoption {state}, asking again")
                break


async def run_connected(url: str, token: str, device_id: str) -> bool:
    """Returns False if the token was rejected."""
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/api/v1/device/ws"
    started = time.time()
    state = {"state": "idle", "volume": 70, "config_version": None}
    try:
        async with websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
            print("connected")

            async def reporter() -> None:
                while True:
                    await ws.send(json.dumps({"type": "status", "status": {
                        "state": state["state"], "firmware": FIRMWARE, "ip": "10.1.216.200", "ssid": "Simulated",
                        "rssi": random.randint(-70, -50), "uptime_s": int(time.time() - started),
                        "free_heap": 180_000, "free_psram": 6_000_000, "battery": 95, "charging": True,
                        "volume": state["volume"], "config_version": state["config_version"]}}))
                    await ws.send(json.dumps({"type": "log", "lines": [
                        {"ts": time.time(), "level": "I", "tag": "sim", "message": f"heartbeat, state={state['state']}"}]}))
                    await asyncio.sleep(10)

            task = asyncio.create_task(reporter())
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                    elif msg["type"] == "config":
                        state["config_version"] = msg["version"]
                        state["volume"] = msg["config"]["audio"]["volume"]
                        print(f"config v{msg['version']}: name={msg['device']['name']} layout={msg['layout']['id']} "
                              f"theme={msg['config']['display']['theme']} character={msg['character']['id']} "
                              f"wifi={[n['ssid'] for n in msg['config']['wifi']]}")
                    elif msg["type"] == "command":
                        print(f"command: {msg['command']} {msg['args']}")
                        await ws.send(json.dumps({"type": "event", "name": "command", "data": msg}))
                        if msg["command"] == "reboot":
                            return True
            finally:
                task.cancel()
    except websockets.exceptions.InvalidStatus as e:
        print(f"websocket refused: HTTP {e.response.status_code}")
        return e.response.status_code not in (401, 403)
    except websockets.exceptions.ConnectionClosed as e:
        print(f"connection closed: {e.rcvd.code if e.rcvd else '?'} {e.rcvd.reason if e.rcvd else ''}")
        return not (e.rcvd and e.rcvd.code == 4401)
    except OSError as e:
        print(f"connection failed: {e}")
    return True


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--id", default="02" + "".join(random.choices("0123456789abcdef", k=10)),
                    help="device id (MAC); a random locally-administered one by default")
    ap.add_argument("--token-file", type=Path, help="where to keep the token (default: sim-<id>.token)")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # so the adoption code shows up even when output is redirected
    url = args.url.rstrip("/")
    token_file = args.token_file or Path(f"sim-{args.id}.token")
    print(f"simulated speaker {args.id} -> {url}")

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            await wait_for_server(client, url)
            token = token_file.read_text().strip() if token_file.exists() else ""
            if not token:
                token = await adopt(client, url, args.id)
                token_file.write_text(token)
            if not await run_connected(url, token, args.id):
                print("token rejected; forgetting it and asking for adoption again")
                token_file.unlink(missing_ok=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

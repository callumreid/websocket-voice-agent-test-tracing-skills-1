"""Replay a 16 kHz PCM question through the deployed bridge with real playback pacing."""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect


async def main():
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8877/ws"
    sim = "local_smoke_7942_" + str(int(time.time()))
    token = (
        (Path.home() / ".local/state/coval-rime-7942/bridge-token").read_text().strip()
    )
    audio = Path("/tmp/rime-question.pcm").read_bytes()
    output = bytearray()
    greeting_played = asyncio.Event()
    playback = asyncio.Queue()
    generation = 0
    clear_count = 0
    async with connect(
        endpoint, additional_headers={"Authorization": "Bearer " + token}
    ) as ws:
        await ws.send(json.dumps({"simulation_id": sim}))

        async def player():
            while True:
                gen, event = await playback.get()
                if gen != generation:
                    continue
                if event["type"] == "audio_chunk":
                    chunk = base64.b64decode(event["data"])
                    output.extend(chunk)
                    await asyncio.sleep(len(chunk) / 48000)
                else:
                    await ws.send(json.dumps(event))

        async def receive():
            nonlocal generation, clear_count
            async for raw in ws:
                e = json.loads(raw)
                if e["type"] == "audio_chunk":
                    await playback.put((generation, e))
                    greeting_played.set()
                elif e["type"] == "mark":
                    await playback.put((generation, e))
                elif e["type"] == "clear":
                    generation += 1
                    clear_count += 1
                elif e["type"] == "error":
                    print(e)

        tasks = [asyncio.create_task(player()), asyncio.create_task(receive())]
        await asyncio.wait_for(greeting_played.wait(), 20)
        await asyncio.sleep(7)
        for i in range(0, len(audio), 1280):
            await ws.send(
                json.dumps(
                    {
                        "type": "audio_chunk",
                        "data": base64.b64encode(audio[i : i + 1280]).decode(),
                    }
                )
            )
            await asyncio.sleep(0.04)
        await asyncio.sleep(18)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(1)
    base = (
        endpoint.replace("wss://", "https://")
        .replace("ws://", "http://")
        .removesuffix("/ws")
    )
    async with httpx.AsyncClient(timeout=25) as client:
        data = (
            await client.get(
                base + "/sessions/" + sim, headers={"Authorization": "Bearer " + token}
            )
        ).json()
    target = Path.home() / ".local/state/coval-rime-7942"
    (target / (sim + ".json")).write_text(json.dumps(data, indent=2))
    import wave

    with wave.open(str(target / (sim + ".wav")), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(output)
    print(
        json.dumps(
            {
                "simulation_id": sim,
                "audio_seconds": len(output) / 48000,
                "turns": data.get("turns"),
                "tools": data.get("tools"),
                "errors": data.get("errors"),
                "playback_acks": data.get("playback_acks"),
                "clears": clear_count,
            },
            indent=2,
        )
    )


asyncio.run(main())

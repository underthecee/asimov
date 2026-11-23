"""A plain TCP relay on the pod: LISTEN_PORT -> 127.0.0.1:TARGET_PORT. It is the direct-TCP fallback for the live
world when the RunPod HTTP proxy fails (L42): the world keeps its proxied port, and the relay's port is the one
exposed as tcp (`runpod_pod.py up --port 8765 --tcp-port 8766`). Run it as a podjob beside the world:

    python3 scripts/tcp_relay.py 8766 8765
"""
import asyncio
import sys


async def pipe(r, w):
    try:
        while data := await r.read(1 << 16):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def handle(cr, cw, target):
    try:
        tr, tw = await asyncio.open_connection("127.0.0.1", target)
    except OSError:
        cw.close()
        return
    await asyncio.gather(pipe(cr, tw), pipe(tr, cw))


async def main(listen, target):
    server = await asyncio.start_server(lambda r, w: handle(r, w, target), "0.0.0.0", listen)
    print(f"relay 0.0.0.0:{listen} -> 127.0.0.1:{target}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))

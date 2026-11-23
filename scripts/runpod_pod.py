"""Start, check and terminate a RunPod GPU pod that serves a model with vLLM.

    python scripts/runpod_pod.py up --model Qwen/Qwen3-4B-Instruct-2507
    python scripts/runpod_pod.py wait POD_ID      # blocks until /v1/models answers
    python scripts/runpod_pod.py list
    python scripts/runpod_pod.py ports POD_ID     # public IP and direct TCP port mappings (up --tcp-port)
    python scripts/runpod_pod.py down POD_ID      # terminate (stops billing)
    python scripts/runpod_pod.py down --all

Needs RUNPOD_API_KEY. The endpoint is https://POD_ID-8000.proxy.runpod.net and is protected
by a per-pod vLLM API key, written to runs/pods/POD_ID.json (gitignored).
"""
import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

API = "https://api.runpod.io/v2"  # REST v1 (rest.runpod.io/v1) was retired 2026-11-15
IMAGE = "vllm/vllm-openai:v0.11.0"
NAME_PREFIX = "asiimov-"  # every pod this script starts; `down --all` touches only these
GPU_TYPES = ["NVIDIA H100 80GB HBM3", "NVIDIA H100 PCIe", "NVIDIA H100 NVL", "NVIDIA A100 80GB PCIe",
             "NVIDIA A100-SXM4-80GB"]
POD_DIR = os.path.join(os.path.dirname(__file__), "..", "runs", "pods")
AGENT_PORT = 8001  # scripts/pod_agent.py: runs jobs on the pod itself (scripts/podjob.py)


class RunPodError(SystemExit):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


def call(method: str, path: str, body: dict | None = None) -> dict | list | None:
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ['RUNPOD_API_KEY']}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "asiimov-runpod/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raise RunPodError(f"RunPod {method} {path} -> {e.code}: {e.read()[:800].decode(errors='replace')}", e.code)


# In-pod dead man's switch: the pod terminates itself after --ttl-hours even if the operator
# (this script's caller) crashes. RunPod injects RUNPOD_POD_ID and a pod-scoped RUNPOD_API_KEY.
# That key gets 403 from the REST API, but GraphQL podTerminate on its own pod works (tested
# 2026-11-03: a pod running this terminated itself). Retries every 5 min until the pod is gone.
TTL_SCRIPT = """import json, os, urllib.request as u
q = 'mutation { podTerminate(input: {podId: "%s"}) }' % os.environ["RUNPOD_POD_ID"]
k, pid = os.environ["RUNPOD_API_KEY"], os.environ["RUNPOD_POD_ID"]
try:
    u.urlopen(u.Request("https://api.runpod.io/graphql?api_key=" + k, data=json.dumps({"query": q}).encode(),
                        headers={"Content-Type": "application/json", "User-Agent": "asiimov-ttl"}), timeout=60)
except Exception:
    pass
try:  # REST v2 as a second route, in case GraphQL goes the way of REST v1
    u.urlopen(u.Request("https://api.runpod.io/v2/pods/" + pid, method="DELETE",
                        headers={"Authorization": "Bearer " + k, "User-Agent": "asiimov-ttl"}), timeout=60)
except Exception:
    pass
"""


def ttl_cmd(expires: int) -> str:
    """Shell prefix for dockerStartCmd: background loop that terminates the pod at epoch `expires`.
    An absolute deadline, so a container restart doesn't reset the clock; past it, it fires at once."""
    b64 = base64.b64encode(TTL_SCRIPT.encode()).decode()  # base64: no shell or JSON quoting to get wrong
    return f"(sleep $(({expires} - $(date +%s))); while true; do echo {b64} | base64 -d | python3; sleep 300; done) & "


HF_CACHE = "/root/.cache/huggingface"


def stall_cmd(stall_s: int, every: int = 30) -> str:
    """Shell prefix for dockerStartCmd: the download watchdog (2026-11-17: a 32B shard sat at 29.7 of ~33 GB for
    16+ minutes and vLLM never served, ~$4.75 lost). While vLLM doesn't answer /health, a weight file is still
    `.incomplete` and the cache's allocated bytes (du -B1, not the apparent size: hf_transfer writes chunks out of
    order, so the apparent size jumps early and then holds while holes fill) haven't grown for `stall_s` seconds, the pod terminates itself through the TTL
    script. Loading into the GPU (no .incomplete left) never trips it; once /health answers it stops."""
    b64 = base64.b64encode(TTL_SCRIPT.encode()).decode()
    return (f"(last=-1; still=0; while true; do sleep {every}; "
            # python3, not curl: the vLLM image is only known to carry python3
            f"python3 -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=5)\" "
            f"2>/dev/null && break; "
            f"inc=$(find {HF_CACHE} -name '*.incomplete' 2>/dev/null | head -1); "
            f"sz=$(du -sB1 {HF_CACHE} 2>/dev/null | cut -f1); "
            f'if [ -n "$inc" ] && [ "$sz" = "$last" ]; then still=$((still+{every})); else still=0; fi; last=$sz; '
            f'if [ $still -ge {stall_s} ]; then echo "asiimov: weight download stalled $still s at $sz bytes, terminating"; '
            f"while true; do echo {b64} | base64 -d | python3; sleep 60; done; fi; done) & ")


def agent_cmd(key: str, model: str) -> str:
    """Shell prefix for dockerStartCmd: the job runner on AGENT_PORT, restarted if it ever exits.
    Jobs live on the container disk under /jobs and outlive the runner itself. It shares vLLM's key, so that
    key now means a shell on the pod, not only model calls: keep runs/pods/ private."""
    with open(os.path.join(os.path.dirname(__file__), "pod_agent.py"), "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return (f"echo {b64} | base64 -d > /pod_agent.py; (while true; do ASIIMOV_JOBS=/jobs python3 /pod_agent.py "
            f"{AGENT_PORT} {key} {model}; sleep 5; done) & ")


def up(args):
    clash = set(args.port) & set(getattr(args, "tcp_port", []))
    if clash:  # L42 (aipt4, 2026-11-22): 8765/http with 8765/tcp left neither route reachable for 10 minutes
        sys.exit(f"port {sorted(clash)} is exposed as both http and tcp; give the tcp fallback its own port "
                 "(e.g. --tcp-port 8766, with scripts/tcp_relay.py 8766 8765 on the pod)")
    key = secrets.token_urlsafe(24)
    expires = int(time.time() + args.ttl_hours * 3600)
    serve = (f"exec vllm serve {args.model} --host 0.0.0.0 --port 8000 --api-key {key} "
             f"--max-model-len {args.max_len} --gpu-memory-utilization 0.92 --enable-prefix-caching "
             f"--served-model-name {args.model} {args.vllm_args}").rstrip()
    cmd = ttl_cmd(expires) + (stall_cmd(args.stall_s) if args.stall_s else "") + agent_cmd(key, args.model) + serve
    body = {
        "name": f"{NAME_PREFIX}{args.name or args.model.split('/')[-1].lower()}"[:60],
        "image": IMAGE,
        "cloud": args.cloud,
        "disk": 80,
        "ports": ["8000/http", f"{AGENT_PORT}/http"] + [f"{p}/http" for p in args.port] + [f"{p}/tcp" for p in getattr(args, "tcp_port", [])],
        "entrypoint": ["bash", "-c"],
        "cmd": [cmd],
        "env": {"HF_HUB_ENABLE_HF_TRANSFER": "1"},
    }
    # v2 takes one GPU type per create, so the old priority list becomes a loop: the next type on any refusal
    # (no capacity), and the last refusal ends it.
    pod, tried = None, []
    for gpu in args.gpu or GPU_TYPES:
        try:
            pod = call("POST", "/pods", {**body, "gpu": {"id": gpu, "count": 1}})
            break
        except RunPodError as e:
            if e.code in (401, 403):
                raise
            tried.append(str(e)[:200])
            print(f"{gpu}: {str(e)[:200]}", file=sys.stderr)
    if pod is None:
        sys.exit("no GPU type could be placed:\n" + "\n".join(tried))
    os.makedirs(POD_DIR, exist_ok=True)
    rec = {"id": pod["id"], "model": args.model, "vllm_args": args.vllm_args, "api_key": key, "created": time.time(),
           "url": f"https://{pod['id']}-8000.proxy.runpod.net",
           "jobs_url": f"https://{pod['id']}-{AGENT_PORT}.proxy.runpod.net", "cost_per_hr": pod.get("cost"),
           "gpu": (pod.get("gpu") or {}).get("id"),
           "expires": expires}
    with open(os.path.join(POD_DIR, f"{pod['id']}.json"), "w") as f:
        json.dump(rec, f, indent=2)
    print(json.dumps({k: v for k, v in rec.items() if k != "api_key"}, indent=2))


def wait(args):
    """Blocks until vLLM answers. Ends early when the pod is gone (its stall watchdog or TTL fired), and on a
    timeout it terminates the pod unless --keep: a pod that never served has no purpose left (joon, 2026-11-18)."""
    rec = json.load(open(os.path.join(POD_DIR, f"{args.pod}.json")))
    t0, n = time.time(), 0
    while time.time() - t0 < args.timeout:
        try:
            req = urllib.request.Request(rec["url"] + "/v1/models",
                                         headers={"Authorization": f"Bearer {rec['api_key']}",
                                                  "User-Agent": "asiimov-runpod/0.1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"ready after {time.time() - t0:.0f}s:", r.read()[:300].decode())
                return
        except Exception as e:  # noqa: BLE001 - startup produces 404/502/timeouts until vLLM is up
            print(f"{time.time() - t0:5.0f}s not ready: {str(e)[:80]}", flush=True)
        n += 1
        if n % 4 == 0 and not pod_alive(args.pod):
            sys.exit(f"pod {args.pod} is gone (stall watchdog or TTL): it never served")
        time.sleep(15)
    if args.keep:
        sys.exit("timed out waiting for the server; the pod is still billing (--keep)")
    down(argparse.Namespace(all=False, pod=args.pod))
    sys.exit("timed out waiting for the server; terminated the pod")


def ports(args):
    """The pod's direct TCP mappings, the fallback when the HTTP proxy fails (Steering 2026-11-20 23:21Z): a port
    exposed with `up --tcp-port P` is reached at ws://IP:PUBLIC/ with no proxy and no TLS. REST v2 lists them under
    runtime.ports as {ip, private, public, type} once the pod runs (seen 2026-11-22)."""
    pod = call("GET", f"/pods/{args.pod}") or {}
    maps = [m for m in ((pod.get("runtime") or {}).get("ports") or []) if m.get("type") == "tcp"]
    for m in maps:
        print(f"{m.get('private')}/tcp -> ws://{m.get('ip')}:{m.get('public')}/")
    if not maps:
        print("no tcp mapping yet (exposed: %s)" % ", ".join(pod.get("ports") or []))


def pod_alive(pid: str) -> bool:
    try:
        pod = call("GET", f"/pods/{pid}") or {}
    except RunPodError as e:
        return e.code != 404
    except Exception:  # noqa: BLE001 - an unreachable API says nothing about the pod
        return True
    return pod.get("status") not in ("TERMINATED", "EXITED", "ERROR")


def pods_list() -> list:
    """Every pod on the account; v2 returns {"pods": [...], "pagination": {...}} and takes no filters."""
    out, cursor = [], None
    while True:
        page = call("GET", "/pods" + (f"?cursor={cursor}" if cursor else "")) or {}
        if isinstance(page, list):  # a bare list, as v1 gave: the whole account in one page
            return out + page
        out += page.get("pods") or []
        cursor = (page.get("pagination") or {}).get("nextCursor")
        if not (page.get("pagination") or {}).get("hasNextPage") or not cursor:
            return out


def ls(_args):
    pods = pods_list()
    for p in pods:
        print(p["id"], p.get("name"), p.get("status"), p.get("cost"), p.get("image"))
    if not pods:
        print("no pods")


def down(args):
    # --all means every pod this script started (name prefix), never other pods on the account.
    if args.all:
        ids = [p["id"] for p in pods_list() if (p.get("name") or "").startswith(NAME_PREFIX)]
    elif args.pod:
        ids = [args.pod]
    else:
        sys.exit("give a POD_ID or --all")
    failed = []
    for pid in ids:
        try:  # keep going: one failed DELETE must not leave the remaining pods billing
            call("DELETE", f"/pods/{pid}")
            print("terminated", pid)
        except SystemExit as e:
            print(f"FAILED to terminate {pid}: {e}", file=sys.stderr)
            failed.append(pid)
    if failed:
        sys.exit(f"still running, terminate by hand: {' '.join(failed)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("up")
    p.add_argument("--model", required=True)
    p.add_argument("--gpu", nargs="*")
    p.add_argument("--cloud", default="SECURE", choices=["SECURE", "COMMUNITY"])
    p.add_argument("--max-len", type=int, default=8192)
    p.add_argument("--vllm-args", default="", help="extra vllm serve flags, e.g. '--async-scheduling'")
    p.add_argument("--name", default=None, help="pod name after the asiimov- prefix (default: the model)")
    p.add_argument("--port", type=int, action="append", default=[],
                   help="another HTTP port to expose at https://POD_ID-PORT.proxy.runpod.net, e.g. 8765 for asiimov.live")
    p.add_argument("--tcp-port", type=int, action="append", default=[],
                   help="also expose this port as direct TCP (no proxy): the fallback if the proxy fails; see `ports`")
    p.add_argument("--ttl-hours", type=float, default=6.0, help="pod terminates itself this long after launch; size it to the planned run")
    p.add_argument("--stall-s", type=int, default=180,
                   help="pod terminates itself when its weight download makes no progress this long (0: off)")
    p.set_defaults(fn=up)
    p = sub.add_parser("wait")
    p.add_argument("pod")
    # 45 min: a 32B download (~33 GB) and load took ~17 min on 2026-11-17; a stall is the watchdog's to catch
    p.add_argument("--timeout", type=int, default=2700)
    p.add_argument("--keep", action="store_true", help="on a timeout, leave the pod running")
    p.set_defaults(fn=wait)
    p = sub.add_parser("ports")
    p.add_argument("pod")
    p.set_defaults(fn=ports)
    sub.add_parser("list").set_defaults(fn=ls)
    p = sub.add_parser("down")
    p.add_argument("pod", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=down)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

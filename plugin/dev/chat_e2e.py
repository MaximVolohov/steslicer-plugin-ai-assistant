"""End-to-end test of the chat path: upload -> /api/chat -> agent loop -> SSE events.

    python chat_e2e.py [--base http://127.0.0.1:8765] [--model samples/bracket.stl]
                       [--text "Кронштейн под нагрузку, PETG, без поддержек"] [--configure-mock]

With --configure-mock the plugin is pointed at dev/mock_llm.py (start it first:
python mock_llm.py).  Prints every transcript item and tool result as it streams.
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: bypass system proxies


def request(base, method, path, body=None, raw=None, timeout=60):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    headers = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with OPENER.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "null")


def stream_events(base, last_id, stop, sink):
    req = urllib.request.Request(base + "/api/events?last=%d" % last_id)
    with OPENER.open(req, timeout=1800) as response:
        for raw in response:
            if stop.is_set():
                return
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("data: "):
                sink(json.loads(line[6:]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--model", default=os.path.join(HERE, "samples", "bracket.stl"))
    parser.add_argument("--text", default="Кронштейн под нагрузку, PETG, без поддержек. Подготовь к печати.")
    parser.add_argument("--configure-mock", action="store_true")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    if args.configure_mock:
        result = request(args.base, "POST", "/api/settings", {"api_base_url": "http://127.0.0.1:8001/v1",
                                                               "model": "mock-agent", "api_key": "test-key"})
        print("settings:", result["changed"])
        print("test connection:", request(args.base, "POST", "/api/settings/test", {}))
    request(args.base, "POST", "/api/reset", {})
    state = request(args.base, "GET", "/api/state")

    with open(args.model, "rb") as stream:
        upload = request(args.base, "POST", "/api/upload?name=" + urllib.request.quote(os.path.basename(args.model)),
                         raw=stream.read())
    print("uploaded:", upload)

    stop = threading.Event()
    finished = threading.Event()
    summary = {"tools": [], "errors": [], "final": None}

    def sink(event):
        data = event.get("data") or {}
        kind = event["type"]
        if kind == "item":
            if data["kind"] == "tool":
                print("  -> %s %s" % (data["name"], json.dumps(data.get("args"), ensure_ascii=False)[:120]))
            elif data["kind"] in ("assistant", "error", "info", "user"):
                print("[%s] %s" % (data["kind"], data.get("text", "")[:600]))
                if data["kind"] == "assistant" and data.get("final"):
                    summary["final"] = data["text"]
                if data["kind"] == "error":
                    summary["errors"].append(data["text"])
        elif kind == "item_update" and data.get("status") in ("ok", "error"):
            summary["tools"].append(data["status"])
            print("     <- %s (%s ms) %s" % (data["status"], data.get("duration_ms"), (data.get("result") or "")[:160].replace("\n", " ")))
        elif kind == "busy" and not data.get("busy"):
            finished.set()

    thread = threading.Thread(target=stream_events, args=(args.base, state["last_event_id"], stop, sink))
    thread.daemon = True
    thread.start()
    time.sleep(0.5)
    started = time.time()
    print(request(args.base, "POST", "/api/chat", {"text": args.text, "attachments": [upload["id"]]}))
    finished.wait(args.timeout)
    stop.set()
    took = time.time() - started
    errors = summary["tools"].count("error")
    print("\n%d tool calls (%d errors), %d chat errors, %.1f s" % (len(summary["tools"]), errors, len(summary["errors"]), took))
    ok = finished.is_set() and summary["final"] and not summary["errors"]
    print("RESULT:", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end smoke test of the plugin's MCP server against a running STE Slicer.

    python mcp_smoke.py [--url http://127.0.0.1:8765/mcp] [--only 3d|5d] [--out DIR] [--keep]

Runs a 3D scenario (L-bracket) and a 5D scenario (tube) through tools/call,
checks the exported G-code and 3MF, and restores the original material,
quality and printing mode at the end (unless --keep).
"""
import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")

REQUIRED_TOOLS = {
    "get_machine_info", "search_settings", "get_settings", "set_settings", "reset_settings", "set_printing_mode",
    "list_quality_profiles", "set_quality_profile", "list_materials", "set_material", "create_profile",
    "save_profile", "load_model", "list_objects", "get_object_info", "move_object", "rotate_object",
    "scale_object", "center_object", "arrange_all", "delete_object", "clear_scene", "start_slice",
    "get_slice_status", "get_slice_result", "export_gcode", "save_project_3mf", "analyze_model",
    "apply_orientation", "get_recommendations", "compare_variants", "get_scene_snapshot",
}


class Blocked(Exception):
    """The scenario cannot run on this machine (e.g. the 5D engine is not activated)."""


class Client(object):
    def __init__(self, url):
        self.url = url
        self.next_id = 0
        # The slicer listens on loopback: never route through a system (VPN) proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(self, method, params=None, timeout=1200):
        self.next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                        "Accept": "application/json, text/event-stream"})
        with self.opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if "error" in data:
            raise RuntimeError("%s: %s" % (method, data["error"]))
        return data["result"]

    def call(self, name, **arguments):
        started = time.time()
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        took = time.time() - started
        data = result.get("structuredContent")
        if data is None:
            text = "".join(p.get("text", "") for p in result["content"] if p["type"] == "text")
            data = json.loads(text) if text else {}
        status = "ERROR" if result.get("isError") else "ok"
        print("  %-22s %-5s %6.1fs  %s" % (name, status, took, short(data)))
        if result.get("isError"):
            raise AssertionError("%s failed: %s" % (name, data))
        return data, result


def short(data):
    text = json.dumps(data, ensure_ascii=False)
    return text if len(text) < 140 else text[:137] + "..."


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("    ✓ " + message)


def check_gcode(path, mode):
    check(os.path.isfile(path) and os.path.getsize(path) > 1000, "G-code exists: %s" % path)
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        head = [next(stream, "") for _ in range(60)]
    marker = [line for line in head if line.startswith(";PRINTING_MODE:")]
    check(bool(marker), "G-code has ;PRINTING_MODE header (%s)" % (marker[0].strip() if marker else "-"))
    if marker:
        check(mode in marker[0], "printing mode in G-code is %s" % mode)


def check_3mf(path):
    check(os.path.isfile(path), "3MF exists: %s" % path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    check("3D/3dmodel.model" in names, "3MF contains 3D/3dmodel.model")
    check(any(n.endswith(".global.cfg") or "machine" in n.lower() or n.endswith(".cfg") for n in names),
          "3MF contains project settings (%d entries)" % len(names))


def scenario_3d(client, out):
    print("\n== 3D scenario: L-bracket, PETG, strength, no supports")
    client.call("clear_scene")
    loaded, _ = client.call("load_model", path=os.path.join(SAMPLES, "bracket.stl"))
    object_id = loaded["loaded"][0]["id"]
    analysis, _ = client.call("analyze_model", object_id=object_id)
    check(analysis["recommendation"]["printing_mode"] == "3D", "analyzer recommends 3D for the bracket")
    client.call("set_printing_mode", mode="3D")
    materials, _ = client.call("list_materials", extruder=0, query="petg")
    check(materials["count"] > 0, "PETG materials are available")
    client.call("set_material", material_id=materials["materials"][0]["id"], extruder=0)
    advice, _ = client.call("get_recommendations", goal="strength", no_supports=True)
    client.call("set_quality_profile", quality_type=advice["recommended_quality"])
    applied, _ = client.call("set_settings", settings=advice["settings"])
    check(not applied.get("errors"), "recommended settings applied without errors")
    client.call("apply_orientation", object_id=object_id)
    info, _ = client.call("get_object_info", object_id=object_id)
    check(abs(info["position"]["z"]) < 0.01, "model stands on the build plate")
    _, raw = client.call("get_scene_snapshot", view="model")
    images = [p for p in raw["content"] if p["type"] == "image"]
    check(bool(images), "snapshot returned an image")
    with open(os.path.join(out, "snapshot_3d.png"), "wb") as stream:
        stream.write(base64.b64decode(images[0]["data"]))
    result, _ = client.call("start_slice")
    check(result["state"] == "done" and result["result"]["print_time_seconds"] > 0, "3D slice finished with a print time")
    gcode, _ = client.call("export_gcode", directory=out, filename="bracket_3d")
    check_gcode(gcode["path"], "classic")
    project, _ = client.call("save_project_3mf", directory=out, filename="bracket_3d")
    check_3mf(project["path"])
    comparison, _ = client.call("compare_variants", variants=[
        {"name": "20%", "settings": {"infill_sparse_density": 20}},
        {"name": "60%", "settings": {"infill_sparse_density": 60}}])
    times = dict((row["name"], row.get("print_time_seconds")) for row in comparison["variants"])
    check(times.get("20%") and times.get("60%") and times["20%"] < times["60%"], "denser infill prints longer: %s" % times)


def scenario_5d(client, out):
    print("\n== 5D scenario: tube lying on its side")
    machine, _ = client.call("get_machine_info")
    if not machine["machine"]["five_axis"]:
        print("  (active printer is not 5-axis, skipping)")
        return
    client.call("clear_scene")
    loaded, _ = client.call("load_model", path=os.path.join(SAMPLES, "tube_side.stl"))
    object_id = loaded["loaded"][0]["id"]
    analysis, _ = client.call("analyze_model", object_id=object_id)
    revolution = analysis.get("revolution") or {}
    check(revolution.get("symmetry_score", 0) > 0.8, "tube detected as a body of revolution (%.2f)" % revolution.get("symmetry_score", 0))
    check(analysis["recommendation"]["printing_mode"] == "5D", "analyzer recommends 5D for the tube")
    client.call("set_printing_mode", mode="5D")
    client.call("apply_orientation", object_id=object_id)
    info, _ = client.call("get_object_info", object_id=object_id)
    check(abs(info["position"]["x"]) < 0.05 and abs(info["position"]["y"]) < 0.05, "tube centred on the rotation axis")
    check(info["size"]["z"] > 39, "tube axis is vertical (height %.1f mm)" % info["size"]["z"])
    try:
        result, _ = client.call("start_slice", timeout_s=1200)
    except AssertionError as e:
        if "glicer" in str(e):
            raise Blocked("5D engine (glicer_console) is not activated on this machine — 5D slicing cannot run")
        raise
    check(result["state"] == "done", "5D slice finished")
    gcode, _ = client.call("export_gcode", directory=out, filename="tube_5d")
    check_gcode(gcode["path"], "cylindrical")
    project, _ = client.call("save_project_3mf", directory=out, filename="tube_5d")
    check_3mf(project["path"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--only", choices=["3d", "5d"])
    parser.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "ste_ai_smoke"))
    parser.add_argument("--keep", action="store_true", help="do not restore the original slicer state")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    client = Client(args.url)

    print("== MCP handshake")
    init = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "mcp_smoke", "version": "1"}})
    print("  server:", init["serverInfo"], "protocol:", init["protocolVersion"])
    tools = {t["name"] for t in client.rpc("tools/list")["tools"]}
    missing = REQUIRED_TOOLS - tools
    check(not missing, "all %d expected tools are listed (%d total)" % (len(REQUIRED_TOOLS), len(tools)))

    original, _ = client.call("get_machine_info")
    client.call("search_settings", query="заполнение", limit=5)
    client.call("get_settings", keys=["layer_height", "infill_sparse_density", "printing_mode"])
    client.call("list_quality_profiles")
    failures = 0
    blocked = []
    try:
        for name, scenario in (("3d", scenario_3d), ("5d", scenario_5d)):
            if args.only and args.only != name:
                continue
            try:
                scenario(client, args.out)
            except Blocked as e:
                blocked.append(name)
                print("  ! BLOCKED: %s" % e)
            except AssertionError as e:
                failures += 1
                print("  ✗ FAILED: %s" % e)
    finally:
        if not args.keep:
            print("\n== restoring the original state")
            client.call("clear_scene")
            client.call("reset_settings", keep_printing_mode=False)
            for extruder in original["extruders"]:
                if extruder.get("material"):
                    client.call("set_material", material_id=extruder["material"]["id"], extruder=extruder["index"])
            if original["quality"]["quality_type"]:
                client.call("set_quality_profile", quality_type=original["quality"]["quality_type"])
    print("\nOutputs in", args.out)
    if blocked:
        print("BLOCKED scenarios (environment):", ", ".join(blocked))
    print("RESULT:", "FAILED (%d scenario(s))" % failures if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

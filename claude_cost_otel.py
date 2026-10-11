#!/usr/bin/env python3
"""One-shot setup for local Claude Code cost telemetry.

Starts the observability stack (docker compose), loads the Perses dashboards and
turns on Claude Code's OpenTelemetry export, including OTEL_LOG_TOOL_DETAILS, which
is what puts real agent and MCP tool names on the cost and token counters.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPOSE_FILE = ROOT / "docker-compose.yml"
DASHBOARD_DIR = ROOT / "perses" / "provisioning" / "dashboards"
# CLAUDE_CODE_ENABLE_TELEMETRY is ignored in project/local settings, so user settings it is.
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

PERSES_URL = "http://localhost:8080"
SERVICES = {
    "Perses": "http://localhost:8080/api/v1/health",
    "Prometheus": "http://localhost:9090/-/ready",
    "Jaeger": "http://localhost:16686/",
    "OpenSearch": "http://localhost:9200/",
}
COLLECTOR_ADDR = ("localhost", 4317)


def telemetry_env(endpoint: str, interval_ms: int, tool_details: bool) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",  # traces (beta)
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_METRIC_EXPORT_INTERVAL": str(interval_ms),
    }
    if tool_details:
        env["OTEL_LOG_TOOL_DETAILS"] = "1"
    return env


# Every variable this tool may write; `disable` removes exactly these.
MANAGED_KEYS = set(telemetry_env("", 0, True))


# --- Claude Code settings ---------------------------------------------------

def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON ({exc}); refusing to touch it.")
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        sys.exit(f"{path} has an unexpected shape (expected an object with an 'env' object).")
    return data


def write_settings(path: Path, data: dict) -> Path | None:
    """Write settings atomically, keeping a timestamped backup of the previous file."""
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S-%f}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def apply_env(path: Path, wanted: dict[str, str] | None, dry_run: bool) -> bool:
    """Set (wanted given) or remove (wanted None) the managed env vars. Returns True if changed."""
    data = load_settings(path)
    env = data.setdefault("env", {})
    changes = []
    if wanted is None:
        for key in sorted(MANAGED_KEYS & env.keys()):
            changes.append((key, env.pop(key), None))
    else:
        # A narrower `enable` (e.g. --no-tool-details) must also clear the flag it omits.
        for key in sorted(MANAGED_KEYS - wanted.keys()):
            if key in env:
                changes.append((key, env.pop(key), None))
        for key, value in wanted.items():
            if env.get(key) != value:
                changes.append((key, env.get(key), value))
                env[key] = value
    if not changes:
        print(f"{path}: already up to date")
        return False
    for key, old, new in changes:
        print(f"  {key}: {old!r} -> {new!r}")
    if dry_run:
        print(f"{path}: dry run, nothing written")
        return True
    backup = write_settings(path, data)
    print(f"{path}: updated" + (f" (backup: {backup.name})" if backup else ""))
    print("Restart running Claude Code sessions for the change to take effect.")
    return True


# --- Docker stack -----------------------------------------------------------

def compose(*args: str) -> None:
    if not shutil.which("docker"):
        sys.exit("docker not found on PATH; install Docker Desktop or the Docker Engine.")
    subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], check=True)


def http(method: str, url: str, body: dict | None = None, timeout: float = 5):
    """Return (status, parsed JSON or None); status 0 means the connection failed."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, OSError):
        return 0, None
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, None


def wait_for(name: str, url: str, timeout_s: int) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if 200 <= http("GET", url)[0] < 300:
            return
        time.sleep(2)
    sys.exit(f"{name} did not become ready at {url} within {timeout_s}s "
             f"(try: docker compose logs {name.lower()})")


# --- Perses dashboards ------------------------------------------------------

def import_dashboard(perses: str, doc: dict) -> str:
    project, name = doc["metadata"]["project"], doc["metadata"]["name"]
    base = f"{perses}/api/v1/projects"
    if http("GET", f"{base}/{project}")[0] == 404:
        http("POST", base, {"kind": "Project", "metadata": {"name": project}})
    status, _ = http("POST", f"{base}/{project}/dashboards", doc)
    if status in (200, 201):
        return "created"
    if status == 409:  # exists: update in place, which needs the stored version
        status, current = http("GET", f"{base}/{project}/dashboards/{name}")
        if status == 200 and current:
            doc = {**doc, "metadata": {**current["metadata"], **doc["metadata"]}}
            doc["metadata"]["version"] = current["metadata"].get("version", 0)
            status, _ = http("PUT", f"{base}/{project}/dashboards/{name}", doc)
            if status == 200:
                return "updated"
    sys.exit(f"Perses refused dashboard {project}/{name} (HTTP {status}).")


def import_dashboards(perses: str, dry_run: bool) -> None:
    # Only the JSON files; the YAML ones are loaded by Perses' own provisioning.
    files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not files:
        sys.exit(f"No dashboard JSON files in {DASHBOARD_DIR}")
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        meta = doc["metadata"]
        label = f"{meta['project']}/{meta['name']}"
        if dry_run:
            print(f"  would import {label} from {path.name}")
        else:
            print(f"  {label}: {import_dashboard(perses, doc)}")


# --- Commands ---------------------------------------------------------------

def cmd_setup(args) -> None:
    if not args.skip_stack:
        print("Starting the stack...")
        if args.dry_run:
            print("  would run: docker compose up -d")
        else:
            compose("up", "-d")
            wait_for("Perses", SERVICES["Perses"], args.timeout)
            wait_for("Prometheus", SERVICES["Prometheus"], args.timeout)
    if not args.skip_dashboard:
        print("Loading dashboards...")
        import_dashboards(args.perses_url, args.dry_run)
    if not args.skip_claude:
        print("Configuring Claude Code telemetry...")
        cmd_enable(args)
    print(f"\nDone. Open {args.perses_url} -> ai-agents -> Agent cost.")
    print("Start (or restart) Claude Code, send a prompt, and data appears within about a minute.")


def cmd_enable(args) -> None:
    wanted = telemetry_env(args.endpoint, args.interval, not args.no_tool_details)
    apply_env(args.settings, wanted, args.dry_run)


def cmd_disable(args) -> None:
    apply_env(args.settings, None, args.dry_run)


def cmd_dashboard(args) -> None:
    import_dashboards(args.perses_url, args.dry_run)


def cmd_down(args) -> None:
    compose("down", *(["-v"] if args.purge else []))


def cmd_status(args) -> None:
    env = load_settings(args.settings).get("env", {})
    print(f"Claude Code settings ({args.settings}):")
    wanted = telemetry_env(args.endpoint, args.interval, True)
    for key, want in wanted.items():
        have = env.get(key)
        mark = "ok     " if have == want else ("differs" if have is not None else "missing")
        print(f"  [{mark}] {key}={have if have is not None else '-'}")
    print("Services:")
    for name, url in SERVICES.items():
        up = 200 <= http("GET", url)[0] < 300
        print(f"  [{'up  ' if up else 'down'}] {name:<11} {url}")
    try:
        socket.create_connection(COLLECTOR_ADDR, timeout=2).close()
        collector = True
    except OSError:
        collector = False
    print(f"  [{'up  ' if collector else 'down'}] {'Collector':<11} {COLLECTOR_ADDR[0]}:{COLLECTOR_ADDR[1]} (OTLP gRPC)")
    status, _ = http("GET", f"{args.perses_url}/api/v1/projects/ai-agents/dashboards/agent-cost")
    print(f"Dashboard ai-agents/agent-cost: {'present' if status == 200 else 'not loaded'}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS,
                        help=f"Claude Code user settings file (default: {DEFAULT_SETTINGS})")
    common.add_argument("--endpoint", default="http://localhost:4317",
                        help="OTLP gRPC endpoint of the collector (default: %(default)s)")
    common.add_argument("--interval", type=int, default=10000, metavar="MS",
                        help="metric export interval in ms (default: %(default)s; Claude Code's is 60000)")
    common.add_argument("--no-tool-details", action="store_true",
                        help="leave OTEL_LOG_TOOL_DETAILS off: tool inputs stay out of telemetry, "
                             "but agent and MCP names are no longer reported on cost metrics")
    common.add_argument("--perses-url", default=PERSES_URL, help="Perses base URL (default: %(default)s)")
    common.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", parents=[common], help="start the stack, load dashboards, enable telemetry")
    setup.add_argument("--skip-stack", action="store_true", help="do not run docker compose")
    setup.add_argument("--skip-dashboard", action="store_true", help="do not import dashboards")
    setup.add_argument("--skip-claude", action="store_true", help="do not touch Claude Code settings")
    setup.add_argument("--timeout", type=int, default=180, help="seconds to wait for services (default: %(default)s)")
    setup.set_defaults(func=cmd_setup)

    for name, func, text in (
        ("enable", cmd_enable, "turn on Claude Code telemetry in user settings"),
        ("disable", cmd_disable, "remove the telemetry variables this tool manages"),
        ("dashboard", cmd_dashboard, "(re)load the dashboards into a running Perses"),
        ("status", cmd_status, "show settings, services and dashboard state"),
    ):
        sub.add_parser(name, parents=[common], help=text).set_defaults(func=func)

    down = sub.add_parser("down", help="stop the stack")
    down.add_argument("--purge", action="store_true", help="also delete the data volumes")
    down.set_defaults(func=cmd_down)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"command failed: {' '.join(map(str, exc.cmd))}")


if __name__ == "__main__":
    main()

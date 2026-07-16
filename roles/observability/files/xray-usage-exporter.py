#!/usr/bin/env python3
"""Prometheus exporter for Xray per-user traffic counters.

Polls each entry node's Xray StatsService (`xray api statsquery`, non-destructive
— never uses -reset, so it does not disturb the counters any future backend
accounting loop relies on) and re-exposes the per-user byte counters as
Prometheus metrics for the top-talkers dashboard. Standard library only.

Data source is keyed by Xray's pseudonymous accounting identifier (the "email"
field). It carries no PII beyond that identifier, consistent with the
`per_user_usage` entry in governance/data-catalog.yml. This is a visibility
feed with Prometheus's normal short retention, not billing-grade accounting;
durable per-user usage remains the backend's responsibility.
"""
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("USAGE_EXPORTER_PORT", "9110"))
INTERVAL = int(os.environ.get("USAGE_EXPORTER_INTERVAL", "30"))
QUERY_TIMEOUT = int(os.environ.get("USAGE_EXPORTER_QUERY_TIMEOUT", "5"))
TOP_N = int(os.environ.get("USAGE_EXPORTER_TOP_N", "50"))
TARGETS_PATH = os.environ.get(
    "USAGE_EXPORTER_TARGETS", "/etc/xray-usage-exporter/targets.json"
)
XRAY_BIN = os.environ.get("USAGE_EXPORTER_XRAY_BIN", "/usr/local/bin/xray")

# user>>>{email}>>>traffic>>>{uplink|downlink}
_STAT_RE = re.compile(r"^user>>>(?P<email>.+)>>>traffic>>>(?P<dir>uplink|downlink)$")
_DIRECTION = {"uplink": "up", "downlink": "down"}

_lock = threading.Lock()
_latest_metrics = (
    "# HELP xray_usage_exporter_up Exporter has produced at least one scrape.\n"
    "# TYPE xray_usage_exporter_up gauge\n"
    "xray_usage_exporter_up 0\n"
)


def _load_targets():
    try:
        with open(TARGETS_PATH, encoding="utf-8") as handle:
            targets = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[exporter] cannot read targets {TARGETS_PATH}: {exc}", flush=True)
        return []
    result = []
    for entry in targets if isinstance(targets, list) else []:
        node = str(entry.get("node", "")).strip()
        server = str(entry.get("server", "")).strip()
        if node and server:
            result.append((node, server))
    return result


def _query_node(server):
    """Return the parsed statsquery 'stat' list for one node, or None on failure."""
    try:
        proc = subprocess.run(
            [
                XRAY_BIN, "api", "statsquery",
                f"--server={server}",
                "-pattern", "user>>>",
                "-timeout", str(QUERY_TIMEOUT),
            ],
            capture_output=True, text=True, timeout=QUERY_TIMEOUT + 5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[exporter] query {server} failed: {exc}", flush=True)
        return None
    if proc.returncode != 0:
        print(f"[exporter] query {server} rc={proc.returncode}: "
              f"{proc.stderr.strip()[:200]}", flush=True)
        return None
    try:
        return json.loads(proc.stdout or "{}").get("stat", []) or []
    except ValueError as exc:
        print(f"[exporter] query {server} bad json: {exc}", flush=True)
        return None


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _collect():
    targets = _load_targets()
    # usage[node][email] = {"up": int, "down": int}
    usage = {}
    node_ok = {}
    for node, server in targets:
        stats = _query_node(server)
        node_ok[node] = stats is not None
        if stats is None:
            continue
        per_email = usage.setdefault(node, {})
        for item in stats:
            match = _STAT_RE.match(str(item.get("name", "")))
            if not match:
                continue
            email = match.group("email")
            direction = _DIRECTION[match.group("dir")]
            value = int(item.get("value", 0) or 0)
            per_email.setdefault(email, {"up": 0, "down": 0})[direction] = value

    if TOP_N > 0:
        for node, per_email in usage.items():
            if len(per_email) <= TOP_N:
                continue
            ranked = sorted(
                per_email.items(),
                key=lambda kv: kv[1]["up"] + kv[1]["down"],
                reverse=True,
            )[:TOP_N]
            usage[node] = dict(ranked)

    return _render(usage, node_ok)


def _render(usage, node_ok):
    lines = [
        "# HELP xray_user_traffic_bytes_total Cumulative per-user traffic bytes "
        "from Xray StatsService (resets on Xray restart).",
        "# TYPE xray_user_traffic_bytes_total counter",
    ]
    for node, per_email in sorted(usage.items()):
        for email, dirs in sorted(per_email.items()):
            node_l, email_l = _escape(node), _escape(email)
            lines.append(
                f'xray_user_traffic_bytes_total{{node="{node_l}",'
                f'email="{email_l}",direction="up"}} {dirs["up"]}'
            )
            lines.append(
                f'xray_user_traffic_bytes_total{{node="{node_l}",'
                f'email="{email_l}",direction="down"}} {dirs["down"]}'
            )

    lines.append("# HELP xray_usage_exporter_node_up StatsService reachable for node.")
    lines.append("# TYPE xray_usage_exporter_node_up gauge")
    for node, ok in sorted(node_ok.items()):
        lines.append(
            f'xray_usage_exporter_node_up{{node="{_escape(node)}"}} {1 if ok else 0}'
        )

    lines.append("# HELP xray_usage_exporter_up Exporter has produced at least one scrape.")
    lines.append("# TYPE xray_usage_exporter_up gauge")
    lines.append("xray_usage_exporter_up 1")
    return "\n".join(lines) + "\n"


def _poll_loop():
    global _latest_metrics
    while True:
        try:
            rendered = _collect()
            with _lock:
                _latest_metrics = rendered
        except Exception as exc:  # never let the poll thread die
            print(f"[exporter] poll error: {exc}", flush=True)
        time.sleep(INTERVAL)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_error(404)
            return
        with _lock:
            body = _latest_metrics.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence per-request stderr logging
        return


def main():
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"[exporter] listening on :{PORT}, interval={INTERVAL}s, "
          f"top_n={TOP_N}, targets={TARGETS_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

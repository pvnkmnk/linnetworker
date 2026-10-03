#!/usr/bin/env python3
"""Provision the Cloudflare Worker observability dashboard into Grafana.

Idempotent: the datasource and the dashboard are both looked up first and only
created when missing, so re-running after a Grafana restart is safe and does not
duplicate anything. Everything goes through the Grafana HTTP API, so nothing
here depends on Grafana's YAML provisioning being mounted.

    python ops/observability/provision_grafana.py

Env: GRAFANA_URL, GRAFANA_AUTH (REQUIRED, "user:password"), VM_URL
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3082").rstrip("/")
VM_URL = os.environ.get("VM_URL", "http://victoriametrics:8428")


def grafana_auth():
    """Read the credential from the environment. No default, no fallback.

    It used to be hardcoded here, which put a live admin password in the working
    tree and contradicted AGENTS.md's rule that credentials stay out of the repo.
    The owning source is the portal-grafana container (GF_SECURITY_ADMIN_PASSWORD);
    a copy of it here is a second source of truth that silently rots when the
    container password changes.
    """
    auth = os.environ.get("GRAFANA_AUTH", "").strip()
    if not auth:
        sys.stderr.write(
            "provision_grafana: GRAFANA_AUTH is not set.\n"
            "  Expected 'user:password' for the Grafana instance.\n"
            "  The value lives with the container that owns it:\n"
            "    docker exec portal-grafana printenv GF_SECURITY_ADMIN_USER\n"
            "    docker exec portal-grafana printenv GF_SECURITY_ADMIN_PASSWORD\n"
        )
        sys.exit(2)
    return auth

DS_UID = "cf-worker-vm"
DASH_UID = "cf-worker-observability"
SCRIPT = "netrunner-linear-webhook"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GRAFANA_URL + path, data=data, method=method,
        headers={
            "Authorization": "Basic " + base64.b64encode(
                grafana_auth().encode()).decode(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def ensure_datasource():
    """Grafana talks to VictoriaMetrics by CONTAINER name -- it resolves inside
    the docker network, so http://127.0.0.1:8428 is wrong from here even though
    it is the same database."""
    try:
        api("GET", "/api/datasources/uid/" + DS_UID)
        print("datasource: already present (%s)" % DS_UID)
        return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    api("POST", "/api/datasources", {
        "uid": DS_UID,
        "name": "Cloudflare Worker (VictoriaMetrics)",
        "type": "prometheus",
        "access": "proxy",
        "url": VM_URL,
        "isDefault": False,
        "jsonData": {
            "httpMethod": "GET",
            "timeInterval": "15s",
            # VictoriaMetrics accepts 1m/1h and rejects Prometheus' 1M/1H forms,
            # which is a 400 on every query rather than an empty graph.
            "prometheusType": "VictoriaMetrics",
            "prometheusVersion": "2.24.0",
        },
    })
    print("datasource: created (%s -> %s)" % (DS_UID, VM_URL))


def target(expr, legend, ref="A"):
    return {
        "datasource": {"type": "prometheus", "uid": DS_UID},
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend,
        "range": True,
        "refId": ref,
    }


def panel(pid, title, ptype, grid, targets, unit="short", desc=""):
    p = {
        "id": pid,
        "type": ptype,
        "title": title,
        "datasource": {"type": "prometheus", "uid": DS_UID},
        "gridPos": grid,
        "targets": targets,
        "options": {},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "palette-classic"},
                "custom": {},
            },
            "overrides": [],
        },
        "description": desc,
    }
    # Distinct refIds are not cosmetic. Two targets sharing a refId are treated
    # as the same query, and the CPU/wall panel rendered "No data" even though
    # each expr returned points through Grafana's own datasource proxy.
    for i, t in enumerate(p["targets"]):
        t["refId"] = chr(ord("A") + i)
    if ptype == "stat":
        p["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "auto",
        }
        p["fieldConfig"]["defaults"]["custom"] = {}
    if ptype == "timeseries":
        p["options"] = {
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        }
        p["fieldConfig"]["defaults"]["custom"] = {
            "drawStyle": "line",
            "lineWidth": 2,
            "fillOpacity": 8,
            "showPoints": "auto",
            "spanNulls": True,
        }
    return p


def build_dashboard():
    return {
        "uid": DASH_UID,
        "title": "Cloudflare Worker - netrunner-linear-webhook",
        "description": (
            "Workers Logs for netrunner-linear-webhook, relayed from "
            "`wrangler tail` into VictoriaMetrics by ops/observability/relay.py. "
            "Counters persist across relay restarts, so a restart does not "
            "make every rate() dip."
        ),
        "tags": ["cloudflare", "workers", "linear-webhook"],
        "timezone": "browser",
        "editable": True,
        "schemaVersion": 39,
        "version": 0,
        "refresh": "15s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            panel(1, "Events observed", "stat", {"h": 4, "w": 6, "x": 0, "y": 0},
                  [target('cf_worker_events_total{script="%s"}' % SCRIPT, "events")],
                  desc="Total Worker invocations seen by the relay."),
            panel(2, "Relay up", "stat", {"h": 4, "w": 6, "x": 6, "y": 0},
                  [target('cf_worker_relay_up{script="%s"}' % SCRIPT, "up")],
                  desc="1 when the relay is pushing; 0 (or no data) means it stopped."),
            panel(3, "Seconds since last event", "stat",
                  {"h": 4, "w": 6, "x": 12, "y": 0},
                  [target('cf_worker_last_event_age_seconds{script="%s"}' % SCRIPT,
                          "age")], unit="s",
                  desc="Rises when the Worker stops receiving traffic."),
            panel(4, "Exceptions", "stat", {"h": 4, "w": 6, "x": 18, "y": 0},
                  [target('sum(cf_worker_exceptions_total{script="%s"})' % SCRIPT,
                          "exceptions")],
                  desc="Non-zero only if the Worker threw."),
            panel(5, "Requests by status and method", "timeseries",
                  {"h": 9, "w": 12, "x": 0, "y": 4},
                  [target('cf_worker_requests_total{script="%s"}' % SCRIPT,
                          "{{method}} {{status}} ({{outcome}})")]),
            panel(6, "CPU and wall time (mean ms)", "timeseries",
                  {"h": 9, "w": 12, "x": 12, "y": 4},
                  [target('cf_worker_cpu_time_ms_avg{script="%s"}' % SCRIPT, "cpu"),
                   target('cf_worker_wall_time_ms_avg{script="%s"}' % SCRIPT, "wall")],
                  unit="ms",
                  desc="Averages over every event the relay has seen."),
        ],
    }


def main():
    api("GET", "/api/health")          # fail loudly here, not mid-provision
    ensure_datasource()
    res = api("POST", "/api/dashboards/db", {
        "dashboard": build_dashboard(),
        "overwrite": True,
        "message": "provisioned by ops/observability/provision_grafana.py",
    })
    print("dashboard: %s (id %s)" % (res.get("status"), res.get("id")))
    print("open: %s/d/%s/%s" % (GRAFANA_URL, DASH_UID, DASH_UID))
    return 0


if __name__ == "__main__":
    sys.exit(main())

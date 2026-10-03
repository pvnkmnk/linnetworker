"""The counters: what one Worker event does to them, and how they reach
VictoriaMetrics.

Concerns that change together: the shape of the state dict, the metrics derived
from it, and the wire format they are shipped in. Capture (the tail, the event
log) and process supervision live elsewhere, so a metric change does not touch
either.

The state dict is the single owner of every number the dashboard shows
-----------------------------------------------------------------------
Counters are persisted, so a restart resumes rather than resetting to zero -- a
counter that dips on restart makes every rate() on the dashboard lie, which is
the same class of defect this repo keeps catching.

Threading: ONE thread touches this dict
---------------------------------------
The relay's main loop is the only caller of record(), build_payload() and
save_state(). The pump thread moves bytes from a pipe to a queue and does not
come in here. That is a structural invariant, not a hope: an earlier version had
a second thread polling on a timer, so save_state()'s json.dump re-read the dict
while the main thread inserted into it and raised

    RuntimeError: dictionary changed size during iteration

which the old poller did not catch (it caught only `(URLError, OSError)`, and
RuntimeError is neither). The exception escaped, the thread died, counters froze
and the dashboard stayed green. Measured 25/25 trials before the fix, 0/25 after.

Worth knowing if anyone "tidies" save_state(): `sort_keys=True` SUPPRESSES that
fault (0/25 with it, 25/25 without), so the shipped code was one kwarg from a
crash. That was luck, not a defence.

The wire format is JSON-lines, not Prometheus text
--------------------------------------------------
VictoriaMetrics /api/v1/import answers 204 No Content to a body it cannot read,
so the relay reported success for as long as it ran. Prometheus exposition was
rejected with `cannot parse json line: unexpected char` for every Content-Type
tried. A 204 is not evidence the data landed; querying the series back is.
"""

import json
import os
import time
import urllib.error
import urllib.request

CF_WORKER = os.environ.get("CF_WORKER", "netrunner-linear-webhook")
VM_URL = os.environ.get("VM_URL", "http://127.0.0.1:8428")
STATE_PATH = os.environ.get(
    "RELAY_STATE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))

METRIC_PREFIX = "cf_worker"
SCRIPT_LABEL = CF_WORKER


def load_state():
    """Read persisted counters, filling in anything an older file lacks."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("requests", {})   # "outcome|status|method" -> count
    st.setdefault("exceptions", {})
    st.setdefault("cpu_sum_ms", 0.0)
    st.setdefault("wall_sum_ms", 0.0)
    st.setdefault("events_total", 0)
    st.setdefault("last_event_ms", 0)
    return st


def save_state(st):
    """Persist via a temp file and rename, so a crash mid-write cannot leave a
    half-written state file that fails to parse on the next start."""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def record(st, ev):
    """Fold one `wrangler tail` event into the counters. True if it counted."""
    if "event" not in ev:
        return False
    req = (ev.get("event") or {}).get("request") or {}
    resp = (ev.get("event") or {}).get("response") or {}
    status = str(resp.get("status", "none"))
    outcome = str(ev.get("outcome", "unknown"))
    method = str(req.get("method", "?"))
    key = "%s|%s|%s" % (outcome, status, method)
    st["requests"][key] = st["requests"].get(key, 0) + 1
    if ev.get("exceptions"):
        exc = ev["exceptions"][0].get("name", "exception")
        st["exceptions"][exc] = st["exceptions"].get(exc, 0) + 1
    st["cpu_sum_ms"] += float(ev.get("cpuTime") or 0)
    st["wall_sum_ms"] += float(ev.get("wallTime") or 0)
    st["events_total"] += 1
    st["last_event_ms"] = int(ev.get("eventTimestamp") or 0)
    return True


def build_payload(st, now_s):
    """VictoriaMetrics JSON-lines: one object per series, newline separated.

    See the module docstring -- Prometheus exposition text is silently rejected
    here with a 204, which reads as success.
    """
    ts_ms = int(now_s * 1000)
    n = max(st["events_total"], 1)
    rows = []

    def add(name, labels, value):
        m = {"__name__": "%s_%s" % (METRIC_PREFIX, name), "script": SCRIPT_LABEL}
        m.update(labels)
        rows.append(json.dumps({"metric": m, "values": [value],
                                "timestamps": [ts_ms]}, ensure_ascii=False))

    for key, count in sorted(st["requests"].items()):
        outcome, status, method = key.split("|")
        add("requests_total",
            {"outcome": outcome, "status": status, "method": method}, count)
    # Always emit the exceptions series, even at zero. With no exceptions there
    # is no dict entry, so a bare `for` emits nothing and the Grafana panel reads
    # "No data" -- indistinguishable from the relay being broken. Zero and absent
    # must not look the same on a health panel.
    if st["exceptions"]:
        for exc, count in sorted(st["exceptions"].items()):
            add("exceptions_total", {"exception": exc}, count)
    else:
        add("exceptions_total", {"exception": "none"}, 0)
    add("events_total", {}, st["events_total"])
    add("cpu_time_ms_avg", {}, round(st["cpu_sum_ms"] / n, 4))
    add("wall_time_ms_avg", {}, round(st["wall_sum_ms"] / n, 4))
    add("relay_up", {}, 1)
    age = (now_s - st["last_event_ms"] / 1000.0) if st["last_event_ms"] else -1
    add("last_event_age_seconds", {}, round(age, 2))
    return "\n".join(rows) + "\n"


def push(st):
    """POST the current counters. Returns the HTTP status.

    Raises URLError/OSError so the caller decides whether a failed push is
    retried soon or merely reported -- the relay retries on its next wakeup.
    """
    body = build_payload(st, time.time())
    req = urllib.request.Request(
        VM_URL.rstrip("/") + "/api/v1/import",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status

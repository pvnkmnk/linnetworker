#!/usr/bin/env python3
"""Machine-readable health for the relay. Prints one JSON object, exits 0/1.

Why this exists
---------------
A dead relay and a quiet Worker look IDENTICAL from the dashboard: the panels go
quiet and read "No data" in both cases, and a monitor that only watches Grafana
cannot tell a broken exporter from an idle stream. The relay's stdout is the only
other signal, and it is gone the moment the container is replaced. This is what a
supervisor (the Docker HEALTHCHECK, or anything polling the service) can read
without parsing prose.

Two independent checks
----------------------
1. The lock is held by a live process -- "is anything running?".
2. state.json was modified recently. It is rewritten after every successful push,
   so freshness means "still shipping", not merely "still alive". The window is
   RELAY_HEALTH_MAX_AGE (default 3x RELAY_INTERVAL) so one slow push does not
   flap the check.

They are deliberately separate because *alive but not shipping* is a real and
different failure from *not running*, and the JSON says which.

Reused, not reimplemented
-------------------------
The lock assessment comes from relay_lock, and the state path from metrics --
the same code the relay itself uses. A health check that keeps its own copy of
"is this lock live" can drift from the writer's idea and then report healthy for
a relay that never started. This file holds the reporting and nothing else.

Run it from the same namespace as the relay. The lock file is shared through the
bind mount but a pid is not, so a probe run on the HOST cannot verify the
CONTAINER's holder and will (correctly) report it as an unverifiable held lock
rather than as healthy. `lock_verifiable` says which case you are in.

Never raises: a broken health check must not look like a broken relay, so every
read is guarded and an unreadable file becomes `ok: false` with a reason. That is
also why the modules it imports validate by raising rather than exiting.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics                                        # noqa: E402
import relay_lock                                     # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

INTERVAL = float(os.environ.get("RELAY_INTERVAL", "15"))
# Three missed pushes before unhealthy: tolerates one slow push and one dropped
# VM write without turning a blip into an alert.
MAX_AGE = float(os.environ.get("RELAY_HEALTH_MAX_AGE", "0")) or INTERVAL * 3


def read_state():
    """(state, error). Never raises -- an unreadable state file is a finding."""
    try:
        with open(metrics.STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except OSError as exc:
        return {}, "state unreadable: %s" % exc
    except ValueError as exc:
        return {}, "state malformed: %s" % exc


def main():
    report = {
        "service": "cf-worker-relay",
        "checked_at_ms": int(time.time() * 1000),
        "ok": False,
        "reason": None,
        "running": False,
        "pid": None,
        "events_total": None,
        "state_age_seconds": None,
        "max_state_age_seconds": MAX_AGE,
        "lock_holder_host": None,
        "lock_verifiable": None,
    }

    # 1. lock present and held by a live process. `verifiable` distinguishes
    #    "conclusively dead" from "held by a relay whose pid this process cannot
    #    check, because it lives in another PID namespace" -- different facts
    #    with different remedies, and an operator needs to know which.
    entry = relay_lock.read_lock()
    if entry is None:
        report["reason"] = ("relay not running: no usable %s"
                            % os.path.basename(relay_lock.LOCK_PATH))
    else:
        held, verifiable = relay_lock.assess(entry)
        report["lock_holder_host"] = entry.get("host")
        report["lock_verifiable"] = verifiable
        if held:
            if verifiable:
                report["running"] = True
                report["pid"] = entry["pid"]
            else:
                # The heartbeat age goes in the reason, not in its own field: a
                # monitor reads `ok` and `reason`, and a number nobody queries is
                # a number that only ever rots against the string beside it.
                beat = entry.get("heartbeat_ms")
                age = ("%.1fs" % (time.time() - beat / 1000.0)
                       if beat is not None else "unknown")
                report["reason"] = (
                    "%s is held by a relay on %s (pid %s there) whose heartbeat "
                    "is %s old; this process cannot verify another namespace's "
                    "pids, so the lock is treated as held"
                    % (os.path.basename(relay_lock.LOCK_PATH),
                       entry.get("host"), entry["pid"], age))
        else:
            report["reason"] = ("relay not running: %s is held by pid %s, which is "
                                "gone or was replaced"
                                % (os.path.basename(relay_lock.LOCK_PATH), entry["pid"]))

    # 2. state freshness = still shipping
    state, err = read_state()
    if err:
        if report["reason"] is None:
            report["reason"] = err
    else:
        report["events_total"] = state.get("events_total")
        try:
            age = round(time.time() - os.path.getmtime(metrics.STATE_PATH), 2)
        except OSError as exc:
            age = None
            if report["reason"] is None:
                report["reason"] = "state mtime unavailable: %s" % exc
        report["state_age_seconds"] = age
        if report["running"] and age is not None:
            if age > MAX_AGE:
                report["reason"] = ("alive but not shipping: state is %.1fs old "
                                    "(max %.1fs)" % (age, MAX_AGE))
            else:
                report["ok"] = True

    print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
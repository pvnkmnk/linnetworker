#!/usr/bin/env python3
"""Relay Cloudflare Workers Logs into the local VictoriaMetrics Grafana reads.

The entry point. It owns the two things that only make sense at this level: how
bytes get out of `wrangler tail`, and when work happens. Everything it does, it
delegates:

    stream text ──► drain()        split wrangler's pretty-printed objects
              ──► metrics.record() fold an event into the counters
              ──► EventLog.write() append the raw event to the bounded log
              ──► metrics.push()   ship the counters to VictoriaMetrics

    relay_lock    who owns the run (a second relay corrupts the dashboard)
    metrics       what is counted, and the wire format it ships in
    event_log     the append-only capture file, its size and generation caps

Why this exists
---------------
`wrangler.toml` turns on Workers Logs, so the Worker is producing telemetry only
Cloudflare can show you. The dashboard's Query Builder is a browser thing; the
`workers/observability/telemetry/query` REST endpoint returned 400 for every
documented body shape, and Workers Logpush -- the supported way to export logs
continuously -- is not enabled on this account (`logpush: false`, no
destinations). So the export path is built here instead: `wrangler tail` speaks
the same Workers Logs stream the dashboard reads, and this turns each event into
VictoriaMetrics JSON-lines over its existing `/api/v1/import`.

Usage
-----
    python relay.py                 # run until Ctrl-C
    python relay.py --seconds 60    # bounded run, then exit

RELAY_WORKER_DIR must point at the observed Worker's checkout (the directory
holding its wrangler.toml); under compose that is the /worker mount.

`--seconds` is honoured on an IDLE stream. The tail is drained on a worker
thread and this loop wakes on a timer, so the deadline does not depend on the
stream producing a line.

Supervision and failure
-----------------------
A dead `wrangler tail` is respawned with backoff (1s, 2s, 4s... capped at 30s)
up to MAX_RESTARTS; once spent the relay exits NON-ZERO with the reason on
stderr, and under compose Docker restarts the whole container. It never dies
quietly: a relay that is not running and a dashboard that looks healthy is the
failure this exists to prevent.

THIS THREAD OWNS THE STATE
--------------------------
One thread touches the counters, and it is this one. The pump thread reads the
child's stdout into a queue and nothing else -- it never records, pushes or
saves. That is what makes the counters safe without a lock; see metrics.py for
the race that removing a second thread eliminated.

Env: CF_WORKER, VM_URL, RELAY_WORKER_DIR, RELAY_INTERVAL, RELAY_STATE,
     RELAY_MAX_RESTARTS, and the relay_log.* knobs documented in event_log.py.
     The lock's heartbeat windows are constants in relay_lock.py, not knobs.
"""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error

import event_log
import metrics
import relay_lock

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
# The directory holding the observed Worker's wrangler.toml. `wrangler tail`
# runs with cwd here and reads that config, and nothing else in this repo knows
# the Worker exists -- the two are separate services, which is why this is a
# knob and not a path derived from where this file sits. In the container it is
# the read-only /worker mount (see docker-compose.yml); set it to a checkout to
# run in the foreground.
WORKER_DIR = os.environ.get("RELAY_WORKER_DIR", "/worker")

INTERVAL = float(os.environ.get("RELAY_INTERVAL", "15"))
MAX_RESTARTS = int(os.environ.get("RELAY_MAX_RESTARTS", "3"))
WAKE = 0.5          # how often this loop re-checks the deadline and the child


def say(msg):
    """Unbuffered: the relay is normally backgrounded, and a buffered print in a
    redirected stream looks exactly like a relay that never started."""
    print("relay: %s" % msg, flush=True)


def die(msg, code=1):
    print("relay: %s" % msg, file=sys.stderr, flush=True)
    sys.exit(code)


def drain(buf):
    """Return (objects, remainder) from a buffer of `wrangler tail` output.

    `wrangler tail --format json` emits PRETTY-PRINTED objects concatenated
    together, NOT JSONL. Two things follow, and both bit a first attempt:

      * line splitting is useless, and whole-buffer json.loads stops after the
        first object, so the objects are walked with raw_decode instead;
      * the stream arrives a line at a time, so a partial object sits at the end
        of the buffer. Testing for "}" is not enough -- a nested object closes
        long before the top-level one does, and clearing the buffer on the first
        "}" destroys every event. The remainder is only the text from the last
        `{` that failed to decode.
    """
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(buf)
    while True:
        j = i
        while j < n and buf[j] != "{":
            j += 1
        if j >= n:
            return out, ""
        try:
            obj, k = dec.raw_decode(buf, j)
        except json.JSONDecodeError:
            return out, buf[j:]
        if isinstance(obj, dict):
            out.append(obj)
        i = k


def start_tail(errlog):
    """Spawn `wrangler tail` and a thread that drains its stdout into a queue.

    The pump thread's ONLY job is to move text into the queue. It never touches
    the counters -- that is the single-owner invariant, enforced by this function
    handing back a queue rather than the child.

    stderr goes to a FILE, never PIPE: wrangler chatters on stderr and an unread
    pipe fills its buffer and deadlocks the child.

    `start_new_session` puts the child in its OWN process group, so stop_tail can
    signal the whole tree at once. Without it a signal reaches only npx and the
    node process npx spawned survives.
    """
    proc = subprocess.Popen(
        ["npx", "wrangler", "tail", metrics.CF_WORKER, "--format", "json"],
        cwd=WORKER_DIR, stdout=subprocess.PIPE, stderr=errlog,
        bufsize=1, universal_newlines=True, encoding="utf-8", errors="replace",
        **({"start_new_session": True} if os.name != "nt" else {}),
    )
    q = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)          # EOF sentinel, so an idle loop still notices

    threading.Thread(target=pump, daemon=True).start()
    return proc, q


def stop_tail(proc):
    """Kill `wrangler tail` AND every process it spawned. Never raises.

    The direct child is npx, which spawns node as a grandchild. Signalling only
    the child leaves that grandchild running: it keeps tail.err open, keeps a
    Cloudflare tail connection, and one leaks per run -- 129 had accumulated on
    this machine, and the leaked handles are what turn a plain directory delete
    into "Device or resource busy".

    Windows first, and first for a reason: taskkill /T walks the parent-to-child
    tree, so it only reaches the grandchild while the parent is still alive to
    be walked from. Killing the parent first orphans the grandchild and the
    tree becomes unreachable.

    Never raises because this runs on the shutdown path, where masking the real
    exit is worse than a surviving process.
    """
    if proc is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        except OSError:
            pass
    else:
        # The child leads its own group (see start_tail), so the group is
        # exactly its tree. SIGTERM first, SIGKILL if the tree does not go.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except OSError:
                break               # no such group: already gone
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:                            # last resort, on the direct child only
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def validate_config():
    """Reject a policy that cannot work, before anything is opened.

    Not at import: health.py imports these modules, so a module that exits on
    import takes the health probe down with it -- precisely when the
    configuration is wrong and health reporting matters most.

    The Worker directory is checked here for the same reason. It used to be
    derived from this file's own location, so it could not be wrong; since this
    repo stands alone, RELAY_WORKER_DIR can point somewhere that is not a Worker
    checkout, and `wrangler tail` would then fail per event instead of once, up
    front, with the reason.
    """
    try:
        event_log.validate_config()
    except event_log.ConfigError as exc:
        die(str(exc))
    if not os.path.isfile(os.path.join(WORKER_DIR, "wrangler.toml")):
        die("RELAY_WORKER_DIR is not a Worker checkout: no wrangler.toml in %s.\n"
            "  It is the directory `wrangler tail` runs in; under compose this is"
            " the /worker mount." % WORKER_DIR)


def run(seconds=0):
    validate_config()
    try:
        relay_lock.acquire_lock()
    except relay_lock.LockHeld as exc:
        # The refusal has to distinguish "definitely another relay" from "a
        # holder I cannot check", because the operator's next action differs:
        # stop that relay, versus confirm it is gone and delete the lock.
        if exc.verifiable:
            die("%s; stop it first, or delete %s" % (exc, exc.path))
        die("%s.\n  Refusing rather than reclaiming: a lock that cannot be "
            "attributed is still held. If you are certain that relay on %s is "
            "gone, delete %s by hand and re-run -- its heartbeat must lapse "
            "first (%gs), because this process cannot observe another "
            "namespace's pids."
            % (exc, exc.holder_host, exc.path, relay_lock.HEARTBEAT_STALE))

    # Ctrl-C already raises KeyboardInterrupt, which the finally below runs
    # through. SIGTERM is different: the default disposition kills the process
    # outright, leaving relay.lock behind for a PID that no longer exists.
    # SystemExit still unwinds through finally, so route SIGTERM to it.
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    except (AttributeError, ValueError):
        pass                          # not available on every platform

    errlog = open(os.path.join(HERE, "tail.err"), "w", encoding="utf-8")
    evlog = event_log.EventLog()
    st = metrics.load_state()
    proc = None
    exit_code = 0
    try:
        proc, q = start_tail(errlog)
        say("tailing %s -> %s (interval %ss)"
            % (metrics.CF_WORKER, metrics.VM_URL, INTERVAL))

        buf = ""
        deadline = (time.time() + seconds) if seconds > 0 else None
        restarts = 0
        next_push = time.time() + INTERVAL
        next_beat = time.time() + relay_lock.HEARTBEAT_EVERY

        def heartbeat():
            """Refresh the lock's heartbeat, on its OWN clock.

            It is the only signal a relay in the other PID namespace can see --
            a pid does not cross the bind mount -- so a relay that stopped
            beating while still alive would have its lock reclaimed and the
            double-relay corruption this lock exists to prevent would come back.
            Hence it is NOT tied to the push: a failing VictoriaMetrics must not
            read as a dead relay.
            """
            nonlocal next_beat
            if time.time() >= next_beat:
                relay_lock.beat()
                next_beat = time.time() + relay_lock.HEARTBEAT_EVERY

        while True:
            heartbeat()
            if deadline is not None and time.time() >= deadline:
                say("--seconds %g reached" % seconds)
                break

            try:
                chunk = q.get(timeout=WAKE)
            except queue.Empty:
                chunk = ""            # idle: fall through and re-check below

            died = (chunk is None) or (proc.poll() is not None)
            if chunk:
                buf += chunk
                events, buf = drain(buf)
                for ev in events:
                    if metrics.record(st, ev):
                        evlog.write(ev)

            if time.time() >= next_push:
                # Push and persist from THIS thread, which is the only thread
                # that touches `st`. A failed push deliberately does not advance
                # next_push, so it is retried on the next wakeup rather than
                # being skipped for a whole interval.
                try:
                    code = metrics.push(st)
                    metrics.save_state(st)
                    next_push = time.time() + INTERVAL
                    say("pushed %d events (VM %s)" % (st["events_total"], code))
                except (urllib.error.URLError, OSError) as exc:
                    say("push failed: %s" % exc)

            if died:
                restarts += 1
                if restarts > MAX_RESTARTS:
                    print("relay: `wrangler tail` died and %d restarts are spent; "
                          "giving up. Re-run to resume -- counts are kept in %s"
                          % (MAX_RESTARTS, metrics.STATE_PATH),
                          file=sys.stderr, flush=True)
                    exit_code = 1
                    break
                backoff = min(30, 2 ** (restarts - 1))
                say("tail died (rc=%s); restart %d/%d in %ds"
                    % (proc.returncode, restarts, MAX_RESTARTS, backoff))
                stop_tail(proc)
                deadline_rem = None if deadline is None else \
                    deadline - time.time()
                if deadline_rem is not None and deadline_rem <= 0:
                    break
                # Sleep in slices so --seconds still bounds a restart backoff. The backoff is
                # capped at 30s against relay_lock's 90s heartbeat window, so the
                # lock cannot lapse while sleeping here.
                waited = 0.0
                while waited < backoff:
                    time.sleep(min(WAKE, backoff - waited))
                    waited += WAKE
                    if deadline is not None and time.time() >= deadline:
                        say("--seconds %g reached" % seconds)
                        deadline = None
                        break
                if deadline is None and seconds > 0:
                    break
                proc, q = start_tail(errlog)
    except KeyboardInterrupt:
        say("interrupted")
    finally:
        try:
            metrics.push(st)
            metrics.save_state(st)
        except (urllib.error.URLError, OSError) as exc:
            say("final push failed: %s" % exc)
        stop_tail(proc)
        evlog.close()
        errlog.close()
        exc = relay_lock.release_lock()
        if exc:
            print("relay: could not remove %s: %s" % (relay_lock.LOCK_PATH, exc),
                  file=sys.stderr, flush=True)
        say("done, %d events total" % st["events_total"])
    return exit_code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Relay Cloudflare Workers Logs to VictoriaMetrics")
    ap.add_argument("--seconds", type=float, default=0,
                    help="run for N seconds then exit (0 = forever)")
    sys.exit(run(**vars(ap.parse_args())))
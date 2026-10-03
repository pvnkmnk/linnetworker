"""The relay.lock file: WHO holds it, and is that holder still there?

Two concerns that are the same concern: "is another relay holding the lock?"
Both the relay (which must refuse to start) and health.py (which must report
unhealthy) need to answer it, and they must not be able to disagree. So the
answer lives here once, and both callers import it.

The lock spans a boundary, so a PID is not an identity
-------------------------------------------------------
The relay runs in a container but its lock file is on a bind mount, so the HOST
and the CONTAINER read and write the same file -- while their PIDs mean
completely different things. The host here is `MVNK`; the container's hostname
is its own container id, which changes every time Docker recreates it, and its
relay is pid 7 there. A PID is only meaningful inside the namespace that issued
it.

That made the lock actively dangerous, not merely imprecise. Measured: with the
containerised relay running and healthy, starting `python relay.py` on the host
reclaimed the container's lock (the container's pid 7 is not a live pid on the
host, so it read as stale) and the two relays then wrote the same state.json,
with the container's health flipping to unhealthy. One command, and the exact
corruption this lock exists to prevent.

So the holder records an identity that includes its namespace:

    {"pid": 7, "start_ticks": 9631820, "host": "<container id>", "heartbeat_ms": ...}

and liveness is decided differently depending on who is asking:

  SAME namespace as the holder   pid + start time decide it exactly. A pid that
                                 is gone, or that now belongs to a different
                                 process, is conclusive proof of death.

  DIFFERENT namespace            the pid proves NOTHING -- it may name a
                                 completely unrelated process, or nothing at
                                 all. The only signal that crosses the boundary
                                 is the heartbeat, which the holder refreshes
                                 on its own clock (HEARTBEAT_EVERY). A
                                 heartbeat older than HEARTBEAT_STALE means the
                                 holder has stopped.

What it can and cannot verify, stated plainly
---------------------------------------------
This can conclusively prove DEATH on the same host, and can infer death across a
boundary from a lapsed heartbeat. It cannot positively confirm LIFE across a
boundary -- there is no way to ask another PID namespace "is this process still
there?". So an unverifiable lock (a live heartbeat, but a pid we cannot check)
is treated as HELD and the newcomer refuses. Refusing on doubt is the whole
point: a lock that can be stolen is not a lock.

Two earlier bugs live in that history, both real, both measured:
  * a PID alone let a restarted container read its OWN pid as a live holder and
    refuse forever -- a restart loop, before start_ticks was recorded;
  * the boundary case above, before `host` and the heartbeat were.

Failures are raised, not printed and exited
-------------------------------------------
acquire_lock raises LockHeld; it does not sys.exit. The entry point decides what
a held lock means for it, and health.py can reuse the read-only half without
inheriting an exit path. That split is also why this module has no logging.
"""

import json
import os
import socket
import time

LOCK_PATH = os.environ.get("RELAY_LOCK",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "relay.lock"))

# A heartbeat older than this means the holder has stopped. Generous enough to
# survive a slow box, tight enough that a dead holder is reclaimed in about a
# minute. Must comfortably exceed HEARTBEAT_EVERY, or a healthy relay reads as
# dead between its own beats.
HEARTBEAT_STALE = 90.0
# How often the holder refreshes its own heartbeat. Deliberately its own clock
# rather than a side effect of the push: a VictoriaMetrics outage must not stop
# the beating of a relay that is very much alive, or the other side of the bind
# mount would reclaim its lock and both relays would write state.json again.
HEARTBEAT_EVERY = 15.0

THIS_HOST = socket.gethostname()


class LockHeld(Exception):
    """Another relay holds the lock.

    Carries enough context for the caller to explain WHY it is refusing, because
    "another relay is running" and "I cannot verify that relay, so I am not
    taking its lock" are different situations and an operator needs to know
    which one they are looking at.
    """

    def __init__(self, pid, path, holder_host=None, verifiable=True):
        self.pid = pid
        self.path = path
        self.holder_host = holder_host
        self.verifiable = verifiable
        if not verifiable:
            msg = ("the lock is held by a relay on %s (pid %s there), which this "
                   "process cannot check: a pid means nothing outside its own "
                   "namespace" % (holder_host, pid))
        else:
            msg = "another relay is already running (pid %d)" % pid
        super().__init__(msg)


def proc_start_ticks(pid):
    """Field 22 of /proc/<pid>/stat -- the process start time in clock ticks.

    Reading needs care: field 2 (comm) may itself contain spaces and
    parentheses, so everything up to the LAST ')' is skipped. Returns None when
    /proc is unavailable (Windows), which falls back to PID liveness alone.
    """
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        rest = data[data.rindex(")") + 1:].split()
        return int(rest[19])          # field 22 overall == index 19 after comm
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid):
    """Is this PID running? Read-only on every platform, deliberately.

    `os.kill(pid, 0)` is the POSIX idiom and it is WRONG on Windows, in two
    separate ways that both bit this lock:

      * CPython routes any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT to
        OpenProcess + TerminateProcess, so signal 0 TERMINATES the target with
        exit code 0 -- a liveness probe that kills what it inspects;
      * for a PID it cannot open it raises SystemError (WinError 87), which is
        NOT an OSError, so `except OSError` does not catch it and the caller
        crashes instead of reclaiming a stale lock.

    So: GetExitCodeProcess via ctypes on Windows, signal 0 everywhere else.
    Read-only in both branches.
    """
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                 # alive, just not ours to signal
    except OSError:
        return False
    return True


def read_lock():
    """Return the holder record, or None if there is no usable lock.

    Returns a dict: {"pid", "start_ticks", "host", "heartbeat_ms"}. Missing
    fields are None -- a legacy bare-PID lock is understood and treated as
    belonging to this host with an unverifiable heartbeat.
    """
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if isinstance(obj, dict) and "pid" in obj:
        try:
            return {"pid": int(obj["pid"]),
                    "start_ticks": obj.get("start_ticks"),
                    "host": obj.get("host"),
                    "heartbeat_ms": obj.get("heartbeat_ms")}
        except (TypeError, ValueError):
            return None
    try:
        return {"pid": int(raw), "start_ticks": None, "host": None,
                "heartbeat_ms": None}      # legacy bare PID
    except ValueError:
        return None


def _same_namespace(holder_host):
    """Is the holder's pid meaningful to THIS process?

    None means the lock predates the `host` field, so it was written by a relay
    on this machine and its pid is ours to check.
    """
    return holder_host is None or holder_host == THIS_HOST


def heartbeat_is_stale(entry):
    """True if the holder has demonstrably stopped.

    The only signal that crosses a PID-namespace boundary. A missing heartbeat
    (legacy lock) is NOT treated as stale -- an absent signal is not a negative
    one, and assuming otherwise would let a newcomer steal a lock it cannot see.
    """
    beat = entry.get("heartbeat_ms")
    if beat is None:
        return False
    return time.time() * 1000 - float(beat) > HEARTBEAT_STALE * 1000


def assess(entry):
    """Decide whether the lock must be respected. Returns (held, verifiable).

    `verifiable` is False when the holder lives in another PID namespace: we can
    only see its heartbeat, never its pid. The caller must treat held=True as
    final regardless -- see the module docstring on refusing on doubt.
    """
    if entry is None:
        return False, True
    if not _same_namespace(entry.get("host")):
        # Another namespace: pid proves nothing here. Only a lapsed heartbeat
        # can free the lock, and "no heartbeat" is not that.
        return not heartbeat_is_stale(entry), False
    # Same namespace: pid + start time decide it exactly.
    pid = entry["pid"]
    if not pid_alive(pid):
        return False, True
    start = entry.get("start_ticks")
    if start is not None and proc_start_ticks(pid) != start:
        return False, True           # the pid was recycled; not our holder
    return True, True


def _identity():
    return {"pid": os.getpid(),
            "start_ticks": proc_start_ticks(os.getpid()),
            "host": THIS_HOST,
            "heartbeat_ms": int(time.time() * 1000)}


def acquire_lock():
    """Take the lock, or raise LockHeld.

    Two relays silently corrupt the dashboard: both poll VictoriaMetrics on the
    same interval and the second writes ITS counters over the first's, so
    cf_worker_events_total reads 0 while the dashboard looks perfectly healthy.
    This happened for real before the lock existed.

    Reclaims ONLY on conclusive death -- a dead pid on this host, or a holder in
    another namespace whose heartbeat has lapsed. An unverifiable holder is
    respected, never reclaimed.
    """
    entry = read_lock()
    if entry is not None:
        held, verifiable = assess(entry)
        if held:
            raise LockHeld(entry["pid"], LOCK_PATH,
                           holder_host=entry.get("host") or THIS_HOST,
                           verifiable=verifiable,
                           )
        os.remove(LOCK_PATH)          # conclusively dead: safe to reclaim
    with open(LOCK_PATH, "w", encoding="utf-8") as fh:
        json.dump(_identity(), fh)


def beat():
    """Refresh our heartbeat. Cheap, and its own clock rather than the push's.

    Written only if the lock still looks like ours, so a relay that has lost the
    lock (to a restart, or a stale reclaim) does not resurrect it and lock out
    the process that legitimately holds it now. A beat that cannot be written is
    not actionable: the far side would read a lapsed heartbeat and reclaim,
    which is the right answer to a holder that cannot record that it lives.
    """
    entry = read_lock()
    if entry is None or entry["pid"] != os.getpid():
        return
    if not _same_namespace(entry.get("host")):
        return
    try:
        entry["heartbeat_ms"] = int(time.time() * 1000)
        tmp = LOCK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, LOCK_PATH)
    except OSError:
        pass


def release_lock():
    """Remove the lock, but only if it is still ours.

    A stale lock must never outlive a dead process, but neither must a dying
    process delete the lock of the relay that replaced it -- that is the window
    where two relays could both believe they hold the run.

    Returns the OSError instead of raising, so a cleanup failure cannot mask the
    shutdown path that called it.
    """
    try:
        entry = read_lock()
        if entry is not None and entry["pid"] != os.getpid():
            return None              # not ours to remove
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError as exc:
        return exc
    return None

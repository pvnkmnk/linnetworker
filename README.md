# linnetworker

Cloudflare Worker observability → local Grafana.

Exports `netrunner-linear-webhook`'s Workers Logs into the Grafana already
running on this machine. No new container, nothing on a paid plan.

```
Worker → Workers Logs → wrangler tail → relay.py → VictoriaMetrics → Grafana
```

This is its own repository, not part of netrunner. The Worker it watches lives
in `djinn-netrunner` (`ops/linear-webhook`) and the relay must keep tailing
while that whole stack is down, so neither belongs inside the other:
`RELAY_WORKER_DIR` in [docker-compose.yml](docker-compose.yml) mounts that
checkout read-only at `/worker`, and nothing else crosses the boundary.

The repository is `linnetworker`; the container, image and compose project are
still `cf-worker-relay`, which is what they have always been called and what
every health check and `docker` command in this README uses.

*Why this exists* lives in [`relay.py`](relay.py)'s module docstring, next to the
code. This file is only what you need to run and operate it.
`relay.py --help` is authoritative for flags; `provision_grafana.py` has its
own header.

## Structure

Four modules plus a health probe, split so each has one reason to change.
Dependencies point one way: nothing imports `relay.py`, and each lower module
imports nothing of ours.

| module | owns | state it owns |
| --- | --- | --- |
| [`relay.py`](relay.py) | the entry point: stream decoding, tail supervision with backoff, when work happens | the run loop's cursor (`buf`, deadline, restarts) |
| [`metrics.py`](metrics.py) | what one event does to the counters, and the JSON-lines wire format | the counter dict, and `state.json` |
| [`event_log.py`](event_log.py) | the append-only capture file: size cap, generation cap, rotation | the open handle, byte count, retry clock |
| [`relay_lock.py`](relay_lock.py) | who owns the run, and whether that holder is still there | `relay.lock` |
| [`health.py`](health.py) | the health report; imports the two above rather than re-reading them | none |

Data flows one direction per event, all on the main thread:

```
wrangler tail ──stdout──► pump thread ──queue──► main loop
                                                    ├─ drain()        text → objects
                                                    ├─ metrics.record()  object → counters
                                                    ├─ EventLog.write()  object → events.log
                                                    └─ metrics.push()    counters → VictoriaMetrics
```

**The main thread is the only writer of the counters.** `start_tail()` hands back
a queue, not the child process, so the pump thread has no way to reach the state.
That is the whole of the single-owner invariant — it is structural, not a lock.
`metrics.py` and `event_log.py` are therefore free of thread handling entirely.

**No config module.** Each module reads the env knobs for the policy it owns, so
a knob sits beside the code that gives it meaning. A central config would only
move the same names somewhere else.

**Failures raise; the entry point exits.** `event_log.validate_config()` raises
`ConfigError`, `relay_lock.acquire_lock()` raises `LockHeld`. Neither calls
`sys.exit`, because `health.py` imports both and an import-time exit would take
the health probe down exactly when the configuration is wrong.

## Run it as a service

```bash
cd linnetworker                   # this directory is the repository root
docker compose up -d --build     # start
docker compose ps                # status, incl. health
docker logs -f cf-worker-relay   # relay stdout
docker compose stop              # clean stop (SIGTERM → finally → lock released)
```

It comes up at boot with Docker (`restart: unless-stopped`) and restarts on
crash. Nothing is exported by hand: the `RELAY_*` knobs, the worker name and
the VictoriaMetrics URL live in [`relay.env`](relay.env) (git-ignored, because
it may hold `GRAFANA_AUTH`). That file is also where `RELAY_WORKER_DIR` goes if
the Worker's checkout is not the sibling path the compose file assumes.

Dashboard: <http://127.0.0.1:3082/d/cf-worker-observability/cf-worker-observability>

### Health

A dead relay used to look exactly like a Worker with no traffic. It does not
any more:

```bash
docker exec cf-worker-relay python3 /app/health.py          # JSON + exit code
docker inspect cf-worker-relay --format '{{.State.Health.Status}}'     # healthy / unhealthy
```

```json
{"service":"cf-worker-relay","ok":true,"running":true,"pid":7,
 "events_total":67,"state_age_seconds":0.44,"max_state_age_seconds":45.0,
 "lock_holder_host":"<container id>","lock_verifiable":true,"reason":null}
```

Two independent checks, because *alive but not shipping* is a different failure
from *not running*: the lock must be held by a live process, and `state.json`
must have been rewritten within `3 × RELAY_INTERVAL`. The second one is what
catches a relay whose `wrangler tail` died and never came back.

**Run it from the same namespace as the relay.** The lock file is shared through
the bind mount; a pid is not. A probe run on the host cannot check the
container's holder, so it reports the lock as held but **unverifiable** and
exits 1 with the reason naming the holder — which is correct, and not a relay
outage. `lock_verifiable` is how you tell the two apart in one read:

| field | meaning |
| --- | --- |
| `lock_holder_host` | the hostname the holder recorded (`MVNK` on the host, the container id in the container) |
| `lock_verifiable` | `false` when the holder is in another PID namespace and only its heartbeat can be seen |

The heartbeat's age appears in `reason` rather than in a field of its own: a
monitor reads `ok` and `reason`, and a number nobody queries is a number that
only rots against the string beside it.

So the reading that counts is the Docker HEALTHCHECK's, which runs inside the
container. A host-side `ok: false` carrying `lock_verifiable: false` is the
expected answer to the wrong question, not a relay outage.

### Why a container and not a Windows service wrapper

Every other long-running process on this box is already a container under
`restart: unless-stopped` (`ops-web`, `ops-worker`, `portal-grafana`,
`portal-victoriametrics`, `komodo-core`). One mechanism, one way to inspect and
stop everything. nssm/WinSW would add third-party software with its own updater
to buy backoff Docker already provides; Task Scheduler has no clean-stop
semantics, no health signal, and would be the only non-container daemon here.

Two consequences worth knowing:

- **`docker kill` does NOT restart the container.** Verified against stock
  alpine: an explicit `kill` is treated as a deliberate stop, so
  `unless-stopped` deliberately leaves it exited. To prove the *crash* path,
  kill the process inside: `docker exec cf-worker-relay sh -c 'kill -9
  $(pgrep -f relay.py)'` — that respawns within a few seconds.
- **`docker compose stop` is graceful.** `tini` forwards SIGTERM, the relay's
  handler exits 143, `finally` runs, exit code is 143, and `relay.lock` is
  removed. Confirmed, not assumed.
- **Shutdown kills the tail's whole process tree, not just `npx`.** The direct
  child is `npx`, which spawns `node` as a grandchild; terminating only the
  child left that grandchild running — measured at **3 leaked processes per
  foreground run**, 129 of them accumulated, each holding `tail.err` and a
  Cloudflare tail connection. On POSIX the child gets its own session and the
  group is signalled; on Windows `taskkill /T` walks the tree, and it has to run
  *before* the parent dies or the grandchild is orphaned and unreachable. Three
  consecutive foreground runs now leave **zero** survivors.

State files (`state.json`, `events.log`, `tail.err`, `relay.lock`) are bind
-mounted from this directory rather than kept in a Docker volume, so counters
survive container replacement and `tail -f events.log` works from the host.

### events.log is bounded

A relay that runs forever under `restart: unless-stopped` cannot also have an
unbounded log — this was an availability risk, not a cosmetic one. Both axes are
capped, in [docker-compose.yml](docker-compose.yml):

| knob | default | meaning |
| --- | --- | --- |
| `RELAY_LOG_MAX_BYTES` | 16 MiB | size cap per file |
| `RELAY_LOG_MAX_FILES` | 3 | retired generations kept |

Worst case on disk is `16 MiB × (1 + 3) = 64 MiB`. Size alone would let a burst
multiply files; count alone would let a quiet day produce a file too big to grep.
Both are needed.

**Lowering `RELAY_LOG_MAX_FILES` used to leak files.** The rotation shift only
touches generations it is about to *move*, so a file already past the cap was
never in the path: seeding `events.log.1`–`.4` and running with
`RELAY_LOG_MAX_FILES=2` left `events.log.4` on disk permanently — four files
against a bound of three, in the mechanism whose only job is bounding file
count. `EventLog._sweep_orphans()` now clears everything above the cap before
each shift (sweeping *before*, not after, so it cannot delete the slot the shift
just filled). It scans upward to `RELAY_LOG_SWEEP_MAX` (64) rather than globbing,
and reports **only what it actually reclaimed**. It used to print on every
rotation — `reclaimed 0 log generation(s)` once per rotation, measured — and a
line that is always zero is a line an operator learns to skip, including the one
that would have mattered. When it does speak, the bounded window stays explicit:
`scanned up to N; anything beyond is NOT scanned — raise RELAY_LOG_SWEEP_MAX if
you see one`. An orphan past the ceiling is not found, and implying otherwise is
the bug that was replaced.

A startup does not sweep: the log is opened for append, and rotation is what
triggers the sweep. So an orphan survives until the next rotation (verified: a
hand-planted `events.log.7` was still there right after a crash-restart, then
gone after the first rotation).

**A log policy that bounds nothing is rejected at startup, not at import.**
`RELAY_LOG_MAX_FILES` below 1 exits 1 with the reason and leaves the log
untouched. That check lives in `validate_config()` called from `run()` rather
than at module scope, because `health.py` imports `relay` — an import-time exit
took the health probe down with it, at exactly the moment the configuration is
wrong and health reporting matters most. (It was worse than that: the guard used
to sit beside the knobs and call `die()`, which is defined ~300 lines below, so
it died with `NameError` instead of reporting anything.)

**Rotation renames; it never truncates.** `os.replace` is `rename(2)`, so the
retired file keeps its inode and an open reader — `tail -f`, a grep, a shipper —
reads it through to EOF uninterrupted. Truncating in place would hand that
reader a file whose first bytes are now *different content*, which is how a log
gets silently corrupted. Files appear oldest-last: `events.log.2` → `events.log.1`
→ `events.log`.

**A reader holding the log defeats rotation, and the relay says so.** Windows
will not rename a file with an open handle, and — measured, contrary to what I
first assumed — **Docker Desktop's bind mount propagates that lock**, so a host
reader on `events.log` makes the container's rename fail with `Errno 13
Permission denied` too. Observed: the log grew to 268,860 bytes against a
40,000-byte cap. In that state the relay **keeps capturing every event and logs
a rate-limited warning** that the cap is not in effect. A cap that quietly stops
capping is worse than no cap. Plain Linux `rename(2)` ignores open handles, so
this is specific to Docker Desktop on Windows.

**A blocked rotation retries on a timer, not per event.** The refusal is
retried after `RELAY_LOG_ROTATE_RETRY_EVERY` (30s), not on the next write. This
matters because the degraded state lasts as long as somebody has the log open in
an editor: retrying per event meant one rename syscall per captured event —
measured at 19 attempts for 200 events — which is O(traffic) of pointless
syscalls for as long as the file stays open. Now it is O(time): the same 200
events produce **1** attempt, and closing the reader still self-heals within
30s without needing a restart. Losslessness is unchanged either way — every
event is on disk in order, with no duplication, blocked or not.

**`RELAY_LOG_MAX_FILES` must be at least 1.** It used to accept 0, meaning
"delete the live log on rotation". No caller set it, nothing tested it, and
unlinking the file someone is reading is a sharper edge than keeping three
generations — so `0` is now a startup error rather than a behaviour.
`RELAY_LOG_MAX_BYTES=0` on its own is still legal: that turns off the size cap
and keeps the count cap.

### Provisioning needs a credential

`provision_grafana.py` reads `GRAFANA_AUTH` as `user:password`. There is **no
default and no fallback**: with it unset the script exits **2** and prints where
to find the value. Run it as a one-shot through the same compose file, so the
credential lives in the git-ignored `relay.env` instead of a shell export:

```bash
echo "GRAFANA_AUTH=admin:$(docker exec portal-grafana printenv GF_SECURITY_ADMIN_PASSWORD)" >> relay.env
docker compose --profile provision run --rm provisioner
```

It is idempotent — datasource and dashboard are looked up first and only
created when missing, so re-running after a Grafana restart duplicates nothing.

### Run it in the foreground instead

```bash
python relay.py                 # foreground, until Ctrl-C
python relay.py --seconds 60    # bounded run
```

Foreground runs need `RELAY_WORKER_DIR` pointed at the Worker's checkout
(the container gets it from the `/worker` mount):

```bash
RELAY_WORKER_DIR=../djinn-netrunner/ops/linear-webhook python relay.py --seconds 60
```

Stop the service first (`docker compose stop`) — the lock refuses a second relay,
whichever side of the bind mount it is on. That is not advice, it is the defect
the lock's current design exists to close: until the holder recorded *where* it
lived, running this against a live container silently took the container's lock.
See the lock section below.

## Behaviour that bites

**A bounded run bounds even when nothing arrives.** `--seconds 20` on an idle
stream exits at 20s with status 0. The tail is drained on a worker thread, so
the deadline does not depend on the stream producing a line.

**Only one thread owns the counters.** The pump thread reads `wrangler tail`'s
stdout into a queue and does nothing else; the main loop drains it, records,
writes the event log, and does the periodic push itself. There is no second
thread to race, so the invariant is structural rather than "the GIL makes it
fine" — which is also why the log rotation needs no lock. See the structure
section above for how the boundary is enforced in code.

**A dead `wrangler tail` is restarted, not fatal.** It is respawned with backoff
(1s, 2s, 4s… capped at 30s) up to `RELAY_MAX_RESTARTS` (default 3). Once they
are spent the relay exits **non-zero** with the reason on stderr — and under
compose, Docker restarts the whole container. Counts are in `state.json`, so
re-running resumes.

**Rotation survives both restart paths.** A hard kill leaves the log wherever it
happened to be, and the next process opens it in append mode — rotating first
only if it is already at the cap, so a crash that lands just before a rotation
does not lose the cut. Proven by SIGKILLing the relay with a log sitting at the
cap *and* a hand-planted orphan generation: Docker restarted it, the bound held
at 3 files once rotation resumed, the orphan was reclaimed, and counters
continued across the restart. A clean stop is the same story with `exit 143`,
a released lock, and no torn line in any generation.

### The lock crosses a namespace boundary

`relay.lock` lives on the bind mount, so the host and the container read and
write the same file — but **a PID is only meaningful inside the namespace that
issued it**. The container's relay is pid 7 *there*; on the host, pid 7 is an
unrelated process or nothing at all.

That was not a theoretical gap. Measured, with the containerised relay running
and healthy: `python relay.py` on the host **reclaimed** the container's lock,
both relays then wrote the same `state.json`, and the container's health flipped
to unhealthy. One command, and precisely the corruption the lock exists to
prevent.

So the holder records where it lives, and liveness is decided differently
depending on who is asking:

```json
{"pid": 7, "start_ticks": 9631820, "host": "<container id>", "heartbeat_ms": 1759…}
```

| the holder is … | what decides | reclaimed when … |
| --- | --- | --- |
| in **this** PID namespace | pid + process start time, exactly | the pid is gone, or now belongs to a different process |
| in **another** PID namespace | the heartbeat only — its pid proves nothing here | the heartbeat is older than 90s |

A lock with **no** heartbeat (the legacy format) counts as *fresh*, not stale.
An absent signal is not a negative one, and reading it as one is exactly how a
newcomer steals a lock it cannot see. A legacy bare-PID lock is understood as
belonging to this host, on pid liveness alone.

**An unverifiable lock is held, so the newcomer refuses.** This part is a
limitation stated plainly rather than a feature: there is no way to ask another
PID namespace *"is that process still there?"*. Life across the boundary can be
**inferred** — from a heartbeat still ticking — but never confirmed. Death can be
inferred; life cannot be proven. So the second relay says so instead of helping
itself:

```
$ python relay.py --seconds 2
relay: the lock is held by a relay on <container id> (pid 7 there), which this
process cannot check: a pid means nothing outside its own namespace.
  Refusing rather than reclaiming: a lock that cannot be attributed is still
  held. If you are certain that relay on <container id> is gone, delete
  relay.lock by hand and re-run -- its heartbeat must lapse
  first (90s), because this process cannot observe another namespace's pids.
```

A lock that can be stolen is not a lock. The cost of the policy is bounded and
known: a relay `kill -9`ed **inside the container** leaves a holder the host
cannot check, so the host refuses for up to 90s until the heartbeat lapses. That
is the intended trade — a 90s wait instead of silent double-relay. `docker
compose down` sends SIGTERM and the relay releases its lock, so that residue is
rare in practice.

**The heartbeat has its own clock, not the push's.** It is the only signal that
crosses the boundary, so tying it to a successful `metrics.push()` would mean a
VictoriaMetrics outage looks exactly like a dead relay — and the lock of a very
alive relay gets reclaimed out from under it. Proven with the push pointed at a
closed port: 8 consecutive failed pushes, and the heartbeat still advanced
17.4s. `relay.py` beats every 15s from the run loop.

**Those two windows are constants in [relay_lock.py](relay_lock.py), not env
knobs** — 15s to beat, 90s to consider the holder gone. They were knobs once and
nothing ever set them. The cross-namespace rule only works with a wide margin
between the two, so a value that breaks the mechanism is not one to expose in
[docker-compose.yml](docker-compose.yml) next to the log bounds, which *are*
policy worth reviewing. `RELAY_LOG_*` stays there; these do not.

**Two earlier lock bugs are still load-bearing.** A restarted container hands the
new relay the *same* PID (7, every time), so a pid-only lock made the fresh relay
read its own pid as a live holder and refuse to start — an endless restart loop,
observed before `start_ticks` was recorded. Comparing `/proc/<pid>/stat` field
22 fixes it *within* a namespace; it says nothing across one, which is the case
above. Both bugs were the same bug wearing different clothes: a pid treated as
an identity outside the place that issued it.

**Only the holder may delete the lock.** `release_lock()` removes the file only
if it still records our pid, so a dying process cannot delete the lock of the
relay that replaced it — the window where two relays would both believe they
own the run. `beat()` checks the same thing, so a relay that has lost the lock
cannot resurrect it and lock out the process that legitimately holds it now.

**`pkill` does not exist** in the Git Bash on this machine. To stop a stray
hand-launched relay:

```bash
python -c "import json;print(json.load(open('relay.lock'))['pid'])"
taskkill //F //T //PID <that number>
```

## Files

| file | role |
| --- | --- |
| `Dockerfile` | python3 + node/wrangler + tini, so `npx` never fetches at start |
| `docker-compose.yml` | the service: restart policy, mounts, env, healthcheck |
| `relay.env` | persisted environment (git-ignored; may hold `GRAFANA_AUTH`) |
| `relay.py` | entry point: stream decoding, tail supervision, the run loop |
| `metrics.py` | the counters, the payload they become, the VM push |
| `event_log.py` | bounded append-only `events.log` and its rotation policy |
| `relay_lock.py` | `relay.lock`: the holder record and whether it is still there |
| `health.py` | JSON health probe; exit code is the verdict |
| `provision_grafana.py` | datasource + dashboard, via the Grafana HTTP API |
| `state.json` | counter totals — a restart does not reset them |
| `events.log` | every captured event, one JSON object per line; rotated |
| `events.log.1`, `.2`, `.3` | retired generations, oldest last, bounded count |
| `tail.err` | `wrangler tail` stderr |
| `relay.lock` | holder identity: pid, process start time, hostname, heartbeat |

Env: `CF_WORKER`, `VM_URL`, `RELAY_WORKER_DIR`, `RELAY_INTERVAL`, `RELAY_STATE`,
`RELAY_LOG`, `RELAY_LOCK`, `RELAY_MAX_RESTARTS`, `RELAY_HEALTH_MAX_AGE`, `GRAFANA_URL`,
`GRAFANA_AUTH`, `RELAY_LOG_MAX_BYTES`, `RELAY_LOG_MAX_FILES`,
`RELAY_LOG_ROTATE_ATTEMPTS`, `RELAY_LOG_ROTATE_RETRY_WAIT`,
`RELAY_LOG_ROTATE_RETRY_EVERY`, `RELAY_LOG_ROTATE_WARN_EVERY`,
`RELAY_LOG_SWEEP_MAX`.
(`VM_URL` means a host URL in `relay.py` and a container-network name in
`provision_grafana.py` — see the parked item, they differ deliberately.)

## Traps that cost real time

**`wrangler tail --format json` emits PRETTY-PRINTED objects, not JSONL.** Line
splitting is useless, and a partial object sits at the end of the buffer. Do not
clear the buffer on the first `}` — a nested object closes long before the
top-level one does, and that silently discards every event.

**VictoriaMetrics `/api/v1/import` wants JSON-lines and answers 204 to a body it
cannot read.** Prometheus exposition was rejected with `cannot parse json line:
unexpected char` in the container log for every `Content-Type` tried. A 204 is
not evidence the data landed; query the series back.

**`os.kill(pid, 0)` is not a liveness probe on Windows.** CPython routes every
signal other than `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` to `OpenProcess` +
`TerminateProcess`, so signal 0 *terminates the target with exit code 0* — a
check that kills what it inspects. It also raises `SystemError` (not
`OSError`) for a PID it cannot open, so `except OSError` misses it and the
caller crashes instead of reclaiming a stale lock. The relay now uses
`GetExitCodeProcess` via ctypes on Windows and signal 0 elsewhere;
`health.py` imports that helper rather than keeping its own copy, so the reader
and the writer of `relay.lock` cannot disagree.

**`json.dump(..., indent=1)` on a dict another thread is mutating raises, and
`sort_keys=True` hides it.** The pre-fix relay had a poll thread calling
`save_state()` while the main loop inserted into the same dict. Measured on this
machine's CPython 3.11, `indent=1` without `sort_keys` raised
`RuntimeError: dictionary changed size during iteration` in **25/25** trials;
adding `sort_keys=True` dropped it to **0/25**. So the shipped code was one
kwarg from a crash — and the old poller caught only `(URLError, OSError)`,
neither of which `RuntimeError` is, so the exception would have killed the
thread outright: counters frozen, dashboard still green. The single-owner
design is 0/25 at *both* settings.

Two things kept this invisible in production: the counter dict holds only a
handful of real `outcome|status|method` combinations, so a *new* key — the only
thing that changes dict size — is rare; and `sorted(st["requests"].items())`
inside `build_payload()` never faulted in any trial.

## Limits

- **Log lines are not in the dashboard.** This Grafana has a Prometheus-type
  datasource only, and VictoriaMetrics has no Loki-compatible endpoint (it 400s
  on `/loki/api/v1/labels`). Raw events land in `events.log`.
- **`wrangler tail` drops events under concurrency — upstream of the relay.**
  This is the biggest caveat on the numbers. Measured against a control where
  raw `wrangler tail` ran with the relay out of the path entirely:
  120 requests at parallelism 4, paced 250ms → **raw tail captured 62**,
  relay captured **53** from the same run. Sequential traffic is exact
  (20/20). So the relay adds no loss of its own — `events.log` line count,
  `sum(requests)`, and `events_total` agree exactly on every run — but the
  dashboard undercounts concurrent bursts, and always did. Worth knowing before
  reading a rate() off it.
- **Only traffic while the relay runs is captured.** `wrangler tail` is live.
  Cloudflare retains the logs for its own window, but this path only sees them
  while attached: the `workers/observability/telemetry/query` REST endpoint
  returned 400 for every documented body shape tried, and Workers Logpush is not
  enabled on this account, so there is no backfill.
- **Traces are off.** `wrangler.toml` sets `observability.traces.enabled =
  false`. The previously deployed version had traces on, so deploying it as
  written turned them off.
- **Boot start is not proven here.** `restart: unless-stopped` is set and the
  crash-restart path was proven; an actual host reboot was not performed.

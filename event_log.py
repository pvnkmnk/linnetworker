"""events.log: an append-only JSON-lines sink with a bounded footprint.

Concerns that change together: the file, its size cap, its generation count, and
what happens when a rename is refused. None of that belongs to capturing events
or shipping metrics, so it is here rather than in relay.py.

The policy, and why each half is needed
---------------------------------------
Bounded on BOTH axes: a size cap per file, and a cap on retained generations.
Size alone lets a burst multiply files; count alone lets one quiet day produce a
file too big to grep. Worst case on disk is max_bytes * (1 + max_files): 16 MiB *
4 = 64 MiB by default. Under `restart: unless-stopped` an unbounded log is an
availability problem, not a cosmetic one.

Rotation renames, never truncates
---------------------------------
`os.replace` is rename(2), so a retired file keeps its inode and an open
descriptor -- `tail -f`, a grep, a log shipper -- reads it to EOF uninterrupted.
Truncating in place would hand that reader a file whose first bytes are now
DIFFERENT CONTENT, which is how a log gets silently corrupted. Files appear
oldest-last: events.log.2 -> events.log.1 -> events.log.

Losing the oldest generation is the only data loss here, and it is deliberate:
that deletion is what makes the bound a bound.

A blocked rename degrades loudly, not silently
----------------------------------------------
Rotation can lose a race against a reader holding the file open. Windows will
not rename a file with an open handle (WinError 32), and Docker Desktop's bind
mount propagates that lock into the container as Errno 13, so a host reader can
block the relay's own rename. Measured: the log grew to 268,860 bytes against a
40,000-byte cap. On plain Linux rename(2) ignores open handles.

In that state the sink keeps every event and says the cap is not in effect. A
cap that quietly stops capping is worse than no cap. The retry runs on a TIMER,
not per event: the degraded state lasts as long as somebody has the log open in
an editor, and retrying per event cost one rename syscall per captured event
(measured 19 attempts for 200 events). Now it is O(time), and closing the reader
still self-heals within retry_every without a restart.

Configuration is validated by validate_config() at startup, not at import: this
module is imported by the health probe, and a module that exits at import takes
health reporting down exactly when the configuration is wrong.
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))

LOG_PATH = os.environ.get("RELAY_LOG", os.path.join(HERE, "events.log"))
MAX_BYTES = int(os.environ.get("RELAY_LOG_MAX_BYTES", str(16 * 1024 * 1024)))
MAX_FILES = int(os.environ.get("RELAY_LOG_MAX_FILES", "3"))
# A blocked rename is retried on a timer; these bound the retry burst.
ROTATE_ATTEMPTS = int(os.environ.get("RELAY_LOG_ROTATE_ATTEMPTS", "3"))
ROTATE_RETRY_WAIT = float(os.environ.get("RELAY_LOG_ROTATE_RETRY_WAIT", "0.5"))
ROTATE_RETRY_EVERY = float(os.environ.get("RELAY_LOG_ROTATE_RETRY_EVERY", "30"))
# The refusal warning is rate-limited separately: once a minute is loud enough
# to notice and quiet enough to read.
ROTATE_WARN_EVERY = float(os.environ.get("RELAY_LOG_ROTATE_WARN_EVERY", "60"))
# How far up the generation numbers sweep_orphan_generations() looks. Bounded so
# one stray events.log.<huge> cannot make it loop or list a huge directory.
SWEEP_MAX_GENERATIONS = int(os.environ.get("RELAY_LOG_SWEEP_MAX", "64"))


def say(msg):
    """Report on this module's own behaviour, to the relay's stdout.

    Unbuffered for the same reason relay.say is: the relay is normally
    backgrounded, and a buffered line in a redirected stream looks exactly like a
    relay that never rotated.
    """
    print("relay: %s" % msg, flush=True)


class ConfigError(Exception):
    """A log policy that cannot bound anything. Raised, never exited here."""


def validate_config():
    """Raise ConfigError if this policy cannot bound the file.

    MAX_FILES must be at least 1, which also makes "both caps zero"
    unrepresentable. Zero once meant "delete the live log on rotation" -- no
    caller wanted it, nothing tested it, and unlinking the file someone is
    reading is a sharper edge than keeping three generations. MAX_BYTES=0 on its
    own is still allowed: that disables the size cap and keeps the count cap.
    """
    if MAX_FILES < 1:
        raise ConfigError(
            "RELAY_LOG_MAX_FILES must be at least 1 (got %d): rotation renames "
            "the live log into a retained generation, so there is nowhere for it "
            "to go. The default is 3." % MAX_FILES)


class EventLog:
    """Append-only JSON-lines sink. Owns its handle, byte count and retry clock.

    One instance per run, used only by the thread that owns the event stream --
    which is why there is no lock here and no way to lose an event to one.
    """

    def __init__(self):
        self.fh = open(LOG_PATH, "a", encoding="utf-8")
        self.written = self.fh.tell()
        # When the next rotation may be ATTEMPTED. Zeroed on success; set into
        # the future on a refusal, which is what turns the retry from per-event
        # work into per-interval work.
        self.retry_at = 0.0
        # Per-instance, not a class attribute: this is mutable state belonging to
        # THIS log, and a class attribute reads like a constant while silently
        # being one shared by every instance.
        self.warned_at = 0.0

    def write(self, obj):
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self.fh.write(line)
        self.fh.flush()
        self.written += len(line.encode("utf-8"))
        if MAX_BYTES > 0 and self.written >= MAX_BYTES \
                and time.time() >= self.retry_at:
            self.rotate()

    def rotate(self):
        """Retire the live log and start a fresh one.

        The old handle is closed first and the line that tripped the cap is
        already flushed and counted, so the cut is never mid-line and never drops
        the event that caused it. A refused rename keeps appending to the same
        file: rotation is housekeeping and must never cost us an event, but it
        must never be silent either.
        """
        try:
            self.fh.flush()
            self.fh.close()
        except (OSError, ValueError):
            pass
        rotated = self._rotate()
        self.fh = open(LOG_PATH, "a", encoding="utf-8")
        self.written = self.fh.tell()
        if rotated:
            say("rotated %s -> %s (cap %d bytes)"
                % (LOG_PATH, generation(1), MAX_BYTES))
        else:
            # Keep counting from the real size so the cap stays tripped, and
            # hold off the next attempt until the interval has passed.
            self.written = max(self.written, MAX_BYTES)
            self.retry_at = time.time() + ROTATE_RETRY_EVERY

    def _rotate(self):
        """True if the rename worked. Never raises: a refusal is a degraded
        state to report, not a reason to drop an event or kill the relay."""
        last = None
        for attempt in range(ROTATE_ATTEMPTS):
            try:
                # The shift moves generations 1..MAX_FILES-1 up by one and
                # retires the live log into slot 1, so it can only ever produce
                # slots 1..MAX_FILES. Sweeping first clears anything ALREADY
                # past the cap -- the state left behind when an operator LOWERS
                # MAX_FILES, where the orphaned file is not in the shift path
                # and would otherwise survive forever. Sweeping before the
                # shift rather than after keeps the at-rest count at exactly
                # 1 + MAX_FILES; sweeping after would delete the slot the shift
                # had just filled.
                self._sweep_orphans()
                for n in range(MAX_FILES - 1, 0, -1):
                    if os.path.exists(generation(n)):
                        os.replace(generation(n), generation(n + 1))
                os.replace(LOG_PATH, generation(1))
                return True
            except OSError as exc:
                last = exc
                if attempt + 1 < ROTATE_ATTEMPTS:
                    time.sleep(ROTATE_RETRY_WAIT)
        self._warn_refused(ROTATE_ATTEMPTS, last)
        return False

    def _sweep_orphans(self):
        """Delete generations above MAX_FILES. Returns how many it removed.

        Scans upward to a bounded ceiling rather than globbing, so one stray
        events.log.<huge> cannot become a huge listing. The trade-off is
        explicit: an orphan beyond the ceiling is NOT found.
        """
        removed = 0
        lo, hi = MAX_FILES + 1, MAX_FILES + SWEEP_MAX_GENERATIONS
        for n in range(lo, hi):
            try:
                os.remove(generation(n))
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                say("could not remove orphaned %s: %s" % (generation(n), exc))
                return removed
        # Only speak when something was actually reclaimed. This runs on every
        # rotation, and a line reporting "removed 0" every time is noise that
        # trains an operator to skip the log -- including the line that would
        # have mattered.
        if removed:
            say("reclaimed %d log generation(s) above the cap of %d "
                "(scanned up to %d; anything beyond is NOT scanned -- "
                "raise RELAY_LOG_SWEEP_MAX if you see one)"
                % (removed, MAX_FILES, hi - 1))
        return removed

    def _warn_refused(self, attempts, exc):
        """Rate-limited. An un-rotatable file would otherwise print one warning
        per event (measured: 252 lines for 300 events) and bury everything else.
        Stored on the instance rather than in a module global so it is scoped to
        the thing it describes."""
        now = time.time()
        if now - self.warned_at < ROTATE_WARN_EVERY:
            return
        self.warned_at = now
        say("WARNING could not rotate %s after %d attempts (%s); "
            "the size cap is NOT in effect and the file WILL keep growing "
            "until whatever holds it open closes. Events are still being "
            "captured -- nothing is lost, the bound is just not enforced. "
            "A host-side reader on a Docker Desktop bind mount is the usual "
            "cause." % (LOG_PATH, attempts, exc))

    def close(self):
        try:
            if not self.fh.closed:
                self.fh.flush()
                self.fh.close()
        except (OSError, ValueError):
            # ValueError means a refused rotation already closed and replaced the
            # handle; closing twice must not raise on the way out of a finally
            # block, or it would mask the real shutdown path.
            pass


def generation(n):
    """Path of the n-th retired generation of events.log (0 = the live file)."""
    if n == 0:
        return LOG_PATH
    return "%s.%d" % (LOG_PATH, n)
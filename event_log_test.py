"""Tests for event_log. Run: python -m unittest event_log_test -v

(`python3` inside the container. On Windows the `python3` on PATH is the
Microsoft Store alias stub and exits 49 without running anything, so `python`
is the form that works on the host.)

Standard library only, deliberately, and in the same idiom as relay_lock_test:
the image is python3-alpine with nothing installed but the relay, so a suite
that needed pytest would not run where the code runs. The fixtures are the same
shape too -- a temporary state directory and patched module globals -- because
event_log reads its whole policy from module globals at call time, exactly as
relay_lock reads LOCK_PATH. They are written out again rather than imported
from the lock suite so each file runs standalone with no cross-import between
test modules.

The weight is on the three behaviours that fail quietly rather than loudly:

  * a cap that stops capping because the rename was refused, which is the
    failure this module exists to make loud;
  * the orphan sweep, which only matters in the state an operator leaves
    behind by LOWERING the cap, so no ordinary run ever reaches it;
  * the refusal path costing an event, which it must never do.

Where a behaviour genuinely cannot be exercised on the running OS, the test
skips and says which OS and why, rather than passing vacuously.
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import event_log


class LogTestCase(unittest.TestCase):
    """A private log directory and policy per test, both removed afterwards.

    The policy is patched onto the module rather than passed in, because
    event_log reads MAX_BYTES, MAX_FILES and friends from module globals inside
    each call. That is the same trick relay_lock_test uses for LOCK_PATH, and
    it means no test can reach the real events.log in the working tree.
    """

    # Defaults chosen so rotation is quick to provoke and rotation *failures*
    # are instant: a refused rename sleeps ROTATE_RETRY_WAIT between attempts,
    # and a test that trips that twenty times would spend the time waiting.
    DEFAULTS = {"MAX_BYTES": 40, "MAX_FILES": 3, "ROTATE_ATTEMPTS": 1,
                "ROTATE_RETRY_WAIT": 0.0, "ROTATE_RETRY_EVERY": 300.0,
                "ROTATE_WARN_EVERY": 0.0, "SWEEP_MAX_GENERATIONS": 16}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="eventlog-test-")
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "events.log")
        self.use(LOG_PATH=self.path, **self.DEFAULTS)

    def use(self, **policy):
        """Set module globals for the duration of this test."""
        for name, value in policy.items():
            original = getattr(event_log, name)
            setattr(event_log, name, value)
            self.addCleanup(setattr, event_log, name, original)

    def open_log(self):
        """An EventLog on the current LOG_PATH, closed on teardown."""
        log = event_log.EventLog()
        self.addCleanup(log.close)
        return log

    def lines(self, path=None):
        """The file's events, parsed. Empty file reads as []."""
        path = path or self.path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def fill_until_rotated(self, log, marker, limit=200):
        """Write until a rotation has retired the live file, or fail.

        Spotted by the live file being empty immediately after a write, which
        is what a rotation leaves behind. Watching a marker reach generation 1
        does not work: the marker write can trip the cap by itself, and the very
        next rotation then shifts that marker up out of generation 1, so it
        would be reported as never having rotated. A refused rotation leaves
        the live file populated, so this cannot fire falsely.
        """
        log.write(marker)
        for _ in range(limit):
            log.write({"pad": "x" * 60})
            if not self.lines():
                return
        self.fail("the size cap never retired the log within %d writes"
                  % limit)

    def write_n(self, log, count, start=0):
        """Write `count` events, returning what was written."""
        written = []
        for n in range(start, start + count):
            obj = {"n": n}
            written.append(obj)
            log.write(obj)
        return written

    def held(self, **policy):
        """Context manager making every rename fail, as a reader would."""
        self.use(ROTATE_ATTEMPTS=1, **policy)
        # event_log.os IS the os module, so this patches os.replace globally
        # for the duration. Nothing else renames inside these tests.
        return mock.patch.object(event_log.os, "replace",
                                 side_effect=PermissionError("held open"))


class WriteTests(LogTestCase):
    def test_write_appends_one_json_object_per_line(self):
        log = self.open_log()
        log.write({"a": 1})
        log.write({"b": "two"})
        self.assertEqual(self.lines(), [{"a": 1}, {"b": "two"}])

    def test_size_cap_counts_bytes_not_characters(self):
        # A character count under-counts every non-ASCII event, so the cap
        # would admit 2x or 3x the bytes it promises on translated metadata.
        self.use(MAX_BYTES=100_000)
        log = self.open_log()
        log.write({"artist": "Bülow", "title": "Grüße"})
        with open(self.path, "rb") as fh:
            raw = fh.read()
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        # The counter charges "\n" as one byte, but the handle is in text mode
        # and Windows writes "\r\n", so on that platform the file is one byte
        # per line larger than what the cap was told about. Small next to a
        # 16 MiB cap, but it is why the equality below is not a plain ==.
        self.assertEqual(log.written, len(raw) - raw.count(b"\r\n"))
        self.assertLess(len(text), log.written,
                        "non-ASCII must be counted in bytes, not characters")

    def test_no_rotation_below_the_cap(self):
        self.use(MAX_BYTES=100_000)
        log = self.open_log()
        log.write({"a": 1})
        self.assertFalse(os.path.exists(event_log.generation(1)))

    def test_max_bytes_zero_disables_the_size_cap(self):
        # Legal on its own: turns off the size cap, keeps the count cap.
        self.use(MAX_BYTES=0)
        log = self.open_log()
        for n in range(50):
            log.write({"pad": "x" * 200})
        self.assertFalse(os.path.exists(event_log.generation(1)))
        self.assertEqual(len(self.lines()), 50)


class RotationTests(LogTestCase):
    def test_rotation_retires_the_file_and_starts_a_fresh_one(self):
        self.use(MAX_BYTES=64)
        log = self.open_log()
        self.fill_until_rotated(log, {"first": True})
        retired = self.lines(event_log.generation(1))
        self.assertTrue(retired, "the retired generation should hold the run")
        self.assertEqual(self.lines(), [], "the live log should start empty")
        self.assertEqual(log.written, 0)
        log.write({"n": 999})
        self.assertEqual(self.lines(), [{"n": 999}],
                         "writes after rotation land in the new live log")

    def test_the_event_that_tripped_the_cap_is_not_lost(self):
        # The line is written, flushed and counted BEFORE rotate() runs, so the
        # cut is never mid-line and never drops the event that caused it.
        self.use(MAX_BYTES=64)
        log = self.open_log()
        written = []
        tripped = None
        for n in range(30):
            obj = {"n": n}
            written.append(obj)
            log.write(obj)
            if os.path.exists(event_log.generation(1)):
                tripped = obj
                break
        self.assertIsNotNone(tripped, "the size cap never tripped")
        retired = self.lines(event_log.generation(1))
        self.assertEqual(retired[-1], tripped,
                         "the tripping line must be the last retired one")
        self.assertEqual(retired + self.lines(), written)

    def test_generations_shift_oldest_last(self):
        self.use(MAX_BYTES=64, MAX_FILES=3)
        log = self.open_log()
        self.fill_until_rotated(log, {"round": 1})
        first = self.lines(event_log.generation(1))
        self.fill_until_rotated(log, {"round": 2})
        self.assertEqual(first[0], {"round": 1})
        self.assertEqual(self.lines(event_log.generation(2)), first,
                         "the previous generation must shift up, not vanish")
        self.assertEqual(self.lines(event_log.generation(1))[0], {"round": 2})

    def test_at_rest_the_count_is_one_plus_max_files(self):
        # The whole point of the bound: 1 live + MAX_FILES retired, whatever
        # state the directory was in beforehand.
        self.use(MAX_FILES=2, MAX_BYTES=64, SWEEP_MAX_GENERATIONS=32)
        for n in (1, 2, 3, 4, 5):
            with open(event_log.generation(n), "w") as fh:
                fh.write("left by an older, larger cap: %d\n" % n)
        log = self.open_log()
        with mock.patch.object(event_log, "say"):
            self.fill_until_rotated(log, {"fresh": True})
        present = [n for n in range(0, 8)
                   if os.path.exists(event_log.generation(n))]
        self.assertEqual(present, [0, 1, 2])

    def test_rotation_is_announced(self):
        spoken = []
        log = self.open_log()
        with mock.patch.object(event_log, "say", spoken.append):
            self.fill_until_rotated(log, {"pad": "y" * 60})
        self.assertTrue(any("rotated" in m for m in spoken),
                        "a rotation that nobody hears about looks like a relay "
                        "that never rotates")


class SweepTests(LogTestCase):
    def test_sweep_removes_generations_above_the_cap(self):
        self.use(MAX_FILES=2, SWEEP_MAX_GENERATIONS=8)
        log = self.open_log()
        for n in (3, 4, 5):
            with open(event_log.generation(n), "w") as fh:
                fh.write("orphan\n")
        self.assertEqual(log._sweep_orphans(), 3)
        for n in (3, 4, 5):
            self.assertFalse(os.path.exists(event_log.generation(n)))

    def test_sweep_stops_at_its_ceiling(self):
        # The scan is bounded, and an orphan past the ceiling is deliberately
        # NOT found. Pinning the exact boundary stops that from becoming an
        # accident of the range arithmetic.
        self.use(MAX_FILES=2, SWEEP_MAX_GENERATIONS=4)
        log = self.open_log()
        for n in (3, 4, 5):
            with open(event_log.generation(n), "w") as fh:
                fh.write("orphan\n")
        beyond = event_log.generation(2 + 4)
        with open(beyond, "w") as fh:
            fh.write("too far to be scanned\n")
        self.assertEqual(log._sweep_orphans(), 3)
        self.assertTrue(os.path.exists(beyond))

    def test_sweep_is_quiet_when_nothing_is_reclaimed(self):
        # It runs on every rotation. A line reporting "removed 0" every time
        # is noise that trains an operator to skip the log.
        self.use(MAX_FILES=2)
        log = self.open_log()
        spoken = []
        with mock.patch.object(event_log, "say", spoken.append):
            self.assertEqual(log._sweep_orphans(), 0)
        self.assertEqual(spoken, [])

    def test_sweep_speaks_only_about_what_it_reclaimed(self):
        self.use(MAX_FILES=2, SWEEP_MAX_GENERATIONS=8)
        log = self.open_log()
        for n in (3, 4):
            with open(event_log.generation(n), "w") as fh:
                fh.write("orphan\n")
        spoken = []
        with mock.patch.object(event_log, "say", spoken.append):
            self.assertEqual(log._sweep_orphans(), 2)
        self.assertEqual(len(spoken), 1)
        self.assertIn("2", spoken[0])

    def test_rotation_sweeps_orphans_it_would_never_shift(self):
        # A generation above the cap is not in the shift path, so only the
        # sweep can remove it -- and only when it runs as part of a rotation.
        self.use(MAX_FILES=2, MAX_BYTES=64, SWEEP_MAX_GENERATIONS=8)
        orphan = event_log.generation(4)
        with open(orphan, "w") as fh:
            fh.write("orphan\n")
        log = self.open_log()
        self.fill_until_rotated(log, {"pad": "z" * 60})
        self.assertFalse(os.path.exists(orphan))


class RefusalTests(LogTestCase):
    """A refused rename, forced directly.

    Patching os.replace reaches the same code path a real held handle reaches,
    but on every platform, which is what makes the degradation assertions
    testable in CI on Linux -- where rename(2) would succeed and the path
    would be unreachable. OpenHandleTests covers the real thing where it can
    happen; this covers it everywhere.
    """

    def test_refused_rename_degrades_instead_of_raising(self):
        log = self.open_log()
        with self.held():
            for n in range(20):
                log.write({"n": n})        # must not raise
        self.assertFalse(os.path.exists(event_log.generation(1)))
        self.assertGreaterEqual(log.written, event_log.MAX_BYTES,
                                "the cap must stay tripped so it keeps trying")

    def test_refusal_costs_no_events(self):
        log = self.open_log()
        with self.held():
            written = self.write_n(log, 20)
        self.assertEqual(self.lines(), written,
                         "rotation is housekeeping; it must never drop an event")

    def test_refusal_is_reported_once_not_once_per_event(self):
        # Measured against the old behaviour: 252 warning lines for 300 events
        # buries everything else in the log.
        log = self.open_log()
        spoken = []
        with self.held(ROTATE_RETRY_EVERY=0.0, ROTATE_WARN_EVERY=3600.0), \
                mock.patch.object(event_log, "say", spoken.append):
            self.write_n(log, 200)
        warnings = [m for m in spoken if "WARNING" in m]
        self.assertEqual(len(warnings), 1)

    def test_the_warning_appears_when_it_is_not_rate_limited(self):
        # Guards the test above from passing vacuously: the limiter is what
        # collapses 200 attempts to one line, not the retry timer.
        log = self.open_log()
        spoken = []
        with self.held(ROTATE_RETRY_EVERY=0.0, ROTATE_WARN_EVERY=0.0), \
                mock.patch.object(event_log, "say", spoken.append):
            self.write_n(log, 200)
        warnings = [m for m in spoken if "WARNING" in m]
        self.assertGreater(len(warnings), 1)

    def test_retry_is_deferred_to_the_timer_not_per_event(self):
        # The measured regression: one rename syscall per captured event,
        # 19 attempts for 200 events, for as long as the reader stays open.
        log = self.open_log()
        with self.held(ROTATE_RETRY_EVERY=300.0) as rename:
            self.write_n(log, 50)
            first = rename.call_count
            self.write_n(log, 50, start=50)
            self.assertEqual(rename.call_count, first,
                             "rotation must not retry per event")
            self.assertGreater(log.retry_at, time.time())

    def test_retry_happens_once_the_interval_has_passed(self):
        log = self.open_log()
        with self.held(ROTATE_RETRY_EVERY=300.0) as rename:
            self.write_n(log, 50)
            first = rename.call_count
            log.retry_at = 0.0            # as if ROTATE_RETRY_EVERY had elapsed
            log.write({"n": "later"})
            self.assertGreater(rename.call_count, first)

    def test_a_later_successful_rotation_clears_the_degraded_state(self):
        log = self.open_log()
        with self.held(ROTATE_RETRY_EVERY=300.0):
            self.write_n(log, 20)
        self.assertFalse(os.path.exists(event_log.generation(1)))
        self.assertGreater(log.retry_at, time.time())
        # The interval is the only thing gating the retry, so once it has
        # elapsed the rename goes through and the cap starts holding again.
        log.retry_at = 0.0
        log.write({"n": "after the reader closed"})
        self.assertTrue(os.path.exists(event_log.generation(1)))
        self.assertLess(log.written, event_log.MAX_BYTES,
                        "a recovered log is bounded again")


class OpenHandleTests(LogTestCase):
    """Rotation losing a real race against a reader holding the file open."""

    def setUp(self):
        super().setUp()
        if os.name != "nt":
            self.skipTest(
                "rename(2) ignores open handles on POSIX, so a refused rename "
                "is unreachable here -- verified that os.replace succeeds with "
                "a reader open on Linux. RefusalTests covers the same "
                "degradation on every platform by refusing the rename directly.")

    def test_a_reader_holding_the_log_degrades_rotation(self):
        self.use(ROTATE_ATTEMPTS=1, ROTATE_RETRY_EVERY=0.0)
        log = self.open_log()
        reader = open(self.path, "r", encoding="utf-8")
        self.addCleanup(reader.close)
        written = self.write_n(log, 20)
        self.assertFalse(os.path.exists(event_log.generation(1)))
        self.assertEqual(self.lines(), written)
        self.assertGreaterEqual(log.written, event_log.MAX_BYTES)

    def test_closing_the_reader_self_heals_without_a_restart(self):
        self.use(ROTATE_ATTEMPTS=1, ROTATE_RETRY_EVERY=0.0)
        log = self.open_log()
        reader = open(self.path, "r", encoding="utf-8")
        self.addCleanup(reader.close)
        self.write_n(log, 20)
        self.assertFalse(os.path.exists(event_log.generation(1)))
        reader.close()
        log.write({"n": "after the reader closed"})
        self.assertTrue(os.path.exists(event_log.generation(1)),
                        "closing the reader must recover rotation on its own")


class ConfigTests(LogTestCase):
    def test_max_files_below_one_is_rejected(self):
        self.use(MAX_FILES=0)
        with self.assertRaises(event_log.ConfigError):
            event_log.validate_config()

    def test_max_bytes_zero_is_allowed(self):
        self.use(MAX_FILES=1, MAX_BYTES=0)
        event_log.validate_config()        # must not raise


class CloseTests(LogTestCase):
    def test_close_is_idempotent(self):
        log = event_log.EventLog()
        log.close()
        log.close()                        # must not raise

    def test_close_after_a_refused_rotation_does_not_raise(self):
        # rotate() already closed and replaced the handle; closing it again
        # must not raise on the way out of a finally block and mask shutdown.
        log = self.open_log()
        with self.held(ROTATE_RETRY_EVERY=0.0):
            self.write_n(log, 20)
        log.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

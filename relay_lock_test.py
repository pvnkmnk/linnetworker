"""Tests for relay_lock. Run: python -m unittest relay_lock_test -v

(`python3` inside the container. On Windows the `python3` on PATH is the
Microsoft Store alias stub and exits 49 without running anything, so `python`
is the form that works on the host.)

Standard library only, deliberately. The image is python3-alpine with nothing
installed but the relay, so a suite that needed pytest would not run where the
code runs. unittest needs no dependency and no config.

What is under test here is the module's refusals, not its happy path. A claim
that succeeds is easy; the claims below are the ones that can quietly corrupt a
dashboard -- reclaiming a lock that is genuinely held lets a second relay write
its counters over the first's, and both then report healthy. So the assertions
lean hard on "must refuse" and "must not reclaim".

Isolation: relay_lock reads LOCK_PATH from the environment at import time, but
every function resolves the module global at call time, so each test points that
global at its own temporary file. Nothing here can touch the real relay.lock.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import relay_lock


def dead_pid():
    """A pid that has certainly exited, without guessing at one.

    Guessing is not safe: a hardcoded number can be a live process on the machine
    running the suite, and a "dead holder" fixture that is secretly alive turns
    the reclaim tests into tests of nothing at all. Start a process, reap it,
    then confirm the module agrees it is gone.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    if relay_lock.pid_alive(proc.pid):
        raise unittest.SkipTest("pid %d was recycled before the test could use "
                                "it; rerun" % proc.pid)
    return proc.pid


def now_ms():
    return int(time.time() * 1000)


class LockTestCase(unittest.TestCase):
    """A private lock path per test, removed afterwards."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="relaylock-test-")
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "relay.lock")
        self.use_lock(self.path)

    def use_lock(self, path):
        """Point the module at `path` for the duration of this test."""
        original = relay_lock.LOCK_PATH
        relay_lock.LOCK_PATH = path
        self.addCleanup(setattr, relay_lock, "LOCK_PATH", original)
        return path

    def write_lock(self, record=None, raw=None):
        """Put a lock file in place, either as a record or as raw bytes."""
        with open(self.path, "wb") as fh:
            if raw is not None:
                fh.write(raw)
            else:
                fh.write(json.dumps(record, sort_keys=True).encode("utf-8"))

    def write_live_holder(self, host=None, heartbeat_age=0.0, start_ticks="live"):
        """A lock naming a process that is definitely running: this one."""
        record = {"pid": os.getpid(),
                  "host": relay_lock.THIS_HOST if host is None else host,
                  "heartbeat_ms": now_ms() - int(heartbeat_age * 1000)}
        record["start_ticks"] = (relay_lock.proc_start_ticks(os.getpid())
                                 if start_ticks == "live" else start_ticks)
        self.write_lock(record)
        return record


class ReadTests(LockTestCase):
    def test_absent_file_is_absent_not_unreadable(self):
        # The one distinction the whole module rests on: no file means take it,
        # and a file we cannot read means somebody may hold it.
        self.assertEqual(relay_lock._lock_view(), (None, False))

    def test_empty_file_counts_as_present(self):
        # What a reader sees between a claim's create and its write. Reading it
        # as "no lock" is how a newcomer steals a lock taken microseconds ago.
        open(self.path, "wb").close()
        self.assertEqual(relay_lock._lock_view(), (None, True))

    def test_garbage_counts_as_present(self):
        self.write_lock(raw=b"not json at all")
        self.assertEqual(relay_lock._lock_view(), (None, True))

    def test_record_without_pid_is_present_and_unnamed(self):
        self.write_lock(record={"host": "elsewhere"})
        self.assertEqual(relay_lock._lock_view(), (None, True))

    def test_non_numeric_pid_is_present_and_unnamed(self):
        self.write_lock(record={"pid": "not-a-number"})
        self.assertEqual(relay_lock._lock_view(), (None, True))

    def test_full_record_is_parsed(self):
        record = {"pid": 4242, "start_ticks": 99, "host": "elsewhere",
                  "heartbeat_ms": 1234}
        self.write_lock(record)
        entry, present = relay_lock._lock_view()
        self.assertTrue(present)
        self.assertEqual(entry, record)

    def test_legacy_bare_pid_is_understood(self):
        self.write_lock(raw=b"4242")
        entry, present = relay_lock._lock_view()
        self.assertTrue(present)
        self.assertEqual(entry["pid"], 4242)
        self.assertIsNone(entry["host"])

    def test_read_lock_collapses_absent_and_unreadable(self):
        # Documented divergence: health.py may treat these alike, acquire_lock
        # may not, which is why _lock_view exists at all.
        self.assertIsNone(relay_lock.read_lock())
        open(self.path, "wb").close()
        self.assertIsNone(relay_lock.read_lock())


class HeartbeatTests(LockTestCase):
    def test_missing_heartbeat_is_not_stale(self):
        # An absent signal is not a negative one. Treating it as stale would let
        # a newcomer steal a lock it cannot see.
        self.assertFalse(relay_lock.heartbeat_is_stale({"heartbeat_ms": None}))

    def test_fresh_heartbeat_is_not_stale(self):
        self.assertFalse(relay_lock.heartbeat_is_stale(
            {"heartbeat_ms": now_ms()}))

    def test_lapsed_heartbeat_is_stale(self):
        self.assertTrue(relay_lock.heartbeat_is_stale(
            {"heartbeat_ms": now_ms()
             - int((relay_lock.HEARTBEAT_STALE + 1) * 1000)}))


class AssessTests(LockTestCase):
    def test_no_entry_is_not_held(self):
        self.assertEqual(relay_lock.assess(None), (False, True))

    def test_our_own_live_process_is_held_and_verifiable(self):
        self.write_live_holder()
        entry, _ = relay_lock._lock_view()
        self.assertEqual(relay_lock.assess(entry), (True, True))

    def test_dead_pid_on_this_host_is_reclaimable(self):
        self.write_lock({"pid": dead_pid(), "host": relay_lock.THIS_HOST,
                         "start_ticks": 1})
        entry, _ = relay_lock._lock_view()
        self.assertEqual(relay_lock.assess(entry), (False, True))

    def test_recycled_pid_is_not_the_holder(self):
        # A live pid whose start time disagrees is a different process wearing
        # the old number. A tick value nothing can produce is portable here:
        # on Linux the real value cannot equal it, and on Windows proc_start_
        # ticks returns None, which also cannot equal it.
        self.write_live_holder(start_ticks=999999999)
        entry, _ = relay_lock._lock_view()
        self.assertEqual(relay_lock.assess(entry), (False, True))

    def test_other_namespace_fresh_heartbeat_is_held_but_unverifiable(self):
        self.write_live_holder(host="some-other-container")
        entry, _ = relay_lock._lock_view()
        self.assertEqual(relay_lock.assess(entry), (True, False))

    def test_other_namespace_lapsed_heartbeat_is_reclaimable(self):
        self.write_live_holder(host="some-other-container",
                               heartbeat_age=relay_lock.HEARTBEAT_STALE + 1)
        entry, _ = relay_lock._lock_view()
        self.assertEqual(relay_lock.assess(entry), (False, False))

    def test_other_namespace_without_heartbeat_is_still_held(self):
        # Refusing on doubt, in the direction that cannot lose a dashboard.
        self.write_lock({"pid": 4242, "host": "some-other-container"})
        entry, _ = relay_lock._lock_view()
        self.assertEqual(relay_lock.assess(entry), (True, False))

    def test_legacy_lock_is_treated_as_this_host(self):
        self.write_lock(raw=str(dead_pid()).encode())
        entry, _ = relay_lock._lock_view()
        self.assertTrue(relay_lock._same_namespace(entry["host"]))
        self.assertEqual(relay_lock.assess(entry), (False, True))


class AcquireTests(LockTestCase):
    def test_claims_when_nothing_is_held(self):
        relay_lock.acquire_lock()
        entry, present = relay_lock._lock_view()
        self.assertTrue(present)
        self.assertEqual(entry["pid"], os.getpid())
        self.assertEqual(entry["host"], relay_lock.THIS_HOST)
        # None on Windows, where there is no /proc to read; comparing against
        # the module's own answer keeps the assertion true on both platforms
        # without pretending the value is always available.
        self.assertEqual(entry["start_ticks"],
                         relay_lock.proc_start_ticks(os.getpid()))

    def test_claim_leaves_no_temp_behind(self):
        relay_lock.acquire_lock()
        leftovers = [n for n in os.listdir(self.dir.name)
                     if ".claim." in n]
        self.assertEqual(leftovers, [])

    def test_refuses_a_live_holder_and_names_it(self):
        self.write_live_holder()
        with self.assertRaises(relay_lock.LockHeld) as ctx:
            relay_lock.acquire_lock()
        exc = ctx.exception
        self.assertEqual(exc.pid, os.getpid())
        self.assertTrue(exc.verifiable)
        self.assertFalse(exc.unclaimable)
        self.assertIn(str(os.getpid()), str(exc))

    def test_refuses_an_unverifiable_holder_and_says_why(self):
        self.write_live_holder(host="some-other-container")
        with self.assertRaises(relay_lock.LockHeld) as ctx:
            relay_lock.acquire_lock()
        exc = ctx.exception
        self.assertFalse(exc.verifiable)
        self.assertIn("namespace", str(exc))

    def test_refuses_a_corrupt_lock_without_inventing_a_holder(self):
        self.write_lock(raw=b"{ truncated")
        with self.assertRaises(relay_lock.LockHeld) as ctx:
            relay_lock.acquire_lock()
        exc = ctx.exception
        self.assertIsNone(exc.pid)
        self.assertFalse(exc.unclaimable)
        self.assertIn("no readable identity", str(exc))

    def test_refuses_when_the_claim_itself_fails(self):
        # No state directory at all: the claim cannot be made, so there is no
        # holder to report and the operator needs a filesystem fact instead.
        self.use_lock(os.path.join(self.dir.name, "absent", "relay.lock"))
        with self.assertRaises(relay_lock.LockHeld) as ctx:
            relay_lock.acquire_lock()
        exc = ctx.exception
        self.assertTrue(exc.unclaimable)
        self.assertIsNone(exc.pid)
        self.assertIn("could not claim", str(exc))
        self.assertIn("hard links", str(exc))

    def test_reclaims_a_dead_holder(self):
        gone = dead_pid()
        self.write_lock({"pid": gone, "host": relay_lock.THIS_HOST,
                         "start_ticks": 1})
        relay_lock.acquire_lock()
        entry, _ = relay_lock._lock_view()
        self.assertEqual(entry["pid"], os.getpid())

    def test_reclaims_a_lapsed_holder_elsewhere(self):
        self.write_live_holder(host="some-other-container",
                               heartbeat_age=relay_lock.HEARTBEAT_STALE + 1)
        relay_lock.acquire_lock()
        entry, _ = relay_lock._lock_view()
        self.assertEqual(entry["host"], relay_lock.THIS_HOST)
        self.assertEqual(entry["pid"], os.getpid())

    def test_does_not_reclaim_the_lock_it_just_took(self):
        # The loser of a race must see the winner, not an entry that no longer
        # describes anything.
        relay_lock.acquire_lock()
        winner, _ = relay_lock._lock_view()
        held, verifiable = relay_lock.assess(winner)
        self.assertTrue(held)
        self.assertTrue(verifiable)


class BeatTests(LockTestCase):
    def test_beats_refresh_our_own_lock(self):
        relay_lock.acquire_lock()
        before, _ = relay_lock._lock_view()
        time.sleep(0.01)
        relay_lock.beat()
        after, _ = relay_lock._lock_view()
        self.assertEqual(after["pid"], before["pid"])
        self.assertEqual(after["start_ticks"], before["start_ticks"])
        self.assertGreater(after["heartbeat_ms"], before["heartbeat_ms"])

    def test_does_not_resurrect_a_dead_holder(self):
        # A relay that has lost the lock must not write it back and lock out
        # the process that legitimately holds the run now.
        record = {"pid": dead_pid(), "host": relay_lock.THIS_HOST,
                  "start_ticks": 1, "heartbeat_ms": 1}
        self.write_lock(record)
        relay_lock.beat()
        entry, present = relay_lock._lock_view()
        self.assertTrue(present)
        self.assertEqual(entry, record)

    def test_does_not_write_into_another_namespace(self):
        record = {"pid": os.getpid(), "host": "some-other-container",
                  "start_ticks": None, "heartbeat_ms": 1}
        self.write_lock(record)
        relay_lock.beat()
        entry, _ = relay_lock._lock_view()
        self.assertEqual(entry["heartbeat_ms"], 1)

    def test_no_lock_is_not_an_error(self):
        relay_lock.beat()              # must not raise

    def test_beat_leaves_no_temp_behind(self):
        relay_lock.acquire_lock()
        relay_lock.beat()
        leftovers = [n for n in os.listdir(self.dir.name)
                     if ".claim." in n or n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class ReleaseTests(LockTestCase):
    def test_releases_our_own_lock(self):
        relay_lock.acquire_lock()
        self.assertIsNone(relay_lock.release_lock())
        self.assertEqual(relay_lock._lock_view(), (None, False))

    def test_leaves_a_lock_belonging_to_someone_else(self):
        # The window where two relays could both believe they hold the run.
        record = {"pid": dead_pid(), "host": relay_lock.THIS_HOST,
                  "start_ticks": 1, "heartbeat_ms": None}
        self.write_lock(record)
        self.assertIsNone(relay_lock.release_lock())
        entry, present = relay_lock._lock_view()
        self.assertTrue(present)
        self.assertEqual(entry, record)

    def test_missing_lock_is_not_an_error(self):
        self.assertIsNone(relay_lock.release_lock())

    def test_unlink_failure_is_returned_not_raised(self):
        # relay.py calls this from a finally; raising here would mask the
        # shutdown path that called it.
        relay_lock.acquire_lock()
        with mock.patch("os.remove", side_effect=PermissionError("busy")):
            result = relay_lock.release_lock()
        self.assertIsInstance(result, OSError)


class DropStaleTests(LockTestCase):
    def test_removes_the_record_it_was_given(self):
        record = {"pid": dead_pid(), "host": relay_lock.THIS_HOST,
                  "start_ticks": 1}
        self.write_lock(record)
        entry, _ = relay_lock._lock_view()
        self.assertTrue(relay_lock._drop_stale(entry))
        self.assertEqual(relay_lock._lock_view(), (None, False))

    def test_refuses_when_the_file_has_moved_on(self):
        # Two reclaimers can read the same dead record; the one that loses must
        # not delete the winner's fresh claim.
        self.write_lock({"pid": dead_pid(), "host": relay_lock.THIS_HOST,
                         "start_ticks": 1})
        stale_entry, _ = relay_lock._lock_view()
        relay_lock.acquire_lock()          # somebody else claims it first
        self.assertFalse(relay_lock._drop_stale(stale_entry))
        entry, present = relay_lock._lock_view()
        self.assertTrue(present)
        self.assertEqual(entry["pid"], os.getpid())


class ContentionTests(LockTestCase):
    def test_exactly_one_starter_wins(self):
        # The invariant the module exists for: one winner, every other process
        # a refusal that NAMES the winner. Real processes rather than threads,
        # because the claim is atomic across a filesystem, not inside one
        # interpreter.
        #
        # The winner must stay ALIVE until every other starter has looked. An
        # earlier version of this test let each winner exit immediately, and
        # every "loser" then correctly reclaimed a dead holder -- which measures
        # stale reclaim, not contention, and passes for the wrong reason.
        racers = 6
        gate = os.path.join(self.dir.name, "gate")
        code = (
            "import os,sys,time\n"
            "sys.path.insert(0, %r)\n"
            "import relay_lock as R\n"
            "R.LOCK_PATH = %r\n"
            "try:\n"
            "    R.acquire_lock()\n"
            "except R.LockHeld as exc:\n"
            "    print(str(exc), flush=True)\n"
            "    sys.exit(3)\n"
            "deadline = time.monotonic() + 60\n"
            "while not os.path.exists(%r) and time.monotonic() < deadline:\n"
            "    time.sleep(0.02)\n"
            "sys.exit(0)\n" % (os.path.dirname(os.path.abspath(__file__)),
                               self.path, gate)
        )
        children = [subprocess.Popen([sys.executable, "-c", code],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
                    for _ in range(racers)]

        # Release the winner only once it is the last one still running, so
        # every refusal was decided against a live holder.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if sum(1 for c in children if c.poll() is None) == 1:
                break
            time.sleep(0.02)
        else:
            for child in children:
                child.kill()
            self.fail("racers never settled to one survivor")

        open(gate, "w").close()
        codes, refusals = [], []
        for child in children:
            out, err = child.communicate(timeout=60)
            if child.returncode not in (0, 3):
                self.fail("a starter exited %d, which is neither a claim nor a "
                          "refusal: %s" % (child.returncode,
                                           err.decode("utf-8", "replace")))
            codes.append(child.returncode)
            if child.returncode == 3:
                refusals.append(out.decode("utf-8", "replace").strip())

        self.assertEqual(codes.count(0), 1,
                         "expected exactly one winner, got %r" % codes)
        self.assertEqual(codes.count(3), racers - 1)
        entry, present = relay_lock._lock_view()
        self.assertTrue(present, "the winner's claim should outlive the race")
        for message in refusals:
            self.assertIn(str(entry["pid"]), message)


if __name__ == "__main__":
    unittest.main(verbosity=2)

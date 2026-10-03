"""Tests for the relay's ingest path. Run: python -m unittest relay_test -v

(`python3` inside the container. On Windows the `python3` on PATH is the
Microsoft Store alias stub and exits 49 without running anything, so `python`
is the form that works on the host.)

Standard library only, and the same idioms as relay_lock_test and
event_log_test: a temporary state directory, and the module globals patched for
the duration of each test. relay, metrics and event_log all read their paths
from module globals at call time, so nothing here can reach the real
state.json, events.log or relay.lock in the working tree. The fixtures are
written out again rather than imported so each file still runs standalone.

Why this suite exists at all: the Worker's own traffic has been idle, so
events_total has sat at the same number across session after session and
push() sends a gauge snapshot rather than a stream. Nothing was ever
demonstrating that a newly arriving event is parsed, counted and written --
every assertion about that path was an argument from reading the code. These
tests drive realistic `wrangler tail --format json` output through it instead.

The shape of the fixture below is taken from an event actually captured in
this repository's own events.log, nested objects and all, because the
interesting part of drain() is precisely the nested braces.

Nothing here asserts what the code ought to do. Where the code does something
surprising, the test pins the surprising thing and says so, so a later change
to it shows up as a failing test rather than as a silent behaviour change.
"""

import json
import os
import queue
import tempfile
import unittest
from unittest import mock

import event_log
import metrics
import relay
import relay_lock


def real_event(outcome="ok", status=200, method="GET", cpu=3, wall=7,
               ts=1791009796405, exceptions=None):
    """One event in the shape this Worker actually emits.

    Nested `scriptVersion` and `event.request.headers` are not decoration:
    each contains a closing brace long before the top-level one does, which is
    the case drain()'s docstring says broke a first attempt.
    """
    return {
        "cpuTime": cpu,
        "diagnosticsChannelEvents": [],
        "event": {
            "request": {
                "cf": {"colo": "LHR", "country": "GB"},
                "headers": {"user-agent": "curl/8.0", "accept": "*/*"},
                "method": method,
                "url": "https://worker.example/api/health",
            },
            "response": {"status": status},
        },
        "eventTimestamp": ts,
        "exceptions": exceptions if exceptions is not None else [],
        "executionModel": "stateless",
        "logs": [],
        "outcome": outcome,
        "scriptName": "linear-webhook",
        "scriptVersion": {"id": "9884658c-f894-466", "tag": "v3",
                          "metadata": {"tag": "v3", "sha": "abc123"}},
        "truncated": False,
        "wallTime": wall,
    }


def pretty(obj):
    """How wrangler tail actually emits it: pretty-printed, not JSONL."""
    return json.dumps(obj, indent=2)


class RelayTestCase(unittest.TestCase):
    """A private state directory, lock, log and Worker checkout per test."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.addCleanup(self.dir.cleanup)
        self.state = os.path.join(self.dir.name, "state.json")
        self.log = os.path.join(self.dir.name, "events.log")
        self.lock = os.path.join(self.dir.name, "relay.lock")
        self.worker = os.path.join(self.dir.name, "worker")
        os.mkdir(self.worker)
        with open(os.path.join(self.worker, "wrangler.toml"), "w") as fh:
            fh.write('name = "linear-webhook"\n')
        self.use(metrics, STATE_PATH=self.state)
        self.use(event_log, LOG_PATH=self.log, MAX_BYTES=0, MAX_FILES=3,
                 ROTATE_ATTEMPTS=1, ROTATE_RETRY_WAIT=0.0,
                 ROTATE_RETRY_EVERY=0.0, ROTATE_WARN_EVERY=0.0)
        self.use(relay_lock, LOCK_PATH=self.lock)
        # HERE is where run() puts tail.err, and WORKER_DIR is what
        # validate_config() insists on. Both would otherwise reach the working
        # tree -- HERE is this repository.
        self.use(relay, HERE=self.dir.name, WORKER_DIR=self.worker,
                 MAX_RESTARTS=99, INTERVAL=3600.0)

    def use(self, module, **globals_):
        """Set module globals for the duration of this test."""
        for name, value in globals_.items():
            original = getattr(module, name)
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, original)

    def load(self):
        return metrics.load_state()

    def logged(self):
        """The events that reached the log, parsed."""
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def feed(self, buf, st, log=None):
        """Exactly the three lines relay.run() runs over one chunk of output.

        Copied rather than imported because run() is not callable in
        isolation -- it takes the lock, spawns wrangler and pushes to
        VictoriaMetrics. RunTests below drive the real function instead; this
        keeps the unit tests fast and lets each one name a single behaviour.
        """
        events, remainder = relay.drain(buf)
        written = []
        for ev in events:
            if metrics.record(st, ev):
                if log is not None:
                    log.write(ev)
                written.append(ev)
        return written, remainder


class DrainTests(RelayTestCase):
    """relay.drain: pretty-printed objects concatenated, arriving in pieces."""

    def test_one_pretty_object_is_one_event(self):
        events, rest = relay.drain(pretty(real_event()))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "ok")
        self.assertEqual(rest, "")

    def test_several_objects_in_one_buffer_are_all_found(self):
        # wrangler tail is not JSONL: json.loads would stop after the first.
        buf = pretty(real_event(status=200)) + pretty(real_event(status=404))
        events, rest = relay.drain(buf)
        self.assertEqual([e["event"]["response"]["status"] for e in events],
                         [200, 404])
        self.assertEqual(rest, "")

    def test_a_nested_brace_does_not_end_the_object(self):
        # scriptVersion.metadata closes with "}" long before the top-level
        # object does. Splitting on the first "}" would truncate every event.
        buf = pretty(real_event()) + pretty(real_event(status=500))
        events, _ = relay.drain(buf)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["scriptVersion"]["metadata"]["tag"], "v3")

    def test_a_partial_object_is_returned_as_the_remainder(self):
        full = pretty(real_event())
        cut = len(full) - 30
        events, rest = relay.drain(full[:cut])
        self.assertEqual(events, [])
        # The remainder is the text from the last "{" that failed to decode,
        # which for a single truncated object is the opening brace itself --
        # so the whole partial object comes back, ready to be completed.
        self.assertEqual(rest, full[:cut],
                         "the partial must be handed back, not dropped")

    def test_the_remainder_plus_the_next_chunk_parses_the_whole_event(self):
        full = pretty(real_event())
        cut = len(full) // 2
        first, rest = relay.drain(full[:cut])
        second, rest2 = relay.drain(rest + full[cut:])
        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["outcome"], "ok")
        self.assertEqual(rest2, "")

    def test_leading_text_before_the_first_brace_is_skipped(self):
        events, rest = relay.drain("wrangler: connecting\n" + pretty(real_event()))
        self.assertEqual(len(events), 1)
        self.assertEqual(rest, "")

    def test_text_with_no_brace_at_all_is_discarded(self):
        # Pinned because it is lossy: anything without a "{" cannot be a
        # partial object, so it is thrown away rather than carried forward.
        events, rest = relay.drain("wrangler: connected\nno json here")
        self.assertEqual(events, [])
        self.assertEqual(rest, "")

    def test_a_complete_json_array_in_the_stream_is_skipped(self):
        # It is skipped by the SCAN, not by any type check: drain walks to the
        # next "{" before decoding, and an array contains none. Worth stating
        # because the `isinstance(obj, dict)` guard in drain() is therefore
        # unreachable -- raw_decode called at a "{" cannot return anything but
        # an object -- so it cannot be covered by any input, and no test here
        # pretends otherwise.
        buf = "[1, 2, 3]" + pretty(real_event())
        events, rest = relay.drain(buf)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "ok")
        self.assertEqual(rest, "")

    def test_empty_and_brace_free_buffers_are_inert(self):
        self.assertEqual(relay.drain(""), ([], ""))
        self.assertEqual(relay.drain("\n\n"), ([], ""))


class RecordTests(RelayTestCase):
    """metrics.record: the folding of one event into the counters."""

    def test_a_realistic_event_increments_the_counter(self):
        st = self.load()
        self.assertEqual(st["events_total"], 0)
        self.assertTrue(metrics.record(st, real_event()))
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(st["requests"], {"ok|200|GET": 1})

    def test_the_counted_keys_are_outcome_status_method(self):
        st = self.load()
        metrics.record(st, real_event(outcome="exception", status=500,
                                      method="POST"))
        self.assertEqual(list(st["requests"]), ["exception|500|POST"])

    def test_timings_accumulate(self):
        st = self.load()
        metrics.record(st, real_event(cpu=3, wall=7))
        metrics.record(st, real_event(cpu=4, wall=1))
        self.assertEqual(st["cpu_sum_ms"], 7)
        self.assertEqual(st["wall_sum_ms"], 8)

    def test_a_message_without_an_event_key_is_not_counted(self):
        # What wrangler emits alongside events, and anything malformed that
        # still parses as a dict.
        st = self.load()
        self.assertFalse(metrics.record(st, {"logs": [], "outcome": "ok"}))
        self.assertEqual(st["events_total"], 0)
        self.assertEqual(st["requests"], {})

    def test_a_null_event_still_counts(self):
        # Pinned, not endorsed: `ev.get("event") or {}` turns a null event into
        # an empty one, so the message is counted as status "none", method "?".
        # It is still a tail message, so counting it is defensible; what
        # matters is that the behaviour is visible rather than assumed.
        st = self.load()
        self.assertTrue(metrics.record(st, {"event": None, "outcome": "ok"}))
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(list(st["requests"]), ["ok|none|?"])

    def test_exceptions_are_folded_by_name(self):
        # Only the FIRST exception on an event is folded in: record() reads
        # ev["exceptions"][0] and stops. Pinned as-is rather than as intended,
        # because it is a genuine undercount -- see the note in the PR. One
        # event carrying two TypeErrors contributes 1, not 2.
        st = self.load()
        metrics.record(st, real_event(exceptions=[{"name": "TypeError"},
                                                  {"name": "TypeError"}]))
        metrics.record(st, real_event(exceptions=[{"name": "RangeError"}]))
        self.assertEqual(st["exceptions"], {"TypeError": 1, "RangeError": 1})

    def test_an_unnamed_exception_falls_back(self):
        st = self.load()
        metrics.record(st, real_event(exceptions=[{}]))
        self.assertEqual(st["exceptions"], {"exception": 1})

    def test_last_event_ms_never_moves_backwards(self):
        st = self.load()
        metrics.record(st, real_event(ts=2000))
        metrics.record(st, real_event(ts=1000))
        self.assertEqual(st["last_event_ms"], 2000)

    def test_missing_timings_and_timestamp_do_not_raise(self):
        st = self.load()
        ev = {"event": {"request": {}, "response": {}}, "outcome": "ok"}
        self.assertTrue(metrics.record(st, ev))
        self.assertEqual(st["cpu_sum_ms"], 0)
        self.assertEqual(st["last_event_ms"], 0)


class CountingTests(RelayTestCase):
    """drain into record: the accounting must not double-count or drop."""

    def test_one_event_increments_once_and_lands_in_the_log(self):
        st = self.load()
        log = event_log.EventLog()
        self.addCleanup(log.close)
        written, rest = self.feed(pretty(real_event()), st, log)
        self.assertEqual(len(written), 1)
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(self.logged(), written)
        self.assertEqual(rest, "")

    def test_several_events_in_one_chunk_are_all_counted(self):
        st = self.load()
        log = event_log.EventLog()
        self.addCleanup(log.close)
        buf = "".join(pretty(real_event(status=s)) for s in (200, 201, 204))
        written, _ = self.feed(buf, st, log)
        self.assertEqual(len(written), 3)
        self.assertEqual(st["events_total"], 3)
        self.assertEqual(len(self.logged()), 3)

    def test_an_event_split_across_chunks_is_counted_exactly_once(self):
        # The double-count risk: the remainder is re-fed with the next chunk,
        # so an object that straddles the boundary must not be counted on both
        # sides -- nor lost between them.
        full = pretty(real_event())
        st = self.load()
        log = event_log.EventLog()
        self.addCleanup(log.close)
        buf, pos = "", 0
        for cut in (40, 120, 260, len(full)):
            written, buf = self.feed(buf + full[pos:cut], st, log)
            pos = cut
        self.assertEqual(buf, "")
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(len(self.logged()), 1)

    def test_a_message_without_an_event_key_is_not_written_to_the_log(self):
        st = self.load()
        log = event_log.EventLog()
        self.addCleanup(log.close)
        buf = pretty({"logs": [], "outcome": "ok"}) + pretty(real_event())
        written, _ = self.feed(buf, st, log)
        self.assertEqual(len(written), 1)
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(len(self.logged()), 1,
                         "only counted events reach the log")

    def test_a_malformed_object_does_not_break_the_stream(self):
        # Truncated JSON between two good events: drain cannot decode it, so
        # the rest of the buffer is carried forward rather than discarded, and
        # the events around it are unaffected.
        st = self.load()
        log = event_log.EventLog()
        self.addCleanup(log.close)
        buf = pretty(real_event(status=200)) + '{"event": {"request":' \
            + pretty(real_event(status=500))
        written, rest = self.feed(buf, st, log)
        self.assertEqual(len(written), 1)
        self.assertEqual(st["events_total"], 1)
        self.assertIn('{"event": {"request":', rest)

    def test_the_counter_survives_a_save_and_load(self):
        st = self.load()
        log = event_log.EventLog()
        self.addCleanup(log.close)
        self.feed(pretty(real_event()), st, log)
        metrics.save_state(st)
        again = metrics.load_state()
        self.assertEqual(again["events_total"], 1)
        self.assertEqual(again["requests"], {"ok|200|GET": 1})


class FakeProc:
    """Stands in for the `wrangler tail` child for the length of a run."""

    returncode = None

    def poll(self):
        return None                    # still alive: no restart path


class RunTests(RelayTestCase):
    """The real run() loop, with only its edges faked.

    Everything between the queue and the counters is the production code: the
    remainder discipline, the record gate, and the order of the two. Only the
    process, the push and the Worker directory are replaced.
    """

    def drive(self, *chunks, seconds=1.0):
        """Feed `chunks` to run() as if they arrived from `wrangler tail`."""
        q = queue.Queue()
        for chunk in chunks:
            q.put(chunk)
        # An empty string is what run() sees as "idle", so the loop keeps
        # polling until the deadline instead of taking the restart path.
        patcher = mock.patch.object(relay, "start_tail",
                                    return_value=(FakeProc(), q))
        self.addCleanup(patcher.stop)
        patcher.start()
        # Never let a test reach stop_tail: on Windows it shells out to
        # `taskkill /PID`, and a fake process must not be handed to it. It also
        # raises on a fake proc lacking .pid, which would abort run()'s finally
        # before it closes the log and the error file.
        reaper = mock.patch.object(relay, "stop_tail", return_value=None)
        self.addCleanup(reaper.stop)
        reaper.start()
        quiet = mock.patch.object(metrics, "push", return_value=200)
        self.addCleanup(quiet.stop)
        quiet.start()
        says = mock.patch.object(relay, "say")
        self.addCleanup(says.stop)
        says.start()
        return relay.run(seconds=seconds)

    def test_a_new_event_increments_the_counter_and_reaches_the_log(self):
        self.assertEqual(self.drive(pretty(real_event())), 0)
        st = metrics.load_state()
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(st["requests"], {"ok|200|GET": 1})
        self.assertEqual(len(self.logged()), 1)

    def test_an_event_split_across_two_chunks_is_counted_once(self):
        full = pretty(real_event())
        cut = len(full) // 2
        self.drive(full[:cut], full[cut:])
        st = metrics.load_state()
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(len(self.logged()), 1,
                         "a straddling object must not be counted twice")

    def test_a_partial_final_object_is_not_counted_and_not_written(self):
        # Nothing completes it, so it must not reach the counters or the log --
        # but it must also not disturb the events that already arrived.
        full = pretty(real_event())
        self.drive(pretty(real_event(status=200)), full[:len(full) - 40])
        st = metrics.load_state()
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(len(self.logged()), 1)

    def test_several_events_across_several_chunks_all_count(self):
        a, b, c = (pretty(real_event(status=s)) for s in (200, 404, 500))
        self.drive(a, b + c)
        st = metrics.load_state()
        self.assertEqual(st["events_total"], 3)
        self.assertEqual(sorted(st["requests"]),
                         ["ok|200|GET", "ok|404|GET", "ok|500|GET"])

    def test_a_message_without_an_event_key_is_not_written_to_the_log(self):
        # Driven through run(), not through the copied loop in CountingTests:
        # only this one can catch the record gate in the real loop being
        # removed, which is exactly what a mutation there does.
        self.drive(pretty({"logs": [], "outcome": "ok"}) + pretty(real_event()))
        st = metrics.load_state()
        self.assertEqual(st["events_total"], 1)
        self.assertEqual(len(self.logged()), 1,
                         "only counted events reach the log")

    def test_the_run_leaves_no_lock_behind(self):
        self.drive(pretty(real_event()))
        self.assertFalse(os.path.exists(self.lock),
                         "run() releases in its finally; a leftover would lock "
                         "the next start out")

    def test_the_state_is_written_before_it_returns(self):
        self.drive(pretty(real_event()))
        with open(self.state, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["events_total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from runner import slots


class SlotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "slots"

    def tearDown(self):
        self._tmp.cleanup()

    def test_n_plus_one_contenders_for_n_slots(self):
        held = [slots.try_acquire(self.dir, f"run-{i}", 3) for i in range(3)]
        self.assertTrue(all(held))
        self.assertEqual(sorted(s.index for s in held), [0, 1, 2])
        self.assertIsNone(slots.try_acquire(self.dir, "run-3", 3))
        held[1].release()
        late = slots.try_acquire(self.dir, "run-3", 3)
        self.assertEqual(late.index, 1)
        for slot in held + [late]:
            slot.release()

    def test_slots_are_exclusive_across_processes(self):
        code = (
            "import sys, time; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path; from runner import slots;"
            "s = slots.try_acquire(Path(sys.argv[2]), 'other', 1); print('got' if s else 'none', flush=True);"
            "time.sleep(1.5)"
        )
        factory_root = str(Path(slots.__file__).resolve().parent.parent)
        proc = subprocess.Popen([sys.executable, "-c", code, factory_root, str(self.dir)], stdout=subprocess.PIPE,
                                text=True)
        self.assertEqual(proc.stdout.readline().strip(), "got")
        self.assertIsNone(slots.try_acquire(self.dir, "mine", 1))
        self.assertEqual(slots.holders(self.dir), {0: f"other {proc.pid}"})
        proc.wait()
        proc.stdout.close()
        slot = slots.try_acquire(self.dir, "mine", 1)
        self.assertIsNotNone(slot)
        slot.release()

    def test_release_after_exception(self):
        try:
            slot = slots.try_acquire(self.dir, "run", 1)
            try:
                raise RuntimeError("stage crashed")
            finally:
                slot.release()
        except RuntimeError:
            pass
        again = slots.try_acquire(self.dir, "run", 1)
        self.assertIsNotNone(again)
        again.release()
        again.release()

    def test_cancellation_while_queued(self):
        blocker = slots.try_acquire(self.dir, "holder", 1)
        waits = []
        cancelled = threading.Event()
        result = {}

        def wait():
            result["slot"] = slots.acquire(self.dir, "waiter", count=lambda: 1, poll_seconds=0.01,
                                           should_abort=cancelled.is_set, on_wait=lambda: waits.append(1))

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.1)
        cancelled.set()
        thread.join(timeout=5)
        self.assertIsNone(result["slot"])
        self.assertTrue(waits)
        blocker.release()

    def test_changed_slot_count_affects_new_acquisitions(self):
        count = {"value": 1}
        first = slots.acquire(self.dir, "a", count=lambda: count["value"], poll_seconds=0.01, should_abort=lambda: False)
        self.assertIsNone(slots.try_acquire(self.dir, "b", count["value"]))
        count["value"] = 2
        second = slots.acquire(self.dir, "b", count=lambda: count["value"], poll_seconds=0.01, should_abort=lambda: False)
        self.assertNotEqual(first.index, second.index)
        count["value"] = 1
        first.release()
        second.release()
        self.assertEqual(slots.prune(self.dir, 1), [1])
        self.assertFalse((self.dir / "1.lock").exists())

    def test_prune_keeps_held_slots(self):
        held = slots.try_acquire(self.dir, "zz", 3)
        other = [slots.try_acquire(self.dir, f"x{i}", 3) for i in range(2)]
        removed = slots.prune(self.dir, 0)
        self.assertNotIn(held.index, removed)
        for slot in [held] + other:
            slot.release()

    def test_rotating_order_spreads_first_choice(self):
        firsts = Counter(slots.scan_order(f"20260915-1420-run-{i}", 4)[0] for i in range(200))
        self.assertEqual(set(firsts), {0, 1, 2, 3})

    def test_no_starvation_under_bounded_contention(self):
        served = Counter()
        stop = time.monotonic() + 2.0

        def contender(name):
            while time.monotonic() < stop:
                slot = slots.acquire(self.dir, name, count=lambda: 2, poll_seconds=0.005,
                                     should_abort=lambda: time.monotonic() >= stop)
                if slot is None:
                    return
                served[name] += 1
                time.sleep(0.01)
                slot.release()
                time.sleep(0.005)

        threads = [threading.Thread(target=contender, args=(f"run-{i}",)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(served), 5, served)
        self.assertTrue(all(count > 0 for count in served.values()))


if __name__ == "__main__":
    unittest.main()

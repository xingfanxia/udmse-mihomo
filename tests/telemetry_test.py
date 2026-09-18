"""Deterministic aggregate telemetry checks; no live engine or systemd calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("telemetry", REPO / "telemetry.py")
telemetry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telemetry)


class Clock:
    def __init__(self):
        self.now = 100.0

    def advance(self, seconds=2):
        self.now += seconds

    def monotonic(self):
        return self.now

    def wall(self):
        return 1700000000 + self.now


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.process = mock.Mock()
        self.connections = mock.Mock()
        self.process.read.return_value = telemetry.ProcessInfo(123, 500, 1, 40960, 10)
        self.connections.read.return_value = (100, 200, 3)
        self.collector = telemetry.Collector(self.process, self.connections, self.clock.monotonic, self.clock.wall)

    def sample(self, cpu=2, upload=300, download=1000):
        self.clock.advance()
        self.process.read.return_value = telemetry.ProcessInfo(123, 500, cpu, 81920, 12)
        self.connections.read.return_value = (upload, download, 4)
        self.collector.sample()
        return self.collector.snapshot()

    def test_first_sample_warming_then_rates_and_one_core_cpu(self):
        self.assertTrue(self.collector.snapshot()["stale"])
        self.collector.sample()
        first = self.collector.snapshot()
        self.assertEqual(first["state"], "warming")
        self.assertIsNone(first["upload_bytes_per_second"])
        self.assertIsNone(first["cpu_percent"])
        current = self.sample(cpu=7)
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["upload_bytes_per_second"], 100)
        self.assertEqual(current["download_bytes_per_second"], 400)
        self.assertEqual(current["cpu_percent"], 300)
        self.assertEqual(current["memory_bytes"], 81920)
        self.assertEqual(current["connections"], 4)
        self.assertEqual(current["uptime_seconds"], 12)
        self.assertEqual(current["metric_scope"], "mihomo")

    def test_delayed_snapshot_never_causes_traffic_or_cpu_burst(self):
        self.process.read.side_effect = lambda: telemetry.ProcessInfo(123, 500, self.clock.now, 40960, self.clock.now - 5)
        self.connections.read.side_effect = lambda: (self.clock.now * 1000, self.clock.now * 2000, 3)
        self.collector.sample()  # Baseline at t=100.
        self.clock.advance()  # Engine snapshot at t=102 arrives at t=104.5.
        def delayed_snapshot():
            counters = self.clock.now * 1000, self.clock.now * 2000, 3
            self.clock.advance(2.5)
            return counters
        self.connections.read.side_effect = delayed_snapshot
        self.collector.sample()
        slow = self.collector.snapshot()
        self.assertEqual(slow["state"], "warming")
        self.assertEqual(slow["upload_total_bytes"], 102000)
        for metric in ("upload_bytes_per_second", "download_bytes_per_second", "cpu_percent"):
            self.assertIsNone(slow[metric])
        # Reproduce the old near-immediate sample despite the new loop wait.
        self.clock.advance(0.01)
        self.connections.read.side_effect = lambda: (self.clock.now * 1000, self.clock.now * 2000, 3)
        self.collector.sample()
        fresh_baseline = self.collector.snapshot()
        self.assertEqual(fresh_baseline["state"], "warming")
        for metric in ("upload_bytes_per_second", "download_bytes_per_second", "cpu_percent"):
            self.assertIsNone(fresh_baseline[metric])
        self.assertNotEqual(slow["history"][-1]["session_id"], fresh_baseline["history"][-1]["session_id"])
        self.clock.advance()
        self.collector.sample()
        running = self.collector.snapshot()
        self.assertEqual(running["state"], "running")
        self.assertAlmostEqual(running["upload_bytes_per_second"], 1000)
        self.assertAlmostEqual(running["download_bytes_per_second"], 2000)
        self.assertAlmostEqual(running["cpu_percent"], 100)

    def test_loop_waits_full_nominal_interval_after_slow_collection(self):
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        self.collector._stop = stop
        self.collector.sample = lambda: self.clock.advance(2.5)
        self.collector._loop()
        stop.wait.assert_called_once_with(2)

    def test_requests_only_read_cache_and_cannot_modify_history(self):
        self.collector.sample()
        for _ in range(10):
            data = self.collector.snapshot()
            data["history"][0]["connections"] = 999
        self.assertEqual(self.process.read.call_count, 2)
        self.assertEqual(self.connections.read.call_count, 1)
        self.assertEqual(self.collector.snapshot()["history"][0]["connections"], 3)

    def test_restart_and_same_pid_counter_reset_make_new_sessions(self):
        self.collector.sample()
        first_session = self.collector.snapshot()["history"][-1]["session_id"]
        self.clock.advance()
        self.process.read.return_value = telemetry.ProcessInfo(123, 900, 100, 40960, 1)
        self.connections.read.return_value = (9000000, 9000000, 0)
        self.collector.sample()
        current = self.collector.snapshot()
        self.assertEqual(current["state"], "warming")
        self.assertIsNone(current["download_bytes_per_second"])
        second_session = current["history"][-1]["session_id"]
        self.assertNotEqual(first_session, second_session)
        self.clock.advance()
        self.connections.read.return_value = (1, 2, 0)
        self.collector.sample()
        current = self.collector.snapshot()
        self.assertEqual(current["state"], "warming")
        self.assertNotEqual(second_session, current["history"][-1]["session_id"])
        self.assertIsNone(current["upload_bytes_per_second"])

    def test_restart_during_api_request_discards_mixed_sample(self):
        self.process.read.side_effect = [telemetry.ProcessInfo(1, 100, 2, 4096, 4), telemetry.ProcessInfo(2, 200, 3, 8192, 1)]
        self.collector.sample()
        current = self.collector.snapshot()
        self.assertEqual(current["state"], "unavailable")
        for metric in telemetry.METRICS:
            self.assertIsNone(current[metric])

    def test_failed_sample_clears_metrics_and_breaks_next_rate(self):
        self.collector.sample()
        self.connections.read.side_effect = ValueError("private hostname secret token")
        self.clock.advance()
        self.collector.sample()
        current = self.collector.snapshot()
        self.assertEqual(current["state"], "unavailable")
        self.assertIsNone(current["upload_total_bytes"])
        self.assertIsNone(current["connections"])
        self.assertIsNone(current["history"][-1]["session_id"])
        self.assertNotIn("private hostname", json.dumps(current))
        self.connections.read.side_effect = None
        self.assertEqual(self.sample(upload=9000000)["state"], "warming")

    def test_stopped_is_explicit_and_never_false_zero(self):
        self.collector.sample()
        previous_calls = self.connections.read.call_count
        self.process.read.side_effect = telemetry.Stopped()
        self.clock.advance()
        self.collector.sample()
        current = self.collector.snapshot()
        self.assertEqual(current["state"], "stopped")
        self.assertEqual(self.connections.read.call_count, previous_calls)
        for metric in telemetry.METRICS:
            self.assertIsNone(current[metric])

    def test_stale_gap_and_nonpositive_clock_interval_rebaseline(self):
        self.collector.sample()
        self.clock.advance(7)
        self.assertTrue(self.collector.snapshot()["stale"])
        self.assertEqual(self.collector.snapshot()["age_seconds"], 7)
        self.connections.read.return_value = (1000000, 2000000, 0)
        self.collector.sample()
        self.assertEqual(self.collector.snapshot()["state"], "warming")
        self.assertFalse(self.collector.snapshot()["stale"])
        self.collector.sample()  # Same monotonic time must not divide by zero.
        self.assertIsNone(self.collector.snapshot()["upload_bytes_per_second"])

    def test_history_retention_is_bounded_by_time_and_count(self):
        for _ in range(200):
            self.sample()
        history = self.collector.snapshot()["history"]
        self.assertEqual(len(history), 151)
        self.assertEqual(history[-1]["timestamp_ms"] - history[0]["timestamp_ms"], 300000)
        self.clock.advance(301)
        self.assertEqual(self.collector.snapshot()["history"], [])

    def test_background_collector_stops(self):
        self.collector.start()
        self.collector.stop()
        self.assertFalse(self.collector._thread.is_alive())


class SourceTest(unittest.TestCase):
    def test_connection_count_null_and_private_payload_stripped(self):
        private = {"uploadTotal": 42, "downloadTotal": 84, "connections": [
            {"metadata": {"sourceIP": "192.168.1.20", "host": "private.invalid"}, "id": "private-id"}
        ], "memory": 999}
        aggregate = telemetry.decode_connections(json.dumps(private).encode())
        self.assertEqual(aggregate, (42, 84, 1))
        self.assertNotIn("private", repr(aggregate))
        private["connections"] = None
        self.assertEqual(telemetry.decode_connections(json.dumps(private).encode()), (42, 84, 0))

    def test_malformed_or_missing_counters_are_unknown(self):
        for payload in ({}, [], {"uploadTotal": True, "downloadTotal": 2, "connections": []},
                        {"uploadTotal": -1, "downloadTotal": 2, "connections": []},
                        {"uploadTotal": 2**63, "downloadTotal": 2, "connections": []},
                        {"uploadTotal": 1, "downloadTotal": 2},
                        {"uploadTotal": 1, "downloadTotal": 2, "connections": "broken"},
                        {"uploadTotal": 1, "downloadTotal": 2, "connections": [1]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                telemetry.decode_connections(json.dumps(payload).encode())
        for raw in (b"{", b'{"uploadTotal":NaN}', b'{"uploadTotal":1,"uploadTotal":2}'):
            with self.assertRaises(ValueError):
                telemetry.decode_connections(raw)

    def make_connection_source(self, raw, size=None, status=200):
        response = mock.Mock(status=status)
        response.getheader.return_value = size
        response.read.return_value = raw
        connection = mock.Mock()
        connection.getresponse.return_value = response
        factory = mock.Mock(return_value=connection)
        return telemetry.ConnectionsSource(lambda: "private-secret", factory), factory, connection, response

    def test_http_uses_fixed_authenticated_endpoint_and_bounded_read(self):
        source, factory, connection, response = self.make_connection_source(b'{"uploadTotal":1,"downloadTotal":2,"connections":null}')
        self.assertEqual(source.read(), (1, 2, 0))
        factory.assert_called_once_with("127.0.0.1", 9090, timeout=3)
        connection.request.assert_called_once_with("GET", "/connections", headers={"Authorization": "Bearer private-secret", "Accept": "application/json"})
        response.read.assert_called_once_with(telemetry.MAX_CONNECTION_BYTES + 1)
        connection.close.assert_called_once()

    def test_oversize_redirect_and_missing_content_length_are_bounded(self):
        for raw, size, status in ((b"", str(telemetry.MAX_CONNECTION_BYTES + 1), 200),
                                  (b"x" * (telemetry.MAX_CONNECTION_BYTES + 1), None, 200),
                                  (b"", "0", 302)):
            source, _, connection, _ = self.make_connection_source(raw, size, status)
            with self.assertRaises(ValueError):
                source.read()
            connection.close.assert_called_once()

    def test_linux_proc_fields_and_systemd_main_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            fields = ["S"] + ["0"] * 30
            for index, value in ((11, "200"), (12, "100"), (19, "500"), (21, "10")):
                fields[index] = value
            (root / "123" / "stat").write_text("123 (mihomo (worker)) " + " ".join(fields))
            (root / "uptime").write_text("100.25 60.0\n")
            run = mock.Mock(return_value=SimpleNamespace(stdout=b"123\n"))
            source = telemetry.LinuxProcessSource(root, run=run, ticks=100, page_size=4096)
            result = source.read()
            self.assertEqual(result, telemetry.ProcessInfo(123, 500, 3, 40960, 95.25))
            self.assertEqual(run.call_args.args[0], ["systemctl", "show", "--property=MainPID", "--value", "mihomo.service"])
            self.assertEqual(run.call_args.kwargs["timeout"], 1)
            run.return_value.stdout = b"0\n"
            with self.assertRaises(telemetry.Stopped):
                source.read()
            run.side_effect = subprocess.TimeoutExpired("systemctl", 1)
            with self.assertRaises(subprocess.TimeoutExpired):
                source.read()


if __name__ == "__main__":
    unittest.main()

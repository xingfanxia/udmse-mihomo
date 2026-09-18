"""RAM-only aggregate Mihomo telemetry. No connection details leave the source."""
from collections import deque
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import NamedTuple
import uuid

# Nominal cadence: wait two seconds after collection, never catch up in bursts.
INTERVAL_SECONDS = 2
# Slow responses cannot reliably timestamp the counters captured by the engine.
MAX_RATE_COLLECTION_SECONDS = INTERVAL_SECONDS / 2
WINDOW_SECONDS = 300
STALE_SECONDS = 6
MAX_HISTORY = 151
MAX_CONNECTION_BYTES = 2 * 1024 * 1024
METRICS = ("upload_bytes_per_second", "download_bytes_per_second", "upload_total_bytes",
           "download_total_bytes", "connections", "cpu_percent", "memory_bytes", "uptime_seconds")
HISTORY_METRICS = ("upload_bytes_per_second", "download_bytes_per_second", "cpu_percent",
                   "memory_bytes", "connections")


def empty_snapshot():
    result = {"state": "warming", "sampled_at_ms": None, "age_seconds": None, "stale": True,
              "interval_seconds": INTERVAL_SECONDS, "window_seconds": WINDOW_SECONDS,
              "metric_scope": "mihomo", "history": [], "message": "Waiting for the first engine sample."}
    result.update({name: None for name in METRICS})
    return result


class Stopped(Exception):
    """systemd reports no main process; different from a failed measurement."""


class ProcessInfo(NamedTuple):
    pid: int
    start_ticks: int
    cpu_seconds: float
    memory_bytes: int
    uptime_seconds: float

    @property
    def identity(self):
        return self.pid, self.start_ticks


class LinuxProcessSource:
    def __init__(self, proc=Path("/proc"), run=subprocess.run, ticks=None, page_size=None):
        self.proc = Path(proc)
        self.run = run
        self.ticks = ticks if ticks is not None else os.sysconf("SC_CLK_TCK")
        self.page_size = page_size if page_size is not None else os.sysconf("SC_PAGE_SIZE")

    def read(self):
        result = self.run(["systemctl", "show", "--property=MainPID", "--value", "mihomo.service"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=1, check=True)
        value = result.stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9]{1,10}", value):
            raise ValueError("Invalid process identifier")
        pid = int(value)
        if pid == 0:
            raise Stopped()
        with (self.proc / str(pid) / "stat").open() as stream:
            line = stream.read(4097)
        # The command name can contain spaces and parentheses. Fields after its
        # final ')' begin with field 3 (state), not with field 1 (PID).
        end = line.rfind(")")
        if len(line) > 4096 or end < 0 or line.split(" ", 1)[0] != str(pid):
            raise ValueError("Invalid process statistics")
        fields = line[end + 1:].split()
        user, system, started, rss = (int(fields[index]) for index in (11, 12, 19, 21))
        with (self.proc / "uptime").open() as stream:
            uptime = float(stream.read(128).split()[0])
        if min(user, system, started, rss) < 0 or not math.isfinite(uptime) or uptime < started / self.ticks:
            raise ValueError("Invalid process statistics")
        return ProcessInfo(pid, started, (user + system) / self.ticks,
                           rss * self.page_size, uptime - started / self.ticks)


def decode_connections(raw):
    if len(raw) > MAX_CONNECTION_BYTES:
        raise ValueError("Engine response exceeds the aggregate collection limit")

    def unique_pairs(items):
        value = {}
        for key, entry in items:
            if key in value:
                raise ValueError("Duplicate engine field")
            value[key] = entry
        return value

    def invalid_constant(value):
        raise ValueError("Invalid engine number")

    payload = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    if not isinstance(payload, dict):
        raise ValueError("Invalid engine statistics")
    uploaded, downloaded = payload.get("uploadTotal"), payload.get("downloadTotal")
    if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (uploaded, downloaded)):
        raise ValueError("Invalid traffic counters")
    if "connections" not in payload:
        raise ValueError("Missing connection count")
    connections = payload["connections"]
    # v1.19.31 serializes a nil Go slice as null when the engine is idle.
    if connections is not None and (not isinstance(connections, list) or not all(isinstance(item, dict) for item in connections)):
        raise ValueError("Invalid connection list")
    # The raw dictionaries are discarded here. No addresses, domains, node
    # settings, connection IDs or credentials are retained by the collector.
    return uploaded, downloaded, len(connections) if connections is not None else 0


class ConnectionsSource:
    def __init__(self, secret_reader, connection_factory=http.client.HTTPConnection):
        self.secret_reader = secret_reader
        self.connection_factory = connection_factory

    def read(self):
        connection = self.connection_factory("127.0.0.1", 9090, timeout=3)
        try:
            connection.request("GET", "/connections", headers={
                "Authorization": "Bearer " + self.secret_reader(), "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("Engine statistics unavailable")
            size = response.getheader("Content-Length")
            if size is not None and (not size.isdecimal() or int(size) > MAX_CONNECTION_BYTES):
                raise ValueError("Invalid engine response length")
            return decode_connections(response.read(MAX_CONNECTION_BYTES + 1))
        finally:
            connection.close()


class Collector:
    def __init__(self, process_source, connection_source, monotonic=time.monotonic, wall_time=time.time):
        self.process_source = process_source
        self.connection_source = connection_source
        self.monotonic = monotonic
        self.wall_time = wall_time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._history = deque(maxlen=MAX_HISTORY)
        self._latest = empty_snapshot()
        self._sample_time = None
        self._baseline = None
        self._session_id = None

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Collector already started")
        self._thread = threading.Thread(target=self._loop, name="mihomo-telemetry", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # Two bounded MainPID reads and one bounded local API request.
            self._thread.join(timeout=7)

    def _loop(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(INTERVAL_SECONDS)

    def sample(self):
        started = self.monotonic()
        values = {name: None for name in METRICS}
        state, message = "unavailable", "Engine metrics are unavailable."
        process = traffic = None
        try:
            before = self.process_source.read()
            traffic = self.connection_source.read()
            process = self.process_source.read()
            if before.identity != process.identity:
                raise ValueError("Engine restarted during collection")
            state, message = "warming", "Waiting for the next engine sample."
        except Stopped:
            state, message = "stopped", "Mihomo is stopped."
            process = traffic = None
        except Exception:
            # Never retain/log the raw exception: engine responses can contain
            # private connection metadata or configuration values.
            process = traffic = None
        now = self.monotonic()
        timestamp = int(self.wall_time() * 1000)
        with self._lock:
            if process is not None and traffic is not None:
                uploaded, downloaded, connections = traffic
                previous = self._baseline
                timely = 0 <= now - started <= MAX_RATE_COLLECTION_SECONDS
                continuous = timely and previous is not None and process.identity == previous[0].identity
                elapsed = now - previous[2] if previous is not None else 0
                continuous = continuous and 0 < elapsed <= STALE_SECONDS
                continuous = continuous and uploaded >= previous[1][0] and downloaded >= previous[1][1] and process.cpu_seconds >= previous[0].cpu_seconds
                if continuous:
                    values["upload_bytes_per_second"] = (uploaded - previous[1][0]) / elapsed
                    values["download_bytes_per_second"] = (downloaded - previous[1][1]) / elapsed
                    # 100% means one CPU core; multithreaded work can exceed 100%.
                    values["cpu_percent"] = (process.cpu_seconds - previous[0].cpu_seconds) * 100 / elapsed
                    state, message = "running", None
                else:
                    self._session_id = uuid.uuid4().hex
                values.update(upload_total_bytes=uploaded, download_total_bytes=downloaded,
                              connections=connections, memory_bytes=process.memory_bytes,
                              uptime_seconds=process.uptime_seconds)
                # A delayed snapshot can contain counters from much earlier
                # than its receipt time. Do not use it on either side of a rate
                # calculation; resume only after two fresh, fast collections.
                self._baseline = (process, traffic, now) if timely else None
                if not timely:
                    message = "Collection was slow; waiting for fresh samples."
            else:
                self._baseline = None
                self._session_id = None
            point = {name: values[name] for name in HISTORY_METRICS}
            point.update(timestamp_ms=timestamp, session_id=self._session_id)
            self._history.append((now, point))
            self._prune(now)
            self._latest = empty_snapshot()
            self._latest.update(values)
            self._latest.update(state=state, sampled_at_ms=timestamp, message=message)
            self._sample_time = now

    def _prune(self, now):
        while self._history and self._history[0][0] < now - WINDOW_SECONDS:
            self._history.popleft()

    def snapshot(self):
        # HTTP polls never touch systemd, /proc, or the engine API.
        now = self.monotonic()
        with self._lock:
            self._prune(now)
            result = self._latest.copy()
            age = None if self._sample_time is None else max(0, now - self._sample_time)
            result.update(age_seconds=None if age is None else round(age, 2),
                          stale=age is None or age > STALE_SECONDS,
                          history=[point.copy() for _, point in self._history])
            return result

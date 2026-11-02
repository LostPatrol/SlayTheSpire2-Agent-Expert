"""Run parallel offline public diagnostics, strict public cells, or literal fixture replays."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from validation import CHARACTERS, clean_environment, models, process_cpu, sha256, write_json

# Every default path belongs to this project; this runner has no live-game connection.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = ROOT / "local/simulation/mentor/mentor-simulation.exe"
DEFAULT_MODELS = ROOT / "vendor/spire-mentor/sts2-pycli/data/ppo"
# Match the established five-seed full-run panel, preserving its declared order.
DEFAULT_SEEDS = tuple(range(1458832210, 1458832215))
# Leave two logical processors available on the current 16-thread Windows machine.
DEFAULT_JOBS = max(1, (os.cpu_count() or 16) - 2)
# Windows FILETIME stores CPU duration in units of 100 ns.
FILETIME_TICKS_PER_SECOND = 10_000_000
# PublicPosition CardIdentity uses h<hand index> and s<card UID>, always at the start of card gaps.
PUBLIC_CARD_HANDLE_PREFIX = re.compile(r"^[hs][0-9]+:")


def json_digest(value):
    """Hash canonical JSON values without depending on indentation or dictionary order."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def error_signatures(value):
    """Collect exact reported error text without inferring a common underlying cause."""
    if not isinstance(value, dict):
        return []
    result = set()
    for rows in (value.get("errors"), value.get("import_errors"),
                 (value.get("support") or {}).get("blocking_reasons"),
                 (value.get("production") or {}).get("errors")):
        if isinstance(rows, list):
            result.update(row if isinstance(row, str) else
                          json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
    for key in ("mapping_error", "projection_error", "error"):
        if isinstance(value.get(key), str):
            result.add(value[key])
    if (value.get("production") or {}).get("ok") is False and not result:
        result.add("public_policy_unavailable_without_reason")
    return sorted(result)


def error_families(records):
    """Group structurally similar symptoms by dropping only the verified leading public card handle."""
    families = {}
    for record in records:
        response = record.get("response", {})
        batch = response.get("batch_diagnostics") or {}
        groups = batch.get("groups", [])
        if isinstance(groups, dict):
            groups = list(groups.values())
        if groups:
            signatures = [(group["signature"], group.get("count", 0)) for group in groups
                          if isinstance(group, dict) and isinstance(group.get("signature"), str)]
        else:
            signatures = [(signature, 1) for signature in error_signatures(response)]
            if isinstance(response.get("adapter_error"), str):
                signatures.append((response["adapter_error"], 1))
        for signature, count in signatures:
            family = PUBLIC_CARD_HANDLE_PREFIX.sub("", signature, count=1)
            row = families.setdefault(family, {"family": family, "occurrences": 0,
                "signatures": set(), "cells": set()})
            row["occurrences"] += count
            row["signatures"].add(signature)
            row["cells"].add(record["cell"])
    result = []
    for row in families.values():
        row["signatures"] = sorted(row["signatures"])
        row["cells"] = sorted(row["cells"])
        row["distinct_signatures"] = len(row["signatures"])
        row["distinct_cells"] = len(row["cells"])
        result.append(row)
    return sorted(result, key=lambda row: (-row["occurrences"], row["family"]))


def compact_cell(record):
    """Expose comparable cell counters while identifying floors inferred from recorded pre-action roots."""
    response = record.get("response", {})
    request = record.get("request", {})
    steps = response.get("steps")
    decisions = response.get("decisions")
    floor = response.get("final_floor", response.get("floor"))
    floor_source = "response" if isinstance(floor, int) else "unavailable"
    if not isinstance(floor, int):
        floors = [step.get("before", {}).get("floor") for step in (steps or []) if isinstance(step, dict)]
        floors += [decision.get("floor") for decision in (decisions or []) if isinstance(decision, dict)]
        floors = [value for value in floors if isinstance(value, int)]
        floor = max(floors) if floors else None
        if floors:
            floor_source = "latest_recorded_pre_action_or_decision_floor"
    actions = response.get("action_trace_count")
    if not isinstance(actions, int):
        actions = len(steps) if isinstance(steps, list) else record.get("action_count")
    return {"cell": record["cell"], "character": request.get("character"), "seed": request.get("seed"),
        "status": record["status"], "outcome": record.get("outcome"), "floor": floor,
        "floor_source": floor_source, "action_count": actions,
        "native_commands_selected": response.get("native_commands_selected"),
        "final_hp": response.get("final_hp"), "act_index": response.get("act_index"),
        "adapter_error": response.get("adapter_error", record.get("adapter_error")),
        "wall_seconds": record.get("wall_seconds")}


class MemoryStatus(ctypes.Structure):
    """Match Windows MEMORYSTATUSEX without an external monitoring dependency."""

    _fields_ = [("length", wintypes.DWORD), ("memory_load", wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in ("total_physical", "available_physical",
        "total_pagefile", "available_pagefile", "total_virtual", "available_virtual",
        "available_extended_virtual")]


class ProcessMemory(ctypes.Structure):
    """Match PROCESS_MEMORY_COUNTERS for child working set and peak working set."""

    _fields_ = [("size", wintypes.DWORD), ("page_fault_count", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("peak_working_set_bytes", "working_set_bytes",
        "quota_peak_paged_pool", "quota_paged_pool", "quota_peak_nonpaged_pool",
        "quota_nonpaged_pool", "pagefile_bytes", "peak_pagefile_bytes")]


class WindowsMetrics:
    """Read actual system/child counters and optionally open all available CPU affinity bits."""

    def __init__(self):
        """Bind APIs once; unsupported systems retain explicit unknown counters."""
        self.available = os.name == "nt"
        if not self.available:
            return
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(ctypes.c_uint64)] * 3
        self.kernel.GetSystemTimes.restype = wintypes.BOOL
        self.kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        self.kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
            ctypes.POINTER(ProcessMemory), wintypes.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.GetProcessAffinityMask.restype = wintypes.BOOL
        self.kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        self.kernel.SetProcessAffinityMask.restype = wintypes.BOOL

    def affinity(self, all_cpus=False):
        """Change only this runner's affinity when explicitly requested by the CLI."""
        if not self.available:
            return {"available": False}
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        handle = self.kernel.GetCurrentProcess()
        if not self.kernel.GetProcessAffinityMask(handle, ctypes.byref(process_mask),
                                                  ctypes.byref(system_mask)):
            return {"available": False, "windows_error": ctypes.get_last_error()}
        result = {"available": True, "original_process_mask": process_mask.value,
                  "system_mask": system_mask.value, "changed": False}
        if all_cpus and process_mask.value != system_mask.value:
            result["changed"] = bool(self.kernel.SetProcessAffinityMask(handle, system_mask.value))
            if not result["changed"]:
                result["windows_error"] = ctypes.get_last_error()
        result["effective_process_mask"] = (system_mask.value if result["changed"] else process_mask.value)
        return result

    def system(self):
        """Read cumulative whole-machine CPU and physical memory counters."""
        if not self.available:
            return {"available": False}
        result = {"available": True}
        values = [ctypes.c_uint64() for _ in range(3)]
        if self.kernel.GetSystemTimes(*(ctypes.byref(value) for value in values)):
            result["cpu_seconds"] = dict(zip(("idle", "kernel", "user"),
                (value.value / FILETIME_TICKS_PER_SECOND for value in values)))
        else:
            result["cpu_windows_error"] = ctypes.get_last_error()
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if self.kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
            result["memory"] = {"total_physical_bytes": memory.total_physical,
                "available_physical_bytes": memory.available_physical,
                "used_physical_bytes": memory.total_physical - memory.available_physical,
                "memory_load_percent": memory.memory_load}
        else:
            result["memory_windows_error"] = ctypes.get_last_error()
        return result

    def child_memory(self, process):
        """Read a child handle before its worker removes it from the active registry."""
        if not self.available:
            return None
        memory = ProcessMemory()
        memory.size = ctypes.sizeof(memory)
        if not self.psapi.GetProcessMemoryInfo(int(process._handle), ctypes.byref(memory), memory.size):
            return {"unavailable_error": ctypes.get_last_error()}
        return {"working_set_bytes": memory.working_set_bytes,
                "peak_working_set_bytes": memory.peak_working_set_bytes,
                "pagefile_bytes": memory.pagefile_bytes,
                "peak_pagefile_bytes": memory.peak_pagefile_bytes}


class BatchMonitor:
    """Sample running children while keeping completed CPU totals in cumulative measurements."""

    def __init__(self, output, interval, metrics=None):
        """Prepare a shared registry whose lock also protects native child handles."""
        self.output = output
        self.interval = interval
        self.metrics = metrics or WindowsMetrics()
        self.lock = threading.Lock()
        self.active = {}
        self.finished_cpu = Counter()
        self.finished_known = 0
        self.finished_unknown = 0
        self.started_children = 0
        self.peak_concurrency = 0
        self.samples = []
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        """Take an initial sample before any worker starts."""
        self.sample()
        self.thread.start()

    def register(self, cell, process):
        """Track actual launched processes rather than executor queue length."""
        with self.lock:
            self.active[cell] = process
            self.started_children += 1
            self.peak_concurrency = max(self.peak_concurrency, len(self.active))

    def finish(self, cell, process):
        """Read final CPU/memory under the same lock used by the sampler."""
        with self.lock:
            cpu = process_cpu(process)
            memory = self.metrics.child_memory(process)
            if isinstance(cpu, dict) and "user_seconds" in cpu and "kernel_seconds" in cpu:
                self.finished_known += 1
                self.finished_cpu.update({key: cpu[key] for key in ("user_seconds", "kernel_seconds")})
            else:
                self.finished_unknown += 1
            self.active.pop(cell, None)
        return cpu, memory

    def sample(self):
        """Persist measured process CPU and working sets alongside whole-machine counters."""
        with self.lock:
            cpu = Counter(self.finished_cpu)
            known, unknown = self.finished_known, self.finished_unknown
            working_sets = []
            children = []
            for cell, process in self.active.items():
                child_cpu = process_cpu(process)
                memory = self.metrics.child_memory(process)
                children.append({"cell": cell, "pid": process.pid, "process_cpu": child_cpu,
                                 "process_memory": memory})
                if isinstance(child_cpu, dict) and "user_seconds" in child_cpu and "kernel_seconds" in child_cpu:
                    known += 1
                    cpu.update({key: child_cpu[key] for key in ("user_seconds", "kernel_seconds")})
                else:
                    unknown += 1
                if isinstance(memory, dict) and "working_set_bytes" in memory:
                    working_sets.append(memory["working_set_bytes"])
            row = {"elapsed_seconds": time.monotonic() - self.started,
                "active_processes": len(self.active), "children": children,
                "batch_child_cpu_seconds": dict(cpu) if known else None,
                "cpu_known_children": known, "cpu_unknown_children": unknown,
                "active_working_set_bytes": sum(working_sets) if working_sets else (0 if not self.active else None),
                "memory_known_active_children": len(working_sets),
                "memory_unknown_active_children": len(self.active) - len(working_sets)}
        row["system"] = self.metrics.system()
        if self.samples:
            previous = self.samples[-1]
            elapsed = row["elapsed_seconds"] - previous["elapsed_seconds"]
            left, right = previous["system"].get("cpu_seconds"), row["system"].get("cpu_seconds")
            if left and right:
                total = (right["kernel"] + right["user"]) - (left["kernel"] + left["user"])
                idle = right["idle"] - left["idle"]
                row["system_cpu_busy_percent_since_previous_sample"] = 100 * (total - idle) / total if total > 0 else None
            left, right = previous["batch_child_cpu_seconds"], row["batch_child_cpu_seconds"]
            if left and right and elapsed > 0:
                seconds = sum(right.values()) - sum(left.values())
                row["batch_measured_cpu_cores_since_previous_sample"] = seconds / elapsed
                row["batch_cpu_percent_of_logical_capacity_since_previous_sample"] = 100 * seconds / elapsed / (os.cpu_count() or 1)
        self.samples.append(row)
        with (self.output / "resource-samples.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _loop(self):
        """Wait interruptibly so stopping the monitor never adds a full sample interval."""
        while not self.stop_event.wait(self.interval):
            self.sample()

    def close(self):
        """Finish sampling after every launched child has been accounted for."""
        self.stop_event.set()
        self.thread.join()
        self.sample()
        return self.summary()

    def summary(self):
        """Use CPU seconds and wall time as utilization evidence, independently of concurrency."""
        elapsed = self.samples[-1]["elapsed_seconds"]
        measured = sum(self.finished_cpu.values()) if self.finished_known else None
        memory = [row["active_working_set_bytes"] for row in self.samples
                  if row["active_working_set_bytes"] is not None]
        left, right = self.samples[0]["system"].get("cpu_seconds"), self.samples[-1]["system"].get("cpu_seconds")
        busy = None
        if left and right:
            total = right["kernel"] + right["user"] - left["kernel"] - left["user"]
            busy = 100 * (total - right["idle"] + left["idle"]) / total if total > 0 else None
        return {"total_wall_seconds": elapsed, "logical_processors": os.cpu_count(),
            "peak_concurrent_processes": self.peak_concurrency,
            "started_children": self.started_children, "sample_count": len(self.samples),
            "sample_interval_seconds": self.interval, "system_cpu_busy_percent": busy,
            "batch_child_cpu_seconds": dict(self.finished_cpu) if self.finished_known else None,
            "cpu_known_children": self.finished_known, "cpu_unknown_children": self.finished_unknown,
            "batch_measured_average_cpu_cores": measured / elapsed if measured is not None and elapsed > 0 else None,
            "batch_cpu_percent_of_logical_capacity": 100 * measured / elapsed / (os.cpu_count() or 1)
                if measured is not None and elapsed > 0 else None,
            "peak_sampled_combined_working_set_bytes": max(memory) if memory else None,
            "scope": "CPU counters cover direct host processes, including completed processes, not their descendants; "
                     "system CPU includes unrelated applications; "
                     "working-set peak is sampled and can miss brief peaks; concurrency is not CPU utilization"}


def invoke(command, cell, request, directory, timeout, monitor):
    """Use one JSON stdin request per child and preserve every byte, timeout, and process failure."""
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "request.json", request)
    environment, removed = clean_environment()
    record = {"cell": cell, "request": request, "request_sha256": json_digest(request),
              "removed_environment_keys": removed, "status": "not_started"}
    stdout, stderr = b"", b""
    process = None
    started = time.monotonic()
    try:
        executable = [str(part) for part in command] if isinstance(command, (tuple, list)) else [str(command)]
        process = subprocess.Popen(executable, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        monitor.register(cell, process)
        record["pid"] = process.pid
        try:
            stdout, stderr = process.communicate((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            record["status"] = "timeout"
        record["returncode"] = process.returncode
        if record["status"] != "timeout":
            parsed = [json.loads(line) for line in stdout.decode("utf-8", errors="replace").splitlines()
                      if line.strip().startswith("{")]
            if process.returncode:
                record["status"] = "process_error"
            elif len(parsed) != 1 or not isinstance(parsed[0], dict):
                record["status"] = "response_count_error"
            else:
                response = parsed[0]
                write_json(directory / "response.json", response)
                record.update(response_path=str((directory / "response.json").resolve()), response=response)
                record["status"] = "completed" if response.get("ok") is True else "response_error"
                record["outcome"] = response.get("outcome")
                record["terminal"] = response.get("ok") is True and response.get("outcome") in ("won", "lost")
                compact = compact_cell(record)
                for key in ("floor", "floor_source", "action_count", "adapter_error"):
                    record[key] = compact[key]
                if "outcome" in response and not record["terminal"]:
                    record["status"] = "noncompletion"
    except Exception as error:
        record.update(status="driver_error", error=f"{type(error).__name__}: {error}")
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                stdout, stderr = process.communicate()
            record["process_cpu"], record["process_memory"] = monitor.finish(cell, process)
        (directory / "stdout.jsonl").write_bytes(stdout)
        (directory / "stderr.log").write_bytes(stderr)
        record["wall_seconds"] = time.monotonic() - started
        write_json(directory / "attempt.json", record)
    return record


def fixture_groups(records, output):
    """Export every host group intact and merge only exact error signatures across cells."""
    clusters = {}
    coverage = Counter()
    missing = []
    post_signatures = set()
    first_signatures = set()
    for row in records:
        batch = row.get("response", {}).get("batch_diagnostics")
        if not isinstance(batch, dict):
            missing.append(row["cell"])
            continue
        for key in ("checks", "failed_checks", "checks_after_first_failure"):
            if isinstance(batch.get(key), int):
                coverage[key] += batch[key]
        first_step = batch.get("first_failure_step")
        events = [error for error in batch.get("errors", []) if isinstance(error, dict)]
        first = {str(error["signature"]) for error in events
                 if "signature" in error and error.get("step") == first_step}
        after = {str(error["signature"]) for error in events if "signature" in error
                 and isinstance(error.get("step"), int) and isinstance(first_step, int)
                 and error["step"] > first_step}
        first_signatures.update(first)
        post_signatures.update(after - first)
        if first_step is not None:
            coverage["cells_with_first_failure"] += 1
        coverage["new_signature_cell_pairs_after_first_failure"] += len(after - first)
        groups = batch.get("groups", [])
        if isinstance(groups, dict):
            groups = list(groups.values())
        for ordinal, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            signature = str(group.get("signature", "unreported_signature"))
            cluster = clusters.setdefault(signature, {"signature": signature, "occurrences": 0,
                "cells": set(), "groups": [], "kinds": set(), "floors": set(), "after_first_failure_occurrences": 0})
            group_key = group.get("group_key")
            first_record = group.get("first_record") or {}
            projection = first_record.get("projection")
            request = {"op": "alignment_run", "diagnostic": "public_fixture",
                       "kind": first_record.get("kind"), "projection": projection}
            fixture = {"schema_version": 1, "source_cell": row["cell"], "signature": signature,
                "group_key": group_key, "host_group": group, "request": request,
                "request_sha256": json_digest(request), "baseline_errors": error_signatures(first_record) or [signature],
                "replayable": isinstance(projection, dict) and isinstance(projection.get("public_state"), dict),
                "original_run_request": row["request"],
                "scope": "literal first public fixture per host group; repeated signatures do not prove the same root cause"}
            destination = output / "fixtures" / row["cell"] / f"group-{ordinal:04d}-{json_digest(group_key)[:12]}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_json(destination, fixture)
            reference = {"cell": row["cell"], "group_key": group_key, "count": group.get("count", 0),
                "first_step": group.get("first_step"), "last_step": group.get("last_step"),
                "fixture": str(destination.resolve()), "fixture_sha256": sha256(destination),
                "replayable": fixture["replayable"]}
            cluster["groups"].append(reference)
            cluster["occurrences"] += group.get("count", 0)
            cluster["cells"].add(row["cell"])
            cluster["kinds"].add(str(first_record.get("kind")))
            cluster["floors"].add(str(first_record.get("floor")))
            cluster["after_first_failure_occurrences"] += sum(error.get("group_key") == group_key
                and isinstance(error.get("step"), int) and isinstance(first_step, int)
                and error["step"] > first_step for error in events)
    result = []
    for cluster in clusters.values():
        for key in ("cells", "kinds", "floors"):
            cluster[key] = sorted(cluster[key])
        cluster["distinct_cells"] = len(cluster["cells"])
        result.append(cluster)
    result.sort(key=lambda group: (-group["occurrences"], group["signature"]))
    write_json(output / "clusters.json", result)
    return {"reported_cells": len(records) - len(missing), "missing_batch_diagnostics_cells": missing,
        **dict(coverage), "distinct_signatures": len(result),
        "new_signatures_after_first_failure_in_at_least_one_cell": sorted(post_signatures),
        "signatures_found_only_after_first_failures_across_panel": sorted(post_signatures - first_signatures),
        "exported_groups": sum(len(cluster["groups"]) for cluster in result),
        "replayable_groups": sum(group["replayable"] for cluster in result for group in cluster["groups"]),
        "scope": "host import/support/meta mapping checks on the audit driver's paths; "
                 "first-failure comparison is within each cell, not a counterfactual strict-policy continuation; "
                 "exact signature clustering does not establish identical root causes"}


def summarize(records, mode):
    """Keep diagnostic driver terminals separate from strict public policy outcomes."""
    result = {"attempts": len(records), "statuses": dict(Counter(row["status"] for row in records)),
              "outcomes": dict(Counter(row.get("outcome", "unreported") for row in records))}
    result["error_families"] = error_families(records)
    result["error_family_scope"] = ("structurally similar symptoms, not established root causes; "
        "only a leading h<digits>: or s<digits>: card handle is removed; "
        "card IDs, other text, and all expected/actual numbers remain unchanged")
    if mode == "audit":
        result["diagnostic_driver"] = {"terminal": sum(bool(row.get("terminal")) for row in records),
            "outcomes": result["outcomes"], "scope": "original driver terminal outcomes are not public policy wins or losses"}
        result["scope"] = "offline_public_boundary_diagnostics_not_public_or_native_win_rate"
    elif mode == "public":
        wins = sum(row.get("terminal") is True and row.get("outcome") == "won" for row in records)
        losses = sum(row.get("terminal") is True and row.get("outcome") == "lost" for row in records)
        result["public_policy"] = {"terminal": wins + losses, "wins": wins, "losses": losses,
            "policy_halt": sum(row.get("outcome") == "policy_halt" for row in records),
            "incomplete": len(records) - wins - losses,
            "win_fraction_all_attempts": wins / len(records) if records else None}
        result["cells"] = [compact_cell(row) for row in sorted(records, key=lambda row: row["cell"])]
        result["cell_counter_scope"] = "action_count counts recorded engine edges; an inferred floor is the latest recorded root, not post-last-action proof"
        result["scope"] = "strict_offline_public_policy_full_runs_not_native_game_win_rate"
    else:
        result["scope"] = "literal_public_fixture_availability_replay_not_search_or_full_run_results"
    timings = []
    for row in records:
        response = row.get("response", {})
        batch = response.get("batch_diagnostics") or {}
        timings.append(response.get("stage_milliseconds", batch.get("stage_milliseconds")))
    keys = sorted({key for timing in timings if isinstance(timing, dict) for key in timing})
    result["host_stage_milliseconds"] = {key: {
        "sum_reported": sum(timing[key] for timing in timings if isinstance(timing, dict)
                            and isinstance(timing.get(key), (int, float))),
        "known_cells": sum(isinstance(timing, dict) and isinstance(timing.get(key), (int, float))
                           for timing in timings),
        "unknown_cells": sum(not isinstance(timing, dict) or not isinstance(timing.get(key), (int, float))
                             for timing in timings)} for key in keys}
    result["host_stage_timing_scope"] = "reported host stages may overlap; stage sums and concurrent child walls are not batch wall time"
    return result


def replay_cells(baseline):
    """Load all exported fixtures and retain their exact request and file hashes."""
    root = baseline / "fixtures"
    sources = sorted(root.rglob("*.json")) if root.is_dir() else []
    cells = []
    for ordinal, source in enumerate(sources):
        fixture = json.loads(source.read_text(encoding="utf-8-sig"))
        request = fixture["request"]
        if request.get("op") != "alignment_run" or request.get("diagnostic") != "public_fixture":
            raise ValueError("Unexpected fixture request: " + str(source))
        digest = json_digest(request)
        if digest != fixture.get("request_sha256"):
            raise ValueError("Fixture request hash changed: " + str(source))
        cells.append((f"replay-{ordinal:05d}-{digest[:12]}", request, {
            "source_fixture": str(source.resolve()), "source_fixture_sha256": sha256(source),
            "source_cell": fixture["source_cell"], "signature": fixture["signature"],
            "group_key": fixture.get("group_key"), "baseline_errors": fixture.get("baseline_errors", []),
            "replayable": fixture.get("replayable", False), "input_request_sha256": digest}))
    return cells


def replay_comparison(record, baseline):
    """Compare exact error sets while retaining unavailable process responses as unknown."""
    before = set(baseline["baseline_errors"])
    response = record.get("response")
    after = set(error_signatures(response)) if isinstance(response, dict) else None
    return {**baseline, "cell": record["cell"], "status": record["status"],
        "input_request_hash_equal": record["request_sha256"] == baseline["input_request_sha256"],
        "baseline_errors": sorted(before), "replay_errors": sorted(after) if after is not None else None,
        "removed_errors": sorted(before - after) if after is not None else None,
        "added_errors": sorted(after - before) if after is not None else None,
        "retained_errors": sorted(before & after) if after is not None else None,
        "baseline_signature_absent": baseline["signature"] not in after if after is not None else None,
        "replay_available": response.get("ok") if isinstance(response, dict) else None,
        "scope": "same literal fixture import/support/meta availability; error absence alone is not a root-cause proof"}


def run_batch(command, cells, output, jobs, timeout, interval=1.0, mode="audit"):
    """Collect independent one-request children and save incremental evidence before summarizing."""
    monitor = BatchMonitor(output, interval)
    monitor.start()
    records, comparisons = [], []
    try:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            pending = {}
            for cell, request, baseline in cells:
                if baseline is not None and not baseline["replayable"]:
                    directory = output / cell
                    directory.mkdir(parents=True, exist_ok=False)
                    write_json(directory / "request.json", request)
                    record = {"cell": cell, "request": request, "request_sha256": json_digest(request),
                              "status": "fixture_unreplayable", "wall_seconds": 0,
                              "error": "first_record_has_no_complete_public_projection"}
                    write_json(directory / "attempt.json", record)
                    with (output / "attempts.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    records.append(record)
                    comparison = replay_comparison(record, baseline)
                    comparisons.append(comparison)
                    write_json(directory / "comparison.json", comparison)
                    continue
                future = executor.submit(invoke, command, cell, request, output / cell, timeout, monitor)
                pending[future] = baseline
            for future in as_completed(pending):
                record = future.result()
                records.append(record)
                if pending[future] is not None:
                    comparison = replay_comparison(record, pending[future])
                    comparisons.append(comparison)
                    write_json(output / record["cell"] / "comparison.json", comparison)
                with (output / "attempts.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(json.dumps({"cell": record["cell"], "status": record["status"],
                    "outcome": record.get("outcome"), "wall_seconds": record["wall_seconds"]}), flush=True)
    finally:
        resources = monitor.close()
    records.sort(key=lambda row: row["cell"])
    write_json(output / "attempts.json", records)
    result = summarize(records, mode)
    result["resources"] = resources
    if mode == "audit":
        result["public_boundary_coverage"] = fixture_groups(records, output)
    elif mode == "replay":
        comparisons.sort(key=lambda row: row["cell"])
        write_json(output / "replay-comparisons.json", comparisons)
        result["replay"] = {"fixtures": len(comparisons),
            "available": sum(row["replay_available"] is True for row in comparisons),
            "unknown_or_unreplayable": sum(row["replay_errors"] is None for row in comparisons),
            "baseline_signature_absent": sum(row["baseline_signature_absent"] is True for row in comparisons),
            "fixtures_with_added_errors": sum(bool(row["added_errors"]) for row in comparisons)}
    write_json(output / "summary.json", result)
    return result


def freeze_host(source, output):
    """Freeze an executable and copy only matching build provenance without guessing its profile."""
    frozen = output / "host" / source.name
    frozen.parent.mkdir(parents=True)
    shutil.copy2(source, frozen)
    digest = sha256(frozen)
    provenance = source.parent / "source-provenance.json"
    matched = False
    if provenance.is_file():
        data = json.loads(provenance.read_text(encoding="utf-8-sig"))
        if data.get("executable_sha256", "").lower() == digest:
            shutil.copy2(provenance, frozen.parent / provenance.name)
            matched = True
    shutil.copy2(Path(__file__), output / "batch-driver.py")
    # The helper remains part of the frozen runner's actual implementation identity.
    shutil.copy2(Path(__file__).with_name("validation.py"), output / "validation.py")
    return frozen, {"source": str(source), "frozen": str(frozen), "sha256": digest,
                    "matching_build_provenance_copied": matched}


def main():
    """Build a fixed five-character panel or replay a previous panel's literal fixtures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, default=DEFAULT_HOST)
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("audit", "public"), default="audit")
    parser.add_argument("--replay-from", type=Path)
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--characters", default=",".join(CHARACTERS))
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--all-cpus", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--profile", choices=("corrected", "pristine"), default="corrected")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--simulations", type=int, default=1000)
    parser.add_argument("--boss-simulations", type=int, default=4000)
    parser.add_argument("--max-steps", type=int, default=100000)
    parser.add_argument("--stall-limit", type=int, default=256)
    parser.add_argument("--stage-profile", choices=("native_ui", "served"), default="native_ui")
    parser.add_argument("--potion-swap", choices=("on", "off"), default="on")
    parser.add_argument("--uncertainty-worlds", type=int, default=32)
    parser.add_argument("--full-unlock", action="store_true")
    parser.add_argument("--capture-internal-roots", action="store_true")
    args = parser.parse_args()
    if args.jobs < 1 or args.timeout <= 0 or args.sample_interval <= 0:
        parser.error("jobs, timeout and sample interval must be positive")
    characters = args.characters.split(",")
    if not args.replay_from and (not characters or any(character not in CHARACTERS for character in characters)):
        parser.error("characters must use the five explicit lowercase character names")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    host, artifact = freeze_host(args.host.resolve(), args.output)
    model_map = {}
    if args.replay_from:
        cells = replay_cells(args.replay_from.resolve())
        mode = "replay"
        if not cells:
            parser.error("replay baseline contains no exported fixtures")
    else:
        mode = args.mode
        model_map = models(args.models.resolve())
        for character, model in model_map.items():
            target = args.output / "models" / f"{character}{Path(model['path']).suffix}"
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(model["path"], target)
            model.update(source_path=model["path"], path=str(target))
        cells = []
        for character in characters:
            for ordinal, seed in enumerate(args.seeds):
                weight = model_map[character]
                request = {"op": "alignment_run", "mode": mode, "seed": seed, "character": character,
                    "model_path": weight["path"], "features": weight["features"],
                    "execution_profile": args.profile, "ascension": args.ascension, "act1": "overgrowth",
                    "simulations": args.simulations, "boss_simulations": args.boss_simulations,
                    "max_steps": args.max_steps, "livelock_stall_limit": args.stall_limit,
                    "timeout_ms": int(args.timeout * 1000), "rollout_policy": "upstream",
                    "action_prior": "upstream", "value_model": "texel", "search_profile": "upstream",
                    "engine_stage_profile": args.stage_profile,
                    "interactive_potion_swap": args.potion_swap == "on",
                    "capture_internal_roots": args.capture_internal_roots,
                    "public_uncertainty": {"worlds": args.uncertainty_worlds,
                                           "full_unlock_catalogue": args.full_unlock}}
                if mode == "audit":
                    request["diagnostic"] = "public_batch"
                cells.append((f"{mode}-{character}-{ordinal:03d}-{seed}", request, None))
    metrics = WindowsMetrics()
    manifest = {"schema_version": 1, "mode": mode, "host": artifact, "models": model_map,
        "jobs": args.jobs, "logical_processors": os.cpu_count(), "affinity": metrics.affinity(args.all_cpus),
        "hard_timeout_seconds": args.timeout, "sample_interval_seconds": args.sample_interval,
        "requests": [{"cell": cell, "request": request, "replay_baseline": baseline}
                     for cell, request, baseline in cells],
        "runner_sha256": sha256(Path(__file__)),
        "helper_sha256": sha256(Path(__file__).with_name("validation.py")),
        "scope": "offline only; audit driver terminals and fixture replays are not public policy win-rate evidence"}
    write_json(args.output / "manifest.json", manifest)
    summary = run_batch(host, cells, args.output, args.jobs, args.timeout, args.sample_interval, mode)
    print(json.dumps({"mode": mode, "attempts": summary["attempts"], "statuses": summary["statuses"],
        "outcomes": summary["outcomes"], "error_family_count": len(summary["error_families"]),
        "resources": summary["resources"], "summary_path": str(args.output / "summary.json")},
        ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

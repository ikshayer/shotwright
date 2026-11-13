"""Independent CPU-only acceptance probes; no model/server imports or GPU work."""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import subprocess
import sys
import time
import traceback
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import psutil

from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows"

CHILD = r"""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from orchestrator.platform.containment import ProjectRoot
root = ProjectRoot(sys.argv[1])
base, role = sys.argv[2:4]
allocation = bytearray(24 * 1024 * 1024)
for i in range(0, len(allocation), 4096): allocation[i] = 37
child = None
if role == 'worker':
    child = subprocess.Popen([sys.executable, '-B', __file__, sys.argv[1], base, 'grandchild'], creationflags=0x08000000)
root.write_text(base + '/' + role + '.json', json.dumps({'pid': os.getpid(), 'role': role, 'executable': sys.executable, 'base_executable': sys._base_executable}))
while True:
    if role == 'worker' and root.resolve(base + '/exit-worker').exists(): break
    time.sleep(.05)
"""

def identities(value):
    """Find serializable identity records without binding to presentation nesting."""
    found = {}
    def walk(item):
        if isinstance(item, dict):
            if isinstance(item.get('pid'), int): found[item['pid']] = item
            for member in item.values(): walk(member)
        elif isinstance(item, (list, tuple)):
            for member in item: walk(member)
    walk(value)
    return found


def alive(identity):
    pid, created = identity
    try:
        proc = psutil.Process(pid)
        return abs(proc.create_time() - created) < .001 and proc.is_running()
    except psutil.NoSuchProcess:
        return False


def await_condition(predicate, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError(message)


def real_tree(module, root, base, orphan):
    root.make_dir(base)
    script = root.write_text(base + '/child.py', CHILD)
    runtime = root.resolve(BASE + '/_work/local_runtime/env/Scripts/python.exe')
    assert runtime.is_file(), 'isolated runtime Python missing'
    proc = subprocess.Popen([str(runtime), '-B', str(script), str(ROOT), base, 'worker'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    owned = module.OwnedProcessTree(proc)
    known = [(proc.pid, psutil.Process(proc.pid).create_time())]
    evidence = {}
    try:
        for role in ('worker', 'grandchild'):
            await_condition(lambda role=role: root.resolve(base + '/' + role + '.json').exists(), role + ' did not start')
            record = json.loads(root.read_text(base + '/' + role + '.json'))
            pid = record['pid']
            known.append((pid, psutil.Process(pid).create_time()))
            evidence[role] = record
        # Windows venv redirector must be represented, not silently replaced by a base-Python fixture.
        assert Path(evidence['worker']['executable']).resolve() == runtime.resolve(), evidence
        owned.refresh()
        snap = owned.snapshot()
        json.dumps(snap)
        ids = identities(snap)
        assert {pid for pid, _ in known} <= set(ids), ('snapshot omitted owned processes', known, snap)
        sample = owned.sample()
        json.dumps(sample)
        assert isinstance(sample, dict) and 'processes' in sample, sample
        samples = identities(sample['processes'])
        assert {evidence['worker']['pid'], evidence['grandchild']['pid']} <= set(samples), sample
        assert any(k in sample for k in ('aggregate_memory_bytes',)), ('aggregate missing', sample)
        aggregate = next(sample[k] for k in ('aggregate_memory_bytes',) if k in sample)
        def rss(record):
            if 'rss' in record: return record['rss']
            if 'rss_bytes' in record: return record['rss_bytes']
            for key in ('memory_bytes',):
                if isinstance(record.get(key), dict): return rss(record[key])
            raise AssertionError(('RSS missing', record))
        per_rss = [rss(record) for record in samples.values()]
        assert rss(aggregate) >= 48 * 1024 * 1024, ('worker allocations not counted', sample)
        assert rss(aggregate) == sum(per_rss), ('aggregate differs from recorded samples', sample)
        evidence.update(snapshot=snap, sample=sample)
        if orphan:
            root.write_text(base + '/exit-worker', 'exit')
            await_condition(lambda: not alive(known[1]), 'worker did not exit')
            await_condition(lambda: proc.poll() is not None, 'venv launcher did not exit')
            assert alive(known[2]), 'grandchild unexpectedly exited with worker'
            owned.refresh()
            assert known[2][0] in identities(owned.snapshot()), 'cached grandchild lost after ancestors exited'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(owned.stop) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        results.append(owned.stop())
        for result in results:
            json.dumps(result)
            assert isinstance(result, dict) and result.get('verified_stopped') is True, ('stop did not verify cleanup', result)
        await_condition(lambda: not any(alive(item) for item in known), 'owned process survived stop', timeout=5)
        evidence['cleanup'] = results
        return evidence
    finally:
        # Independent fixture recovery only: never enumerate/kill arbitrary processes.
        for pid, created in reversed(known):
            if alive((pid, created)):
                try: psutil.Process(pid).kill()
                except psutil.NoSuchProcess: pass
        for pid, created in known:
            try:
                if alive((pid, created)): psutil.Process(pid).wait(timeout=5)
            except psutil.NoSuchProcess: pass
        proc.wait(timeout=5)


class FakeProcess:
    pid = 1900000001
    def __init__(self):
        self.created = 100.0
        self.signals = []
        self.deny = False
        self.memory_reads = 0
    def create_time(self): return self.created
    def children(self, recursive=False): return []
    def is_running(self): return True
    def status(self): return psutil.STATUS_RUNNING
    def memory_info(self):
        self.memory_reads += 1
        if self.deny: raise psutil.AccessDenied(self.pid)
        return namedtuple('pmem', 'rss vms private')(rss=4096, vms=8192, private=4096)
    def terminate(self):
        self.signals.append('terminate')
        if self.deny: raise psutil.AccessDenied(self.pid)
    def kill(self):
        self.signals.append('kill')
        if self.deny: raise psutil.AccessDenied(self.pid)
    def wait(self, timeout=None): raise psutil.TimeoutExpired(timeout, self.pid)


class FakePopen:
    pid = FakeProcess.pid
    def poll(self): return None
    def wait(self, timeout=None): raise subprocess.TimeoutExpired('fixture', timeout)


def guarded_fake(module, denied):
    fake = FakeProcess()
    with patch.object(psutil, 'Process', side_effect=lambda pid: fake if pid == fake.pid else (_ for _ in ()).throw(psutil.NoSuchProcess(pid))):
        tree = module.OwnedProcessTree(FakePopen())
        tree.refresh()
        assert fake.pid in identities(tree.snapshot()), 'fixture identity was never captured'
        if denied: fake.deny = True
        else:
            fake.created += 10
            reads = fake.memory_reads
            try:
                observed = tree.sample()
                assert fake.pid not in identities(observed.get('processes', [])), ('reused PID sampled', observed)
            except (psutil.NoSuchProcess, RuntimeError):
                pass
            assert fake.memory_reads == reads, 'reused PID memory was read'
        started = time.monotonic()
        try:
            result = tree.stop()
            json.dumps(result)
        except Exception as exc:  # noqa: BLE001 - the stop contract permits typed cleanup exceptions
            result = {'exception': type(exc).__name__, 'message': str(exc)}
        assert time.monotonic() - started < 30, 'cleanup not bounded'
        if not denied:
            assert not fake.signals, ('reused PID was signaled', fake.signals)
        else:
            assert 'exception' in result or result.get('verified_stopped') is False, ('unresolved cleanup claimed success', result)
        return {'result': result, 'signals': fake.signals}



class BoundaryProcess:
    """Small psutil boundary fixture: real public methods, independently set failures."""
    def __init__(self, pid, parent_pid=0):
        self.pid = pid
        self.parent_pid = parent_pid
        self.created = float(pid)
        self.descendants = []
        self.identity_denied = False
        self.memory_denied = False
        self.running = True
        self.signals = []
        self.returncode = None
        self.memory_reads = 0

    def create_time(self):
        if self.identity_denied:
            raise psutil.AccessDenied(self.pid)
        return self.created

    def children(self, recursive=False):
        return [child for child in self.descendants if child.running]

    def is_running(self): return self.running
    def status(self): return psutil.STATUS_RUNNING if self.running else psutil.STATUS_DEAD
    def ppid(self): return self.parent_pid
    def name(self): return 'python.exe'
    def exe(self): return 'D:/fixture/python.exe'
    def cmdline(self): return ['D:/fixture/python.exe', '-B', 'contained_fixture.py']

    def memory_info(self):
        self.memory_reads += 1
        if self.memory_denied:
            raise psutil.AccessDenied(self.pid)
        return namedtuple('pmem', 'rss vms private')(4096, 8192, 4096)

    def memory_full_info(self): return self.memory_info()

    def terminate(self):
        self.signals.append('terminate')
        self.running = False
        self.returncode = 0

    def kill(self):
        self.signals.append('kill')
        self.running = False
        self.returncode = 0

    def wait(self, timeout=None):
        if self.running:
            raise psutil.TimeoutExpired(timeout, self.pid)
        return self.returncode


def denied_boundary(module, memory_only):
    launcher = BoundaryProcess(FakePopen.pid)
    worker = BoundaryProcess(FakePopen.pid + 1, launcher.pid)
    launcher.descendants = [worker]
    processes = {process.pid: process for process in (launcher, worker)}

    def lookup(pid):
        process = processes.get(pid)
        if process is None or not process.running:
            raise psutil.NoSuchProcess(pid)
        return process

    with patch.object(psutil, 'Process', side_effect=lookup):
        tree = module.OwnedProcessTree(FakePopen())
        tree.refresh()
        captured = tree.snapshot()
        assert set(processes) <= set(identities(captured)), ('both identities must be captured before denial', captured)
        baseline = tree.sample()
        assert set(processes) <= set(identities(baseline['processes'])), baseline
        if memory_only:
            worker.memory_denied = True
            caught = None
            try:
                tree.sample()
            except Exception as exc:  # noqa: BLE001 - contract requires accounting failure to propagate
                caught = {'type': type(exc).__name__, 'message': str(exc)}
            assert caught is not None, 'memory access denial silently returned partial accounting'
            assert not launcher.signals and not worker.signals, 'sampling signaled a process'
            return {'captured': captured, 'baseline': baseline, 'sample_error': caught}

        launcher.identity_denied = True
        outcomes = []
        for _attempt in range(2):
            started = time.monotonic()
            try:
                outcome = tree.stop()
            except Exception as exc:  # noqa: BLE001 - either retained evidence or a raised cleanup error is allowed
                outcome = {'exception': type(exc).__name__, 'message': str(exc)}
            json.dumps(outcome)
            assert time.monotonic() - started < 30, 'denied cleanup was not bounded'
            assert 'exception' in outcome or outcome.get('verified_stopped') is False, ('unresolved ownership became verified', outcome)
            assert not launcher.signals, 'identity-denied process was signaled'
            assert worker.signals, 'verifiable cached worker was abandoned because another identity was denied'
            assert not worker.running, 'verifiable worker survived cleanup'
            outcomes.append(outcome)
        return {'captured': captured, 'outcomes': outcomes, 'launcher_signals': launcher.signals, 'worker_signals': worker.signals}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', default=BASE + '/_work/local_runs/_lifecycle/report.json')
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    spec = importlib.util.spec_from_file_location('local_h3_under_test', root.resolve(BASE + '/local_h3.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    run = BASE + '/_work/local_runs/_lifecycle/' + str(time.time_ns())
    report = {'platform': sys.platform, 'python': sys.executable, 'cases': [], 'unverified': ['GPU inference, watchdog run-finally integration, model loading, real PID reuse (simulated identity change only)']}
    assert sys.platform == 'win32', 'Windows-only acceptance probe'
    cases = [('live_venv_tree_concurrent_idempotent_stop', lambda: real_tree(module, root, run + '/live', False)), ('cached_orphan_after_ancestor_exit', lambda: real_tree(module, root, run + '/orphan', True)), ('pid_reuse_guard', lambda: guarded_fake(module, False)), ('unresolved_cleanup_is_not_success', lambda: guarded_fake(module, True)), ('identity_denial_still_cleans_verified_member_and_remains_failed', lambda: denied_boundary(module, False)), ('memory_denial_cannot_return_partial_accounting', lambda: denied_boundary(module, True))]
    for name, case in cases:
        try: report['cases'].append({'name': name, 'passed': True, 'evidence': case()})
        except Exception:  # noqa: BLE001 - retain every independent acceptance failure in the report
            report['cases'].append({'name': name, 'passed': False, 'traceback': traceback.format_exc()})
        root.write_text(args.report, json.dumps(report, indent=2))
    report['passed'] = all(case['passed'] for case in report['cases'])
    root.write_text(args.report, json.dumps(report, indent=2))
    print(json.dumps({'report': args.report, 'passed': report['passed'], 'cases': [{k: c[k] for k in ('name', 'passed')} for c in report['cases']]}, indent=2))
    return 0 if report['passed'] else 1

if __name__ == '__main__': raise SystemExit(main())

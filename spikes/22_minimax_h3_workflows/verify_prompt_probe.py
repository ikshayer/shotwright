"""Independent Amendment7 phase-B CPU tests and actual prompt-probe verification.

Expectations were authored from the contract and retained A, without reading the
runner. Self-tests never invoke the native controller: only its call boundary is
mocked. Actual verification delegates unchanged media checks to the frozen verifier.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import socket
import sys
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows"
RUNS = BASE + "/_work/local_runs"
INVESTIGATION = RUNS + "/_latent_investigation"
PROBE = INVESTIGATION + "/prompt_b_v2"
ORIGINAL = BASE + "/_work/test_inputs/20261109T165721Z_e1e1bf74/manifest.json"
ORIGINAL_SHA = "c31180332c4732dcb2d73e8e38f72415467369389d3c4775c0e895f08b7042ab"
BASELINE = RUNS + "/20261110T162940Z_89b935ab"
BASELINE_GRAPH_SHA = "feb9b98e4ef5b67e0596caaa78f45884b8e71a72dc5b6bbf77e14d3819c77b35"
DRIVER = BASE + "/prompt_probe.py"
CONTROLLER = BASE + "/local_h3.py"
EXTENSION = BASE + "/stage_extension/h3_staging/__init__.py"
FROZEN = {
    CONTROLLER: "8783207c2ba05777538c2e8b6d55be76c39e3546b87233bd5c2427b25a8b6331",
    EXTENSION: "1936bc43f48e4512cb42d6ac64bdbf598f632cbf5e26f18fd292216abfb2e519",
}
HEADROOM = 10 * 1024**3
SEED = 9952211358998416546
HEADINGS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load(root, relative):
    return json.loads(root.read_text(relative))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            result.update(block)
    return result.hexdigest()


def identity(root, relative):
    path = root.resolve(relative)
    return {"path": relative, "sha256": digest(path), "bytes": path.stat().st_size}


def checked(root, record):
    observed = identity(root, record["path"])
    require(observed["sha256"] == record["sha256"], "hash mismatch: " + record["path"])
    if "bytes" in record:
        require(record["bytes"] == observed["bytes"], "byte count mismatch: " + record["path"])
    return observed


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def records(value):
    return [row for row in walk(value) if isinstance(row, dict) and isinstance(row.get("path"), str) and isinstance(row.get("sha256"), str)]


def exact_prompt(root):
    text = root.read_text(INVESTIGATION + "/next_probe_alt_findings.txt")
    block = re.search(r"^```text\s*\n(.*?)^```\s*$", text, re.MULTILINE | re.DOTALL)
    require(block is not None, "retained first text block absent")
    prompt = block.group(1).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    require(re.findall(r"^([a-z_]+):$", prompt, re.MULTILINE) == HEADINGS, "six prompt headings differ")
    require("<Subject 1>" in prompt and "<Video 1>" in prompt and "<Audio" not in prompt, "reference roles differ")
    require(prompt.endswith("non_diegetic_music:\nN/A\n"), "music section differs")
    return prompt.encode("utf-8")


def original_checks(root):
    require(digest(root.resolve(ORIGINAL)) == ORIGINAL_SHA, "canonical original manifest changed")
    original = load(root, ORIGINAL)
    baseline = load(root, BASELINE + "/generation.json")
    require(baseline["provenance"]["source_manifest"] == original, "retained A source manifest disagrees")
    for key in ("source", "input_video", "prompt", "request_template", "contact_sheet"):
        checked(root, original[key])
    require(digest(root.resolve(BASELINE + "/graph.json")) == BASELINE_GRAPH_SHA, "retained A graph changed")
    checked(root, baseline["graph"])
    checked(root, baseline["runtime"]["schema"])
    for rel, expected in FROZEN.items():
        require(digest(root.resolve(rel)) == expected, "frozen code changed: " + rel)
    for record in records(baseline["provenance"]["current_source_notices"].get("current_source_snapshots", [])):
        checked(root, record)
    return original, baseline


def manifest_checks(root, candidate, probe_rel):
    original, baseline = original_checks(root)
    require(set(candidate) == set(original) | {"supersedes", "diagnostic_variant"}, "manifest top-level fields differ")
    retained = copy.deepcopy(candidate)
    retained.pop("supersedes")
    retained.pop("diagnostic_variant")
    retained["prompt"] = original["prompt"]
    require(retained == original, "manifest changed a non-prompt original field")
    prompt_path = probe_rel + "/prompt.txt"
    require(candidate["prompt"]["path"] == prompt_path, "candidate prompt path differs")
    checked(root, candidate["prompt"])
    require(root.read_bytes(prompt_path) == exact_prompt(root), "candidate prompt bytes differ")
    supersedes = candidate["supersedes"]
    require(supersedes["path"] == ORIGINAL and supersedes["sha256"] == ORIGINAL_SHA, "supersedes differs")
    checked(root, supersedes)
    variant = candidate["diagnostic_variant"]
    require(variant["kind"] == "prompt_only" and variant["baseline_run"] in (BASELINE, Path(BASELINE).name) and variant["changed_fields"] == ["prompt"], "diagnostic variant differs")
    require(variant["launcher"]["path"] == DRIVER, "launcher path differs")
    checked(root, variant["launcher"])
    require("bytes" in variant["launcher"], "launcher byte count missing")
    return original, baseline


def expected_graph(root):
    baseline = load(root, BASELINE + "/graph.json")
    require(baseline["3"]["inputs"]["prompt"] == root.read_text(load(root, ORIGINAL)["prompt"]["path"]), "retained A prompt differs")
    graph = copy.deepcopy(baseline)
    graph["3"]["inputs"]["prompt"] = exact_prompt(root).decode("utf-8")
    require(graph["6"]["inputs"]["noise_seed"] == SEED, "fixed seed differs")
    return graph


def prepared_checks(root, probe_rel=PROBE):
    candidate = load(root, probe_rel + "/manifest.json")
    manifest_checks(root, candidate, probe_rel)
    variant = load(root, probe_rel + "/variant.json")
    require(variant["status"] == "prepared_not_queued", "variant falsely claims queue/completion")
    rows = records(variant)
    for row in rows:
        checked(root, row)
    indexed = {row["path"]: row for row in rows}
    for rel in (ORIGINAL, probe_rel + "/manifest.json", probe_rel + "/prompt.txt", DRIVER, CONTROLLER, EXTENSION):
        require(rel in indexed, "variant identity missing: " + rel)
    for rel in (DRIVER, CONTROLLER, EXTENSION):
        code = identity(root, rel)
        archives = [row for row in rows if row["path"] != rel and row["sha256"] == code["sha256"] and root.resolve(row["path"]).is_relative_to(root.resolve(probe_rel))]
        require(bool(archives), "frozen code archive missing: " + rel)
    # Read graph evidence via actual hashed JSON records or inline graph objects;
    # names/benign extra metadata do not define graph correctness.
    values = list(walk(variant))
    for row in rows:
        if row["path"].endswith(".json") and root.resolve(row["path"]).stat().st_size < 5_000_000:
            values.extend(walk(load(root, row["path"])))
    before = load(root, BASELINE + "/graph.json")
    after = expected_graph(root)
    require(any(value == before for value in values), "exact before graph evidence missing")
    require(any(value == after for value in values), "exact prompt-only after graph missing")
    require(HEADROOM in list(walk(variant)), "10 GiB headroom policy missing")
    return {"candidate": identity(root, probe_rel + "/manifest.json"), "prompt": identity(root, probe_rel + "/prompt.txt"), "variant": identity(root, probe_rel + "/variant.json"), "launcher": identity(root, DRIVER), "seed": SEED, "graph_only_changed_field": "3.inputs.prompt", "schema_evidence": "retained A fixture; actual native server validation still required"}


def import_module(relative, name):
    sys.path.insert(0, str(ROOT / BASE))
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def no_execution():
    """Prevent accidental process/network execution in CPU contract tests."""
    def denied(*args, **kwargs):
        raise AssertionError("CPU test attempted process/network execution")
    with patch("subprocess.Popen", side_effect=denied), patch("os.system", side_effect=denied), patch.object(socket.socket, "connect", side_effect=denied), patch.object(socket.socket, "connect_ex", side_effect=denied):
        yield


_AUDIT = None


def audit_hook(event, args):
    if _AUDIT is None:
        return
    if event == "open":
        path, mode, flags = args
        if (isinstance(mode, str) and any(char in mode for char in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            _AUDIT.append((event, path))
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.truncate", "os.chmod", "os.utime"}:
        _AUDIT.extend((event, path) for path in args[:2] if isinstance(path, (str, bytes, os.PathLike)))


sys.addaudithook(audit_hook)


@contextmanager
def writes_only(root, allowed=None):
    global _AUDIT
    require(_AUDIT is None, "nested audit scope")
    events = []
    _AUDIT = events
    try:
        yield events
    finally:
        _AUDIT = None
        for event, raw_path in events:
            require(allowed is not None, "import mutated filesystem: " + str((event, raw_path)))
            path = Path(os.fsdecode(raw_path)).resolve()
            allowed_roots = allowed if isinstance(allowed, (tuple, list)) else (allowed,)
            contained = any(path.is_relative_to(root.resolve(relative)) for relative in allowed_roots)
            require(contained or path.is_relative_to(root.resolve(".projectroot-atomic-write")), "write escaped scratch: " + str((event, raw_path)))


def immutable_snapshot(root):
    paths = [ORIGINAL, BASELINE + "/generation.json", BASELINE + "/graph.json", CONTROLLER, EXTENSION, DRIVER]
    original = load(root, ORIGINAL)
    paths += [original[key]["path"] for key in ("input_video", "prompt", "source")]
    return {rel: digest(root.resolve(rel)) for rel in paths}


def exception_nodes(exc):
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend((current.__cause__, current.__context__))


def rejects(operation):
    try:
        operation()
    except Exception as exc:  # noqa: BLE001 - retain rejection evidence from arbitrary validation failures
        require(not any(isinstance(item, AssertionError) for item in exception_nodes(exc)), "forbidden execution or test assertion in exception chain: " + str(exc))
        return type(exc).__name__ + ": " + str(exc)
    raise ValueError("operation accepted divergent artifacts")


def self_test(root):
    suite = INVESTIGATION + "/verify_prompt_probe_" + uuid.uuid4().hex
    root.make_dir(suite)
    checks = []
    driver = None
    before = immutable_snapshot(root)

    def case(name, operation):
        try:
            evidence = operation()
            checks.append({"name": name, "passed": True, "evidence": evidence})
        except Exception as exc:  # noqa: BLE001 - collect every independent test failure
            checks.append({"name": name, "passed": False, "error": type(exc).__name__ + ": " + str(exc), "traceback": traceback.format_exc()})

    def import_test():
        nonlocal driver
        existing = set(sys.modules)
        with no_execution(), writes_only(root):
            driver = import_module(DRIVER, "prompt_probe_blind_" + uuid.uuid4().hex)
        require(not any(key == "torch" or key.startswith(("torch.", "comfy.")) for key in set(sys.modules)-existing), "runner imported model/tensor runtime")
        require(driver.ROOT == ROOT and driver.PROBE_REL == PROBE, "public root/default path differs")
        require(callable(driver.prepare_probe) and callable(driver.run_probe) and callable(driver.available_ram_bytes), "public API absent")
        return {"writes": 0, "processes": 0, "model_imports": 0}

    case("import_model_mutation_process_free", import_test)
    if driver is None:
        return {"passed": False, "checks": checks, "scratch": suite}

    def public_seam():
        require(hasattr(driver, "h3"), "contracted public h3 native-controller seam missing")
        require(driver.h3.INPUT == ORIGINAL, "import changed native INPUT")
        return {"INPUT":driver.h3.INPUT}
    case("public_h3_controller_seam", public_seam)

    @contextmanager
    def scratch(name, extra_allowed=()):
        relative = suite + "/" + name
        allowed = (suite, *extra_allowed)
        with patch.object(driver, "PROBE_REL", relative), no_execution(), writes_only(root, allowed):
            yield relative

    def exact_preparation():
        with scratch("exact") as rel:
            result = driver.prepare_probe()
            require(isinstance(result, dict), "prepare result is not a dictionary")
            evidence = prepared_checks(root, rel)
            info = load(root, BASELINE + "/object_info.json")
            native_helper = import_module(CONTROLLER, "prompt_probe_real_builder_" + uuid.uuid4().hex)
            graph, seed = native_helper.build_graph(info, load(root, BASELINE + "/graph.json")["1"]["inputs"]["file"], exact_prompt(root).decode(), staged=True, audio_diagnostic=True)
            require(graph == expected_graph(root) and seed["value"] == SEED, "real native builder disagrees with exact retained graph expectation")
            return evidence

    case("exact_candidate_manifest_graph_and_code_archives", exact_preparation)

    def repeat_preparation():
        with scratch("idempotence") as rel:
            driver.prepare_probe()
            first = {str(path.relative_to(ROOT)): digest(path) for path in root.resolve(rel).rglob("*") if path.is_file()}
            driver.prepare_probe()
            second = {str(path.relative_to(ROOT)): digest(path) for path in root.resolve(rel).rglob("*") if path.is_file()}
            require(first == second, "preparation is not immutable/idempotent")
            return {"prepared_file_count":len(first),"bytes_unchanged":True}
    case("matching_existing_preparation_is_idempotent", repeat_preparation)

    for kind in ("prompt", "manifest"):
        def divergent(kind=kind):
            with scratch("divergent_" + kind) as rel:
                driver.prepare_probe()
                target = rel + ("/prompt.txt" if kind == "prompt" else "/manifest.json")
                wrong = b"divergent existing prompt\n" if kind == "prompt" else b'{"wrong":true}\n'
                root.write_bytes(target, wrong)
                error = rejects(driver.prepare_probe)
                require(root.read_bytes(target) == wrong, "divergent existing bytes were overwritten")
                return {"prepare_rejection": error, "existing_bytes_preserved":True}
        case("reject_preexisting_divergent_" + kind, divergent)

    def reject_modified_prepared_artifacts():
        evidence = []
        for kind in ("prompt", "manifest", "graph", "code_archive"):
            with scratch("modified_" + kind) as rel:
                driver.prepare_probe()
                if kind in {"prompt", "manifest"}:
                    target = rel + ("/prompt.txt" if kind == "prompt" else "/manifest.json")
                else:
                    rows = records(load(root, rel + "/variant.json"))
                    if kind == "graph":
                        target = next(row["path"] for row in rows if row["path"].endswith(".json") and root.resolve(row["path"]).is_relative_to(root.resolve(rel)) and load(root,row["path"]) == expected_graph(root))
                    else:
                        expected = identity(root, DRIVER)["sha256"]
                        target = next(row["path"] for row in rows if row["sha256"] == expected and root.resolve(row["path"]).is_relative_to(root.resolve(rel)))
                root.write_bytes(target,b"divergent prepared evidence\n")
                with patch.object(driver, "available_ram_bytes", return_value=HEADROOM), patch.object(driver.h3, "run_local") as native:
                    error = rejects(driver.run_probe)
                    native.assert_not_called()
                require(root.read_bytes(target) == b"divergent prepared evidence\n", "run repaired divergent evidence")
                evidence.append({"kind":kind,"rejection":error})
        return evidence
    case("run_rechecks_prepared_prompt_manifest_graph_code_archive", reject_modified_prepared_artifacts)

    def low_headroom():
        with scratch("held") as rel:
            driver.prepare_probe()
            frozen = {name: root.read_bytes(rel + "/" + name) for name in ("prompt.txt", "manifest.json", "variant.json")}
            with patch.object(driver, "available_ram_bytes", return_value=HEADROOM-1), patch.object(driver.h3, "run_local") as native:
                for _ in range(2):
                    result = driver.run_probe()
                    require(result.get("status") == "held_headroom_no_queue", "low RAM did not return held status")
                native.assert_not_called()
            attempts = list(root.resolve(rel + "/attempts").glob("*.json"))
            require(len(attempts) == 2, "held calls did not retain two new receipts")
            for path in attempts:
                receipt = json.loads(path.read_text())
                require(receipt["status"] == "held_headroom_no_queue", "held receipt status differs")
                require(HEADROOM-1 in list(walk(receipt)), "held receipt lacks actual headroom")
            require(all(root.read_bytes(rel + "/" + name) == data for name,data in frozen.items()), "held run changed prepared artifacts")
            return {"native_calls": 0, "append_only_receipts": 2, "observed_ram": HEADROOM-1}
    case("low_headroom_no_native_call_append_only_receipts", low_headroom)

    def exception_chain(exc):
        chain = []
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            chain.append({"type": type(exc).__name__, "message": str(exc)})
            exc = exc.__cause__ or exc.__context__
        return chain

    original_write_stream = ProjectRoot.write_stream
    original_write_bytes = ProjectRoot.write_bytes

    def native_boundary(failure=False, copy_failure=False):
        label = "failure_copy_failure" if failure and copy_failure else "success_copy_failure" if copy_failure else "failure" if failure else "success"
        native_run = RUNS + "/_verification_native_" + uuid.uuid4().hex
        with scratch(label, extra_allowed=(native_run,)) as rel:
            driver.prepare_probe()
            old_input = driver.h3.INPUT
            calls = []
            frozen_globals = dict(vars(driver.h3))
            candidate = load(root, rel + "/manifest.json")
            result = copy.deepcopy(load(root, BASELINE + "/generation.json"))

            def relocate(value):
                if isinstance(value, str):
                    return value.replace(BASELINE, native_run)
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, dict):
                    return {key: relocate(item) for key, item in value.items()}
                return value

            result = relocate(result)
            result["provenance"]["source_manifest"] = candidate
            result["prompt"] = candidate["prompt"]
            result["status"] = "failed" if failure else "generated_candidate_unapproved"
            native_generation_bytes = None
            require(not root.resolve(native_run).exists(), "native run directory existed before invocation")

            def boundary(*args, **kwargs):
                nonlocal native_generation_bytes
                calls.append({"args": args, "kwargs": kwargs, "input": driver.h3.INPUT})
                require(args == () and kwargs == {"staged": True, "audio_diagnostic": True}, "native arguments changed")
                require(driver.h3.INPUT == rel + "/manifest.json", "candidate INPUT override missing")
                require(set(vars(driver.h3)) == set(frozen_globals), "native global names changed")
                require(all(vars(driver.h3)[key] is value for key, value in frozen_globals.items() if key not in {"INPUT", "run_local"}), "native helpers/globals changed beyond INPUT")
                # Native discovery must observe this run only after the call starts.
                root.write_text(native_run + "/graph.json", json.dumps(expected_graph(root)))
                result["graph"] = identity(root, native_run + "/graph.json")
                root.write_text(native_run + "/generation.json", json.dumps(result))
                native_generation_bytes = root.read_bytes(native_run + "/generation.json")
                if failure:
                    raise RuntimeError("independent_native_failure_sentinel")
                return result

            with patch.object(driver, "available_ram_bytes", return_value=HEADROOM), patch.object(driver.h3, "run_local", side_effect=boundary):
                if copy_failure:
                    def injected_write_stream(instance, relative, *args, **kwargs):
                        if relative.startswith(native_run + "/provenance/"):
                            raise OSError("independent_provenance_copy_sentinel")
                        return original_write_stream(instance, relative, *args, **kwargs)
                    def injected_write_bytes(instance, relative, *args, **kwargs):
                        if relative.startswith(native_run + "/provenance/"):
                            raise OSError("independent_provenance_copy_sentinel")
                        return original_write_bytes(instance, relative, *args, **kwargs)
                    with patch.object(ProjectRoot, "write_stream", autospec=True, side_effect=injected_write_stream), patch.object(ProjectRoot, "write_bytes", autospec=True, side_effect=injected_write_bytes):
                        chain = rejects_chain(driver.run_probe)
                elif failure:
                    chain = rejects_chain(driver.run_probe)
                else:
                    returned = driver.run_probe()
                    require(isinstance(returned, dict), "run result is not a dictionary")
                    chain = []

            require(len(calls) == 1, "native controller called more/less than once")
            require(driver.h3.INPUT == old_input, "native INPUT was not restored")
            require(native_generation_bytes is not None, "mock boundary did not capture native generation bytes")
            require(root.read_bytes(native_run + "/generation.json") == native_generation_bytes, "native generation receipt changed after boundary capture")
            attempts = list(root.resolve(rel + "/attempts").glob("*.json"))
            require(bool(attempts), "native call/error receipt not retained")
            receipts = [json.loads(path.read_text(encoding="utf-8")) for path in attempts]
            linked_receipts = [row for row in receipts if native_run in list(walk(row)) or native_run + "/generation.json" in list(walk(row))]
            require(bool(linked_receipts), "attempt receipt lacks actual native run linkage")
            if failure:
                require(any("independent_native_failure_sentinel" in row["message"] for row in chain), "native failure absent from __cause__ chain")
            if failure or copy_failure:
                receipt = next((row for row in receipts if row.get("copy_error") is not None or row.get("native_error") is not None or row.get("error") is not None), None)
                require(receipt is not None, "failed attempt receipt lacks native/copy error evidence")
                require(native_run in list(walk(receipt)) or native_run + "/generation.json" in list(walk(receipt)), "failed attempt receipt lacks actual native run linkage")
                if failure:
                    require("independent_native_failure_sentinel" in json.dumps(receipt.get("error")), "retained receipt lost native failure")
                else:
                    require(receipt.get("native_error") is None, "successful native call was classified as native failure")
                if copy_failure:
                    require("independent_provenance_copy_sentinel" in json.dumps(receipt.get("copy_error")), "retained receipt lost copy failure sentinel")
                else:
                    require(receipt.get("copy_error") is None, "unexpected copy_error classification")
                require(sum(len(row.get("native_calls", [])) if isinstance(row.get("native_calls"), list) else 0 for row in receipts) <= 1, "receipt indicates a retry")
                copies = [path for path in root.resolve(native_run).rglob("*") if path.is_file() and "provenance" in str(path).lower()]
                if not copy_failure:
                    for rel_source in (DRIVER, rel + "/variant.json", rel + "/prompt.txt", rel + "/manifest.json"):
                        require(any(digest(path) == digest(root.resolve(rel_source)) for path in copies), "failure provenance copy missing: " + rel_source)
            return {"calls": len(calls), "input_restored": True, "native_receipt_unchanged": True, "failure": failure, "copy_failure": copy_failure, "receipt_count": len(receipts)}

    # Capture exception objects directly so the native cause chain is independently inspected.
    def rejects_chain(operation):
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - evidence capture intentionally accepts native wrapper types
            return exception_chain(exc)
        raise ValueError("operation unexpectedly succeeded")

    for failure, copy_failure, name in ((False, False, "success"), (True, False, "failure"), (True, True, "failure_copy_failure"), (False, True, "success_copy_failure")):
        def run_boundary(failure=failure, copy_failure=copy_failure):
            return native_boundary(failure=failure, copy_failure=copy_failure)
        case("one_native_call_exact_args_INPUT_restored_" + name, run_boundary)

    def tamper_dependencies():
        baseline = load(root, BASELINE + "/generation.json")
        targets = [
            ("canonical_manifest", ORIGINAL),
            ("retained_schema", baseline["runtime"]["schema"]["path"]),
            ("retained_baseline_graph", BASELINE + "/graph.json"),
            ("current_launcher", DRIVER),
            ("current_controller", CONTROLLER),
            ("current_extension", EXTENSION),
        ]
        targets.extend((kind, None) for kind in (
            "prepared_baseline_graph", "candidate_graph", "controller_archive", "extension_archive"
        ))
        original_open = Path.open
        evidence = []

        for index, (kind, fixed_source) in enumerate(targets):
            rel = suite + "/run_tamper_" + str(index)
            with patch.object(driver, "PROBE_REL", rel), no_execution(), writes_only(root, suite):
                driver.prepare_probe()
                source_rel = fixed_source
                if source_rel is None:
                    variant = load(root, rel + "/variant.json")
                    before_graph = load(root, BASELINE + "/graph.json")
                    after_graph = expected_graph(root)
                    if kind == "prepared_baseline_graph":
                        source_rel = next((row["path"] for row in records(variant)
                            if row["path"].endswith(".json")
                            and root.resolve(row["path"]).is_relative_to(root.resolve(rel))
                            and load(root, row["path"]) == before_graph), None)
                    elif kind == "candidate_graph":
                        source_rel = next((row["path"] for row in records(variant)
                            if row["path"].endswith(".json")
                            and root.resolve(row["path"]).is_relative_to(root.resolve(rel))
                            and load(root, row["path"]) == after_graph), None)
                    else:
                        expected = FROZEN[CONTROLLER if kind == "controller_archive" else EXTENSION]
                        source_rel = next((row["path"] for row in records(variant)
                            if row["sha256"] == expected
                            and root.resolve(row["path"]).is_relative_to(root.resolve(rel))), None)
                require(source_rel is not None, "tamper fixture was not found for " + kind)
                original_bytes = root.read_bytes(source_rel)
                corrupted = original_bytes + b"\nindependent tamper\n"
                mapped = suite + "/tamper_bytes/" + str(index) + ".bin"
                root.write_bytes(mapped, corrupted)
                source_path = root.resolve(source_rel)

                mapped_reads = []

                def mapped_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None,
                                source_path=source_path, mapped=mapped, original_open=original_open, mapped_reads=mapped_reads):
                    try:
                        matches = path.resolve(strict=False) == source_path
                    except (OSError, RuntimeError):
                        matches = False
                    if matches and mode in {"rb", "r"}:
                        mapped_reads.append(mode)
                        binary = io.BytesIO(root.read_bytes(mapped))
                        if "b" in mode:
                            return binary
                        return io.TextIOWrapper(binary, encoding=encoding or "utf-8", errors=errors, newline=newline)
                    return original_open(path, mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)

                with patch.object(Path, "open", new=mapped_open), patch.object(driver, "available_ram_bytes", return_value=HEADROOM), patch.object(driver.h3, "run_local") as native:
                    error = rejects(driver.run_probe)
                    native.assert_not_called()
                require(mapped_reads, "targeted dependency read was not mapped: " + kind)
                require(root.read_bytes(source_rel) == original_bytes, "real immutable dependency was mutated")
                require(root.read_bytes(mapped) == corrupted, "tampered scratch bytes changed")
                categories = {
                    "canonical_manifest": ("manifest",),
                    "retained_schema": ("schema", "object_info"),
                    "retained_baseline_graph": ("graph",),
                    "current_launcher": ("launcher",),
                    "current_controller": ("controller",),
                    "current_extension": ("extension",),
                    "prepared_baseline_graph": ("graph", "baseline"),
                    "candidate_graph": ("graph", "candidate"),
                    "controller_archive": ("controller", "archive", "code"),
                    "extension_archive": ("extension", "archive", "code"),
                }
                rejection = error.lower()
                require(any(token in rejection for token in categories[kind]), "rejection does not identify expected dependency category: " + kind + ": " + error)
                evidence.append({
                    "dependency": kind,
                    "path": source_rel,
                    "retained_original_sha256": hashlib.sha256(original_bytes).hexdigest(),
                    "injected_sha256": hashlib.sha256(corrupted).hexdigest(),
                    "pre_native_rejection": error,
                    "native_calls": 0,
                    "scratch_mapping": mapped,
                })
        return evidence

    case("pre_native_rejects_all_immutable_dependency_tampering", tamper_dependencies)

    def tamper_verifier():
        with scratch("adversarial") as rel:
            driver.prepare_probe()
            manifest = load(root, rel + "/manifest.json")
            for key in ("input_video", "source", "provenance", "preparation"):
                changed = copy.deepcopy(manifest)
                changed[key] = {"tampered":True}
                rejects(lambda changed=changed: manifest_checks(root, changed, rel))
            changed = copy.deepcopy(manifest)
            changed["prompt"]["sha256"] = "0"*64
            rejects(lambda changed=changed: manifest_checks(root, changed, rel))
            changed = copy.deepcopy(manifest)
            changed["prompt"]["path"] = "../outside.txt"
            rejects(lambda changed=changed: manifest_checks(root, changed, rel))
            benign = copy.deepcopy(manifest)
            benign["prompt"]["diagnostic_note"] = "benign additive metadata"
            benign["diagnostic_variant"]["diagnostic_note"] = "benign additive metadata"
            manifest_checks(root, benign, rel)
            return {"rejected_nonprompt_changes":4, "rejected_hash_path":True, "additive_metadata_accepted":True}
    case("independent_manifest_adversarial_checks", tamper_verifier)
    case("immutable_sources_and_retained_A", lambda: require(immutable_snapshot(root) == before, "immutable artifacts changed during tests"))
    return {"schema_version":1, "passed":all(row["passed"] for row in checks), "checks":checks, "scratch":suite, "quality_accepted":False, "limitations":["CPU tests mock the native-call/headroom boundary and selected Path.open dependency reads plus ProjectRoot write failures; no native server, GPU, model load or generation executed.", "No actual prompt_b_v2 artifacts verified by self-test.", "Dependency tampering maps selected Path.open reads to unique ProjectRoot scratch bytes; real immutable dependencies are never modified. This exercises read boundaries, not alternate OS/filesystem behavior."]}


def raw_evidence(root, run, helper):
    import torch
    from safetensors.torch import load_file
    capture = helper.verify_capture(root, run, actual=True)
    tensors = load_file(str(root.resolve(capture["receipt"]["tensor_archive"]["path"])), device="cpu")
    wave = tensors["raw_decoded_waveform"]
    require(list(wave.shape) == [1,2,256000] and bool(torch.isfinite(wave).all()), "actual raw waveform shape/nonfinite differs")
    rate = capture["receipt"]["sample_rate"]
    result = {"full":helper.waveform_metrics(wave,rate), "first_seven_seconds":helper.waveform_metrics(wave[...,:7*rate],rate), "per_second":[{"start_sample":start,"metrics":helper.waveform_metrics(wave[...,start:start+rate],rate)} for start in range(0,8*rate,rate)], "terminal_last_second":helper.waveform_metrics(wave[...,7*rate:],rate), "terminal_last_twenty_ms":helper.waveform_metrics(wave[...,-640:],rate)}
    for row in result["per_second"]:
        start = row["start_sample"]
        row["metrics"]["stereo_flattened"] = helper.summary(wave[...,start:start+rate],rate)
    result["first_seven_seconds"]["stereo_flattened"] = helper.summary(wave[...,:7*rate],rate)
    result["full"]["stereo_flattened"] = helper.summary(wave,rate)
    return result


def verify_run(root, run):
    require(root.resolve(run).is_relative_to(root.resolve(RUNS)), "run outside local_runs")
    require(root.resolve(run) != root.resolve(BASELINE), "A is not a B attempt")
    generation = load(root, run + "/generation.json")
    candidate = generation["provenance"]["source_manifest"]
    prompt_path = candidate["prompt"]["path"]
    probe_rel = str(Path(prompt_path).parent).replace("\\", "/")
    require(probe_rel == PROBE, "actual B must use authorized prompt_b candidate")
    manifest_checks(root, candidate, probe_rel)
    require(candidate == load(root, probe_rel + "/manifest.json"), "native generation source manifest differs from candidate")
    preparation = prepared_checks(root, probe_rel)
    graph_record = checked(root, generation["graph"])
    require(root.resolve(graph_record["path"]).is_relative_to(root.resolve(run)), "graph belongs to foreign run")
    require(load(root, graph_record["path"]) == expected_graph(root), "actual B graph changed beyond prompt")
    require(generation["seed"]["value"] == SEED, "actual seed differs")
    require(generation["code"]["path"] == CONTROLLER and generation["code"]["sha256"] == FROZEN[CONTROLLER], "actual controller differs")
    checked(root, generation["code"])
    checked(root, generation["runtime"]["extension"])
    require(generation["runtime"]["extension"]["sha256"] == FROZEN[EXTENSION], "actual extension differs")
    # Provenance copies must preserve frozen bytes. Derive file locations from
    # actual artifacts instead of inventing a new launcher-specific record schema.
    targets = [DRIVER, probe_rel + "/variant.json", probe_rel + "/prompt.txt", probe_rel + "/manifest.json"]
    provenance_files = [path for path in root.resolve(run).rglob("*") if path.is_file() and path.suffix in {".py", ".json", ".txt"} and path.stat().st_size < 5_000_000 and "provenance" in str(path.relative_to(root.resolve(run))).lower()]
    for rel in targets:
        require(any(digest(path) == digest(root.resolve(rel)) for path in provenance_files), "run frozen provenance copy missing: " + rel)
    linked = []
    for path in root.resolve(probe_rel + "/attempts").glob("*.json"):
        receipt = json.loads(path.read_text())
        values = list(walk(receipt))
        if run in values or run + "/generation.json" in values:
            linked.append(receipt)
    require(bool(linked), "append-only B run-link receipt missing")
    for receipt in linked:
        for row in records(receipt):
            checked(root, row)
    candidate_id = preparation["candidate"]
    require(any(any(row["path"] == candidate_id["path"] and row["sha256"] == candidate_id["sha256"] for row in records(receipt)) for receipt in linked), "run-link candidate identity absent")
    require(any(any(row["path"] == DRIVER and row["sha256"] == preparation["launcher"]["sha256"] for row in records(receipt)) for receipt in linked), "run-link launcher identity absent")
    available = [value for receipt in linked for row in walk(receipt) if isinstance(row,dict) for key,value in row.items() if "ram" in key.lower() and "available" in key.lower() and isinstance(value,int)]
    require(any(value >= HEADROOM for value in available), "run-link lacks actual >=10 GiB pre-controller headroom")
    media = import_module(BASE + "/verify_local_outputs.py", "prompt_probe_original_media_" + uuid.uuid4().hex)
    original_input = media.INPUT_MANIFEST
    try:
        # This is the ONLY substituted constant/helper in the media verifier.
        media.INPUT_MANIFEST = probe_rel + "/manifest.json"
        result = media.Verifier(root, run).verify()
    finally:
        media.INPUT_MANIFEST = original_input
    result["prompt_probe"] = preparation
    helper = import_module(BASE + "/verify_audio_diagnostic.py", "prompt_probe_original_capture_" + uuid.uuid4().hex)
    # Original capture verifier retains all native tensor/math/codec checks.
    result["audio_capture"] = helper.verify_run(root, run)
    result["raw_prompt_contrast"] = {"A":raw_evidence(root, BASELINE, helper), "B":raw_evidence(root, run, helper), "interpretation":"Observed contrast for one seed/configuration; wording and formatting changed together. No quantization, repeatability, general fidelity or audio causation conclusion."}
    result["quality_accepted"] = False
    result["creator_approval"] = "pending"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--run")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    report_path = root.resolve(args.report)
    allowed = INVESTIGATION if args.self_test else RUNS
    require(report_path.is_relative_to(root.resolve(allowed)), "report outside authorized report root")
    require(not report_path.exists(), "refusing to overwrite existing report")
    try:
        result = self_test(root) if args.self_test else verify_run(root, args.run)
    except Exception as exc:  # noqa: BLE001 - serialize verifier failures into the requested report
        result = {"schema_version":1,"passed":False,"structural_media_passed":False,"generated_media_verified":False,"quality_accepted":False,"error":type(exc).__name__ + ": " + str(exc),"traceback":traceback.format_exc()}
    helper = import_module(BASE + "/verify_audio_diagnostic.py", "prompt_probe_json_safe_" + uuid.uuid4().hex)
    result = helper.safe(result)
    root.write_text(args.report, json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    passed = result.get("passed", result.get("structural_media_passed", False))
    print(json.dumps({"passed":passed,"report":args.report,"checks":len(result.get("checks",[]))}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare and run the single contracted prompt-only MiniMax H3 B probe.

The module is model-free at import. Preparation uses retained A artifacts; runtime
delegates once to the existing controller after a fresh 10 GiB RAM check.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
SPIKE = "spikes/22_minimax_h3_workflows"
PROBE_REL = SPIKE + "/_work/local_runs/_latent_investigation/prompt_b_v2"
CANONICAL_REL = SPIKE + "/_work/test_inputs/20261109T165721Z_e1e1bf74/manifest.json"
CANONICAL_SHA = "a27d7f2a4d56f144f79c4efbb1740ff59a68e1610b9010f5dc764cb7aa3d0a2c"
BASELINE = SPIKE + "/_work/local_runs/20261110T162940Z_89b935ab"
FINDINGS_REL = SPIKE + "/_work/local_runs/_latent_investigation/next_probe_alt_findings.txt"
MIN_RAM_BYTES = 10 * 1024**3


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _read_json(rel: str) -> Any:
    return json.loads(PROJECT.read_text(rel))


def _identity(rel: str) -> dict[str, Any]:
    data = PROJECT.read_bytes(rel)
    return {"path": rel, "bytes": len(data), "sha256": _h(data)}


def _write_exact(rel: str, data: bytes) -> dict[str, Any]:
    path = PROJECT.resolve(rel)
    if path.exists():
        actual = PROJECT.read_bytes(rel)
        if actual != data:
            raise ValueError(f"Existing prepared artifact differs; retained unchanged: {rel}")
    else:
        PROJECT.write_bytes(rel, data)
    return {"path": rel, "bytes": len(data), "sha256": _h(data)}


def _copy_exact(source_rel: str, target_rel: str) -> dict[str, Any]:
    return _write_exact(target_rel, PROJECT.read_bytes(source_rel))


def _runner_module():
    spec = importlib.util.spec_from_file_location("_prompt_probe_local_h3", PROJECT.resolve(SPIKE + "/local_h3.py"))
    if spec is None or spec.loader is None:
        raise ImportError("Could not load the frozen local_h3 controller")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# One shared, model-free controller module is the only native-call test seam.
h3 = _runner_module()


def _candidate_prompt() -> str:
    source = PROJECT.read_text(FINDINGS_REL)
    match = re.search(r"```text\s*\n(.*?)\n```", source, flags=re.DOTALL)
    if match is None:
        raise ValueError("Candidate text block is missing from retained findings")
    return match.group(1).replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _source_identities() -> dict[str, dict[str, Any]]:
    return {
        "launcher": _identity(SPIKE + "/prompt_probe.py"),
        "controller": _identity(SPIKE + "/local_h3.py"),
        "extension": _identity(SPIKE + "/stage_extension/h3_staging/__init__.py"),
    }


def prepare_probe() -> dict[str, Any]:
    """Create immutable prompt B preparation artifacts without starting a server."""
    canonical_identity = _identity(CANONICAL_REL)
    canonical = _read_json(CANONICAL_REL)
    if canonical.get("input_video", {}).get("sha256") != CANONICAL_SHA:
        raise ValueError("Canonical input video hash differs from the contracted identity")
    source_prompt = canonical["prompt"]
    if _identity(source_prompt["path"])["sha256"] != source_prompt["sha256"]:
        raise ValueError("Canonical A prompt identity is invalid")

    prompt = _candidate_prompt()
    prompt_rel = PROBE_REL + "/prompt.txt"
    prompt_record = _write_exact(prompt_rel, prompt.encode("utf-8"))
    manifest = copy.deepcopy(canonical)
    manifest["prompt"] = {"path": prompt_rel, "sha256": prompt_record["sha256"]}
    manifest["supersedes"] = {"path": CANONICAL_REL, "sha256": canonical_identity["sha256"]}
    identities = _source_identities()
    manifest["diagnostic_variant"] = {
        "kind": "prompt_only",
        "baseline_run": "20261110T162940Z_89b935ab",
        "changed_fields": ["prompt"],
        "launcher": identities["launcher"],
    }
    manifest_rel = PROBE_REL + "/manifest.json"
    manifest_record = _write_exact(manifest_rel, _json_bytes(manifest))

    baseline_graph_rel = BASELINE + "/graph.json"
    baseline_info_rel = BASELINE + "/object_info.json"
    baseline_graph = _read_json(baseline_graph_rel)
    info = _read_json(baseline_info_rel)
    graph, seed = h3.build_graph(info, "model_proxy.mp4", prompt, staged=True, audio_diagnostic=True)
    expected = copy.deepcopy(baseline_graph)
    if expected.get("3", {}).get("class_type") != "CCTVH3ConditionAndRelease":
        raise ValueError("Retained A graph is not the authorized staged graph")
    expected["3"]["inputs"]["prompt"] = prompt
    if graph != expected:
        raise ValueError("Prepared graph differs from retained A outside node 3 prompt")
    if seed.get("value") != 9952211358998416546:
        raise ValueError("Derived seed differs from the retained A seed")

    baseline_graph_copy = PROBE_REL + "/baseline_graph.json"
    candidate_graph_copy = PROBE_REL + "/prepared_graph.json"
    baseline_graph_record = _write_exact(baseline_graph_copy, PROJECT.read_bytes(baseline_graph_rel))
    candidate_graph_record = _write_exact(candidate_graph_copy, _json_bytes(graph))

    archive_base = PROBE_REL + "/code_archive"
    archives: dict[str, dict[str, Any]] = {}
    for label, identity in identities.items():
        suffix = "prompt_probe.py" if label == "launcher" else ("local_h3.py" if label == "controller" else "h3_staging.py")
        source_rel = identity["path"]
        archive_rel = archive_base + "/" + suffix
        archived = _copy_exact(source_rel, archive_rel)
        if archived["sha256"] != identity["sha256"] or archived["bytes"] != identity["bytes"]:
            raise ValueError(f"Code archive differs from source: {label}")
        archives[label] = {"source": identity, "archive": archived}

    variant = {
        "schema_version": 1,
        "status": "prepared_not_queued",
        "kind": "prompt_only",
        "created_utc": datetime.now(UTC).isoformat(),
        "canonical_manifest": canonical_identity,
        "candidate_manifest": manifest_record,
        "prompt": prompt_record,
        "baseline": {
            "run": BASELINE,
            "graph": _identity(baseline_graph_rel),
            "object_info": _identity(baseline_info_rel),
            "prepared_graph_copy": baseline_graph_record,
        },
        "candidate_graph": candidate_graph_record,
        "graph_comparison": {
            "equal_after_replacing_only_node3_inputs_prompt": True,
            "changed_path": "3.inputs.prompt",
            "baseline_graph_sha256": _h(PROJECT.read_bytes(baseline_graph_rel)),
            "candidate_graph_sha256": candidate_graph_record["sha256"],
        },
        "seed": seed,
        "headroom_policy": {
            "minimum_available_ram_bytes": MIN_RAM_BYTES,
            "reason": "experiment-specific preparation gate; not a measured model minimum",
            "native_controller_admission_and_watchdog_unchanged": True,
        },
        "code": {**identities, "archives": archives},
        "preparation_evidence": "retained_A_object_info; not a claim of a new live server schema check",
    }
    variant_rel = PROBE_REL + "/variant.json"
    if PROJECT.resolve(variant_rel).is_file():
        existing_variant = _read_json(variant_rel)
        existing_created_utc = existing_variant.get("created_utc")
        if isinstance(existing_created_utc, str):
            variant["created_utc"] = existing_created_utc
    variant_record = _write_exact(variant_rel, _json_bytes(variant))
    return {"status": "prepared_not_queued", "probe": PROBE_REL, "variant": variant_record,
            "manifest": manifest_record, "prompt": prompt_record, "seed": seed["value"]}


def available_ram_bytes() -> int:
    """Return current system available RAM bytes, not a model-demand estimate."""
    import psutil
    return int(psutil.virtual_memory().available)


def _new_attempt_rel() -> str:
    nonce = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    return PROBE_REL + "/attempts/" + nonce + ".json"


def _verify_prepared() -> tuple[dict[str, Any], dict[str, Any]]:
    variant = _read_json(PROBE_REL + "/variant.json")
    if variant.get("status") != "prepared_not_queued":
        raise ValueError("Prepared variant status is invalid")

    canonical_identity = variant["canonical_manifest"]
    if canonical_identity["path"] != CANONICAL_REL or _identity(CANONICAL_REL) != canonical_identity:
        raise ValueError("Canonical manifest identity changed")
    canonical = _read_json(CANONICAL_REL)
    if canonical.get("input_video", {}).get("sha256") != CANONICAL_SHA:
        raise ValueError("Canonical input video hash differs from the contracted identity")
    source_prompt = canonical["prompt"]
    if _identity(source_prompt["path"])["sha256"] != source_prompt["sha256"]:
        raise ValueError("Canonical A prompt identity changed")

    baseline = variant["baseline"]
    if baseline["run"] != BASELINE:
        raise ValueError("Retained A baseline path changed")
    for key, relative in (("graph", BASELINE + "/graph.json"),
                          ("object_info", BASELINE + "/object_info.json")):
        if baseline[key]["path"] != relative or _identity(relative) != baseline[key]:
            raise ValueError(f"Retained baseline {key} identity changed")
    prepared_baseline = baseline["prepared_graph_copy"]
    if prepared_baseline["path"] != PROBE_REL + "/baseline_graph.json":
        raise ValueError("Prepared baseline graph path changed")
    if _identity(prepared_baseline["path"]) != prepared_baseline:
        raise ValueError("Prepared baseline graph copy changed")
    if prepared_baseline["sha256"] != baseline["graph"]["sha256"]:
        raise ValueError("Prepared baseline graph copy differs from retained A graph")

    for name, identity in (("manifest.json", variant["candidate_manifest"]),
                           ("prompt.txt", variant["prompt"]),
                           ("prepared_graph.json", variant["candidate_graph"])):
        expected_path = PROBE_REL + "/" + name
        if identity["path"] != expected_path or _identity(expected_path) != identity:
            raise ValueError(f"Prepared candidate {name} identity changed")

    candidate_manifest = _read_json(PROBE_REL + "/manifest.json")
    candidate_prompt = PROJECT.read_text(PROBE_REL + "/prompt.txt")
    candidate_graph = _read_json(PROBE_REL + "/prepared_graph.json")
    baseline_graph = _read_json(PROBE_REL + "/baseline_graph.json")
    expected_graph = copy.deepcopy(baseline_graph)
    expected_graph["3"]["inputs"]["prompt"] = candidate_prompt
    if candidate_graph != expected_graph:
        raise ValueError("Prepared graph differs from retained A only at node 3 prompt")
    comparison = variant["graph_comparison"]
    if (comparison["baseline_graph_sha256"] != baseline["graph"]["sha256"]
            or comparison["candidate_graph_sha256"] != variant["candidate_graph"]["sha256"]
            or comparison["changed_path"] != "3.inputs.prompt"
            or comparison["equal_after_replacing_only_node3_inputs_prompt"] is not True):
        raise ValueError("Prepared graph comparison record is inconsistent")
    if variant["seed"].get("value") != 9952211358998416546:
        raise ValueError("Prepared graph seed differs from the retained A seed")

    code = variant["code"]
    archives = code["archives"]
    expected_sources = {
        "launcher": SPIKE + "/prompt_probe.py",
        "controller": SPIKE + "/local_h3.py",
        "extension": SPIKE + "/stage_extension/h3_staging/__init__.py",
    }
    archive_names = {
        "launcher": "prompt_probe.py",
        "controller": "local_h3.py",
        "extension": "h3_staging.py",
    }
    for label, source_path in expected_sources.items():
        identity = code[label]
        if identity["path"] != source_path or _identity(source_path) != identity:
            raise ValueError(f"Frozen source identity changed: {label}")
        archive = archives[label]["archive"]
        expected_archive = PROBE_REL + "/code_archive/" + archive_names[label]
        if archive["path"] != expected_archive or _identity(expected_archive) != archive:
            raise ValueError(f"Frozen code archive changed: {label}")
        if archive["sha256"] != identity["sha256"] or archive["bytes"] != identity["bytes"]:
            raise ValueError(f"Frozen code archive differs from source: {label}")
        if archives[label]["source"] != identity:
            raise ValueError(f"Recorded source/archive identity differs: {label}")

    expected_manifest = copy.deepcopy(canonical)
    expected_manifest["prompt"] = {
        "path": variant["prompt"]["path"],
        "sha256": variant["prompt"]["sha256"],
    }
    expected_manifest["supersedes"] = {
        "path": CANONICAL_REL,
        "sha256": variant["canonical_manifest"]["sha256"],
    }
    expected_manifest["diagnostic_variant"] = {
        "kind": "prompt_only",
        "baseline_run": "20261110T162940Z_89b935ab",
        "changed_fields": ["prompt"],
        "launcher": code["launcher"],
    }
    if candidate_manifest != expected_manifest:
        raise ValueError("Prepared candidate manifest differs from the authorized prompt-only change")
    return variant, candidate_manifest


def _copy_run_provenance(run_rel: str) -> dict[str, Any]:
    destination = run_rel + "/provenance/prompt_probe"
    PROJECT.make_dir(destination)
    copied: dict[str, Any] = {}
    for name, rel in (("runner.py", SPIKE + "/prompt_probe.py"),
                      ("variant.json", PROBE_REL + "/variant.json"),
                      ("prompt.txt", PROBE_REL + "/prompt.txt"),
                      ("manifest.json", PROBE_REL + "/manifest.json")):
        copied[name] = _copy_exact(rel, destination + "/" + name)
    return copied


def run_probe() -> dict[str, Any]:
    """Run one controller call only when current available RAM is at least 10 GiB."""
    variant, _manifest = _verify_prepared()
    ram = available_ram_bytes()
    attempt_rel = _new_attempt_rel()
    if ram < MIN_RAM_BYTES:
        record = {
            "schema_version": 1, "status": "held_headroom_no_queue",
            "created_utc": datetime.now(UTC).isoformat(), "available_ram_bytes": ram,
            "minimum_available_ram_bytes": MIN_RAM_BYTES,
            "policy_is_experiment_specific_not_measured_demand": True,
            "candidate_manifest": variant["candidate_manifest"], "launcher": variant["code"]["launcher"],
            "native_controller_invoked": False,
        }
        _write_exact(attempt_rel, _json_bytes(record))
        return record

    input_before = h3.INPUT
    before = {p.name for p in PROJECT.resolve(SPIKE + "/_work/local_runs").iterdir() if p.is_dir()}
    native_result: dict[str, Any] | None = None
    native_error: BaseException | None = None
    try:
        h3.INPUT = variant["candidate_manifest"]["path"]
        native_result = h3.run_local(staged=True, audio_diagnostic=True)
    except BaseException as exc:  # noqa: BLE001 -- retain native failure; finally restores INPUT.
        native_error = exc
    finally:
        h3.INPUT = input_before

    after = {p.name for p in PROJECT.resolve(SPIKE + "/_work/local_runs").iterdir() if p.is_dir()}
    new_dirs = sorted(after - before)
    run_rel = None
    if native_result and native_result.get("graph", {}).get("path"):
        run_rel = native_result["graph"]["path"].rsplit("/", 1)[0]
    elif len(new_dirs) == 1:
        run_rel = SPIKE + "/_work/local_runs/" + new_dirs[0]
    copied = None
    copy_error: BaseException | None = None
    try:
        if run_rel and PROJECT.resolve(run_rel).is_dir():
            copied = _copy_run_provenance(run_rel)
    except BaseException as exc:  # noqa: BLE001 -- retain copy failure and still write the receipt.
        copy_error = exc
    record = {
        "schema_version": 1,
        "status": "controller_failed" if native_error else (native_result or {}).get("status", "controller_returned_no_status"),
        "created_utc": datetime.now(UTC).isoformat(),
        "available_ram_bytes_before_controller": ram,
        "minimum_available_ram_bytes": MIN_RAM_BYTES,
        "policy_is_experiment_specific_not_measured_demand": True,
        "native_controller_invoked": True,
        "controller_call": {"staged": True, "audio_diagnostic": True, "prepare_only": False,
                            "input_manifest": variant["candidate_manifest"]["path"]},
        "candidate_manifest": variant["candidate_manifest"],
        "launcher": variant["code"]["launcher"],
        "run_path": run_rel,
        "generation": ({"path": run_rel + "/generation.json", **_identity(run_rel + "/generation.json")}
                       if run_rel and PROJECT.resolve(run_rel + "/generation.json").is_file() else None),
        "run_provenance_copies": copied,
        "input_global_restored": h3.INPUT == input_before,
    }
    if native_error is not None:
        record["error"] = {"type": type(native_error).__name__, "message": str(native_error)}
    elif native_result is not None:
        record["native_status"] = native_result.get("status")
    if copy_error is not None:
        record["copy_error"] = {"type": type(copy_error).__name__, "message": str(copy_error)}
    _write_exact(attempt_rel, _json_bytes(record))
    if native_error is not None:
        raise RuntimeError(f"Native controller failed; attempt receipt retained at {attempt_rel}") from native_error
    if copy_error is not None:
        raise RuntimeError(f"Run provenance copy failed; attempt receipt retained at {attempt_rel}") from copy_error
    if native_result is None or native_result.get("status") != "generated_candidate_unapproved":
        raise RuntimeError(f"Native controller did not complete generation; attempt receipt retained at {attempt_rel}")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    try:
        result = prepare_probe() if args.action == "prepare" else run_probe()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports retained probe errors with a nonzero exit.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

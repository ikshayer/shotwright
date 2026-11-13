"""Independent Amendment6 known-audio fixture, graph and artifact verifier.

No GPU generation or listening. Floating reconstruction quality is measured,
never accepted by an invented threshold. Heavy numeric dependencies are lazy.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import math
import subprocess
import sys
import traceback
import uuid
from contextlib import ExitStack
from functools import cache
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

BASE = "spikes/22_minimax_h3_workflows"
RUNS = BASE + "/_work/local_runs"
RATE = 32000
COUNT = 256000


@cache
def support():
    spec = importlib.util.spec_from_file_location("roundtrip_independent_audio", ROOT / BASE / "verify_audio_diagnostic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(value, message):
    if not value:
        raise ValueError(message)


def fixture():
    """Contract synthesis; intentionally independent of the driver's function."""
    import numpy as np
    x = np.zeros((2, COUNT), dtype=np.float64)
    seconds = np.arange(64000, dtype=np.float64) / RATE
    x[0, :64000] = .1 * (np.sin(2*np.pi*220*seconds) + .5*np.sin(2*np.pi*440*seconds))
    x[1, :64000] = .1 * (np.sin(2*np.pi*330*seconds) + .5*np.sin(2*np.pi*660*seconds))
    x[0, 64000:128000] = .12*np.sin(2*np.pi*(110*seconds + .5*1945*seconds*seconds))
    x[1, 64000:128000] = .12*np.sin(2*np.pi*(220*seconds + .5*2890*seconds*seconds))
    x[:, 128000:224000] = .03*np.random.Generator(np.random.PCG64(221110)).standard_normal((2,96000))
    fade = np.linspace(0,1,640)
    for start,end in ((0,64000),(64000,128000),(128000,224000)):
        x[:,start:start+640] *= fade
        x[:,end-640:end] *= fade[::-1]
    return x.astype("<f4")


def native_input_loader(root):
    import av
    import torch
    path = BASE + "/_work/local_runtime/source/ComfyUI/comfy_extras/nodes_audio.py"
    tree = ast.parse(root.read_text(path))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"f32_pcm","load"}]
    require(len(functions) == 2, "native input helper extraction incomplete")
    namespace = {"av":av,"torch":torch}
    exec(compile(ast.Module(body=functions,type_ignores=[]),path,"exec"),namespace)  # noqa: S102 - exact pinned input functions, no Comfy module import
    return namespace["load"]


def audio_name(info):
    entry = info["LoadAudio"]["input"]["required"]["audio"]
    choices = entry[0] if isinstance(entry[0], list) else entry[1]["options"]
    require("known_audio.wav" in choices, "known fixture absent from actual LoadAudio choices")
    return "known_audio.wav"


def validate_graph(graph, info, catalog):
    audio = next(x for x in catalog["components"] if x["role"] == "audio_vae")
    expected = {
        "1": {"class_type":"VAELoader", "inputs":{"vae_name":Path(audio["model_relative_path"]).name}},
        "2": {"class_type":"LoadAudio", "inputs":{"audio":audio_name(info)}},
        "3": {"class_type":"VAEEncodeAudio", "inputs":{"audio":["2",0], "vae":["1",0]}},
        "4": {"class_type":"CCTVH3AudioDiagnostic", "inputs":{"samples":["3",0], "vae":["1",0]}},
        "5": {"class_type":"SaveAudio", "inputs":{"audio":["4",0], "filename_prefix":"roundtrip_trigger"}},
    }
    require(graph == expected, "roundtrip graph differs from exact five-node native route")
    outputs = {"VAELoader":["VAE"],"LoadAudio":["AUDIO"],"VAEEncodeAudio":["LATENT"],"CCTVH3AudioDiagnostic":["AUDIO"],"SaveAudio":["AUDIO"]}
    port_types = {"VAEEncodeAudio":{"audio":"AUDIO","vae":"VAE"}, "CCTVH3AudioDiagnostic":{"samples":"LATENT","vae":"VAE"}, "SaveAudio":{"audio":"AUDIO","filename_prefix":"STRING"}}
    for node in graph.values():
        cls = node["class_type"]
        require(info[cls]["output"] == outputs[cls], "native schema output differs: " + cls)
        required = info[cls]["input"]["required"]
        require(set(required) == set(node["inputs"]), "required schema ports differ: " + cls)
        for port,kind in port_types.get(cls,{}).items():
            require(required[port][0] == kind, "schema port type differs: " + cls + "." + port)
    require(Path(audio["model_relative_path"]).name in info["VAELoader"]["input"]["required"]["vae_name"][0], "catalog checkpoint missing in native schema")
    return {"classes": sorted(outputs), "exact_edges": True, "sampler_nodes": 0}


def record(root, item, within=None):
    return support().checked_record(root,item,within)


def history_check(history, graph, prompt_id=None):
    # Native /history entry may be wrapped by prompt ID; retained controller
    # receipts commonly retain the entry itself.
    if "status" not in history and len(history) == 1:
        history = next(iter(history.values()))
    status = history["status"]
    require(status.get("completed") is True and status.get("status_str") == "success", "native execution not completed successfully")
    messages = status.get("messages", [])
    require(not any("error" in str(row[0]).lower() for row in messages), "history contains execution failure")
    prompt = history["prompt"]
    if prompt_id is not None:
        require(prompt[1] == prompt_id, "native history prompt identity differs")
        require(any(row[0] == "execution_success" and row[1].get("prompt_id") == prompt_id for row in messages), "execution_success prompt identity differs")
    require(prompt[2] == graph, "history graph differs from retained exact graph")
    require(any(row[0] == "execution_success" for row in messages), "history lacks execution_success evidence")
    return {"native_status":"success", "retained_prompt_graph_equal":True}


def signal_evidence(input_wave, raw, post):
    import torch
    helper = support()
    evidence = {}
    for label,wave in (("input",input_wave),("raw",raw),("post",post)):
        evidence[label] = {"full":helper.waveform_metrics(wave,RATE), "first_seven_seconds":helper.waveform_metrics(wave[...,:7*RATE],RATE), "last_second_silence_region":helper.waveform_metrics(wave[...,7*RATE:],RATE)}
        evidence[label]["per_channel_one_second_bins"] = [[{"start_sample":start, **helper.summary(channel[start:start+RATE],RATE)} for start in range(0,COUNT,RATE)] for channel in wave[0]]
        evidence[label]["first_nonzero_sample_indices"] = [int(torch.nonzero(channel != 0)[0]) if bool((channel != 0).any()) else None for channel in wave[0]]
        spectrum = []
        for index,channel in enumerate(wave[0,:,:2*RATE].double()):
            power = torch.fft.rfft(channel).abs().square()
            frequency = torch.fft.rfftfreq(channel.numel(),1/RATE)
            dominant = torch.topk(power,5).indices
            spectrum.append({"channel":index,"dominant_frequencies_hz":frequency[dominant].tolist(),"tone_power":{str(tone):float(power[(frequency-tone).abs() <= 1].sum()) for tone in ((220,440) if index == 0 else (330,660))}, "first_twenty_ms":helper.summary(channel[:640],RATE), "following_twenty_ms":helper.summary(channel[640:1280],RATE)})
        evidence[label]["first_two_seconds_spectrum"] = spectrum
    evidence["raw_vs_input"] = helper.codec_comparison(input_wave,raw,RATE)
    evidence["post_vs_input"] = helper.codec_comparison(input_wave,post,RATE)
    for label,wave in (("raw",raw),("post",post)):
        x = input_wave.double().square().mean().sqrt()
        y = wave.double().square().mean().sqrt()
        evidence[label+"_direct_level_ratio_db"] = 20*math.log10(float(y/x)) if y > 0 else "-inf"
        ratios = []
        for reference, reconstructed in zip(input_wave[0],wave[0]):
            denominator = reference.double().square().mean().sqrt()
            numerator = reconstructed.double().square().mean().sqrt()
            ratios.append(20*math.log10(float(numerator/denominator)) if numerator > 0 else "-inf")
        evidence[label+"_per_channel_direct_level_ratio_db"] = ratios
    evidence["comparison_kind"] = "VAE reconstruction; no codec test, fitted gain, normalization, or quality threshold"
    return evidence


def capture_shape(capture, tensors, run, owned):
    import torch
    receipt = capture["receipt"]
    require(receipt["invocation_id"] == Path(run).name and receipt["sample_rate"] == RATE, "roundtrip rate/invocation differs")
    identity = (receipt["pid"],receipt["create_time"])
    require(identity in {(row["pid"],row["create_time"]) for row in owned}, "capture is not bound to observed owned process")
    require(list(tensors["sampled_audio_latent"].shape) == [1,32,2,320], "encoded input latent shape differs")
    for name in ("raw_decoded_waveform","post_native_waveform"):
        require(list(tensors[name].shape) == [1,2,COUNT] and bool(torch.isfinite(tensors[name]).all()), "roundtrip waveform shape/nonfinite differs")
    require(all(bool(torch.isfinite(t).all()) for t in tensors.values()), "nonfinite roundtrip encoded/native tensor")
    require(receipt["crop"]["retained_samples"] == COUNT and receipt["crop"]["discarded_tail_samples"] == 0 and receipt["crop"]["padding_samples"] == 0, "roundtrip unexpectedly cropped/padded")


def policy_and_admission(result):
    expected = {"admission_available_ram_bytes":6*1024**3,"stop_available_ram_bytes":2*1024**3,"stop_consecutive_samples":3,"sample_interval_seconds":1,"startup_deadline_seconds":180,"schema_preparation_deadline_seconds":180,"generation_deadline_seconds":1800,"limits_are_configured_safety_policy_not_measured_model_demand":True}
    for key,value in expected.items():
        require(result["policy"][key] == value, "configured safety policy differs: " + key)
    baseline = result["resource_baseline"]
    require(baseline["ram_available_bytes"] >= expected["admission_available_ram_bytes"], "queued roundtrip violated RAM admission")
    require(baseline["windows_commit_available_bytes"] > 0, "commit availability absent")
    gpu = baseline["gpu_whole_device_mb"]
    require(all(math.isfinite(gpu[key]) and gpu[key] >= 0 for key in ("used_mb","free_mb","total_mb")) and gpu["total_mb"] > 0, "invalid baseline GPU observation")
    disks = baseline["disk_free_bytes_by_drive"]
    require(any(str(key).upper().startswith("C") for key in disks) and any(str(key).upper().startswith("D") for key in disks) and all(value > 0 for value in disks.values()), "both drive availability observations required")
    require(not result.get("watchdog_failure") and not result.get("errors"), "successful roundtrip retains watchdog/error failure")
    return {"configured_policy_verified":True,"admission_available_ram_bytes":baseline["ram_available_bytes"]}


def verify_run(root, run):
    import numpy as np
    import torch
    from safetensors.torch import load_file
    require(root.resolve(run).is_relative_to(root.resolve(RUNS)) and Path(run).name.startswith("vae_roundtrip_"), "roundtrip run outside authorized path")
    result = json.loads(root.read_text(run+"/roundtrip.json"))
    require(result["schema_version"] == 1 and result["strategy"] == "known_audio_vae_roundtrip" and result["run"] == run, "roundtrip result identity differs")
    require(result["status"] == "roundtrip_captured" and result["quality_accepted"] is False and result["production_state_mutated"] is False, "roundtrip success/approval flags differ")
    policy_evidence = policy_and_admission(result)
    helper = support()
    capture = helper.verify_capture(root,run,actual=False)
    require(record(root,result["audio_diagnostic"],run) == root.resolve(run+"/audio_diagnostic/receipt.json"), "capture result pointer differs")
    tensors = load_file(str(record(root,capture["receipt"]["tensor_archive"],run)))
    observed_owned = result["runtime"]["owned_processes"] + result["runtime"].get("owned_processes_final", [])
    capture_shape(capture,tensors,run,observed_owned)
    expected = fixture()
    wav = record(root,result["fixture"]["wav"],run)
    decoded = helper.decode_pcm(wav,2)
    require(tuple(decoded.shape) == (1,2,COUNT) and np.array_equal(decoded[0].numpy(),expected), "input WAV differs from contract synthesis")
    native_wave, native_rate = native_input_loader(root)(str(wav))
    require(native_rate == RATE and np.array_equal(native_wave.numpy(), expected), "native PyAV LoadAudio helper changes fixture")
    interleaved = expected.T.copy().tobytes()
    require(root.read_bytes(result["fixture"]["raw_f32le"]["path"]) == interleaved, "interleaved float input bytes differ")
    record(root,result["fixture"]["raw_f32le"],run)
    construction = json.loads(record(root,result["fixture"]["construction"],run).read_text())
    require(bool(construction), "construction evidence empty")
    catalog = json.loads(root.read_text(BASE+"/LOCAL_MODEL_CATALOG.json"))
    audio = next(x for x in catalog["components"] if x["role"] == "audio_vae")
    checkpoint = record(root,result["audio_vae"])
    require(checkpoint == root.resolve(catalog["model_root"]+"/"+audio["model_relative_path"]) and checkpoint.stat().st_size == audio["expected_bytes"] and result["audio_vae"]["sha256"] == audio["expected_sha256"], "audio checkpoint differs from catalog")
    graph = json.loads(record(root,result["graph"],run).read_text())
    schema = json.loads(record(root,result["object_info"],run).read_text())
    graph_evidence = validate_graph(graph,schema,catalog)
    prompt_id = result["history"]["prompt_id"]
    response = json.loads(record(root,result["prompt_response"],run).read_text())
    require(bool(prompt_id) and prompt_id == result["prompt_id"] == response["prompt_id"], "current prompt ID missing or inconsistent")
    history = history_check(json.loads(record(root,result["history"],run).read_text()),graph,prompt_id)
    runtime = result["runtime"]
    exact_code = {"source": BASE+"/_work/local_runtime/source/ComfyUI/main.py", "extension": BASE+"/stage_extension/h3_staging/__init__.py", "driver":BASE+"/vae_roundtrip.py"}
    for key,expected_path in exact_code.items():
        require(record(root,runtime[key]) == root.resolve(expected_path), "executed code identity points at wrong path: " + key)
    for archive_role,source_role in (("driver","driver"),("audio_diagnostic_extension","extension")):
        archived = runtime["code_archive"][archive_role]
        record(root,archived,run)
        require(archived["sha256"] == runtime[source_role]["sha256"] and archived["bytes"] == runtime[source_role]["bytes"], "executed code archive differs: " + archive_role)
    helpers = runtime["code_archive"]["local_h3_helpers"]
    record(root,helpers,run)
    require(helpers["sha256"] == hashlib.sha256(root.read_bytes(BASE+"/local_h3.py")).hexdigest(), "archived lifecycle helper differs from actual source")
    command = runtime["command"]
    require(str(root.resolve(exact_code["source"])) in command, "runtime command did not execute pinned native main.py")
    native_sources = result["native_sources"]
    require(record(root,native_sources["receipt"]) == root.resolve(BASE+"/_work/local_runtime/receipts/source_receipt.json"), "native source receipt path differs")
    source_receipt = json.loads(root.read_text(native_sources["receipt"]["path"]))
    require(source_receipt["commit"] == "2c75dc1b379a59e0880619d029607af5169a62ef" and len(source_receipt["files"]) == 1332, "native source receipt identity/count differs")
    expected_sources = {(row["path"],row["bytes"],row["sha256"]) for row in source_receipt["files"]}
    for phase in ("source_pre","source_post"):
        require({(row["path"],row["bytes"],row["sha256"]) for row in native_sources[phase]} == expected_sources, "native pre/post source identities differ")
    for row in source_receipt["files"]:
        record(root,row)
    cleanup = runtime["owned_process_cleanup"]
    owned_identities = {(row["pid"],row["create_time"]) for row in observed_owned}
    cleanup_identities = {(row["pid"],row.get("create_time",row.get("created"))) for row in cleanup["identities"]}
    require(owned_identities <= cleanup_identities, "cleanup does not account for observed owned process identities")
    require(not result.get("watchdog_failure") and not result.get("errors") and not runtime.get("watchdog_failure"), "roundtrip retained watchdog/error failure")
    require(cleanup["verified_stopped"] is True and not cleanup["survivors"] and not cleanup["failures"] and not cleanup["unverifiable_identities"] and runtime["watchdog_thread_stopped"] is True, "roundtrip owned cleanup unresolved")
    return {"passed":True,"run":run,"quality_accepted":False,"graph":graph_evidence,"history":history,"policy":policy_evidence,"encoded_latent_role":"encoded known input; not diffusion sampled; latent RMS is not dBFS","signals":signal_evidence(torch.from_numpy(expected).unsqueeze(0),tensors["raw_decoded_waveform"],tensors["post_native_waveform"]),"limitations":["Synthetic VAE reconstruction is not natural-ambience quality acceptance or definitive attribution of prior near-silence.","No listening performed."]}


def self_test(root):
    import numpy as np
    import torch
    helper = support()
    checks = []
    def check(name, operation):
        try:
            operation()
            checks.append({"name":name,"passed":True})
        except Exception as exc:  # noqa: BLE001 - report all independent checks
            checks.append({"name":name,"passed":False,"error":type(exc).__name__+": "+str(exc),"traceback":traceback.format_exc()})
    # Author expectations before importing the driver; no implementation source read.
    expected = fixture()
    catalog = json.loads(root.read_text(BASE+"/LOCAL_MODEL_CATALOG.json"))
    info = json.loads(root.read_text(RUNS+"/20261110T162940Z_89b935ab/object_info.json"))
    info["LoadAudio"]["input"]["required"]["audio"][1]["options"] = ["known_audio.wav"]
    spec = importlib.util.spec_from_file_location("blind_vae_roundtrip_driver",root.resolve(BASE+"/vae_roundtrip.py"))
    driver = importlib.util.module_from_spec(spec)
    with ExitStack() as stack:
        for method in ("write_text","write_bytes","write_with","append_text"):
            if hasattr(ProjectRoot,method):
                stack.enter_context(patch.object(ProjectRoot,method,side_effect=AssertionError("driver wrote during import")))
        stack.enter_context(patch("subprocess.Popen",side_effect=AssertionError("driver spawned process during import")))
        spec.loader.exec_module(driver)
    checks.append({"name":"model_free_mutation_free_import","passed":True})
    def synthesis():
        actual = driver.make_fixture()
        require(isinstance(actual,np.ndarray) and actual.dtype == np.dtype("<f4") and actual.shape == (2,COUNT), "fixture array contract differs")
        require(np.array_equal(actual,expected), "driver synthesis differs sample-exactly")
        require(np.count_nonzero(actual[:,224000:]) == 0, "final silence altered")
        require(not np.array_equal(actual[0],actual[1]), "channel order/identity collapsed")
    check("exact_independent_fixture",synthesis)
    def native_wav_and_hash_paths():
        run = RUNS + "/_vae_roundtrip/fixture_" + uuid.uuid4().hex
        relative = run + "/known_audio.wav"
        raw = expected.T.copy().tobytes()
        def writer(path):
            subprocess.run(["ffmpeg","-v","error","-f","f32le","-ar",str(RATE),"-ac","2","-i","-","-c:a","pcm_f32le","-f","wav","-y",str(path)],input=raw,capture_output=True,check=True,timeout=30)
        root.write_with(relative,writer)
        waveform, rate = native_input_loader(root)(str(root.resolve(relative)))
        require(rate == RATE and waveform.shape == (2,COUNT) and np.array_equal(waveform.numpy(),expected), "actual native PyAV float input differs")
        require(np.array_equal(helper.decode_pcm(root.resolve(relative),2)[0].numpy(),expected), "FFmpeg float input differs")
        data = root.read_bytes(relative)
        item = {"path":relative,"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)}
        record(root,item,run)
        helper.rejection(lambda:record(root,dict(item,sha256="0"*64),run),"wrong fixture hash accepted")
        helper.rejection(lambda:record(root,dict(item,path="../outside.wav"),run),"traversal fixture path accepted")
        try:
            record(root,dict(item,path=run+"/missing.wav"),run)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing fixture accepted")
    check("native_pyav_exact_float_wav_hash_path_rejection",native_wav_and_hash_paths)
    check("exact_native_graph_and_schema",lambda:validate_graph(driver.build_graph(copy.deepcopy(info)),info,catalog))
    def invalid_schema():
        for cls,port in (("VAEEncodeAudio","vae"),("CCTVH3AudioDiagnostic","samples"),("SaveAudio","audio")):
            bad = copy.deepcopy(info)
            bad[cls]["input"]["required"][port][0] = "WRONG"
            helper.rejection(lambda bad=bad:driver.build_graph(bad),"driver accepted wrong required schema type")
        bad = copy.deepcopy(info)
        bad["LoadAudio"]["input"]["required"]["audio"][1]["options"] = []
        helper.rejection(lambda:driver.build_graph(bad),"driver accepted missing fixture choice")
    check("invalid_schema_and_missing_fixture_rejected",invalid_schema)
    def shape_identity():
        run = RUNS+"/vae_roundtrip_unit"
        capture = {"receipt":{"invocation_id":"vae_roundtrip_unit","pid":1,"create_time":2,"sample_rate":RATE,"crop":{"retained_samples":COUNT,"discarded_tail_samples":0,"padding_samples":0}}}
        tensors = {"sampled_audio_latent":torch.zeros(1,32,2,320),"raw_decoded_waveform":torch.zeros(1,2,COUNT),"post_native_waveform":torch.zeros(1,2,COUNT)}
        owned = [{"pid":1,"create_time":2}]
        capture_shape(capture,tensors,run,owned)
        for field,value in (("sample_rate",44100),("invocation_id","foreign")):
            bad = copy.deepcopy(capture)
            bad["receipt"][field] = value
            helper.rejection(lambda bad=bad:capture_shape(bad,tensors,run,owned),"invalid capture binding accepted")
        helper.rejection(lambda:capture_shape(capture,tensors,run,[{"pid":1,"create_time":3}]),"reused PID accepted")
        bad = dict(tensors,raw_decoded_waveform=torch.zeros(1,2,1))
        helper.rejection(lambda:capture_shape(capture,bad,run,owned),"wrong waveform shape accepted")
        bad = dict(tensors,raw_decoded_waveform=tensors["raw_decoded_waveform"].clone())
        bad["raw_decoded_waveform"][0,0,4] = float("nan")
        helper.rejection(lambda:capture_shape(capture,bad,run,owned),"nonfinite reconstruction accepted")
    check("actual_tensor_shape_rate_nan_owned_identity",shape_identity)
    return {"passed":all(row["passed"] for row in checks),"checks":checks,"limitations":["No GPU inference; driver watchdog/deadline integration still requires separately injected lifecycle seams or actual retained-run review.","Native VAE quality remains untested by controlled CPU graph/fixture checks."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--self-test",action="store_true")
    parser.add_argument("--report",required=True)
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    require(root.resolve(args.report).is_relative_to(root.resolve(RUNS)),"report outside local_runs")
    require(bool(args.run) != args.self_test,"choose --run or --self-test")
    if args.self_test:
        require(root.resolve(args.report).is_relative_to(root.resolve(RUNS+"/_vae_roundtrip")),"self-test report outside _vae_roundtrip")
    try:
        result = self_test(root) if args.self_test else verify_run(root,args.run)
    except Exception as exc:  # noqa: BLE001 - retain verifier failure without false pass
        result = {"passed":False,"error":type(exc).__name__+": "+str(exc),"traceback":traceback.format_exc()}
    output = json.dumps(support().safe(result),indent=2,allow_nan=False)
    root.write_text(args.report,output+"\n")
    print(output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

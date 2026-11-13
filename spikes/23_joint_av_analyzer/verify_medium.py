"""Independent Amendment 7 verification. CPU only; never loads a model or uses CUDA."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import traceback
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
BASE = "spikes/23_joint_av_analyzer/_work"
RESULTS: list[dict] = []


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def rejects(operation) -> dict:
    try:
        operation()
    except (TypeError, ValueError, RuntimeError) as exc:
        return {"rejected": f"{type(exc).__name__}: {exc}"}
    raise AssertionError("invalid input accepted")


def check(name: str, operation) -> None:
    row = {"check": name, "status": "fail"}
    RESULTS.append(row)
    try:
        row["evidence"] = operation()
        row["status"] = "pass"
    except Exception as exc:  # noqa: BLE001 - preserve independent target failures
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()
    print(row["status"] + ": " + name, flush=True)


def load_target():
    spec = importlib.util.spec_from_file_location("medium_target", ROOT / "spikes/23_joint_av_analyzer/analyze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pure(module) -> None:
    # Explicit boundary cases are independent of the production expression.
    for duration, expected in ((0.001,4),(1,4),(2.5,4),(5,4),(5.000001,6),
                               (7.5,6),(7.500001,8),(10,8),(10.000001,10),
                               (12.5,10),(12.500001,12),(15,12)):
        def valid(duration=duration, expected=expected):
            got = module.sampled_frame_count(duration)
            require(type(got) is int and got == expected, f"duration {duration} expected {expected}, got {got}")
            return {"duration":duration,"count":got}
        check("frame_budget_"+str(duration), valid)
    for index, value in enumerate((None,True,False,0,-1,15.000001,float("nan"),float("inf"),
                                    float("-inf"),"10",b"10",[],{},complex(10,0))):
        check("frame_budget_invalid_"+str(index), lambda value=value: rejects(lambda: module.sampled_frame_count(value)))


def fixture(invocation: str, duration: float, *, video_rate: str="24", video_duration=None) -> Path:
    target = invocation + "/media_" + str(duration).replace(".","p") + "_" + video_rate.replace("/","x") + ".mkv"
    command = ["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-f","lavfi","-i",
               f"testsrc2=size=64x48:rate={video_rate}:duration={duration if video_duration is None else video_duration}",
               "-f","lavfi","-i",f"sine=frequency=731:sample_rate=48000:duration={duration}",
               "-map","0:v:0","-map","1:a:0","-c:v","ffv1","-c:a","pcm_f32le","-ac","2","-y"]
    def write(path):
        actual = [*command,str(path)]
        run = subprocess.run(actual,capture_output=True,text=True,timeout=45,check=False)
        PROJECT.write_text(target + ".command.json",json.dumps({"command":actual,"returncode":run.returncode,"stderr":run.stderr},indent=2)+"\n")
        require(run.returncode==0,run.stderr)
    return PROJECT.write_with(target,write)


def frame_pts(path: Path) -> list[float]:
    command=["ffprobe","-v","error","-select_streams","v:0","-show_frames","-show_entries",
             "frame=best_effort_timestamp_time","-of","json",str(path)]
    run=subprocess.run(command,capture_output=True,text=True,timeout=30,check=False)
    require(run.returncode==0,run.stderr)
    return [float(row["best_effort_timestamp_time"]) for row in json.loads(run.stdout)["frames"]]


def decode_checks(module,invocation: str) -> None:
    import av
    import numpy as np

    media={duration:fixture(invocation,duration) for duration in (5,7.25,10,15,16)}
    for duration,count in ((5,4),(7.25,6),(10,8),(15,12)):
        def decoded(duration=duration,count=count):
            frames,wave,meta=module.decode_media(media[duration],"joint",duration)
            require(wave.dtype==np.float32 and np.isfinite(wave).all(),"waveform type/finite mismatch")
            require(len(wave)==round(duration*16000),"actual audio sample count mismatch")
            require(abs(meta["audio_duration_seconds"]-duration)<1e-6,"audio duration differs")
            require(len(frames)==count==meta["video_frames_sampled"],"actual frame count/budget mismatch")
            require(all(frame.ndim==3 and frame.shape[2]==3 for frame in frames),"non-RGB frame")
            pts=meta["video_sample_pts_relative_seconds"]
            require(len(pts)==count and len(set(pts))==count,"PTS lost or duplicate frames")
            actual_pts=frame_pts(media[duration])
            require(len(actual_pts)==round(duration*24)==meta["video_decoded_frames"],"decoded source frame count differs")
            targets=[duration*index/count for index in range(count)]
            errors=[abs(point-target) for point,target in zip(pts,targets,strict=True)]
            require(all(error<=1/24+0.0011 for error in errors),"sample PTS not evenly spaced over actual audio duration")
            require(all(any(abs(point-actual)<0.0011 for actual in actual_pts) for point in pts),"reported PTS was not decoded")
            require(pts[-1]<duration,"endpoint=False violated")
            require(abs(meta["audio_first_pts_seconds"])<0.0011 and abs(meta["video_first_pts_seconds"])<0.0011,"media start shifted")
            error_fields={key:value for key,value in meta.items() if "error" in key.lower() and isinstance(value,list)}
            require(any(len(value)==count for value in error_fields.values()),"per-selected-frame timing errors not retained")
            return {"media":str(media[duration].relative_to(ROOT)),"sha256":digest(media[duration]),"metadata":meta,"independent_frame_count":len(actual_pts),"independent_targets":targets,"independent_absolute_errors":errors}
        check("real_decode_joint_"+str(duration),decoded)
    for duration in (10,15):
        def audio(duration=duration):
            frames,wave,meta=module.decode_media(media[duration],"audio",duration)
            require(frames==[] and len(wave)==duration*16000,"audio mode duration or frames changed")
            require(meta["video_decoded_frames"]==0 and meta["video_frames_sampled"]==0,"audio mode decoded video")
            return meta
        check("real_decode_audio_"+str(duration),audio)
    for mode in ("joint","audio"):
        check("caller_limit_preserved_"+mode,lambda mode=mode:rejects(lambda:module.decode_media(media[10],mode,8)))
        check("maximum15_preserved_"+mode,lambda mode=mode:rejects(lambda:module.decode_media(media[16],mode,15)))
    sparse=fixture(invocation,10,video_rate="1/10")
    check("insufficient_decoded_frames",lambda:rejects(lambda:module.decode_media(sparse,"joint",10)))

    def early():
        original=av.open
        observed={"decoded_frames":0,"max_audio_end":0.0,"max_video_pts":0.0}
        class Packet:
            def __init__(self,packet):self.packet=packet
            def decode(self):
                for frame in self.packet.decode():
                    observed["decoded_frames"]+=1
                    if isinstance(frame,av.AudioFrame):
                        end=float(frame.pts*frame.time_base)+frame.samples/frame.sample_rate
                        observed["max_audio_end"]=max(observed["max_audio_end"],end)
                    elif isinstance(frame,av.VideoFrame):
                        observed["max_video_pts"]=max(observed["max_video_pts"],float(frame.pts*frame.time_base))
                    yield frame
        class Container:
            def __init__(self,container):self.container=container;self.streams=container.streams
            def demux(self,*args):
                for packet in self.container.demux(*args):yield Packet(packet)
            def close(self):self.container.close()
        with patch.object(av,"open",lambda *args,**kwargs:Container(original(*args,**kwargs))):
            rejects(lambda:module.decode_media(media[16],"joint",8))
        require(max(observed["max_audio_end"],observed["max_video_pts"])<=9.1,f"decoder consumed beyond existing1s early-bound margin: {observed}")
        return observed
    check("duration_decoder_still_rejects_early",early)


@contextmanager
def cpu_tensor_boundary():
    """Keep real analyze_one tensors on CPU and fail any CUDA initialization."""
    import torch
    require(not torch.cuda.is_initialized(), "CUDA was initialized before CPU wiring check")
    evidence = {"transfers": [], "cuda_seed_calls": [], "forbidden_cuda_calls": []}
    original_to = torch.Tensor.to
    def forbidden(name):
        def fail(*args, **kwargs):
            evidence["forbidden_cuda_calls"].append(name)
            raise AssertionError("CUDA initialization/use forbidden: " + name)
        return fail
    def transfer(tensor, *args, **kwargs):
        requested = kwargs.get("device", args[0] if args else None)
        if isinstance(requested, (str, torch.device)) and torch.device(requested).type == "cuda":
            dtype = kwargs.get("dtype", args[1] if len(args) > 1 else tensor.dtype)
            require(str(torch.device(requested)) == "cuda:0", "analyzer did not pin CUDA0")
            expected = torch.bfloat16 if tensor.is_floating_point() else tensor.dtype
            require(dtype == expected, "analyzer transfer dtype differs from BF16/retained integer policy")
            require(tensor.device.type == "cpu", "fixture tensor is not CPU")
            evidence["transfers"].append({"requested_device": str(requested), "requested_dtype": str(dtype),
                "source_dtype": str(tensor.dtype), "shape": list(tensor.shape), "executed_device": "cpu"})
            replaced = dict(kwargs)
            if "device" in replaced:
                replaced["device"] = "cpu"
            else:
                args = (torch.device("cpu"), *args[1:])
            result = original_to(tensor, *args, **replaced)
            require(result.device.type == "cpu" and result.dtype == expected, "intercepted transfer not CPU with expected dtype")
            return result
        return original_to(tensor, *args, **kwargs)
    def seed(value):
        require(type(value) is int and value == 0, "CUDA seed changed from integer0")
        evidence["cuda_seed_calls"].append(value)
    with ExitStack() as stack:
        stack.enter_context(patch.object(torch.Tensor, "to", transfer))
        stack.enter_context(patch.object(torch.cuda, "manual_seed_all", seed))
        for name in ("init", "_lazy_init", "synchronize"):
            stack.enter_context(patch.object(torch.cuda, name, forbidden("torch.cuda." + name)))
        if hasattr(torch._C, "_cuda_init"):
            stack.enter_context(patch.object(torch._C, "_cuda_init", forbidden("torch._C._cuda_init")))
        yield evidence
        require(not torch.cuda.is_initialized(), "CUDA initialized during CPU wiring check")
        require(evidence["forbidden_cuda_calls"] == [], "CUDA tripwire invoked")


def processor_checks(module, cases=None) -> None:
    import numpy as np
    import torch

    class Processor:
        tokenizer=SimpleNamespace(eos_token_id=151645,pad_token_id=151643)
        def __init__(self):self.calls=[];self.batch=None
        def apply_chat_template(self,*args,**kwargs):return "neutral CPU test prompt"
        def __call__(self,*args,**kwargs):
            self.calls.append(kwargs)
            self.batch={"input_ids":torch.tensor([[1,2]]),"attention_mask":torch.ones((1,2),dtype=torch.int64),
                        "input_features":torch.zeros((1,2,3)),"feature_attention_mask":torch.ones((1,2)),
                        "pixel_values_videos":torch.zeros((4,3,2,2)),"video_grid_thw":torch.ones((1,3),dtype=torch.int64)}
            return self.batch
        def batch_decode(self,*args,**kwargs):return ["CPU test output"]
        def decode(self,*args,**kwargs):return "CPU test output"
    class Model:
        device="cuda:0"
        def __init__(self):self.calls=[]
        def generate(self,**kwargs):
            self.calls.append(kwargs)
            tensors = [value for value in kwargs.values() if isinstance(value, torch.Tensor)]
            require(tensors and all(value.device.type == "cpu" for value in tensors), "model fake received non-CPU tensor")
            require(all(value.dtype == torch.bfloat16 for value in tensors if value.is_floating_point()), "float tensors not converted to BF16")
            return torch.tensor([[1,2,7,151645]])
    for duration,count,limit in (cases if cases is not None else ((5,4,8),(10,8,15),(15,12,15),(11.25,10,15))):
        def wiring(duration=duration,count=count,limit=limit):
            processor=Processor();model=Model()
            metadata={"audio_duration_seconds":duration,"audio_sample_rate_hz":16000,
                      "video_frames_sampled":count,"video_sample_pts_relative_seconds":[duration*i/count for i in range(count)]}
            decoded=([np.zeros((16,16,3),dtype=np.uint8) for _ in range(count)],np.zeros(round(duration*16000),dtype=np.float32),metadata)
            with cpu_tensor_boundary() as boundary, patch.object(module,"decode_media",return_value=decoded):
                result=module.analyze_one(processor,model,Path("synthetic-no-media-open"),1417,"joint",384,limit)
            require(len(boundary["transfers"]) == 6, "did not intercept every real input Tensor.to(cuda:0)")
            require(len(boundary["cuda_seed_calls"]) >= 1, "actual CUDA seeding was not intercepted")
            require(len(processor.calls)==1,"processor not called exactly once")
            call=processor.calls[0]
            video_options=call.get("videos_kwargs",call)
            require(math.isclose(video_options["fps"],count/duration,rel_tol=0,abs_tol=1e-10),"processor fps differs from actual selected count / decoded audio duration")
            require(call["use_audio_in_video"] is True,"joint preprocessing lost embedded audio policy")
            require(len(call["videos"][0])==count,"processor received different frame count")
            generated=model.calls[0]
            require(generated["max_new_tokens"]==384 and generated["do_sample"] is False,"generation cap/greedy changed")
            require(generated["eos_token_id"]==151645 and generated["pad_token_id"]==151643,"native EOS/PAD changed")
            require(generated["use_audio_in_video"] is True,"joint generation lost audio policy")
            require(result["termination_reason"]=="eos" and not result["token_cap_reached"],"termination audit lost")
            return {"decoded_duration":duration,"caller_limit":limit,"sample_count":count,"actual_processor_fps":video_options["fps"],"tensor_shapes":result["tensor_shapes"],"cpu_boundary":boundary,"scope":"Decoder is stubbed; processor/model are CPU fakes; actual analyze_one and CPU tensors execute. Tensor.to CUDA0 transfers are redirected to CPU with dtype checks; CUDA seeding is logged and initialization forbidden."}
        check("processor_actual_duration_fps_"+str(duration),wiring)


def source_checks(module, maximum=15) -> None:
    # Source inspection occurs only when the already-authored verifier executes.
    source=PROJECT.read_text("spikes/23_joint_av_analyzer/analyze.py")
    tree=ast.parse(source)
    def cli_default():
        calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="add_argument" and any(isinstance(arg,ast.Constant) and arg.value=="--duration-limit" for arg in node.args)]
        require(len(calls)==1,"duration CLI argument missing/duplicated")
        defaults=[kw.value.value for kw in calls[0].keywords if kw.arg=="default" and isinstance(kw.value,ast.Constant)]
        require(defaults==[8],"duration CLI default changed from8")
        return {"default":8}
    check("cli_duration_default8",cli_default)
    def cli_bound():
        guards=[node for node in ast.walk(tree) if isinstance(node,ast.Compare) and isinstance(node.left,ast.Constant) and node.left.value==0 and len(node.ops)==2 and isinstance(node.ops[0],ast.Lt) and isinstance(node.ops[1],ast.LtE) and isinstance(node.comparators[0],ast.Attribute) and node.comparators[0].attr=="duration_limit" and isinstance(node.comparators[1],ast.Constant)]
        require(any(node.comparators[1].value==maximum for node in guards),f"CLI accepted upper bound is not{maximum}")
        return {"minimum_exclusive":0,"maximum_inclusive":maximum,"scope":"AST CLI validation wiring; no CLI worker/model invocation."}
    check("cli_duration_maximum"+str(maximum),cli_bound)
    def core():
        required=("local_files_only=True","trust_remote_code=False","use_safetensors=True","load_in_4bit=True","bnb_4bit_quant_type=\"nf4\"","bnb_4bit_use_double_quant=True","attn_implementation=\"sdpa\"")
        compact="".join(source.split()).replace("'",'"')
        for needle in required:require(needle in compact,"core policy source absent: "+needle)
        values=[]
        for node in ast.walk(tree):
            if isinstance(node,(ast.Constant,ast.BinOp)) and all(isinstance(child,(ast.Constant,ast.BinOp,ast.Mult,ast.Pow,ast.Add,ast.Sub,ast.Div)) for child in ast.walk(node)):
                try: values.append(eval(compile(ast.Expression(node),"<numeric policy>","eval"),{"__builtins__":{}}))
                except (TypeError,ZeroDivisionError): pass
        require(all(value in values for value in (900,2*1024**3,6*1024**3,8*1024**3)),"resource guard values changed")
        require("low_samples >= 3" in source and "OwnedProcessTree" in source and "expected_env" in source,"three-sample owned supervision/runtime isolation absent")
        return {"scope":"Static policy wiring only; existing real supervisor/lifecycle verifiers remain required."}
    check("core_offline_precision_guards_retained",core)


def runtime_checks(relative: str, durations=(10,15), source_start=1417) -> None:
    receipt=json.loads(PROJECT.read_text(relative),parse_constant=lambda value:(_ for _ in ()).throw(ValueError(value)))
    def audit():
        require(receipt["status"]=="success" and receipt["mode"]=="joint","medium receipt not successful joint")
        require(receipt["model_id"]=="Qwen/Qwen2.5-Omni-7B" and receipt["revision"]=="ae9e1690543ffd5c0221dc27f79834d0294cba00","model pin changed")
        require(receipt["seed"]==0 and receipt["do_sample"] is False and receipt["max_new_tokens"]==384,"medium run generation policy changed")
        meta=receipt["decoded_media"];duration=meta["audio_duration_seconds"]
        require(any(abs(duration-target)<0.002 for target in durations),"receipt is not authorized duration experiment")
        expected={10:8,15:12,25:20}[next(target for target in durations if abs(duration-target)<0.002)]
        require(meta["video_frames_sampled"]==expected,"medium sampled frame count differs")
        pts=meta["video_sample_pts_relative_seconds"]
        require(len(pts)==expected and all(0<=point<duration for point in pts),"PTS outside decoded duration")
        require(all(abs(point-duration*i/expected)<=1/24+0.002 for i,point in enumerate(pts)),"medium sample spacing differs")
        require(receipt["source_start_seconds"]==source_start,"window source start shifted")
        require(meta["video_frame_budget"]==expected and math.isclose(meta["video_sample_fps"],expected/duration,abs_tol=1e-10),"retained frame budget/sample fps differ")
        require(meta["audio_sample_rate_hz"]==16000 and meta["audio_samples"]==round(duration*16000),"retained native processor audio evidence differs")
        for key in ("input_features","feature_attention_mask","pixel_values_videos","video_grid_thw"):
            shape=receipt["tensor_shapes"][key]
            require(isinstance(shape,list) and bool(shape) and all(type(dim) is int and dim>0 for dim in shape),"missing/empty modality tensor"+key)
        require(all(receipt["loading_info"][key]==[] for key in ("missing_keys","unexpected_keys","mismatched_keys")),"loading diagnostics nonempty")
        kwargs=receipt["effective_generation_kwargs"]
        require(kwargs["eos_token_id"]==151645 and kwargs["pad_token_id"]==151643 and kwargs["use_audio_in_video"] is True and kwargs["do_sample"] is False,"effective generation policy changed")
        tokens=receipt["output_token_ids"]
        require(tokens and len(tokens)==receipt["output_token_count"]<=384 and all(type(token) is int and token>=0 for token in tokens),"invalid raw output tokens")
        require(tokens[-1]==151645 and 151645 not in tokens[:-1] and receipt["termination_reason"]=="eos" and receipt["token_cap_reached"] is False,"medium output incomplete or continued after EOS")
        require(isinstance(receipt["output_text"],str) and receipt["output_text"].strip(),"raw model output absent")
        require(digest(PROJECT.resolve("spikes/23_joint_av_analyzer/analyze.py"))==receipt["script_sha256"],"current analyzer source hash differs")
        require(digest(PROJECT.resolve(receipt["media_path"]))==receipt["media_sha256"],"actual media hash differs")
        telemetry=receipt["telemetry"]
        cleanup=telemetry["owned_process_tree_cleanup"]
        require(cleanup["verified_stopped"] is True and cleanup["survivors"]==[] and cleanup.get("failures",[])==[] and cleanup.get("unverifiable_identities",[])==[],"owned cleanup not conclusive")
        identities=telemetry["owned_process_identities_final"]
        require(identities and all(row["live_identity_verified"] is False and type(row["pid"]) is int and isinstance(row["create_time"],(int,float)) for row in identities),"final identity proof absent")
        for stem in ("resources","module_coverage"):
            require(digest(PROJECT.resolve(telemetry[stem+"_path"]))==telemetry[stem+"_sha256"],stem+" artifact hash differs")
        coverage=json.loads(PROJECT.read_text(telemetry["module_coverage_path"]))
        for names_key,count_key,hash_key,expected_count in (("quantized_linear4bit","quantized_linear4bit_count","quantized_linear4bit_sha256",196),("unquantized_linear","unquantized_linear_count","unquantized_linear_sha256",420)):
            names=coverage[names_key]
            require(len(names)==len(set(names))==receipt[count_key]==expected_count,"actual module coverage count differs")
            require(hashlib.sha256("\n".join(names).encode()).hexdigest()==receipt[hash_key],"module list hash differs")
        require(not any("audio_tower" in name or "visual" in name for name in coverage["quantized_linear4bit"]),"encoder was quantized")
        require(all("audio_tower" in name or "visual" in name or "lm_head" in name for name in coverage["unquantized_linear"]),"eligible language layer unquantized")
        rows=[json.loads(line) for line in PROJECT.read_text(telemetry["resources_path"]).splitlines() if line.strip()]
        samples=[row for row in rows if row.get("event")=="sample"]
        require(samples and any(len(row["owned_processes"])>=2 for row in samples),"actual launcher-child telemetry absent")
        require(not any(row.get("event")=="supervisor_failure" or row.get("error") or row.get("cleanup_error") for row in rows),"resource stream retained a supervisor failure")
        require(not receipt.get("error") and not receipt.get("error_type"),"success receipt retained failure diagnostics")
        require(cleanup.get("observation_failures",[])==[],"cleanup retained persistent observation failures")
        require(telemetry["ram_available_samples"]==[{key:value for key,value in row.items() if key!="event"} for row in samples],"receipt/resource sample evidence differs")
        low_count=0
        previous=-1.0
        for sample in samples:
            elapsed=sample["elapsed_seconds"]
            require(type(elapsed) in (int,float) and math.isfinite(elapsed) and previous<=elapsed<900,"sample elapsed invalid, nonmonotonic or beyond stop deadline")
            previous=elapsed
            available=sample["available_ram_bytes"]
            require(type(available) is int and available>=0,"invalid available RAM evidence")
            low_count=low_count+1 if available<2*1024**3 else 0
            require(low_count<3,"success retained three consecutive RAM-stop samples")
            require(type(sample["owned_worker_rss_bytes"]) is int and sample["owned_worker_rss_bytes"]>=0,"invalid aggregate RSS evidence")
            gpu=sample["gpu_device"]
            require(gpu["index"]==0 and all(type(gpu[key]) in (int,float) and math.isfinite(gpu[key]) and gpu[key]>=0 for key in ("free_mib","used_mib")),"invalid GPU0 resource sample")
        require(telemetry["owned_worker_peak_rss_bytes"]==max(row["owned_worker_rss_bytes"] for row in samples),"peakRSS differs")
        for sample in samples:
            require(sample["owned_worker_rss_bytes"]==sum(row["memory_bytes"]["rss"] for row in sample["owned_processes"]),"aggregate ownedRSS differs")
            require(all(type(row["pid"]) is int and isinstance(row["create_time"],(int,float)) for row in sample["owned_processes"]),"sample lacks owned PID/create-time")
        require(telemetry["duration_seconds"]<=900,"owned worker exceeded deadline")
        require(receipt["disk_free_bytes_before"]>=8*1024**3 and receipt["cuda_free_bytes_before_load"]>=8*1024**3,"recorded admission disk/GPU below guard")
        require(telemetry["gpu_device_baseline"]["index"]==0 and telemetry["gpu_device_baseline"]["free_mib"]*1024**2>=8*1024**3,"GPU0 admission evidence differs")
        require(receipt["allocator_warmup_policy"]=="disabled_during_from_pretrained_only","warmup experiment changed")
        return {"receipt":relative,"sha256":digest(PROJECT.resolve(relative)),"duration":duration,"sample_count":expected,"output_tokens":len(tokens),"peak_rss_bytes":telemetry["owned_worker_peak_rss_bytes"],"scope":"Retained evidence audit; no semantic validation, model reload, or parameter replay.","limitations":["No explicit pre-load RAM-admission receipt: samples cannot establish the >=6GiB admission check.","Coverage names/counts/hashes are retained metadata, not a replay of live parameter dtypes.","Successful retained samples do not exercise supervisor failure-path behavior."]}
    check("medium_runtime_receipt",audit)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native",action="store_true",help=argparse.SUPPRESS)
    parser.add_argument("--runtime-receipt")
    args=parser.parse_args()
    if not args.native:
        python=PROJECT.resolve(BASE+"/env/Scripts/python.exe")
        require(python.is_file(),"existing isolated CPU decoder runtime missing")
        scratch=PROJECT.make_dir(BASE+"/verification/medium/temp_"+uuid.uuid4().hex[:10])
        env=os.environ.copy()
        for key in ("PYTHONPATH","PYTHONHOME"):env.pop(key,None)
        env.update(TMP=str(scratch),TEMP=str(scratch),TMPDIR=str(scratch),HF_HOME=str(scratch),XDG_CACHE_HOME=str(scratch),CUDA_VISIBLE_DEVICES="",HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",PYTHONDONTWRITEBYTECODE="1",PYTHONNOUSERSITE="1")
        command=[str(python),"-B",str(Path(__file__)),"--native"]
        if args.runtime_receipt:command += ["--runtime-receipt",args.runtime_receipt]
        return subprocess.run(command,env=env,cwd=ROOT,check=False,timeout=240).returncode
    invocation=BASE+"/verification/medium/"+datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:10]
    module=load_target()
    if args.runtime_receipt:
        check("runtime_receipt_read_and_audit",lambda:runtime_checks(args.runtime_receipt))
    else:
        pure(module)
        check("real_decoder_fixtures_and_checks",lambda:decode_checks(module,invocation))
        processor_checks(module)
        source_checks(module)
    report={"utc":datetime.now(UTC).isoformat(),"checks":RESULTS,"analyzer_sha256":digest(PROJECT.resolve("spikes/23_joint_av_analyzer/analyze.py")),"verifier_sha256":digest(Path(__file__)),"limitations":["No GPU/model inference or semantic accuracy check.","Processor/model are fakes for analyze_one wiring; native PyAV decoding and CPU tensors are real.","Static guard inspection does not replace existing real supervisor/lifecycle verification.","Receipt samples do not establish pre-load >=6GiB RAM admission; coverage metadata does not replay live parameter dtypes.","Historical CPU-only claim withdrawn for four old processor wiring cases; corrected evidence is in cpu_boundary."]}
    report["passed"]=all(row["status"]=="pass" for row in RESULTS)
    path=PROJECT.write_text(invocation+"/report.json",json.dumps(report,indent=2,allow_nan=False)+"\n")
    print(path,flush=True)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())

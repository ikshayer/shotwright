"""Independent Amendment8 CPU verification and optional retained25s receipt audit.

Authored from CONTRACT_25 before the analyzer's25second implementation. Never
loads the model. Wiring runs actual analyze_one with real CPU tensors, explicit
CUDA transfer interception and initialization tripwires from verify_medium.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_medium as shared

PROJECT = shared.PROJECT
ROOT = shared.ROOT
BASE = shared.BASE
check = shared.check
require = shared.require
rejects = shared.rejects


def budgets(module) -> None:
    for duration, expected in ((5,4),(10,8),(15,12),(15.000001,14),(17.5,14),
                               (17.500001,16),(20,16),(20.000001,18),
                               (22.5,18),(22.500001,20),(25,20)):
        def valid(duration=duration, expected=expected):
            actual=module.sampled_frame_count(duration)
            require(type(actual) is int and actual==expected, f"budget {duration}: expected {expected}, got {actual}")
            return {"duration":duration,"frame_budget":actual}
        check("fit25_budget_"+str(duration),valid)
    for index,value in enumerate((25.000001,26,0,-1,True,False,None,"25",b"25",[],{},complex(25,0),float("nan"),float("inf"),float("-inf"))):
        check("fit25_budget_malformed_"+str(index),lambda value=value:rejects(lambda:module.sampled_frame_count(value)))


def decoded25(module,invocation: str) -> None:
    import numpy as np
    media=shared.fixture(invocation,25)
    overlong=shared.fixture(invocation,26)
    sparse=shared.fixture(invocation,25,video_rate="1/25")
    def joint():
        frames,wave,meta=module.decode_media(media,"joint",25)
        require(wave.dtype==np.float32 and len(wave)==400000 and np.isfinite(wave).all(),"25s real audio is not400000finite float32samples")
        require(meta["audio_duration_seconds"]==25 and meta["audio_sample_rate_hz"]==16000,"actual duration/rate differs")
        require(len(frames)==meta["video_frames_sampled"]==meta["video_frame_budget"]==20,"actual25s joint decode did not select20frames")
        require(all(frame.shape==(48,64,3) and frame.dtype==np.uint8 for frame in frames),"selected native syntheticRGB frame shape differs")
        source_pts=shared.frame_pts(media)
        pts=meta["video_sample_pts_relative_seconds"]
        require(len(source_pts)==meta["video_decoded_frames"]==600,"actual25s fixture is not600frames")
        require(len(pts)==len(set(pts))==20,"selected PTS duplicate/missing")
        targets=[index*1.25 for index in range(20)]
        errors=[abs(point-target) for point,target in zip(pts,targets,strict=True)]
        require(all(error<=1/24+0.0011 for error in errors),"25s samples not evenly spaced endpoint=False")
        require(all(any(abs(point-native)<0.0011 for native in source_pts) for point in pts),"selected PTS not present in actual decodedvideo")
        require(pts[-1]<25 and abs(meta["audio_first_pts_seconds"])<0.0011 and abs(meta["video_first_pts_seconds"])<0.0011,"endpoint/start alignment differs")
        require(math.isclose(meta["video_sample_fps"],0.8,abs_tol=1e-10),"native decoder sample fps differs")
        recorded=meta["video_sample_target_error_seconds"]
        require(len(recorded)==20 and all(abs(abs(value)-error)<0.0011 for value,error in zip(recorded,errors,strict=True)),"recorded selection errors differ")
        return {"media":str(media.relative_to(ROOT)),"sha256":shared.digest(media),"actual_decoded_metadata":meta,"independent_source_frame_count":len(source_pts),"independent_targets":targets,"independent_absolute_errors":errors}
    check("fit25_native_pyav_joint20frames_400000samples",joint)
    def audio():
        frames,wave,meta=module.decode_media(media,"audio",25)
        require(frames==[] and len(wave)==400000 and meta["audio_duration_seconds"]==25,"25s audio-only behavior differs")
        require(meta["video_decoded_frames"]==meta["video_frames_sampled"]==0,"audio-only decoded video")
        return meta
    check("fit25_native_audio25seconds",audio)
    for mode in ("joint","audio"):
        for caller_limit in (8,15,24):
            check(f"fit25_caller{caller_limit}_preserved_{mode}",lambda mode=mode,caller_limit=caller_limit:rejects(lambda:module.decode_media(media,mode,caller_limit)))
        check("fit25_global_maximum_preserved_"+mode,lambda mode=mode:rejects(lambda:module.decode_media(overlong,mode,25)))
    check("fit25_insufficient_frames_rejected",lambda:rejects(lambda:module.decode_media(sparse,"joint",25)))


def runtime25(relative: str) -> None:
    # Reuses the exact10/15receipt audits with the authorized25s duration/start;
    # a separate primary receipt is read, never rewritten to satisfy the checker.
    shared.runtime_checks(relative,durations=(25,),source_start=1407)
    def details():
        receipt=json.loads(PROJECT.read_text(relative))
        meta=receipt["decoded_media"]
        require(receipt["duration_limit_seconds"]==25,"25s caller limit differs")
        require(meta["audio_samples"]==400000 and meta["video_decoded_frames"]==600,"25s retained source sample/frame count differs")
        require("25.00-second" in receipt["prompt"],"retained prompt did not use actual25s duration")
        return {"primary_receipt":relative,"source_interval":[1407,1432],"scope":"Original retained receipt/hash/telemetry audit; no semantic accuracy validation or model replay."}
    check("fit25_original_receipt_duration_and_source",details)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native",action="store_true",help=argparse.SUPPRESS)
    parser.add_argument("--runtime-receipt")
    args=parser.parse_args()
    if not args.native:
        python=PROJECT.resolve(BASE+"/env/Scripts/python.exe")
        require(python.is_file(),"existing isolated PyAVruntime missing")
        scratch=PROJECT.make_dir(BASE+"/fit25_20261111/verification/temp_"+uuid.uuid4().hex[:10])
        env=os.environ.copy()
        for key in ("PYTHONPATH","PYTHONHOME"):env.pop(key,None)
        env.update(TMP=str(scratch),TEMP=str(scratch),TMPDIR=str(scratch),HF_HOME=str(scratch),XDG_CACHE_HOME=str(scratch),CUDA_VISIBLE_DEVICES="",HF_HUB_OFFLINE="1",TRANSFORMERS_OFFLINE="1",PYTHONDONTWRITEBYTECODE="1",PYTHONNOUSERSITE="1")
        command=[str(python),"-B",str(Path(__file__)),"--native"]
        if args.runtime_receipt:command += ["--runtime-receipt",args.runtime_receipt]
        return subprocess.run(command,env=env,cwd=ROOT,check=False,timeout=240).returncode
    invocation=BASE+"/fit25_20261111/verification/"+datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:10]
    shared.RESULTS.clear()
    if args.runtime_receipt:
        check("fit25_runtime_read_and_audit",lambda:runtime25(args.runtime_receipt))
    else:
        module=shared.load_target()
        budgets(module)
        check("fit25_real_decoder_fixtures_and_checks",lambda:decoded25(module,invocation))
        shared.processor_checks(module,cases=((25,20,25),(23.25,20,25)))
        shared.source_checks(module,maximum=25)
    report={"utc":datetime.now(UTC).isoformat(),"checks":shared.RESULTS,
            "analyzer_sha256":shared.digest(PROJECT.resolve("spikes/23_joint_av_analyzer/analyze.py")),
            "verifier_sha256":shared.digest(Path(__file__)),"shared_verifier_sha256":shared.digest(Path(shared.__file__)),
            "limitations":["No GPU/model loading or semantic validation.","Native PyAVaudio/video fixture decoding is real; processor/model are fakes for actual analyze_one with real CPU tensors.","Tensor.to CUDA0 interception asserts requested device/dtype and executes CPU conversion; CUDA seed calls are logged and initialization explicitly forbidden.","Coverage names/count/hash evidence supports NF4encoder exclusion; does not independently read live module dtypes.","Optional receipt audit reads actual immutable artifacts; absent runtime receipt means runtime fit remains unverified.","RAM periodic samples cannot prove pre-load >=6GiB admission; retained success evidence does not exercise supervisor failure paths."]}
    report["passed"]=all(row["status"]=="pass" for row in shared.RESULTS)
    path=PROJECT.write_text(invocation+"/report.json",json.dumps(report,indent=2,allow_nan=False)+"\n")
    print(path,flush=True)
    return 0 if report["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())

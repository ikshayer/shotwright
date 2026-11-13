"""Independent Amendment 9 verifier; no model, GPU, or reference-media inference."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import sys
import traceback
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

PROJECT = ProjectRoot(ROOT)
BASE = "spikes/23_joint_av_analyzer/_work"
FROZEN = BASE + "/prompt_detail_comparison_20261112/frozen_before/analyze.py"
ROWS = []
EXACT = "  Describe details.\r\nKeep {duration} literal.\nUncertainty: café 音.\n "

def require(ok, message):
    if not ok:
        raise AssertionError(message)

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, PROJECT.resolve(relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def check(name, operation):
    row = {"check": name, "status": "fail"}
    ROWS.append(row)
    try:
        row["evidence"] = operation()
        row["status"] = "pass"
    except Exception as exc:  # noqa: BLE001 - independent target failures must be retained
        row.update(error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    print(row["status"] + ": " + name, flush=True)

def rejects(fn):
    try:
        fn()
    except (TypeError, ValueError, RuntimeError, OSError, SystemExit) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    raise AssertionError("invalid input accepted")

def pure(target, before):
    for name in ("prompt_for_mode", "analyze_one", "_worker", "run_supervised"):
        def signature(name=name):
            params=list(inspect.signature(getattr(target,name)).parameters.values())
            require(params[-1].name=="analysis_prompt" and params[-1].default is None,
                    name+" must end with analysis_prompt=None")
            return str(inspect.signature(getattr(target,name)))
        check("trailing_public_seam_"+name, signature)
    for mode in ("joint","audio"):
        for duration in (None,0.1,5,15):
            def default(mode=mode,duration=duration):
                expected=before.prompt_for_mode(mode,duration)
                require(target.prompt_for_mode(mode,duration)==expected,"default bytes changed")
                require(target.prompt_for_mode(mode,duration,None)==expected,"explicit None changed")
                require(target.prompt_for_mode(mode,duration,EXACT)==EXACT,"override reformatted")
                return {"mode":mode,"duration":duration,"default_sha256":hashlib.sha256(expected.encode()).hexdigest()}
            check(f"default_and_exact_{mode}_{duration}", default)
    for value in (""," \r\n\t","\0","abc\0def",True,False,1,b"text",[],{}):
        check("invalid_override_"+repr(value),lambda value=value:rejects(lambda:target.prompt_for_mode("joint",15,value)))
    for value in (True,False,0,-1,"15",float("nan"),float("inf"),[]):
        check("override_does_not_bypass_duration_"+repr(value),
              lambda value=value:rejects(lambda:target.prompt_for_mode("joint",value,EXACT)))
    check("override_does_not_bypass_mode",lambda:rejects(lambda:target.prompt_for_mode("bogus",15,EXACT)))

def actual_one(target, medium, mode):
    import numpy as np
    import torch
    calls={}
    class Processor:
        tokenizer=SimpleNamespace(eos_token_id=151645,pad_token_id=151643)
        def apply_chat_template(self,messages,**kwargs):
            calls["messages"]=messages
            return "CPU template"
        def __call__(self,**kwargs):
            calls["processor"]=kwargs
            tensors={"input_ids":torch.tensor([[1,2]]),"attention_mask":torch.ones((1,2),dtype=torch.int64),
                     "input_features":torch.zeros((1,2,3)),"feature_attention_mask":torch.ones((1,2))}
            if mode=="joint":
                tensors.update(pixel_values_videos=torch.zeros((12,3,2,2)),
                               video_grid_thw=torch.ones((1,3),dtype=torch.int64))
            return tensors
        def batch_decode(self,*args,**kwargs):return ["CPU output"]
    class Model:
        def generate(self,**kwargs):
            calls["generate"]=kwargs
            require(all(v.device.type=="cpu" for v in kwargs.values() if isinstance(v,torch.Tensor)),"nonCPU tensor")
            return torch.tensor([[1,2,7,151645]])
    decoded=([np.zeros((8,8,3),dtype=np.uint8) for _ in range(12)] if mode=="joint" else [],
             np.zeros(240000,dtype=np.float32),
             {"audio_duration_seconds":15,"audio_sample_rate_hz":16000,"video_frames_sampled":12 if mode=="joint" else 0})
    with medium.cpu_tensor_boundary() as boundary, patch.object(target,"decode_media",return_value=decoded):
        result=target.analyze_one(Processor(),Model(),Path("synthetic"),1417,mode,384,15,EXACT)
    require(calls["messages"][0]["content"][0]["text"]==EXACT,"native chat text changed")
    require(result["prompt"]==EXACT,"window receipt prompt differs")
    require(len(boundary["transfers"])==(6 if mode=="joint" else 4),"not all transfers intercepted")
    require(boundary["cuda_seed_calls"],"seed interception missing")
    opts=calls["generate"]
    require(opts["max_new_tokens"]==384 and opts["do_sample"] is False,"cap/greedy changed")
    require(opts["eos_token_id"]==151645 and opts["pad_token_id"]==151643,"EOS/PAD changed")
    require(opts["use_audio_in_video"]==(mode=="joint"),"modality option changed")
    if mode=="joint":
        require(calls["processor"]["videos_kwargs"]["fps"]==0.8,"fps changed")
    return {"cpu_boundary":boundary,"prompt":result["prompt"],"tensor_shapes":result["tensor_shapes"],
            "scope":"Real analyze_one and CPU tensors; decoder, processor and model replaced. No inference."}

def cli(target, invocation, raw, label, valid):
    relative=invocation+"/"+label+".txt"
    PROJECT.write_bytes(relative,raw)
    prompt_path=PROJECT.resolve(relative)
    media=PROJECT.write_bytes(invocation+"/synthetic_media.bin",b"not decoded")
    seen={"reads":0,"supervisor":0}
    original_open=Path.open
    def opened(path,*args,**kwargs):
        if Path(path).resolve()==prompt_path:
            seen["reads"]+=1
        return original_open(path,*args,**kwargs)
    supervisor_signature=inspect.signature(target.run_supervised)
    def supervisor(*args,**kwargs):
        seen["supervisor"]+=1
        bound=supervisor_signature.bind(*args,**kwargs)
        bound.apply_defaults()
        seen["prompt"]=bound.arguments["analysis_prompt"]
        seen["result_rel"]=bound.arguments["result_rel"]
        return {"status":"failure","error_type":"SyntheticStop","error":"CPU verifier stops before inference","results":[]}, {}
    argv=["analyze.py","run","--media",str(media),"--start","1417","--duration-limit","15",
          "--max-new-tokens","384","--prompt-file",str(prompt_path)]
    with ExitStack() as stack:
        stack.enter_context(patch.object(sys,"argv",argv))
        stack.enter_context(patch.object(sys,"prefix",str(PROJECT.resolve(BASE+"/env"))))
        stack.enter_context(patch.object(target,"run_supervised",supervisor))
        stack.enter_context(patch.object(Path,"open",opened))
        try:target.main()
        except (Exception,SystemExit) as exc:seen["exit"]=f"{type(exc).__name__}: {exc}"  # noqa: BLE001 - retain CLI rejection evidence
    if valid:
        require(seen["supervisor"]==1,"valid prompt did not reach supervisor: "+str(seen))
        require(seen["prompt"]==raw.decode("utf-8"),"CLI changed exact UTF8 text")
        require(seen["reads"]==1,"prompt file not read exactly once: "+str(seen["reads"]))
        receipt_path=PROJECT.resolve(seen["result_rel"]).parent/"analysis.json"
        receipt=json.loads(PROJECT.read_text(str(receipt_path.relative_to(ROOT)).replace("\\","/")))
        require(receipt["prompt"]==raw.decode("utf-8"),"primary failure prompt missing")
        encoded=json.dumps(receipt,ensure_ascii=False).replace("\\\\","/")
        require(hashlib.sha256(raw).hexdigest() in encoded,"original bytes hash absent")
        require(relative in encoded,"original contained relative path absent")
        seen["receipt"]=str(receipt_path.relative_to(ROOT))
    else:
        require(seen["supervisor"]==0,"invalid file launched worker")
        require("exit" in seen,"invalid file silently accepted")
    return seen

def outside_cli(target, prompt_path=None):
    with patch.object(sys,"argv",["analyze.py","run","--media",str(PROJECT.resolve(FROZEN)),
                 "--start","1417","--prompt-file",str(prompt_path or ROOT.parent/"outside.txt")]), \
         patch.object(sys,"prefix",str(PROJECT.resolve(BASE+"/env"))), \
         patch.object(target,"run_supervised",side_effect=AssertionError("worker must not launch")) as launch:
        evidence=rejects(target.main)
        require(not launch.called,"outside prompt launched worker")
        return evidence

def propagation(target, invocation):
    # Intercept at process creation: exercise real supervisor JSON construction without a child.
    captured={}
    class StopBeforeLaunch(Exception):pass
    def popen(command,**kwargs):
        captured["spec"]=json.loads(command[command.index("--_worker-json")+1])
        raise StopBeforeLaunch()
    with patch.object(target,"_gpu_memory_sample",return_value={}), \
         patch.object(target,"_load_process_tree_module"), \
         patch.object(target.subprocess,"Popen",popen):
        try:
            target.run_supervised([],"joint",384,15,invocation+"/worker_result.json",
                                  invocation+"/stdout",invocation+"/stderr",invocation+"/resources",EXACT)
        except StopBeforeLaunch:pass
    require(captured["spec"]["analysis_prompt"]==EXACT,"worker JSON lost exact override")
    worker_signature=inspect.signature(target._worker)
    for supplied in (False,True):
        spec={"items":[],"mode":"joint","max_new_tokens":384,"duration_limit":15,
              "result_rel":invocation+"/unused"}
        if supplied:spec["analysis_prompt"]=EXACT
        with patch.object(sys,"argv",["analyze.py","--_worker-json",json.dumps(spec)]), \
             patch.object(target,"_worker",return_value=0) as worker:
            try:target.main()
            except SystemExit as exc:require(exc.code==0,"worker dispatcher failed")
            bound=worker_signature.bind(*worker.call_args.args,**worker.call_args.kwargs)
            bound.apply_defaults()
            require(bound.arguments["analysis_prompt"]==(EXACT if supplied else None),"worker dispatcher default/override differs")
    return captured

def worker_forward(target, invocation):
    # Load seam is replaced before actual worker entry; no library/model loads occur.
    seen=[]
    one_signature=inspect.signature(target.analyze_one)
    def one(*args,**kwargs):
        bound=one_signature.bind(*args,**kwargs)
        bound.apply_defaults();seen.append(bound.arguments["analysis_prompt"])
        return {"prompt":EXACT,"text":"CPU output"}
    with patch.object(target,"load_model",return_value=(object(),object(),{},["q"],["u"],{},1,{})), \
         patch.object(target,"analyze_one",one):
        code=target._worker([{"media":"synthetic","start":1417}],"joint",384,15,
                            invocation+"/forward/worker_result.json",EXACT)
    require(code==0 and seen==[EXACT],"worker forwarding failed")
    return {"forwarded":seen}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--runtime-receipt")
    args=parser.parse_args()
    invocation=BASE+"/prompt_verification/"+uuid.uuid4().hex
    PROJECT.make_dir(invocation)
    target=load("prompt_target","spikes/23_joint_av_analyzer/analyze.py")
    before=load("prompt_before",FROZEN)
    medium=load("prompt_medium","spikes/23_joint_av_analyzer/verify_medium.py")
    pure(target,before)
    for mode in ("joint","audio"):
        check("actual_analyze_one_"+mode,lambda mode=mode:actual_one(target,medium,mode))
    for label,raw,valid in (("exact",EXACT.encode("utf-8"),True),("empty",b"",False),
                            ("whitespace",b" \r\n",False),("embedded_nul",b"one\0two",False),
                            ("bad_utf8",b"\xff\xfe",False)):
        check("cli_"+label,lambda raw=raw,label=label,valid=valid:cli(target,invocation,raw,label,valid))
    check("cli_outside",lambda:outside_cli(target))
    check("cli_directory",lambda:outside_cli(target,PROJECT.resolve(invocation)))
    check("cli_missing",lambda:outside_cli(target,PROJECT.resolve(invocation+"/missing.txt")))
    check("supervisor_and_dispatch_json",lambda:propagation(target,invocation))
    check("worker_to_analyze_one",lambda:worker_forward(target,invocation))
    report={"status":"pass" if all(row["status"]=="pass" for row in ROWS) else "fail",
            "checks":ROWS,"limitations":["No GPU/model/native processor inference. CLI and supervisor child creation are intercepted.",
            "No semantic accuracy, real cleanup, media decoding, or runtime fit certification; parent audits actual runtime.",
            "Runtime receipt comparison is not implemented in this CPU script; use parent actual-artifact audit."],
            "runtime_receipt_requested":args.runtime_receipt}
    path=PROJECT.write_text(invocation+"/report.json",json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(path)
    raise SystemExit(0 if report["status"]=="pass" else 1)

if __name__=="__main__":main()

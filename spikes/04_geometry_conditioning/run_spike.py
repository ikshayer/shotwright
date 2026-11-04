"""Phase 0 spike 04 - does FLUX.2 reference conditioning preserve camera geometry?

9's geometry-conditioned image synthesis assumes the FR-29 grey-box render can
steer generation so that camera geometry stays continuous across generated
shots. Spike 03 established that FLUX.2 klein has no ControlNet path in this
build; it conditions through ReferenceLatent + VAEEncode instead.

So the question is narrower and sharper than "does ControlNet work": does a
reference latent built from a grey-box render hold the *layout*, or only the
subject matter?

Controlled comparison, same prompt and same seed throughout:
  control    prompt only, no reference        - what the prompt alone produces
  reference  prompt + grey-box reference      - what the geometry adds

Run from the orchestrator venv:
  .venv\\Scripts\\python.exe spikes\\04_geometry_conditioning\\run_spike.py [--introspect]
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "03_flux_fit"))

import run_spike as s3  # noqa: E402  - reuse launch/sampler/release

GREYBOX = os.path.join(HERE, "_work", "greybox_1280x960.png")
COMFY_INPUT = os.path.join(s3.COMFY_ROOT, "ComfyUI", "input")
WIDTH, HEIGHT = 1280, 960
SEED = 20051114

PROMPT = ("grainy black and white security camera still of a cluttered room, "
          "overhead corner view, cheap CCTV optics, flat practical ceiling "
          "lighting, crushed blacks, low detail, mundane")

EXTRA_NODES = ["LoadImage", "VAEEncode", "ReferenceLatent",
               "ImageScaleToTotalPixels", "GetImageSize"]


def base_graph(ref_image=None):
    """Text-to-image, optionally with a reference latent attached."""
    g = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "flux-2-klein-4b.safetensors",
                         "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen_3_4b_fp4_flux2.safetensors",
                         "type": "flux2", "device": "default"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"text": PROMPT, "clip": ["2", 0]}},
        "5": {"class_type": "ConditioningZeroOut",
              "inputs": {"conditioning": ["4", 0]}},
        "6": {"class_type": "CFGGuider",
              "inputs": {"model": ["1", 0], "positive": ["4", 0],
                         "negative": ["5", 0], "cfg": 1.0}},
        "7": {"class_type": "Flux2Scheduler",
              "inputs": {"steps": 4, "width": WIDTH, "height": HEIGHT}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": SEED}},
        "10": {"class_type": "EmptyFlux2LatentImage",
               "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
        "11": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["9", 0], "guider": ["6", 0],
                          "sampler": ["8", 0], "sigmas": ["7", 0],
                          "latent_image": ["10", 0]}},
        # Tiled decode: plain VAEDecode OOMs ("bad allocation") at 1280x960 on
        # this card - it is the memory peak of the graph, not the diffusion
        # model. This is 8.3's lower-memory fallback, named concretely.
        "12": {"class_type": "VAEDecodeTiled",
               "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage",
               "inputs": {"images": ["12", 0], "filename_prefix": "spike04"}},
    }
    if ref_image:
        g["20"] = {"class_type": "LoadImage", "inputs": {"image": ref_image}}
        g["21"] = {"class_type": "ImageScaleToTotalPixels",
                   "inputs": {"image": ["20", 0], "upscale_method": "nearest-exact",
                              "megapixels": 1.0}}
        g["22"] = {"class_type": "VAEEncode",
                   "inputs": {"pixels": ["21", 0], "vae": ["3", 0]}}
        g["23"] = {"class_type": "ReferenceLatent",
                   "inputs": {"conditioning": ["4", 0], "latent": ["22", 0]}}
        # the guider's positive conditioning now carries the reference
        g["6"]["inputs"]["positive"] = ["23", 0]
        g["5"]["inputs"]["conditioning"] = ["23", 0]
    return g


def fill_required_defaults(graph, info):
    """Supply any required input the graph omits, using ComfyUI's own default.

    Hand-built graphs drift from node signatures as ComfyUI changes. Reading
    the declared default is strictly better than guessing one, and it reports
    what it filled so the omission is visible rather than silent.
    """
    filled = []
    for nid, node in graph.items():
        spec = info.get(node["class_type"])
        if not spec:
            continue
        for key, decl in spec["input"].get("required", {}).items():
            if key in node["inputs"]:
                continue
            default = None
            if isinstance(decl, list) and len(decl) > 1 and isinstance(decl[1], dict):
                default = decl[1].get("default")
            if default is None and isinstance(decl, list) and isinstance(decl[0], list):
                default = decl[0][0] if decl[0] else None
            if default is None:
                raise SystemExit("node %s (%s) needs '%s' and declares no default"
                                 % (nid, node["class_type"], key))
            node["inputs"][key] = default
            filled.append("%s.%s=%r" % (node["class_type"], key, default))
    if filled:
        print("filled from node defaults: %s" % ", ".join(filled))
    return graph


def img2img_graph(ref_image, denoise_steps=4, total_steps=8):
    """True img2img: the grey-box IS the starting latent, partially denoised.

    ReferenceLatent turned out to be content conditioning, not structural
    control. Starting from the grey-box latent and denoising only part of the
    way is the mechanism that can actually hold layout.
    """
    g = base_graph()
    g["20"] = {"class_type": "LoadImage", "inputs": {"image": ref_image}}
    g["22"] = {"class_type": "VAEEncode",
               "inputs": {"pixels": ["20", 0], "vae": ["3", 0]}}
    g["7"]["inputs"]["steps"] = total_steps
    g["24"] = {"class_type": "SplitSigmas",
               "inputs": {"sigmas": ["7", 0], "step": total_steps - denoise_steps}}
    # SplitSigmas output 1 is the later (low-noise) half: start partway in
    g["11"]["inputs"]["sigmas"] = ["24", 1]
    g["11"]["inputs"]["latent_image"] = ["22", 0]
    return g


def run(name, graph, sampler):
    mark = time.time()
    body = s3.post("/prompt", {"prompt": graph, "client_id": "spike04"})
    pid = body.get("prompt_id")
    if not pid:
        return {"name": name, "error": "no prompt_id", "response": body}
    t0 = time.time()
    entry, err = None, None
    while time.time() - t0 < 900:
        hist = s3.get("/history/%s" % pid)
        if pid in hist:
            entry = hist[pid]
            st = entry.get("status", {})
            if st.get("status_str") == "error":
                err = [str(m)[:400] for m in st.get("messages", [])][-1:]
            break
        time.sleep(0.5)
    out = {"name": name, "seconds": round(time.time() - t0, 1),
           "peak_gb": round(sampler.peak(since=mark), 2),
           "completed": bool(entry and not err)}
    if err:
        out["error"] = err
    files = []
    if entry:
        for node_out in (entry.get("outputs") or {}).values():
            for img in node_out.get("images", []):
                files.append(img.get("filename"))
    out["files"] = files
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--introspect", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(GREYBOX):
        raise SystemExit("grey-box render missing: %s" % GREYBOX)
    ref_name = "spike04_greybox.png"
    shutil.copyfile(GREYBOX, os.path.join(COMFY_INPUT, ref_name))

    os.makedirs(os.path.join(HERE, "_work"), exist_ok=True)
    s3.HERE = HERE  # so the ComfyUI log lands in this spike's _work
    sampler = s3.MemorySampler()
    used, total = sampler.read_gb()
    print("device %.2f GB | baseline %.2f GB" % (total, used))
    sampler.start()

    proc = None
    if s3.already_running():
        print("using the ComfyUI already on %d" % s3.PORT)
    else:
        proc, boot = s3.launch(clean_nodes=True)
        print("ComfyUI ready in %.1f s" % boot)

    try:
        if args.introspect:
            info = s3.get("/object_info")
            for t in EXTRA_NODES:
                if t not in info:
                    print("%-24s MISSING" % t)
                    continue
                req = info[t]["input"].get("required", {})
                print("%-24s required=%s" % (t, list(req.keys())))
            return 0

        info = s3.get("/object_info")
        results = []
        for name, graph in (("img2img-greybox-d6of8", img2img_graph(ref_name, 6, 8)),
                            ("img2img-greybox-d7of8", img2img_graph(ref_name, 7, 8))):
            print("\n=== %s ===" % name)
            r = run(name, fill_required_defaults(graph, info), sampler)
            r["release"] = s3.measured_release(sampler, used + 0.5)
            print(json.dumps(r, indent=2))
            results.append(r)

        out_dir = os.path.join(s3.COMFY_ROOT, "ComfyUI", "output")
        for r in results:
            for f in r.get("files", []):
                src = os.path.join(out_dir, f)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(HERE, "_work",
                                                      "%s__%s" % (r["name"], f)))
        with open(os.path.join(HERE, "results.json"), "w") as fh:
            json.dump({"prompt": PROMPT, "seed": SEED,
                       "resolution": [WIDTH, HEIGHT], "runs": results},
                      fh, indent=2, sort_keys=True)
        print("\nwrote results.json")
        return 0
    finally:
        sampler.stop()
        if proc:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())

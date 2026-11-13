"""Isolated pinned acquisition/setup. Run with existing orchestrator Python, never mutate it."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from orchestrator.platform.containment import ProjectRoot

R = ProjectRoot(Path(__file__).resolve().parents[2])
B = "spikes/21_video_foley/_work"


def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def env():
    e = os.environ.copy()
    for k, v in {
        "TMP": "tmp",
        "TEMP": "tmp",
        "TMPDIR": "tmp",
        "HF_HOME": "cache/hf",
        "HUGGINGFACE_HUB_CACHE": "cache/hf/hub",
        "TORCH_HOME": "cache/torch",
        "XDG_CACHE_HOME": "cache/xdg",
        "UV_CACHE_DIR": "cache/uv",
        "PIP_CACHE_DIR": "cache/pip",
        "NUMBA_CACHE_DIR": "cache/numba",
    }.items():
        e[k] = str(R.make_dir(B + "/" + v))
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    e["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return e


E = env()
M = json.loads(R.read_text(B + "/hf_metadata.json"))
repos = [
    ("tencent/HunyuanVideo-Foley", "3abd4e833b95b8db0fc9c687afc52483a48e9a97", M["model"], "foley"),
    ("google/siglip2-base-patch16-512", M["siglip"]["sha"], M["siglip_files"], "siglip"),
    ("laion/larger_clap_general", M["clap"]["sha"], M["clap_files"], "clap"),
]
items = []
for repo, rev, files, group in repos:
    if isinstance(files, dict):
        files = files.get("siblings", [])
    for f in files:
        name = f.get("path", f.get("rfilename", ""))
        selected = (
            name
            in [
                "hunyuanvideo_foley_xl.pth",
                "synchformer_state_dict.pth",
                "vae_128d_48k.pth",
                "LICENSE",
                "NOTICE",
                "config_xl.yaml",
            ]
            if group == "foley"
            else name not in [".gitattributes", "tokenizer.model"]
            and (
                not name.endswith((".bin", ".safetensors"))
                or name in ["model.safetensors", "pytorch_model.bin"]
            )
        )
        if not selected or f.get("type", "file") != "file":
            continue
        lfs = f.get("lfs") or {}
        expected = lfs.get("oid", lfs.get("sha256"))
        blob = f.get("oid", f.get("blobId"))
        if not expected and not blob:
            raise RuntimeError("No authoritative hash " + name)
        items.append(
            {
                "repo": repo,
                "revision": rev,
                "name": name,
                "path": f"{B}/models/{group}/{name}",
                "sha256_expected": expected,
                "git_blob_sha1": blob,
                "size": f.get("size", lfs.get("size")),
            }
        )
if "--env" not in sys.argv:
    R.write_text(B + "/download-lock.json", json.dumps(items, indent=2) + "\n")
print(
    "Pinned acquisition",
    len(items),
    "files, GB",
    sum(i.get("size") or 0 for i in items) / 1e9,
    flush=True,
)


def fetch(i):
    p = R.resolve(i["path"])
    if not p.exists() or (i["sha256_expected"] and sha(p) != i["sha256_expected"]):
        u = f"https://huggingface.co/{i['repo']}/resolve/{i['revision']}/{i['name']}?download=true"
        req = urllib.request.Request(u, headers={"User-Agent": "video-pipeline-foley-spike/1"})
        with urllib.request.urlopen(req, timeout=90) as response:
            R.write_stream(i["path"], response)
    data_sha = sha(p)
    if i["sha256_expected"]:
        assert data_sha == i["sha256_expected"], i["name"] + " SHA256 mismatch"
    else:
        data = p.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        assert actual == i["git_blob_sha1"], i["name"] + " git blob mismatch"
    i["sha256"] = data_sha
    print("Verified", i["repo"], i["name"], p.stat().st_size, flush=True)
    return i


if "--download" in sys.argv:
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(fetch, items))
    R.write_text(B + "/verified-models.json", json.dumps(results, indent=2) + "\n")
if "--env" in sys.argv:
    target = R.make_dir(B + "/env")
    py = target / "Scripts/python.exe"

    def cmd(args, label):
        print("SETUP", label, flush=True)

        def work(path):
            with path.open("wb") as out:
                cp = subprocess.run(args, env=E, stdout=out, stderr=subprocess.STDOUT, check=False)
                if cp.returncode:
                    raise RuntimeError(label + " failed; see " + str(path))

        R.write_with(B + "/" + label + ".log", work)

    if not py.exists():
        cmd(
            [
                "C:/Users/spark/.local/bin/uv.exe",
                "venv",
                "--python",
                "C:/Users/spark/AppData/Local/Programs/Python/Python311/python.exe",
                str(target),
            ],
            "venv",
        )
    cmd(
        [
            "C:/Users/spark/.local/bin/uv.exe",
            "pip",
            "install",
            "--python",
            str(py),
            "torch==2.7.1",
            "torchvision==0.22.1",
            "torchaudio==2.7.1",
            "--index-url",
            "https://download.pytorch.org/whl/cu128",
        ],
        "install-torch",
    )
    cmd(
        [
            "C:/Users/spark/.local/bin/uv.exe",
            "pip",
            "install",
            "--python",
            str(py),
            "torch==2.7.1",
            "torchvision==0.22.1",
            "torchaudio==2.7.1",
            "numpy==1.26.4",
            "transformers==4.51.3",
            "diffusers==0.33.1",
            "accelerate==1.6.0",
            "timm==1.0.15",
            "descript-audiotools==0.7.2",
            "av==15.1.0",
            "einops==0.8.1",
            "loguru==0.7.3",
            "omegaconf==2.3.0",
            "pandas==2.2.3",
            "pydantic==2.12.5",
            "easydict==1.13",
            "sentencepiece==0.2.0",
        ],
        "install-deps",
    )
    print("ENV_READY", str(py), flush=True)

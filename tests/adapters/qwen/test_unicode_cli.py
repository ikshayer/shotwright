"""Actual CLI Unicode UTF-8 brief and report roundtrip without inference."""
import json
import struct
import subprocess
import sys
import uuid
from pathlib import Path

from orchestrator.platform.containment import ProjectRoot


def test_real_cli_unicode_intent_snapshot_and_json_roundtrip():
    repo = Path(__file__).resolve().parents[3]
    root = ProjectRoot(repo)
    prefix = "_work/qwen_contract_tests/unicode/" + uuid.uuid4().hex
    fmt = struct.pack("<HHIIHH", 1, 1, 48000, 96000, 2, 16)
    pcm = b"\0" * 96000
    wav = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt
    wav += b"data" + struct.pack("<I", len(pcm)) + pcm
    media = prefix + "/source.wav"
    root.write_bytes(media, wav)
    brief = "  Intention: voix lointaine — 静けさ 🎙️\nKeep intentional hiss.  "
    brief_rel = prefix + "/brief.txt"
    root.write_text(brief_rel, brief)
    result = subprocess.run([sys.executable, "-B", "-m", "orchestrator.adapters.qwen", "review",
        "--media", media, "--mode", "audio", "--brief-file", brief_rel, "--dry-run"],
        check=False, cwd=repo, capture_output=True, timeout=45, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    report = json.loads(result.stdout.decode("utf-8"))
    assert report["brief"]["text"] == brief
    disk = json.loads(root.read_text(report["report_path"]))
    assert disk["brief"]["text"] == brief
    assert brief in root.read_text(report["markdown_path"])

"""CPU evidence verifier; measurements never constitute semantic/human approval.

Metadata schema: frame_positions [{frame_index, ball_center_z, ball_radius,
floor_z}], sources [{path, sha256}]. All paths are worktree relative.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.platform.containment import ProjectRoot

CONTACTS = [20, 55, 90, 125, 160, 195]


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, check=True, timeout=360)


def probe(path: Path) -> dict:
    return json.loads(run(['ffprobe', '-v', 'error', '-count_frames',
                           '-show_streams', '-show_format', '-of', 'json', str(path)]).stdout)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return digest.hexdigest()


def video_hash(path: Path) -> str:
    return run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                '-c', 'copy', '-f', 'streamhash', '-hash', 'sha256', '-']).stdout.decode().strip()


def safe_number(value: str | float) -> float | str:
    number = float(value)
    return number if math.isfinite(number) else ('-inf' if number < 0 else '+inf' if number > 0 else 'nan')


def audio_metrics(path: Path) -> dict:
    info = probe(path)
    stream = next(s for s in info['streams'] if s['codec_type'] == 'audio')
    rate, channels = int(stream['sample_rate']), int(stream['channels'])
    raw = run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
               '-f', 'f64le', '-acodec', 'pcm_f64le', '-']).stdout
    samples = array.array('d')
    samples.frombytes(raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples or not all(math.isfinite(v) for v in samples):
        raise ValueError('empty or nonfinite audio')
    if len(samples) % channels:
        raise ValueError('partial interleaved audio frame')
    count = len(samples) // channels
    loudness = run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(path),
                    '-map', '0:a:0', '-af', 'loudnorm=I=-23:TP=-1:LRA=7:print_format=json',
                    '-f', 'null', '-'])
    matches = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', loudness.stderr.decode())
    measured = json.loads(matches[-1])

    def window(center: float) -> dict:
        start = max(0, round((center - .05) * rate))
        end = min(count, round((center + .05) * rate))
        segment = samples[start * channels:end * channels]
        energy = sum(v * v for v in segment) / len(segment) if segment else 0
        return {'center_seconds': center, 'start_sample': start, 'end_sample': end,
                'mean_square': energy, 'rms': math.sqrt(energy)}

    event = [window(i / 25) for i in CONTACTS]
    noncontact = [window((a + b) / 50) for a, b in pairwise(CONTACTS)]
    baseline = sum(w['mean_square'] for w in noncontact) / len(noncontact)
    event_energy = sum(w['mean_square'] for w in event) / len(event)
    return {'codec': stream['codec_name'], 'sample_rate': rate, 'channels': channels,
            'sample_count_per_channel': count, 'duration_seconds': count / rate,
            'dc': sum(samples) / len(samples),
            'rms': math.sqrt(sum(v*v for v in samples) / len(samples)),
            'sample_peak': max(map(abs, samples)),
            'clipped_sample_values': sum(abs(v) >= 1 for v in samples),
            'integrated_lufs': safe_number(measured['input_i']),
            'true_peak_dbfs': safe_number(measured['input_tp']),
            'contact_windows_100ms': event, 'noncontact_windows_100ms': noncontact,
            'contact_to_noncontact_energy_ratio': event_energy / baseline if baseline else None}


def verify(args: argparse.Namespace, root: ProjectRoot) -> dict:
    failures: list[str] = []
    def check(ok: bool, message: str) -> None:
        if not ok:
            failures.append(message)
    video = root.resolve(args.video)
    metadata = json.loads(root.read_bytes(args.metadata))
    info = probe(video)
    streams = info['streams']
    stream = next(s for s in streams if s['codec_type'] == 'video')
    check(not any(s['codec_type'] == 'audio' for s in streams), 'control video contains audio')
    check(stream['codec_name'] == 'h264', 'control video must be H264')
    check(int(stream.get('nb_read_frames', -1)) == 200, 'control video must have 200 decoded frames')
    check(Fraction(stream['avg_frame_rate']) == 25, 'control video must be 25fps')
    check(abs(float(stream['duration']) - 8) < .001, 'control video must be 8 seconds')
    check((int(stream['width']), int(stream['height'])) in [(640,480),(512,384)], 'unexpected control dimensions')
    positions = metadata.get('frame_positions', [])
    by_frame = {int(p['frame_index']): p for p in positions}
    check(set(by_frame) == set(range(200)), 'metadata must record all 200 exact frame positions')
    gaps = {i: float(p['ball_center_z']) - float(p['ball_radius']) - float(p['floor_z']) for i,p in by_frame.items()}
    for i in CONTACTS:
        check(i in gaps and abs(gaps[i]) < 1e-5, f'no recorded ground contact at frame {i}')
    for a,b in pairwise(CONTACTS):
        check(any(gaps.get(i, 0) > .05 for i in range(a+1,b)), f'no airborne arc between {a} and {b}')
    check(all(g >= -1e-5 for g in gaps.values()), 'metadata records floor penetration')
    sources = metadata.get('sources', [])
    check(bool(sources), 'metadata missing pinned source identities')
    source_checks = []
    for item in sources:
        source_path = item.get('path', item.get('media_path'))
        if not isinstance(source_path, str):
            raise ValueError('source record requires path or media_path')  # noqa: TRY004 -- malformed evidence, not API type misuse
        actual = sha(root.resolve(source_path))
        check(actual == item['sha256'], 'source SHA256 mismatch: ' + source_path)
        source_checks.append({'path': source_path, 'sha256': actual})
    result = {'video': {'path': args.video, 'sha256': sha(video), 'probe': info},
              'metadata': {'path': args.metadata, 'sha256': sha(root.resolve(args.metadata)),
                           'source_checks': source_checks, 'contact_indices': CONTACTS},
              'limitations': ['Renderer positions are metadata assertions; visual identity, framing and physical plausibility require independent human inspection.',
                              'Source hashes establish local byte identity, not remote authenticity, asset rights or consent.',
                              'Audio levels and event energy do not establish correct sound semantics or synchronization.',
                              'Raw sample duration is measured without padding; sampled-frame runtime may yield 7.96 seconds.'],
              'human_semantic_approval': None}
    if args.raw_audio:
        audio = root.resolve(args.raw_audio)
        metrics = audio_metrics(audio)
        check(metrics['sample_rate'] == 48000, 'raw audio must be 48kHz')
        check(metrics['codec'] in ('pcm_f32le','pcm_f64le'), 'raw audio must be floating point PCM')
        result['raw_audio'] = {'path': args.raw_audio, 'sha256': sha(audio), **metrics}
    if args.audition:
        audition = root.resolve(args.audition)
        original_hash, audition_hash = video_hash(video), video_hash(audition)
        check(original_hash == audition_hash, 'audition compressed video packets differ')
        check(any(s['codec_type'] == 'audio' for s in probe(audition)['streams']), 'audition has no audio')
        result['audition'] = {'path': args.audition, 'sha256': sha(audition),
                              'original_video_packet_hash': original_hash, 'audition_video_packet_hash': audition_hash}
    result['failures'] = failures
    result['structural_checks_passed'] = not failures
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', required=True)
    parser.add_argument('--metadata', required=True)
    parser.add_argument('--raw-audio')
    parser.add_argument('--audition')
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    root = ProjectRoot(ROOT)
    try:
        result = verify(args, root)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result = {'structural_checks_passed': False, 'failures': [f'{type(exc).__name__}: {exc}'],
                  'human_semantic_approval': None}
    root.write_bytes(args.report, (json.dumps(result, indent=2, allow_nan=False) + '\n').encode())
    print(json.dumps({'report': args.report, 'passed': result['structural_checks_passed'], 'failures': result['failures']}))
    return 0 if result['structural_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

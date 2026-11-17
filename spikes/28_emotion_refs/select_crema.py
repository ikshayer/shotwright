"""Pick CREMA-D actors whose Fear/Sad clips listeners recognised from audio alone, and join
each actor's strongest clips into one ~14 s IndexTTS-2 emotion reference.

Inputs (from github.com/CheyneyComputerScience/CREMA-D, ODbL 1.0 / DbCL 1.0):
processedResults/summaryTable.csv, VideoDemographics.csv and the chosen AudioWAV clips,
downloaded into _work/crema/. Outputs: _work/refs/<emotion>_<sex>_<actor>.wav.
"""

import collections
import csv
import wave
from pathlib import Path

WORK = Path(__file__).resolve().parent / "_work"
CREMA = WORK / "crema"
REFS = WORK / "refs"


def rank(rows: list[dict], sex: dict[str, str], emotion: str, vote: str) -> None:
    """Print the top actors per sex: recognised clip count, mean audio-only intensity."""
    score: dict[str, list] = collections.defaultdict(lambda: [0, 0.0])
    for row in rows:
        actor, _sentence, emo, _level = row["FileName"].split("_")
        if emo == emotion and row["VoiceVote"] == vote:
            score[actor][0] += 1
            score[actor][1] += float(row["VoiceLevel"] or 0)
    for gender in ("Male", "Female"):
        best = sorted(((n, total / max(n, 1), actor) for actor, (n, total) in score.items()
                       if sex[actor] == gender), reverse=True)[:3]
        print(emotion, gender, [(actor, n, round(level)) for n, level, actor in best])


def join(actor: str, emotion: str, vote: str, rows: list[dict], label: str,
         limit_seconds: float = 14.5, gap_seconds: float = 0.35) -> None:
    """Concatenate an actor's recognised clips, most intense first, up to `limit_seconds`."""
    chosen = sorted((r for r in rows if r["FileName"].startswith(actor + "_")
                     and r["FileName"].split("_")[2] == emotion and r["VoiceVote"] == vote),
                    key=lambda r: -float(r["VoiceLevel"] or 0))
    frames, used, params = b"", [], None
    for row in chosen:
        with wave.open(str(CREMA / "clips" / f"{row['FileName']}.wav")) as clip:
            params = params or clip.getparams()
            data = clip.readframes(clip.getnframes())
        gap = b"\x00\x00" * int(gap_seconds * params.framerate)
        if used and (len(frames) + len(data)) / 2 / params.framerate > limit_seconds:
            break
        frames += (gap if frames else b"") + data
        used.append(row["FileName"])
    REFS.mkdir(parents=True, exist_ok=True)
    with wave.open(str(REFS / f"{label}.wav"), "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    print(f"{label}: {len(frames) / 2 / params.framerate:.1f} s from {', '.join(used)}")


if __name__ == "__main__":
    demographics = list(csv.DictReader(open(CREMA / "VideoDemographics.csv")))
    sex = {r["ActorID"]: r["Sex"] for r in demographics}
    rows = list(csv.DictReader(open(CREMA / "summaryTable.csv")))
    rank(rows, sex, "FEA", "F")
    rank(rows, sex, "SAD", "S")
    for actor, emotion, vote, label in (("1036", "FEA", "F", "fear_male_1036"),
                                        ("1029", "FEA", "F", "fear_female_1029"),
                                        ("1059", "SAD", "S", "sad_male_1059"),
                                        ("1028", "SAD", "S", "sad_female_1028")):
        join(actor, emotion, vote, rows, label)

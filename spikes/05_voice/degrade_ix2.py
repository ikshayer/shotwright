import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_spike as rs
profiles = {p["profile_id"]: p for p in
            json.load(open(os.path.join(rs.HERE, "audio_profiles.json")))["profiles"]}
for src_name, pid in [("ix2_performance_edit","dvr-mic-2005"),("ix2_performance_edit","phone-call")]:
    src = os.path.join(rs.WORK, src_name + ".wav")
    prof = profiles[pid]; graph, extra = rs.build_chain(prof, src_name)
    dst = os.path.join(rs.WORK, "%s__%s.wav" % (src_name, pid)); enc = prof["encode"]
    rs.sh(["ffmpeg","-hide_banner","-loglevel","error","-y","-fflags","+bitexact","-i",src]
          + extra + ["-filter_complex",graph,"-map","[out]","-c:a",enc["codec"],
                     "-ar",str(enc["rate"]),"-ac","1","-flags","+bitexact",dst])
    print("%-26s -> %-13s LUFS %.1f" % (src_name, pid, rs.measure(dst).get("lufs",0)))

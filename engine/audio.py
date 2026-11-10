"""Voice chain + two-pass linear loudness, so the level is corrected without pumping."""
import json
import re

from common import run


def chain(a):
    return (
        f"highpass=f={a['highpass_hz']},"
        f"afftdn=nr={a['denoise_nr']}:nf={a['denoise_floor']},"
        f"equalizer=f=250:t=q:w=1.2:g={a['mud_db']},"
        f"equalizer=f=3200:t=q:w=1.5:g={a['presence_db']},"
        f"deesser=i={a['deess']},"
        f"acompressor=threshold={a['comp_threshold']}dB:ratio={a['comp_ratio']}:attack=20:release=200"
    )


def measure(source, a, trims=""):
    """First loudnorm pass; nothing is written to disk."""
    target = f"loudnorm=I={a['lufs']}:TP={a['true_peak']}:LRA={a['lra']}:print_format=json"
    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", source, "-vn",
              "-af", f"{trims}{chain(a)},{target}", "-f", "null", "-").stderr
    return json.loads(err[err.rindex("{"):err.rindex("}") + 1])


def final_filter(a, m):
    return (
        f"{chain(a)},loudnorm=I={a['lufs']}:TP={a['true_peak']}:LRA={a['lra']}:linear=true:"
        f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
        f"measured_thresh={m['input_thresh']}:offset={m['target_offset']},aresample=48000"
    )


def verify_linear(source, a, m):
    """loudnorm silently falls back to dynamic if the gain would break the peak target."""
    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", source, "-vn",
              "-af", final_filter(a, m).replace(":linear=true:", ":linear=true:print_format=json:"), "-f", "null", "-").stderr
    return json.loads(err[err.rindex("{"):err.rindex("}") + 1])["normalization_type"]


def clipping_report(source):
    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", source, "-vn", "-af", "astats=metadata=0:measure_overall=Peak_level+Number_of_samples:measure_perchannel=none",
              "-f", "null", "-").stderr
    peak = re.search(r"Peak level dB: ([-\d.]+|-?inf)", err)
    return {"peak_db": peak.group(1) if peak else "unknown"}

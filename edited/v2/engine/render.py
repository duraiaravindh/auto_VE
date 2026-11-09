"""Filter graph and encodes. final = clean; review = plus timecode, shot data and safe zones."""
import os

from common import run

FONT = "edited/v2/fonts/Poppins-Bold.ttf"


def zoom_scale(amount, direction, start, length):
    """Eased zoom across a shot, then cropped back to frame. Assumes t counts from start."""
    ease = f"(0.5-0.5*cos(PI*min((t-{start:.6f})/{length:.6f},1)))"
    z = f"(1+{amount:.4f}*{ease})" if direction == "in" else f"(1+{amount:.4f}*(1-{ease}))"
    return f"scale=w='2*trunc(540*{z})':h='2*trunc(960*{z})':eval=frame:flags=lanczos,crop=1080:1920"


def graph(shots, ass_path, audio_filter, fps, review=False, exact=False, pre="", shot_filter=None, mask=None, broll=None, hflip=False, bed=None, source_filter=""):
    parts, labels = [], ""
    for i, s in enumerate(shots):
        w, h, x, y = s["crop"]
        crop = f"{'hflip,' if hflip else ''}crop={w}:{h}:{x}:{y},"
        note = ""
        if review:
            tag = f"S{i + 1:02d}  {round(s['zoom'] * 100)}pct  src {s['in']:.2f}"
            note = f",drawtext=fontfile={FONT}:text='{tag}':x=40:y=60:fontsize=40:fontcolor=white:box=1:boxcolor=black@0.6"
        # exact: cut on frame numbers, so rounding a time can never add or drop a frame
        cut = (f"trim=start_frame={round(s['in'] * fps)}:end_frame={round(s['out'] * fps)}" if exact
               else f"trim={s['in']:.4f}:{s['out']:.4f}")
        extra = shot_filter(s) if shot_filter else ""
        parts.append(f"[0:v]{cut},setpts=PTS-STARTPTS,{crop}scale=1080:1920:flags=lanczos,setsar=1{("," + source_filter) if source_filter else ""}{extra}{note}[v{i}];")
        length = s["out"] - s["in"]
        # 8 ms edge fades prevent clicks at cuts; every cut already sits in a pause
        a_in, a_out = (f"{s['in']:.6f}", f"{s['out']:.6f}") if exact else (f"{s['in']:.4f}", f"{s['out']:.4f}")
        parts.append(f"[0:a]atrim={a_in}:{a_out},asetpts=PTS-STARTPTS,"
                     f"afade=t=in:d=0.008,afade=t=out:st={length - 0.008:.4f}:d=0.008[a{i}];")
        labels += f"[v{i}][a{i}]"
    guides = ""
    if review:
        # Instagram UI zones: top 220, bottom 480, right 120
        guides = (",drawbox=y=220:w=1080:h=2:color=red@0.7:t=fill,drawbox=y=1440:w=1080:h=2:color=red@0.7:t=fill,"
                  "drawbox=x=960:y=0:w=2:h=1920:color=red@0.7:t=fill,"
                  f"drawtext=fontfile={FONT}:text='%{{pts\\:hms}}':x=40:y=1840:fontsize=44:fontcolor=yellow:box=1:boxcolor=black@0.6")
    fontsdir = "edited/v2/fonts"
    picture = f"[vc]unsharp=5:5:0.35,fps={fps}"
    if mask:
        picture += f"[vb];movie={mask},format=rgba[mk];[vb][mk]overlay=eof_action=repeat"
    # cutaways lie over the speaker picture only: video from the clip, never its audio. The grade and
    # captions come after, so length and voice cannot change and every clip gets the same look.
    for i, b in enumerate(broll or ()):
        d = b["end"] - b["start"]
        size = (zoom_scale(b["zoom_amount"], b["zoom"], b["start"], d) if b.get("zoom") else "scale=1080:1920:flags=lanczos")
        picture += (f"[vm{i}];movie={b['file']},trim=start={b['clip_start']}:duration={d:.6f},"
                    f"setpts=PTS-STARTPTS+{b['start']:.6f}/TB,fps={fps},{size},setsar=1,format=yuva420p"
                    # two clips that touch cut straight to each other, so the speaker never blinks through
                    + ("" if b.get("cut_in") else f",fade=t=in:st={b['start']:.6f}:d={b['fade']}:alpha=1")
                    + ("" if b.get("cut_out") else f",fade=t=out:st={b['end'] - b['fade']:.6f}:d={b['fade']}:alpha=1")
                    + f"[bc{i}];"
                    f"[vm{i}][bc{i}]overlay=eof_action=pass")
    sound = f"[ac]{audio_filter}[a]"
    if bed:
        # the voice chain is unchanged; the limiter only catches peaks where the bed adds to the voice
        sound = (f"[ac]{audio_filter},aformat=channel_layouts=stereo[vo];"
                 f"amovie={bed},aformat=sample_rates=48000:channel_layouts=stereo[bd];"
                 f"[vo][bd]amix=inputs=2:duration=first:normalize=0,"
                 f"alimiter=limit=0.87:attack=2:release=60:level=false:latency=true[a]")
    return "".join(parts) + (
        f"{labels}concat=n={len(shots)}:v=1:a=1[vc][ac];"
        f"{picture},{pre}ass={ass_path}:fontsdir={fontsdir}{guides},format=yuv420p[v];{sound}"
    )


def encode(source, graph_text, graph_path, out, crf="17", preset="slow", maxrate=None):
    with open(graph_path, "w", encoding="utf-8") as f:
        f.write(graph_text)
    if os.path.exists(out):
        raise SystemExit(f"Refusing to overwrite {out}; bump the version in edit.json")
    run("ffmpeg", "-v", "error", "-n", "-i", source, "-/filter_complex", graph_path, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", preset, "-crf", crf, "-profile:v", "high",
        *(("-maxrate", maxrate, "-bufsize", f"{2 * int(maxrate.rstrip('M'))}M") if maxrate else ()), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", out)


def validate_graph(source, graph_text, graph_path):
    """Runs 2 s through the graph to a null sink. Proves it parses; writes no video."""
    with open(graph_path, "w", encoding="utf-8") as f:
        f.write(graph_text)
    run("ffmpeg", "-v", "error", "-i", source, "-/filter_complex", graph_path, "-map", "[v]", "-map", "[a]",
        "-t", "2", "-f", "null", "-")


def preview(source, graph_text, graph_path, out, start, seconds):
    """A short clip cut from the finished graph, so timing matches the real edit."""
    with open(graph_path, "w", encoding="utf-8") as f:
        f.write(graph_text)
    run("ffmpeg", "-v", "error", "-y", "-i", source, "-/filter_complex", graph_path, "-map", "[v]", "-map", "[a]",
        "-ss", str(start), "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out)


def measure_length(source, graph_text, graph_path):
    """Runs the whole graph to a null sink and returns (frames, seconds). Writes no video."""
    with open(graph_path, "w", encoding="utf-8") as f:
        f.write(graph_text)
    out = run("ffmpeg", "-v", "error", "-nostats", "-i", source, "-/filter_complex", graph_path, "-map", "[v]", "-map", "[a]",
              "-progress", "pipe:1", "-f", "null", "-").stdout
    frames = [int(x.split("=")[1]) for x in out.splitlines() if x.startswith("frame=")][-1]
    return frames, frames / 30

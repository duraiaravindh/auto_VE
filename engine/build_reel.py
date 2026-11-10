"""Reproducible Namma Kadhai reel edit: python edited/project/build_reel.py
Edit CUES (captions, framing) or the constants below and re-run. raw/ is never written to."""
import glob, json, os, re, subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(ROOT)
RAW = glob.glob("raw/*.mp4")[0]
OUT = "edited/namma-kadhai-reel.mp4"
ASS = "edited/project/captions.ass"
FONT_DIR = "code/fonts"

PAD = 0.08            # room kept each side of a removed pause
EMOTIONAL = (40.8, 56.3)  # reveal -> payoff: pauses stay untouched
MIN_CUT = 0.15        # shortest pause worth removing
MIN_SEG = 0.5         # shortest shot allowed between cuts
TAIL = 0.35           # hold after the last word
HIGHLIGHT = "&H0019C8FF&"  # warm yellow (ASS is BGR)

# (start, end, zoom, caption chunks). "|" = next caption, *x* = highlighted.
CUES = [
 (0.00, 1.65, 1.08, "Namma ellaar life-layume|oru person iruppaanganga."),
 (2.07, 5.06, 1.08, "Namma face-ah paathey|namma happy-ah irukoma|sad-ah irukomannu kandupidichiruvaanga."),
 (5.31, 7.58, 1.08, "Namakku edhaavadhu oru problem-na|first aala vandhu nippaanga."),
 (8.03, 9.37, 1.00, "Nammala thanga thattula thaanguvaanga."),
 (9.73, 13.05, 1.00, "Adhuve *avangalukku edhaavadhu problem-nu* vaingalen,|vaaye thorakka maattaanga."),
 (13.70, 16.36, 1.10, "Indha maadhiri person ungaloda irukkaangala?|Illa neenga ippadi yaaraiyaavadhu notice pannirukkeengala?"),
 (16.69, 18.27, 1.10, "Naan recent-aa en friend-ah poi meet panneanga."),
 (18.88, 20.00, 1.00, "Avangalum naan sonna maadhiridhaan."),
 (20.55, 23.98, 1.00, "Eppavume smile pannikitte|suthi irukkavangala sirikka vachukittu,|edhaavadhu onnu help panna"),
 (24.50, 26.55, 1.16, "*200% effort* pottu|ellaame pannuvaanga."),
 (27.21, 28.41, 1.06, "Namma face sari illa-nu vaingalen,"),
 (28.97, 31.10, 1.00, "avanga adha paathuttu normal aagura varaikkum|koodaye iruppaanga."),
 (31.55, 34.91, 1.10, "Adhuve avangalukku edhaavadhu onnu-na|avanga vaaye thorakka maattaanga."),
 (35.22, 37.63, 1.00, "Suddenly oru naal meet pannalaama-nu kettaanga,|naanum kelambi ponen."),
 (38.33, 40.58, 1.00, "Konja neram pesikitte irundhaanga.|Appo dhaan purinjadhu,"),
 (41.00, 44.46, 1.10, "eppavume sirichuttu irukkravanga thaniyaa|avanga manasukulla|evvalavu vishayatha handle panraanga-nu."),
 (45.21, 47.84, 1.10, "Naan onnum perusa pesave illa.|*Just* avangaloda *ukkaandhudhaan irundhen.*"),
 (48.15, 51.89, 1.10, "Andha chinna meet avangalukku|evvalavu periya important-aa irundhirukkum-ndradhu|enakku appadhaan purinjadhu."),
 (52.13, 56.08, 1.16, "Eppavume ellaarukkum support-aa|irukkura en friend-ukku|annaikku oru naal *naan support-aa irundhen.*"),
 (56.37, 58.20, 1.00, "Namma life-la ippadi kandippa|oruthanga iruppaanganga."),
 (58.93, 60.55, 1.00, "Namma kashtathula namma koodaye nippaanga."),
 (60.87, 63.41, 1.00, "Nammaloda happiness-la nammala vida|avanga romba sandhoshappaduvaanga."),
 (63.61, 64.41, 1.14, "Aana oru question:"),
 (64.55, 68.96, 1.14, "Namakku oru kashtam-na|namma kooda nikkura sila perukku|avanga struggle varum bodhu|*naama kooda irundhurukoma?*"),
 (69.31, 72.71, 1.06, "Ungalukku ippadi yaaravadhu kooda irundhaa|avangalukku *call pannunga,*|illa poi meet pannunga."),
 (72.94, 76.27, 1.14, "Perusa edhuvum panna vendaam.|*Just kooda irunga.*"),
 (76.93, 80.31, 1.00, "Avangalukkum kashtam-na|yaaravadhu oruthanga kooda irukkaangandra|andha confidence-ah kodunga."),
 (80.77, 84.89, 1.06, "Eppavume mathavangalukku|endha oru expectation-um illaama|anbum paasathaiyum kuduthutte|irukkura orutharukku,"),
 (85.55, 88.20, 1.06, "sudden-aa adha receive pannum bodhu|andha sandhoshathukku alavey illa."),
 (88.98, 92.68, 1.00, "Indha maadhiri yaaravadhu unga life-la irundhurukkaangala?|Illa neengale indha situation-la irundhurukkeengala?"),
 (92.99, 96.70, 1.06, "Indha story ungalukku|yaaraiyaavadhu remember pannirundhaa|adha avangalukku share pannunga."),
 (97.28, 98.81, 1.00, "Naan Durai.|Idhu Namma Kadhai."),
]


def run(*a, **k):
    return subprocess.run(a, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", **k)


def source_duration():
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", RAW).stdout)


def pauses():
    err = run("ffmpeg", "-hide_banner", "-nostats", "-i", RAW, "-af", "silencedetect=noise=-30dB:d=0.18", "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return list(zip(starts, ends))


def zoom_at(t):
    z = CUES[0][2]
    for i, c in enumerate(CUES):
        # framing switches midway through the gap before each cue
        boundary = 0 if i == 0 else (CUES[i - 1][1] + c[0]) / 2
        if t >= boundary:
            z = c[2]
    return z


def build_segments(duration):
    end_of_edit = min(CUES[-1][1] + TAIL, duration)
    keep, pos = [], 0.0
    for s, e in pauses():
        if e >= end_of_edit:  # trailing pause is handled by TAIL
            break
        if EMOTIONAL[0] <= s <= EMOTIONAL[1]:
            continue
        if s <= 0.01:  # dead air before the first word
            pos = max(0.0, e - 0.03)
            continue
        # skip cuts too small to help (they only add jump-cut fatigue) or too close to the last one
        if e - s - 2 * PAD >= MIN_CUT and s + PAD - pos >= MIN_SEG:
            keep.append((pos, s + PAD))
            pos = e - PAD
    keep.append((pos, end_of_edit))
    # split kept ranges wherever the framing changes
    bounds = [(CUES[i - 1][1] + CUES[i][0]) / 2 for i in range(1, len(CUES)) if CUES[i][2] != CUES[i - 1][2]]
    segs = []
    for a, b in keep:
        cuts = [a] + [x for x in bounds if a + 0.02 < x < b - 0.02] + [b]
        segs += [(x, y, zoom_at((x + y) / 2)) for x, y in zip(cuts, cuts[1:])]
    return segs


def remap(t, segs):
    """source time -> output time (snaps forward out of removed pauses)"""
    out = 0.0
    for a, b, _ in segs:
        if t < a:
            return out
        if t <= b:
            return out + t - a
        out += b - a
    return out


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_captions(segs):
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Alignment 8 + MarginV puts the block's top edge below the chin and above Instagram's bottom UI
        "Style: Cap,Poppins,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,8,90,120,1215,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines, srt, n = [], [], 0
    for start, end, _, text in CUES:
        chunks = text.split("|")
        weight = [len(c.replace("*", "")) for c in chunks]
        t = start
        for chunk, w in zip(chunks, weight):
            next_t = t + (end - start) * w / sum(weight)
            a, b = remap(t, segs), remap(next_t, segs)
            parts = chunk.split("*")
            styled = "".join(f"{{\\c{HIGHLIGHT}}}{p}{{\\c&H00FFFFFF&}}" if i % 2 else p for i, p in enumerate(parts))
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{styled}")
            n += 1
            srt.append(f"{n}\n{ass_time(a)} --> {ass_time(b)}\n{chunk.replace('*', '')}\n")
            t = next_t
    with open(ASS, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")


def render(segs):
    parts, labels = [], ""
    for i, (a, b, z) in enumerate(segs):
        # crop is anchored on the face line (42% down) so the eyes stay put across punch-ins
        crop = f"crop=iw/{z}:ih/{z}:(iw-iw/{z})/2:(ih-ih/{z})*0.42,"
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,{crop}scale=1080:1920:flags=lanczos,setsar=1[v{i}];")
        fade = min(0.008, (b - a) / 2)
        parts.append(
            f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={fade},"
            f"afade=t=out:st={b - a - fade:.3f}:d={fade}[a{i}];"
        )
        labels += f"[v{i}][a{i}]"
    voice = ("highpass=f=80,afftdn=nr=8:nf=-40,equalizer=f=250:t=q:w=1.2:g=-1.5,equalizer=f=3200:t=q:w=1.5:g=1.5,"
             "acompressor=threshold=-20dB:ratio=2:attack=15:release=180,loudnorm=I=-14:TP=-1.5:LRA=7,aresample=48000")
    graph = "".join(parts) + (
        f"{labels}concat=n={len(segs)}:v=1:a=1[vc][ac];"
        f"[vc]unsharp=5:5:0.45,fps=30,ass=edited/project/captions.ass:fontsdir={FONT_DIR},format=yuv420p[v];[ac]{voice}[a]"
    )
    with open("edited/project/filter_graph.txt", "w", encoding="utf-8") as f:
        f.write(graph)
    run("ffmpeg", "-v", "error", "-y", "-i", RAW, "-/filter_complex", "edited/project/filter_graph.txt",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", OUT)


if __name__ == "__main__":
    segs = build_segments(source_duration())
    write_captions(segs)
    with open("edited/project/timeline.json", "w", encoding="utf-8") as f:
        json.dump([{"source_in": round(a, 3), "source_out": round(b, 3), "zoom": z} for a, b, z in segs], f, indent=1)
    render(segs)
    print(f"{len(segs)} segments, {sum(b - a for a, b, _ in segs):.2f}s -> {OUT}")

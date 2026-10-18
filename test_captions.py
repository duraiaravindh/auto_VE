import json
import captions as c


def W(t, s=0.0, e=0.5, hl=False):
    return c.Word(t, s, e, hl)


def test_ass_time_and_colors():
    assert c.ass_time(0) == "0:00:00.00"
    assert c.ass_time(3661.5) == "1:01:01.50"
    assert c.ass_style_color("F5E8D0") == "&H00D0E8F5"      # BGR order
    assert c.ass_inline_color("F3C96B") == "&H6BC9F3&"


def test_tokenize_highlights():
    assert c._tokenize("a *b c* d") == [("a", False), ("b", True), ("c", True), ("d", False)]
    assert c._tokenize("*festival*") == [("festival", True)]


def test_line_break_balanced_no_orphan():
    ws = [W(x) for x in "Chinna vayasula festival vandhaale,".split()]
    lines = c.break_lines(ws, 32)
    assert [" ".join(w.text for w in l) for l in lines] == ["Chinna vayasula", "festival vandhaale,"]


def test_short_text_one_line():
    assert len(c.break_lines([W("hello"), W("da")], 32)) == 1


def test_never_exceeds_limits():
    prof = c.make_profile("landscape", 1920, 1080)
    segs = {"segments": [{"start": 0, "end": 20, "text": " ".join(["vandhaale"] * 30)}]}
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "t.json"
    p.write_text(json.dumps(segs))
    events = c.build_events(c.make_chunks(c.load_transcript(p), prof), prof)
    for ev in events:
        assert len(ev.lines) <= 2
        assert all(c._len(l) <= prof.max_chars for l in ev.lines)
    for a, b in zip(events, events[1:]):
        assert a.end <= b.start + 1e-6                     # no overlap


def test_comma_and_sentence_split():
    prof = c.make_profile("landscape", 1920, 1080)
    ws = [W(t, i * 0.4, i * 0.4 + 0.35) for i, t in
          enumerate("Chinna vayasula festival vandhaale, namakku oru thani excitement!".split())]
    ch = c.make_chunks(ws, prof)
    assert len(ch) == 2 and ch[0][-1].text == "vandhaale,"


def test_edit_keeps_timings_when_word_count_matches():
    seg = {"text": "Hello *world*", "start": 0, "end": 2,
           "words": [{"word": "Helo", "start": 0.1, "end": 0.5}, {"word": "wrld", "start": 1.0, "end": 1.4}]}
    ws = c._segment_words(seg)
    assert [(w.text, w.start, w.hl) for w in ws] == [("Hello", 0.1, False), ("world", 1.0, True)]


def test_srt_loads(tmp_path):
    f = tmp_path / "a.srt"
    f.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello *there* friend\n", encoding="utf-8")
    ws = c.load_transcript(f)
    assert len(ws) == 3 and ws[1].hl and abs(ws[0].start - 1.0) < 1e-6 and abs(ws[-1].end - 3.0) < 1e-6


def test_vertical_profile_scales_with_frame():
    v = c.make_profile("vertical", 1080, 1920)
    l = c.make_profile("landscape", 848, 478)
    assert v.font_px > 60 and 25 <= l.font_px <= 35 and v.max_chars < l.max_chars


def test_ass_contains_highlight_and_playres():
    prof = c.make_profile("landscape", 848, 478)
    ev = [c.Event(1, 3, [[W("festival", hl=True), W("time")]])]
    ass = c.build_ass(ev, prof)
    assert "PlayResX: 848" in ass and "{\\c&H6BC9F3&}festival" in ass


def test_report_flags_words_with_implausibly_long_timings(capsys):
    prof = c.make_profile("landscape", 1920, 1080)
    ev = [c.Event(10.44, 35.72, [[W("21", 10.44, 35.72)]])]

    c.print_report(ev, prof)

    output = capsys.readouterr().out
    assert "word timing >2.5s" in output
    assert "'21' (25.28s)" in output
    assert "1 warning(s)" in output


def test_console_encoding_uses_utf8(monkeypatch):
    class FakeStream:
        def __init__(self):
            self.encoding_options = None

        def reconfigure(self, **kwargs):
            self.encoding_options = kwargs

    stdout = FakeStream()
    stderr = FakeStream()
    monkeypatch.setattr(c.sys, "stdout", stdout)
    monkeypatch.setattr(c.sys, "stderr", stderr)

    c.configure_console_encoding()

    assert stdout.encoding_options == {"encoding": "utf-8", "errors": "replace"}
    assert stderr.encoding_options == {"encoding": "utf-8", "errors": "replace"}

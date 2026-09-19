# -*- coding: utf-8 -*-
"""audio_qc 测试：音频侧 QC（静音/削波/响度）+ 语音判定（silero ‖ ffmpeg 回退）。

运行：python -m unittest discover tests          （skill 根目录，快测试全跑）
     SLOW=1 python -m unittest discover tests   （含真跑 ffmpeg 的慢测试）

设计：解析与判定全是**纯函数**（测试不碰 ffmpeg/模型）；模型推理与 ffmpeg
调用是薄 IO 壳，用假后端注入验证"选了哪条路"。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_slow = not os.environ.get("SLOW")


def _aq():
    import importlib
    return importlib.import_module("audio_qc")


# 真实 ffmpeg 7.1 输出样本（含"末尾静音无 end"的边界）
_SILENCEDETECT = """\
[silencedetect @ 0x55d1f0] silence_start: 1.20
[silencedetect @ 0x55d1f0] silence_end: 3.40 | silence_duration: 2.20
[silencedetect @ 0x55d1f0] silence_start: 8.00
"""
_VOLUMEDETECT = """\
[Parsed_volumedetect_0 @ 0x55d200] n_samples: 441000
[Parsed_volumedetect_0 @ 0x55d200] mean_volume: -23.4 dB
[Parsed_volumedetect_0 @ 0x55d200] max_volume: -0.2 dB
[Parsed_volumedetect_0 @ 0x55d200] histogram_0db: 10
"""
# 关键陷阱：Channel 段与 Overall 段的 Number of clips 不同 → 必须取 Overall
_ASTATS = """\
[Parsed_astats_0 @ 0x55d300] Channel: 1
[Parsed_astats_0 @ 0x55d300] Peak level dB: -1.000000
[Parsed_astats_0 @ 0x55d300] RMS level dB: -18.200000
[Parsed_astats_0 @ 0x55d300] Number of clips: 0
[Parsed_astats_0 @ 0x55d300] Channel: 2
[Parsed_astats_0 @ 0x55d300] Peak level dB: -2.000000
[Parsed_astats_0 @ 0x55d300] RMS level dB: -19.000000
[Parsed_astats_0 @ 0x55d300] Number of clips: 7
[Parsed_astats_0 @ 0x55d300] Overall
[Parsed_astats_0 @ 0x55d300] Peak level dB: -1.000000
[Parsed_astats_0 @ 0x55d300] RMS level dB: -18.500000
[Parsed_astats_0 @ 0x55d300] Flat factor: 0.000000
[Parsed_astats_0 @ 0x55d300] Number of clips: 2
[Parsed_astats_0 @ 0x55d300] Noise floor dB: -60.000000
"""


class TestParseSilencedetect(unittest.TestCase):
    """silencedetect 解析：配对 start/end；末尾只有 start = 静音到片尾。"""

    def test_pairs_and_open_tail(self):
        segs = _aq().parse_silencedetect(_SILENCEDETECT)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0]["start"], 1.20, places=2)
        self.assertAlmostEqual(segs[0]["end"], 3.40, places=2)
        self.assertAlmostEqual(segs[1]["start"], 8.00, places=2)
        self.assertIsNone(segs[1]["end"], "末尾静音没有 silence_end，必须给 None 而不是丢弃")

    def test_empty_input(self):
        self.assertEqual(_aq().parse_silencedetect(""), [])


class TestParseVolumedetect(unittest.TestCase):
    def test_mean_and_max(self):
        v = _aq().parse_volumedetect(_VOLUMEDETECT)
        self.assertAlmostEqual(v["mean_db"], -23.4, places=1)
        self.assertAlmostEqual(v["max_db"], -0.2, places=1)

    def test_missing_returns_none(self):
        v = _aq().parse_volumedetect("no stats here")
        self.assertIsNone(v["mean_db"])
        self.assertIsNone(v["max_db"])


class TestParseAstats(unittest.TestCase):
    """astats 必须取 Overall 段——Channel 段的同名键是逐声道值，取错会误判削波。"""

    def test_takes_overall_not_channel(self):
        st = _aq().parse_astats(_ASTATS)
        self.assertEqual(st["clips"], 2, "必须取 Overall 的 Number of clips(=2)，不是 Channel 2 的 7")
        self.assertAlmostEqual(st["peak_db"], -1.0, places=2)
        self.assertAlmostEqual(st["rms_db"], -18.5, places=2)
        self.assertAlmostEqual(st["flat_factor"], 0.0, places=2)

    def test_no_overall_section_returns_empty(self):
        self.assertEqual(_aq().parse_astats("garbage"), {})


class TestQcVerdict(unittest.TestCase):
    """判定是纯函数：给定 (时长, 静音段, 响度, astats) 产出 (级别, 说明) 列表。"""

    def _seg(self, start, end):
        return {"start": start, "end": end, "dur": end - start}

    def test_clean_is_empty(self):
        v = _aq().qc_verdict(10.0, [], {"mean_db": -20.0, "max_db": -3.0},
                             {"clips": 0, "peak_db": -3.0})
        self.assertEqual(v, [], "正常音轨不应产生任何判定")

    def test_mostly_silent_is_fail(self):
        v = _aq().qc_verdict(10.0, [self._seg(0, 9.0)], {"mean_db": -50.0, "max_db": -40.0},
                             {"clips": 0, "peak_db": -40.0})
        levels = [x[0] for x in v]
        self.assertIn("FAIL", levels, "≈90% 静音必须 FAIL（音轨没接上/录制失败）")

    def test_clipping_warn_then_fail(self):
        v = _aq().qc_verdict(10.0, [], {"mean_db": -12.0, "max_db": -0.1},
                             {"clips": 3, "peak_db": -0.1})
        self.assertIn("WARN", [x[0] for x in v], "少量削波 → WARN")
        v2 = _aq().qc_verdict(10.0, [], {"mean_db": -12.0, "max_db": 0.0},
                              {"clips": 200, "peak_db": 0.0})
        self.assertIn("FAIL", [x[0] for x in v2], "严重削波（大量采样触顶）→ FAIL")

    def test_too_quiet_and_too_loud_warn(self):
        quiet = _aq().qc_verdict(10.0, [], {"mean_db": -60.0, "max_db": -50.0}, {"clips": 0, "peak_db": -50.0})
        loud = _aq().qc_verdict(10.0, [], {"mean_db": -2.0, "max_db": -0.1}, {"clips": 0, "peak_db": -0.1})
        self.assertIn("WARN", [x[0] for x in quiet], "平均 -60dB 近乎无声 → WARN")
        self.assertIn("WARN", [x[0] for x in loud], "平均 -2dB 过高 → WARN")

    def test_long_single_silence_warn(self):
        v = _aq().qc_verdict(30.0, [self._seg(5.0, 12.0)], {"mean_db": -20.0, "max_db": -3.0},
                             {"clips": 0, "peak_db": -3.0})
        self.assertIn("WARN", [x[0] for x in v], "存在 7s 长静音段 → WARN")

    def test_none_values_do_not_crash(self):
        v = _aq().qc_verdict(None, [], {"mean_db": None, "max_db": None}, {})
        self.assertEqual(v, [], "探测失败（全 None）时不应产出假判定，也不应崩")


class TestSpeechFromSilences(unittest.TestCase):
    """无模型回退路径：用静音段反推语音段。"""

    def test_inverts_silence(self):
        segs, ratio = _aq().speech_from_silences(10.0, [{"start": 2.0, "end": 4.0, "dur": 2.0}])
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(segs[0]["end"], 2.0, places=2)
        self.assertAlmostEqual(segs[1]["start"], 4.0, places=2)
        self.assertAlmostEqual(segs[1]["end"], 10.0, places=2)
        self.assertAlmostEqual(ratio, 0.8, places=2)

    def test_open_tail_silence(self):
        segs, ratio = _aq().speech_from_silences(10.0, [{"start": 6.0, "end": None, "dur": None}])
        self.assertEqual(len(segs), 1, "末尾静音延伸到片尾 → 只剩前面一段语音")
        self.assertAlmostEqual(segs[0]["end"], 6.0, places=2)
        self.assertAlmostEqual(ratio, 0.6, places=2)

    def test_all_silent_no_speech(self):
        segs, ratio = _aq().speech_from_silences(10.0, [{"start": 0.0, "end": None, "dur": None}])
        self.assertEqual(segs, [])
        self.assertAlmostEqual(ratio, 0.0, places=2)


class TestProbsToSegments(unittest.TestCase):
    """silero 概率窗 → 语音段（纯函数，测试不需要模型）。"""

    def test_merge_consecutive_windows(self):
        # 窗长 0.1s；0.2-0.5s 三窗超阈 → 合并为一段
        probs = [0.1, 0.1, 0.9, 0.9, 0.9, 0.1, 0.1]
        segs = _aq().probs_to_segments(probs, 0.1, threshold=0.5, min_speech=0.05)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[0]["start"], 0.2, places=3)
        self.assertAlmostEqual(segs[0]["end"], 0.5, places=3)

    def test_drops_too_short(self):
        probs = [0.0, 0.9, 0.0, 0.0]
        segs = _aq().probs_to_segments(probs, 0.1, threshold=0.5, min_speech=0.25)
        self.assertEqual(segs, [], "仅 0.1s 的疑似语音低于 min_speech，应丢弃（避免噪声误判）")

    def test_two_separate_segments(self):
        probs = [0.9, 0.9, 0.0, 0.0, 0.9, 0.9, 0.9]
        segs = _aq().probs_to_segments(probs, 0.1, threshold=0.5, min_speech=0.05)
        self.assertEqual(len(segs), 2)


class TestSpeechSummary(unittest.TestCase):
    def test_has_speech_threshold(self):
        segs = [{"start": 0.0, "end": 3.0, "dur": 3.0}]
        s = _aq().speech_summary(segs, 10.0)
        self.assertTrue(s["has_speech"])
        self.assertAlmostEqual(s["ratio"], 0.3, places=2)

    def test_tiny_blip_is_not_speech(self):
        segs = [{"start": 0.0, "end": 0.1, "dur": 0.1}]
        self.assertFalse(_aq().speech_summary(segs, 10.0)["has_speech"],
                         "总时长 <0.3s 的碎片不算'有语音'")

    def test_zero_duration_safe(self):
        self.assertFalse(_aq().speech_summary([], 0.0)["has_speech"])


class TestVadBackend(unittest.TestCase):
    """后端选择是纯函数（显式传 models_dir），缺依赖/缺模型时如实回退。"""

    def test_falls_back_when_model_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_aq().vad_backend(td), "ffmpeg",
                             "模型文件不在 → 回退 ffmpeg（不静默失败）")

    def test_uses_silero_when_model_present(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "silero_vad.onnx").write_bytes(b"\x00" * 2048)
            with mock.patch.object(_aq(), "_onnx_available", return_value=True):
                self.assertEqual(_aq().vad_backend(td), "silero")

    def test_falls_back_when_runtime_missing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "silero_vad.onnx").write_bytes(b"\x00" * 2048)
            with mock.patch.object(_aq(), "_onnx_available", return_value=False):
                self.assertEqual(_aq().vad_backend(td), "ffmpeg",
                                 "有模型但没 onnxruntime → 也必须回退，而不是崩")


class TestDetectSpeechBackendSelection(unittest.TestCase):
    """薄 IO 壳：验证"走了哪条后端"（不真跑模型）。"""

    def test_ffmpeg_fallback_path_used(self):
        aq = _aq()
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "c.mp4"
            fake.write_bytes(b"x")
            with mock.patch.object(aq, "vad_backend", return_value="ffmpeg"), \
                 mock.patch.object(aq, "_run_silencedetect",
                                   return_value=[{"start": 2.0, "end": 4.0, "dur": 2.0}]), \
                 mock.patch.object(aq, "probe_duration", return_value=10.0):
                r = aq.detect_speech(str(fake), models_dir=td)
        self.assertEqual(r["backend"], "ffmpeg")
        self.assertTrue(r["has_speech"])
        self.assertAlmostEqual(r["ratio"], 0.8, places=2)

    def test_json_shape_is_stable(self):
        aq = _aq()
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "c.mp4"
            fake.write_bytes(b"x")
            with mock.patch.object(aq, "vad_backend", return_value="ffmpeg"), \
                 mock.patch.object(aq, "_run_silencedetect", return_value=[]), \
                 mock.patch.object(aq, "probe_duration", return_value=5.0):
                r = aq.detect_speech(str(fake), models_dir=td)
        self.assertEqual(set(r), {"has_speech", "ratio", "total", "segments", "backend", "duration"})


class TestTriageVadPrefilter(unittest.TestCase):
    """VAD 前置：无语音时直接判 yes，**省掉一次远端 ASR 调用**。"""

    def _at(self):
        import importlib
        return importlib.import_module("audio_triage")

    def test_no_speech_short_circuits_without_remote_asr(self):
        d = self._at().triage_decision(True, "", speech={"has_speech": False, "ratio": 0.0})
        self.assertEqual(d["verdict"], "yes", "本地 VAD 判无语音（纯音乐/环境音）→ 直接建议补旁白")
        self.assertIn("VAD", d["reason"], "理由要说明是本地 VAD 判的，便于排查")

    def test_speech_present_keeps_old_behavior(self):
        d = self._at().triage_decision(True, "这是中文人声", speech={"has_speech": True, "ratio": 0.6})
        self.assertEqual(d["verdict"], "no", "有语音时行为不变，仍按转录文本判")

    def test_speech_none_is_backward_compatible(self):
        old = self._at().triage_decision(True, "这是中文人声")
        self.assertEqual(old["verdict"], "no", "不传 speech 时与旧版完全一致")

    def test_no_audio_still_wins(self):
        d = self._at().triage_decision(False, "", speech={"has_speech": False, "ratio": 0.0})
        self.assertEqual(d["verdict"], "yes")
        self.assertIn("无音轨", d["reason"], "无音轨的判断优先于 VAD")


class TestTriageOneSkipsRemoteWhenNoSpeech(unittest.TestCase):
    """集成：本地 VAD 判"无语音"时，triage_one **不得调用远端转写**。

    这是本轮最实打实的收益（省一次 ASR 额度）。没有这条测试，删掉前置判断
    也不会有人发现——功能悄悄退回"每次都花额度"。
    """

    def test_remote_transcribe_not_called(self):
        import importlib
        at = importlib.import_module("audio_triage")
        called = {"n": 0}

        def fake_transcribe(*a, **k):
            called["n"] += 1
            return "不该被调用"

        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "c.mp4"
            clip.write_bytes(b"x")
            with mock.patch.object(at, "probe",
                                   return_value={"audio": True, "duration": 10.0}), \
                 mock.patch.object(at, "_local_vad",
                                   return_value={"has_speech": False, "ratio": 0.0,
                                                 "backend": "ffmpeg"}), \
                 mock.patch.object(at, "_transcribe", side_effect=fake_transcribe):
                res = at.triage_one(clip)

        self.assertEqual(called["n"], 0, "VAD 判无语音时不该花远端 ASR 额度")
        self.assertEqual(res["verdict"], "yes")
        self.assertIn("VAD", res["reason"])
        self.assertEqual(res["vad"]["backend"], "ffmpeg", "VAD 结论要带后端标注")

    def test_remote_transcribe_still_used_when_speech(self):
        """回归保护：有语音时仍走远端转写（别为了省额度把正常路径也砍了）。"""
        import importlib
        at = importlib.import_module("audio_triage")
        called = {"n": 0}

        def fake_transcribe(*a, **k):
            called["n"] += 1
            return "这是中文人声"

        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "c.mp4"
            clip.write_bytes(b"x")
            with mock.patch.object(at, "probe",
                                   return_value={"audio": True, "duration": 10.0}), \
                 mock.patch.object(at, "_local_vad",
                                   return_value={"has_speech": True, "ratio": 0.6,
                                                 "backend": "silero"}), \
                 mock.patch.object(at, "_extract_sample", return_value=True), \
                 mock.patch.object(at, "_scan_tts_slot",
                                   return_value=(1, "http://b/v1", "k")), \
                 mock.patch.object(at, "_transcribe", side_effect=fake_transcribe):
                res = at.triage_one(clip)

        self.assertEqual(called["n"], 1, "有语音必须仍走远端转写")
        self.assertEqual(res["verdict"], "no")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestAudioQcEndToEnd(unittest.TestCase):
    """真跑 ffmpeg：造已知属性的片段，验证判定链（内容级，不只看 rc）。"""

    @staticmethod
    def _make(td, *, audio_dur: float, video_dur: float = 10.0) -> str:
        """video_dur 秒画面；前 audio_dur 秒有 440Hz 正弦，其余补静音。

        `apad=whole_dur=<时长>` 而非裸 apad——裸 apad + -shortest 会挂起（v4.7.7 实证）。"""
        import importlib
        import subprocess as sp
        ff = importlib.import_module("postprocess")._ffmpeg()
        v = os.path.join(td, "v.mp4")
        a = os.path.join(td, "a.m4a")
        out = os.path.join(td, "out.mp4")
        sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                f"color=c=blue:s=320x240:d={video_dur}", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", v], check=True)
        sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                f"sine=f=440:d={audio_dur}", "-c:a", "aac", a], check=True)
        sp.run([ff, "-y", "-loglevel", "error", "-i", v, "-i", a,
                "-filter_complex", f"[1:a]aresample=48000,apad=whole_dur={video_dur}[au]",
                "-map", "0:v", "-map", "[au]", "-c:v", "copy", "-c:a", "aac",
                "-shortest", out], check=True)
        return out

    def test_mostly_silent_flags_warn(self):
        """3s 有声 + 7s 静音 → 静音占比 ≈70% → 必须报 WARN（原 QC 完全不看音频）。"""
        with tempfile.TemporaryDirectory() as td:
            clip = self._make(td, audio_dur=3.0)
            rep = _aq().analyze(clip, want_vad=False)
        self.assertAlmostEqual(rep["duration"], 10.0, delta=0.3,
                               msg="apad + -shortest 会得到 ~9.96s，容差 0.3s")
        levels = {v["level"] for v in rep["verdict"]}
        self.assertIn("WARN", levels, f"70% 静音应 WARN，实际 verdict={rep['verdict']}")
        self.assertNotIn("FAIL", levels, f"70% 静音不该 FAIL，实际 verdict={rep['verdict']}")

    def test_fully_silent_is_fail(self):
        """整条无音轨内容（音频全静音）→ 静音占比 ≈100% → FAIL。"""
        with tempfile.TemporaryDirectory() as td:
            clip = self._make(td, audio_dur=0.1)
            rep = _aq().analyze(clip, want_vad=False)
        self.assertIn("FAIL", {v["level"] for v in rep["verdict"]},
                      f"≈全静音必须 FAIL（音轨没接上/旁白丢失），实际 {rep['verdict']}")

    def test_speech_detection_shape_and_backend(self):
        """真跑一次 VAD：形状完整 + backend 如实标注。

        ⚠️ **不断言 has_speech=True**：本用例的音频是 440Hz 正弦——能量法会判"有语音"，
        而 silero 会正确判"非语音"（它识的是**语音**，不是能量；这正是神经 VAD 的价值）。
        把断言写死在某个模型对合成音的意见上，等于把测试绑死到模型版本。"""
        with tempfile.TemporaryDirectory() as td:
            clip = self._make(td, audio_dur=6.0)
            r = _aq().detect_speech(clip)
        self.assertIn(r["backend"], ("silero", "ffmpeg"))
        self.assertEqual(set(r), {"has_speech", "ratio", "total", "segments",
                                  "backend", "duration"})
        self.assertIsInstance(r["has_speech"], bool)

    def test_all_silent_no_speech_on_both_backends(self):
        """全静音时**两条后端都必须**判"无语音"——这是 triage 省远端额度的前提。"""
        with tempfile.TemporaryDirectory() as td:
            clip = self._make(td, audio_dur=0.05)
            for be in ("ffmpeg", "auto"):
                r = _aq().detect_speech(clip, backend=be)
                self.assertFalse(r["has_speech"],
                                 f"全静音必须判无语音（请求={be}→实际={r['backend']}）：{r}")

    def test_vad_backend_reflects_environment(self):
        """后端选择必须与"模型文件在不在"一致，不许假装用了 silero。"""
        aq = _aq()
        be = aq.vad_backend()
        has_model = (Path(aq.MODELS_DIR) / aq.SILERO_MODEL).exists()
        if be == "silero":
            self.assertTrue(has_model and aq._onnx_available(),
                            "报 silero 就必须真有模型 + onnxruntime")
        else:
            self.assertTrue(not has_model or not aq._onnx_available(),
                            "报 ffmpeg 就必须确实缺模型或缺运行时")

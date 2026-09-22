# -*- coding: utf-8 -*-
"""批十二红灯测试：标点恢复（word_axis）+ 说话人分离（speakers.py）。"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import word_axis as wa  # noqa: E402
import speakers as spk  # noqa: E402

_slow = not os.environ.get("SLOW")


class TestMergePunctIntoWords(unittest.TestCase):
    """标点回填：标点插入的是**字间**，必须挂回前一个字的词目（纯函数）。"""

    def test_trailing_punct_attached(self):
        words = [{"w": "你", "at": 0.0, "dur": 0.1},
                 {"w": "好", "at": 0.1, "dur": 0.1}]
        out = wa.merge_punct_into_words(words, "你好，")
        self.assertEqual(out[0]["w"], "你")
        self.assertEqual(out[1]["w"], "好，", "逗号应挂在前字后面")

    def test_mid_and_end_punct(self):
        words = [{"w": c, "at": i * 0.1, "dur": 0.1}
                 for i, c in enumerate("大家如果对我的研究感兴趣呢")]
        out = wa.merge_punct_into_words(words, "大家如果对我的研究感兴趣呢。")
        self.assertEqual(out[-1]["w"], "呢。")
        self.assertEqual([w["w"] for w in out[:5]], list("大家如果对"))

    def test_no_punct_unchanged(self):
        words = [{"w": "你", "at": 0.0, "dur": 0.1}]
        out = wa.merge_punct_into_words(words, "你")
        self.assertEqual(out[0]["w"], "你")

    def test_mismatch_falls_back(self):
        """标点后文本对不上原字符序列（模型异常改字）→ 原样返回，不编造。"""
        words = [{"w": "你", "at": 0.0, "dur": 0.1}]
        out = wa.merge_punct_into_words(words, "他好，")
        self.assertEqual(out, words)

    def test_timestamps_untouched(self):
        words = [{"w": "好", "at": 0.55, "dur": 0.2}]
        out = wa.merge_punct_into_words(words, "好！")
        self.assertEqual(out[0]["at"], 0.55)
        self.assertEqual(out[0]["dur"], 0.2)


class TestRenumberSpeakers(unittest.TestCase):
    """说话人重编号：按**首次出现**顺序归零（纯函数）。"""

    def test_renumber_by_first_appearance(self):
        segs = [{"start": 5.0, "end": 8.0, "speaker": 3},
                {"start": 0.0, "end": 2.0, "speaker": 7}]
        out = spk.renumber_speakers(segs)
        self.assertEqual([s["speaker"] for s in out], [0, 1], "7 先出现 → 0")
        self.assertEqual([s["start"] for s in out], [0.0, 5.0], "输出按时间排序")

    def test_already_canonical_unchanged(self):
        segs = [{"start": 0.0, "end": 1.0, "speaker": 0},
                {"start": 2.0, "end": 3.0, "speaker": 1}]
        self.assertEqual(spk.renumber_speakers(segs), segs)

    def test_empty(self):
        self.assertEqual(spk.renumber_speakers([]), [])


class TestSpeakerBackendGuards(unittest.TestCase):
    """缺依赖/缺模型 → 指引明确，rc 语义对（不静默假装）。"""

    def test_backend_probe_shape(self):
        be = spk.speakers_backend()
        self.assertIn(be, ("sherpa", ""), "backend 只允许 sherpa 或空")

    def test_probe_requires_both_models(self):
        """半套模型（只有切分、缺嵌入）必须判不可用——跑不起来的探针是假绿灯。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "segmentation.onnx").write_bytes(b"x" * 2_000_000)
            self.assertEqual(spk.speakers_backend(d), "", "缺嵌入模型必须判不可用")
            (d / "embedding.onnx").write_bytes(b"x" * 2_000_000)
            self.assertEqual(spk.speakers_backend(d), "sherpa")


    def test_install_hint_mentions_models(self):
        hint = spk.install_hint(Path("~/.nonexistent-models").expanduser())
        self.assertIn("segmentation", hint)
        self.assertIn("embedding", hint)

    def test_explicit_backend_without_models_raises(self):
        """显式点名 sherpa 而模型不齐 → 报错必须是**安装指引**，
        不是后续 decode 之类别的 RuntimeError（否则守卫形同虚设）。"""
        with self.assertRaises(RuntimeError) as cm:
            spk.diarize("whatever.wav", models_dir=str(
                Path("~/.nonexistent-models").expanduser()), backend="sherpa")
        self.assertIn("pip install", str(cm.exception), "必须是安装指引")
        self.assertIn("segmentation", str(cm.exception))


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
class TestDiarizeEndToEnd(unittest.TestCase):
    """真跑：0.wav + 1s 静音 + 1.wav（模型自带两段中文语音），内容级校验。"""

    @classmethod
    def setUpClass(cls):
        cls.m = spk
        import shutil
        import wave
        P = (Path.home() / ".workbuddy" / "models" / "sherpa-paraformer-zh" / "test_wavs")
        if not (P / "0.wav").exists() or not (P / "1.wav").exists():
            raise unittest.SkipTest("需要 sherpa-paraformer-zh test_wavs")
        cls.wav = Path.home() / ".workbuddy" / "models" / "_diar_mix.wav"
        with wave.open(str(P / "0.wav"), "rb") as a, \
                wave.open(str(P / "1.wav"), "rb") as b, \
                wave.open(str(cls.wav), "wb") as o:
            o.setnchannels(1); o.setsampwidth(2); o.setframerate(16000)
            o.writeframes(a.readframes(a.getnframes()))
            o.writeframes(b"\x00" * 32000)          # 1s 静音分隔
            o.writeframes(b.readframes(b.getnframes()))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "wav", None) and cls.wav.exists():
            cls.wav.unlink()

    def test_two_speakers_and_coverage(self):
        r = self.m.diarize(str(self.wav), num_speakers=2)
        self.assertEqual(r["backend"], "sherpa")
        segs = r["segments"]
        self.assertGreater(len(segs), 0)
        self.assertGreaterEqual(r["num_speakers"], 2)
        speakers = {s["speaker"] for s in segs}
        self.assertEqual(len(speakers), 2, "num_clusters=2 应聚出两簇")
        times = [(s["start"], s["end"]) for s in segs]
        for st, en in times:
            self.assertGreaterEqual(st, 0.0)
            self.assertLessEqual(en, r["duration"] + 0.5, "段不得超出音频范围")
            self.assertGreater(en, st, "段长必须为正")
        # 覆盖两个来源区（0.wav 区 & 1.wav 区，中间 1s 静音）
        self.assertTrue(any(en > 6.5 for _, en in times), "第二段音频必须有覆盖")

    def test_auto_backend_zh(self):
        r = self.m.diarize(str(self.wav))          # auto
        self.assertEqual(r["backend"], "sherpa")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
@unittest.skipUnless(
    wa.punct_backend(), "需要本地 sherpa 标点模型（~/.workbuddy/models/sherpa-punct）")
class TestPunctEndToEnd(unittest.TestCase):
    """真跑标点恢复（ct-transformer）：内容级——标点必须出现在 text 且挂回词目。"""

    @classmethod
    def setUpClass(cls):
        P = (Path.home() / ".workbuddy" / "models" / "sherpa-paraformer-zh"
             / "test_wavs" / "0.wav")
        if not P.exists():
            raise unittest.SkipTest("需要 sherpa-paraformer-zh test_wavs")
        cls.r = wa.transcribe(str(P), backend="sherpa", language="zh", punctuate=True)

    def test_text_gained_punctuation(self):
        self.assertEqual(self.r.get("punct"), "sherpa")
        self.assertTrue(any(c in self.r["text"] for c in "，。！？"),
                        f"标点后文本应有标点：{self.r['text']!r}")

    def test_punct_attached_to_words(self):
        self.assertTrue(self.r["words"][-1]["w"].endswith(("。", "！", "？", "，")),
                        "末词应带句尾标点")
        self.assertEqual(sum(len(w["w"]) for w in self.r["words"]),
                         len(self.r["text"]),
                         "词目字符总数（含挂回的标点）应等于标点后文本长度")
        times = [w["at"] for w in self.r["words"]]
        self.assertEqual(times, sorted(times), "标点不影响时间戳单调性")

    def test_mismatch_guarded(self):
        """标点文本对不上原字 → 原样返回（不编造）。"""
        words = [{"w": "你", "at": 0.0, "dur": 0.1}]
        self.assertEqual(wa.merge_punct_into_words(words, "他好。"), words)

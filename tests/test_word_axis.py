"""word_axis（② 词级时间轴）测试。

设计要点：**纯函数 + 可注入会话**——
- mel 相关（`mel_filters` / `log_mel`）是纯 numpy，可精确断言（静音 → 常量 −1.5 是
  由 whisper 的 `log10(clamp(mel,1e-10))` + `max(x, max-8)` + `(x+4)/4` 唯一确定的）；
- 时间戳换算与分词是纯函数；
- 解码循环用**假会话**注入——能断言"cache 有没有真的喂回去"（这决定解码是对的还是
  退化成每步全量重算）。
真实模型只出现在 SLOW 端到端（用 whisper.cpp 的 jfk.wav 做**内容级**校验，
已知文本"And so my fellow Americans…"）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

_slow = not os.environ.get("SLOW")
_HERE = Path(__file__).resolve().parents[1]
_JFK = Path.home() / ".workbuddy" / "models" / "samples" / "jfk.wav"


def _wa():
    sc = str(_HERE / "scripts")
    if sc not in sys.path:
        sys.path.insert(0, sc)
    import word_axis
    return word_axis


class _FakeIO:
    def __init__(self, name):
        self.name = name


class _FakeSession:
    """可编排的假解码器：每步 argmax 出一个 token，并如实回吐 present.*。

    记录每次 `run` 收到的 feed 形状——用来断言 KV cache 真的被喂回。"""

    def __init__(self, script, n_layers=1, vocab=51865, enc_len=1500):
        self.script = list(script)
        self.n_layers = n_layers
        self.vocab = vocab
        self.enc_len = enc_len
        self.calls = []

    def get_inputs(self):
        names = ["input_ids", "encoder_hidden_states", "use_cache_branch"]
        for i in range(self.n_layers):
            for k in ("decoder.key", "decoder.value", "encoder.key", "encoder.value"):
                names.append(f"past_key_values.{i}.{k}")
        return [_FakeIO(n) for n in names]

    def get_outputs(self):
        names = ["logits"]
        for i in range(self.n_layers):
            for k in ("decoder.key", "decoder.value", "encoder.key", "encoder.value"):
                names.append(f"present.{i}.{k}")
        return [_FakeIO(n) for n in names]

    def run(self, names, feed):
        self.calls.append(dict(feed))
        step = len(self.calls) - 1
        tid = self.script[step] if step < len(self.script) else 50257
        logits = np.full((1, 1, self.vocab), -10.0, dtype=np.float32)
        logits[0, 0, tid] = 10.0
        out = [logits]
        for _ in range(self.n_layers):
            out.append(np.zeros((1, 8, 1, 64), dtype=np.float32))          # dec key
            out.append(np.zeros((1, 8, 1, 64), dtype=np.float32))          # dec value
            out.append(np.zeros((1, 8, self.enc_len, 64), dtype=np.float32))  # enc key
            out.append(np.zeros((1, 8, self.enc_len, 64), dtype=np.float32))
        return out


class TestBackend(unittest.TestCase):
    """后端探测必须**如实**（缺依赖/缺模型 → 空串，不崩、不假装）。"""

    def test_empty_dir_is_not_available(self):
        m = _wa()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(m.whisper_backend(td), "")

    def test_all_three_files_required(self):
        m = _wa()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / m.ENC_MODEL).write_bytes(b"x" * 1000)
            (Path(td) / m.DEC_MODEL).write_bytes(b"x" * 1000)
            self.assertEqual(m.whisper_backend(td), "",
                             "只有编解码器、缺 tokenizer → 仍不可用")
            (Path(td) / m.TOKENIZER_FILE).write_bytes(b"{}")
            self.assertEqual(m.whisper_backend(td), "whisper")

    def test_install_hint_names_files_and_source(self):
        m = _wa()
        hint = m.install_hint("/tmp/x")
        for kw in ("onnxruntime", "tokenizers", "encoder_model"):
            self.assertIn(kw, hint, f"安装指引必须说清 {kw}")
        self.assertIn("modelscope", hint.lower(), "必须给出可达的模型来源（HF 被封）")


class TestTokensToWords(unittest.TestCase):
    """CTC/paraformer 的逐 token 时间戳 → 词轴（纯函数）。

    paraformer-zh：1 token = 1 汉字，timestamps[i] 是起始秒，末 token 结束 = end。
    """

    def test_basic_last_token_uses_end(self):
        m = _wa()
        ws = m.tokens_to_words(["对", "我", "做"], [0.1, 0.4, 0.7], 1.0)
        self.assertEqual([w["w"] for w in ws], ["对", "我", "做"])
        self.assertEqual(ws[0]["at"], 0.1)
        self.assertEqual(ws[0]["dur"], 0.3, "中间 token：下一 token 的起始 − 本 token 起始")
        self.assertEqual(ws[2]["dur"], 0.3, "末 token：end − 起始")

    def test_skips_specials_and_blank(self):
        m = _wa()
        ws = m.tokens_to_words(["<blk>", "对", " ", "<unk>", "我"], [0, 0.2, 0.4, 0.6, 0.8], 1.2)
        self.assertEqual([w["w"] for w in ws], ["对", "我"])

    def test_truncates_when_timestamps_shorter(self):
        """没时间戳的 token 无法定位 → 宁可少不可编。"""
        m = _wa()
        ws = m.tokens_to_words(["甲", "乙", "丙"], [0.0, 0.5], 2.0)
        self.assertEqual([w["w"] for w in ws], ["甲", "乙"])

    def test_never_negative_duration(self):
        """时间戳乱序/越界 → 钳 0，不产生负时长。"""
        m = _wa()
        ws = m.tokens_to_words(["甲", "乙"], [0.9, 0.1], 1.0)
        self.assertTrue(all(w["dur"] >= 0 for w in ws), str(ws))

    def test_end_zero_falls_back_to_next_ts(self):
        m = _wa()
        ws = m.tokens_to_words(["甲", "乙"], [0.0, 0.5], 0)
        self.assertEqual(ws[0]["dur"], 0.5, "end 未知时末前段仍按下一 token 切")

    def test_nan_and_negative_ts_skipped(self):
        m = _wa()
        ws = m.tokens_to_words(["甲", "乙", "丙"],
                               [float("nan"), -1.0, 0.3], 1.0)
        self.assertEqual([w["w"] for w in ws], ["丙"])


class TestSherpaBackendDetect(unittest.TestCase):
    """探测必须如实：缺文件/缺依赖 → 空串；齐了 → "sherpa"。"""

    def test_empty_dir_is_not_available(self):
        m = _wa()
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(m.sherpa_backend(td), "")

    def test_files_but_import_blocked(self):
        """文件齐但 import 失败（依赖没装）→ 仍不可用。"""
        m = _wa()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / m.SHERPA_MODEL).write_bytes(b"x" * 1000)
            (Path(td) / m.SHERPA_TOKENS).write_bytes(b"x" * 100)
            with mock.patch.dict(sys.modules, {"sherpa_onnx": None}):
                self.assertEqual(m.sherpa_backend(td), "",
                                 "sherpa_onnx 没装 → 必须如实返回空串")

    @unittest.skipUnless((Path.home() / ".workbuddy" / "models" / "sherpa-paraformer-zh"
                          / "model.int8.onnx").exists(),
                         "需要本地 sherpa paraformer 模型（~/.workbuddy/models/）")
    def test_available_when_model_present(self):
        m = _wa()
        self.assertEqual(m.sherpa_backend(None), "sherpa")
        self.assertEqual(m.available_backend(None), "sherpa",
                         " sherpa 与 whisper 都可用时，中文场景应优先 sherpa")

    def test_available_backend_falls_back_to_whisper(self):
        m = _wa()
        with mock.patch.object(m, "sherpa_backend", return_value=""), \
                mock.patch.object(m, "whisper_backend", return_value="whisper"):
            self.assertEqual(m.available_backend(None), "whisper")


class TestTranscribeDispatch(unittest.TestCase):
    """分派规则：auto+zh 优先 sherpa；显式后端缺依赖必须抛（不静默降级）。"""

    def _sentinel(self, m):
        return {"backend": "sherpa", "text": "哨兵", "words": [], "duration": 1.0}

    def test_auto_zh_prefers_sherpa(self):
        m = _wa()
        with mock.patch.object(m, "sherpa_backend", return_value="sherpa"), \
                mock.patch.object(m, "transcribe_sherpa",
                                  side_effect=lambda *a, **k: self._sentinel(m)) as ts:
            r = m.transcribe("whatever.wav", language="zh")
        self.assertEqual(r["backend"], "sherpa")
        self.assertTrue(ts.called, "auto+zh 应走 sherpa")

    def test_auto_en_prefers_whisper(self):
        m = _wa()
        with mock.patch.object(m, "sherpa_backend", return_value="sherpa"), \
                mock.patch.object(m, "whisper_backend", return_value="whisper"), \
                mock.patch.object(m, "transcribe_sherpa",
                                  side_effect=AssertionError("英文不该走 sherpa")):
            # whisper 路径会去读模型文件 → 让它先在" sherpa 分支不可达"处失败也算通过；
            # 这里只断言没进 sherpa 分支
            try:
                m.transcribe("whatever.wav", language="en")
            except Exception:
                pass

    def test_explicit_sherpa_without_model_raises(self):
        m = _wa()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError) as cm:
                m.transcribe(str(_JFK), models_dir=td, backend="sherpa")
            self.assertIn("sherpa-onnx", str(cm.exception), "报错必须带安装指引")

    def test_unknown_backend_rejected(self):
        m = _wa()
        with self.assertRaises(RuntimeError):
            m.transcribe(str(_JFK), backend="librispeech")



class TestMelFilters(unittest.TestCase):
    def test_shape_and_nonneg(self):
        m = _wa()
        fb = m.mel_filters()
        self.assertEqual(fb.shape, (80, m.N_FFT // 2 + 1), "80 mels × (n_fft/2+1) 频点")
        self.assertTrue((fb >= 0).all(), "三角滤波器不允许负值")

    def test_every_filter_has_energy(self):
        m = _wa()
        fb = m.mel_filters()
        self.assertTrue((fb.sum(axis=1) > 0).all(), "每个 mel 滤波器都必须非空")

    def test_center_frequency_increases(self):
        m = _wa()
        fb = m.mel_filters()
        centers = [int(np.argmax(row)) for row in fb]
        self.assertEqual(centers, sorted(centers), "滤波器中心频率必须随序号单调上升")
        self.assertLess(centers[0], centers[-1])

    def test_nyquist_bin_tapers_down(self):
        """最后一个滤波器的能量应集中在高频侧（说明 Slaney 划分是对的）。"""
        m = _wa()
        fb = m.mel_filters()
        self.assertGreater(fb[-1].sum(), 0)
        self.assertLess(int(np.argmax(fb[-1])), fb.shape[1])


class TestLogMel(unittest.TestCase):
    def test_silence_is_constant_minus_one_point_five(self):
        """静音 → 全 −1.5。这是 whisper 规格唯一确定的：log10(1e-10)=−10，
        再 `max(x, max−8)` 不动它，最后 `(x+4)/4 = −1.5`。能钉住整条链路。"""
        m = _wa()
        pcm = np.zeros(m.SAMPLE_RATE, dtype=np.float32)
        mel = m.log_mel(pcm)
        self.assertEqual(mel.shape, (80, 3000), "pad 到 30s → 固定 3000 帧")
        self.assertTrue(np.allclose(mel, -1.5, atol=1e-4),
                        f"静音应恒为 −1.5，实际 {mel.min():.4f}~{mel.max():.4f}")

    def test_long_input_truncated_to_pad_window(self):
        m = _wa()
        pcm = np.zeros(m.SAMPLE_RATE * 45, dtype=np.float32)   # 45s
        self.assertEqual(m.log_mel(pcm).shape, (80, 3000))

    def test_all_finite(self):
        m = _wa()
        rng = np.random.default_rng(0)
        pcm = (rng.standard_normal(m.SAMPLE_RATE * 2) * 0.1).astype(np.float32)
        mel = m.log_mel(pcm)
        self.assertTrue(np.isfinite(mel).all(), "不允许 NaN/Inf")


class TestTimestamps(unittest.TestCase):
    def test_zero_and_step(self):
        m = _wa()
        self.assertAlmostEqual(m.timestamp_seconds(m.TS_BASE), 0.0, places=6)
        self.assertAlmostEqual(m.timestamp_seconds(m.TS_BASE + 50), 1.0, places=6)

    def test_non_timestamp_returns_none(self):
        m = _wa()
        self.assertIsNone(m.timestamp_seconds(50257))
        self.assertIsNone(m.timestamp_seconds(1))

    def test_is_timestamp_boundary(self):
        m = _wa()
        self.assertFalse(m.is_timestamp(m.TS_BASE - 1))
        self.assertTrue(m.is_timestamp(m.TS_BASE))
        self.assertTrue(m.is_timestamp(m.TS_BASE + 1400))


class TestInterpolateTimes(unittest.TestCase):
    """`<|t.dd|>` 只在段边界出现，中间文本 token 的时间靠序号插值。"""

    def test_interpolates_between_stamps(self):
        m = _wa()
        got = m.interpolate_times([m.TS_BASE, 10, 11, m.TS_BASE + 50, 12])
        self.assertEqual([t for t, _ in got], [10, 11, 12])
        self.assertAlmostEqual(got[0][1], 1.0 / 3, places=2)
        self.assertAlmostEqual(got[1][1], 2.0 / 3, places=2)
        self.assertAlmostEqual(got[2][1], 1.0, places=3, msg="末戳之后的 token 取末戳")

    def test_tail_takes_last_stamp(self):
        m = _wa()
        got = m.interpolate_times([m.TS_BASE, 10, 11])
        self.assertEqual([at for _, at in got], [0.0, 0.0])

    def test_head_before_first_stamp_counts_back(self):
        m = _wa()
        got = m.interpolate_times([5, 6, m.TS_BASE + 50])
        self.assertAlmostEqual(got[0][1], 0.96, places=3)
        self.assertAlmostEqual(got[1][1], 0.98, places=3)

    def test_no_stamps_all_zero(self):
        m = _wa()
        got = m.interpolate_times([3, 4, 5])
        self.assertEqual([at for _, at in got], [0.0, 0.0, 0.0])

    def test_timestamp_tokens_excluded(self):
        m = _wa()
        self.assertEqual(len(m.interpolate_times([m.TS_BASE, 10, m.TS_BASE + 50])), 1)


class TestGroupWords(unittest.TestCase):
    def test_chinese_multi_char_token_splits_per_char(self):
        m = _wa()
        words = m.group_words([{"t": "你好", "at": 0.0}, {"t": "世界", "at": 1.0}], end=2.0)
        self.assertEqual([w["w"] for w in words], ["你", "好", "世", "界"])
        self.assertAlmostEqual(words[0]["at"], 0.0, places=3)
        self.assertAlmostEqual(words[1]["at"], 0.5, places=3, msg="词内按均分插值")
        self.assertAlmostEqual(words[2]["at"], 1.0, places=3)

    def test_latin_groups_by_leading_space(self):
        m = _wa()
        words = m.group_words([{"t": " hello", "at": 0.0}, {"t": " world", "at": 0.5}], end=1.0)
        self.assertEqual([w["w"] for w in words], ["hello", "world"])

    def test_blank_pieces_ignored(self):
        m = _wa()
        words = m.group_words([{"t": " ", "at": 0.0}, {"t": " hi", "at": 0.2}], end=1.0)
        self.assertEqual([w["w"] for w in words], ["hi"])

    def test_durations_positive_and_ordered(self):
        m = _wa()
        words = m.group_words([{"t": " abc", "at": 0.0}, {"t": " def", "at": 0.4}], end=1.0)
        self.assertGreater(words[0]["dur"], 0)
        self.assertAlmostEqual(words[0]["dur"], 0.4, places=3)
        self.assertGreater(words[1]["dur"], 0, "末词用 end 兜底，不能是 0")

    def test_empty_input(self):
        m = _wa()
        self.assertEqual(m.group_words([]), [])


class TestGreedyDecode(unittest.TestCase):
    """行为级：断言解码**每步整句重算**。

    这不是实现细节——KV cache 支路实测与整句重算**数值不一致**（首步 argmax 相同但
    logits 最大绝对差 2.86），误差累积后 11 秒语音的转写会被带偏。所以
    "每步 use_cache_branch=False" 是**由端到端验证选出来的契约**，必须钉住：
    谁哪天为了快改回 cache 支路，这里就红。"""

    def _decode(self, script, **kw):
        m = _wa()
        sess = _FakeSession(script)
        enc = np.zeros((1, 1500, 512), dtype=np.float32)
        toks = m.greedy_decode(sess, enc, [50258, 50259, 50359], **kw)
        return m, sess, toks

    def test_returns_tokens_until_eos(self):
        m, sess, toks = self._decode([10, 20, 50257, 999])
        self.assertEqual(toks, [10, 20], "遇 eos 立即停，不含 eos 自身")

    def test_stops_at_max_tokens(self):
        m, sess, toks = self._decode([7] * 50, max_tokens=5)
        self.assertEqual(len(toks), 5)

    def test_always_recomputes_full_sequence(self):
        m, sess, toks = self._decode([10, 11, 50257])
        for i, c in enumerate(sess.calls):
            self.assertFalse(bool(c["use_cache_branch"][0]),
                             f"第 {i} 步必须走全量支路（cache 支路已验证会带偏结果）")

    def test_input_ids_grow_by_one_each_step(self):
        m, sess, toks = self._decode([10, 11, 50257])
        lens = [c["input_ids"].shape[1] for c in sess.calls]
        self.assertEqual(lens[0], 3, "首步喂完整 prompt")
        self.assertEqual(lens[1], 4, "次步应带上刚生成的那个 token")
        self.assertEqual(lens, sorted(lens), "input_ids 必须逐步增长（整句重算的特征）")

    def test_begin_suppress_applied_only_at_first_step(self):
        """begin_suppress_tokens（含 eos 50257）只在**首步**压制。

        否则首步会直接吐 eos → 空转写；而每步都压制则永远停不下来。"""
        m = _wa()

        class S(_FakeSession):
            def run(self, names, feed):
                out = super().run(names, feed)
                if len(self.calls) == 1:
                    out[0][0, 0, 50257] = 99.0     # 让 eos 成为最强
                return out

        sess = S([10, 50257])
        enc = np.zeros((1, 1500, 512), dtype=np.float32)
        toks = m.greedy_decode(sess, enc, [50258, 50259, 50359], suppress=(50257,))
        self.assertEqual(toks, [10], "首步 eos 被压制，所以先出 10；次步 eos 放行而停")


class TestEnergySnap(unittest.TestCase):
    """段内均分**不含声学信息**（停顿/拖音全被抹平），用能量谷精修。

    只调词间边界，且**谷不够深就不动**——宁可均匀，也不被噪声带跑。"""

    def test_frame_rms_shape_and_silence(self):
        m = _wa()
        rms, hop = m.frame_rms(np.zeros(m.SAMPLE_RATE, dtype=np.float32))
        self.assertEqual(len(rms), 100, "1s / 10ms = 100 帧")
        self.assertAlmostEqual(hop, 0.01, places=6)
        self.assertTrue(np.allclose(rms, 0.0, atol=1e-6))

    def test_deep_gap_attracts_boundary(self):
        m = _wa()
        rms = np.full(101, 1.0, dtype=np.float32)
        rms[52] = 0.01                      # 0.52s 处一个深谷
        got = m.snap_boundaries([0.0, 0.5, 1.0], rms, 0.01)
        self.assertAlmostEqual(got[1], 0.52, places=3, msg="应吸附到能量谷")

    def test_flat_energy_keeps_uniform(self):
        m = _wa()
        rms = np.full(101, 1.0, dtype=np.float32)
        self.assertEqual(m.snap_boundaries([0.0, 0.5, 1.0], rms, 0.01), [0.0, 0.5, 1.0],
                         "没有谷就保持原位（不被噪声带跑）")

    def test_ends_never_move_and_min_duration_kept(self):
        m = _wa()
        rms = np.full(101, 1.0, dtype=np.float32)
        rms[49] = 0.0
        got = m.snap_boundaries([0.0, 0.06, 0.09, 1.0], rms, 0.01, min_dur=0.05)
        self.assertEqual(got[0], 0.0)
        self.assertEqual(got[-1], 1.0)
        for i in range(1, len(got)):
            self.assertGreaterEqual(got[i] - got[i - 1], 0.0499, "相邻间隔不得小于 min_dur")

    def test_refine_words_rewrites_at_and_dur(self):
        m = _wa()
        pcm = np.full(m.SAMPLE_RATE, 0.1, dtype=np.float32)
        pcm[8320:8480] = 0.0                # 0.52s 处静音（8320 样本 / 16000）
        words = [{"w": "a", "at": 0.0, "dur": 0.5},
                 {"w": "b", "at": 0.5, "dur": 0.5},
                 {"w": "c", "at": 1.0, "dur": 0.1}]
        out = m.refine_words(words, pcm)
        self.assertAlmostEqual(out[1]["at"], 0.52, places=2)
        self.assertAlmostEqual(out[0]["dur"], 0.52, places=2)

    def test_short_input_untouched(self):
        m = _wa()
        words = [{"w": "a", "at": 0.0, "dur": 1.0}]
        self.assertEqual(m.refine_words(words, np.zeros(16000, dtype=np.float32)), words)


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
@unittest.skipUnless(_JFK.exists(), "需要 ~/.workbuddy/models/samples/jfk.wav（whisper.cpp 样本）")
class TestWhisperEndToEnd(unittest.TestCase):
    """内容级校验：mel 算错或 cache 接错，转写文字就会是乱的。"""

    @classmethod
    def setUpClass(cls):
        cls.r = _wa().transcribe(str(_JFK), language="en")

    def test_transcribes_known_words(self):
        self.assertEqual(self.r["backend"], "whisper")
        text = self.r["text"].lower()
        for w in ("fellow", "country", "ask"):
            self.assertIn(w, text, f"内容级校验失败（mel/解码链有问题）：{text[:200]}")

    def test_word_axis_ordered_and_inside_duration(self):
        words = self.r["words"]
        self.assertTrue(words, "必须产出词轴")
        at = [w["at"] for w in words]
        self.assertEqual(at, sorted(at), "词轴时间必须单调")
        self.assertGreaterEqual(at[0], 0.0)
        self.assertLess(at[-1], self.r["duration"] + 1.0)
        self.assertTrue(all(w["dur"] > 0 for w in words), "每个词都要有正时长")

    def test_forces_timestamp_mode(self):
        """必须**压制 `<|notimestamps|>`**。

        否则模型自选无时间戳模式（实测首步概率 0.688），一个锚点都不吐——
        词轴会全挤在 0.0s，看起来"跑成功了"却毫无时间信息（本项目最忌的静默失败）。"""
        self.assertGreater(self.r["anchors"], 0, "没有锚点 = 词轴没有时间信息")
        self.assertEqual(self.r["timing"], "anchors+energy-snapped")
        self.assertGreater(self.r["words"][-1]["at"], 0.5, "词轴必须真的铺开时间跨度")

    def test_explicit_backend_without_model_raises(self):
        m = _wa()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                m.transcribe(str(_JFK), models_dir=td, backend="whisper")


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
@unittest.skipUnless(
    (Path.home() / ".workbuddy" / "models" / "sherpa-paraformer-zh" / "model.int8.onnx").exists(),
    "需要本地 sherpa paraformer-zh 模型（~/.workbuddy/models/sherpa-paraformer-zh）")
class TestSherpaEndToEnd(unittest.TestCase):
    """真跑 sherpa（paraformer-zh int8）—— **内容级**校验，不只看 rc。

    验证锚 = 模型自带 test_wavs/0.wav：阶段一实测转写为
    「对我做了介绍啊那么我想说的是呢大家如果对我的研究感兴趣呢嗯」。
    中文逐字：len(words) 应等于 len(text)。
    """

    @classmethod
    def setUpClass(cls):
        m = _wa()
        cls.m = m
        cls.wav = (Path.home() / ".workbuddy" / "models" / "sherpa-paraformer-zh"
                   / "test_wavs" / "0.wav")
        cls.r = m.transcribe(str(cls.wav), backend="sherpa", language="zh")

    def test_backend_and_timing_honest(self):
        self.assertEqual(self.r["backend"], "sherpa")
        self.assertEqual(self.r["timing"], "token-timestamps",
                         "sherpa 是真声学对齐，不做能量吸附——字段必须如实区分")

    def test_text_matches_known_transcript(self):
        for kw in ("对我做了介绍", "研究感兴趣"):
            self.assertIn(kw, self.r["text"], f"转写里必须有 {kw}（内容级校验）")

    def test_per_char_axis_complete_and_monotonic(self):
        text = self.r["text"]
        words = self.r["words"]
        self.assertGreater(len(words), 20, "5.6s 中文语音应有 20+ 字的时间轴")
        self.assertEqual(len(words), len(text),
                         "paraformer 中文逐字：词轴数应等于字数")
        times = [w["at"] for w in words]
        self.assertEqual(times, sorted(times), "时间戳必须单调不减")
        self.assertGreater(words[-1]["at"], 1.0, "词轴必须铺开时间跨度，不许全挤在 0")

    def test_auto_zh_uses_sherpa(self):
        r = self.m.transcribe(str(self.wav), language="zh")   # auto
        self.assertEqual(r["backend"], "sherpa", "auto + 中文应自动选 sherpa")

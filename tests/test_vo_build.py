# -*- coding: utf-8 -*-
"""vo_build 声音链测试——按模块拆分自 test_media_gen.py。

运行：python -m unittest discover tests          （skill 根目录，快测试全跑）
     SLOW=1 python -m unittest discover tests   （含真跑 ffmpeg 的慢测试）
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import media_gen as mg  # noqa: E402,F401
import mg_core  # noqa: E402,F401
import mg_batch  # noqa: E402,F401

# 慢测试门控：真跑 ffmpeg/子进程的类默认跳过；SLOW=1 全跑（提交前必须 SLOW=1 过一遍）
_slow = not os.environ.get("SLOW")



class TestFirstRunTts(unittest.TestCase):
    """首次运行（lines 目录无既有录音）必须走 TTS 合成。

    v4.7.7 修 P0：`f = None`（找不到录音）后直接 `f.exists()`
    → AttributeError，且崩在**调用 TTS 之前**（连合成请求都没发出去）。
    `--skip-tts` 分支有 `f is None` 判空、非 skip_tts 分支没有 = 两处语义不一致。"""

    def test_missing_recording_reaches_tts(self):
        import vo_build as vb

        class _ReachedTts(Exception):
            pass

        calls = []

        def fake_tts(*a, **k):
            calls.append(a)
            raise _ReachedTts()      # 在真调 TTS 前截断，断言"确实走到了合成这一步"

        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vo = d / "vo_lines_at.json"
        vo.write_text(json.dumps({"acts": [], "lines": [
            {"id": "L1", "text": "第一句", "at": 0.0}]}, ensure_ascii=False),
            encoding="utf-8")
        argv = ["vo_build.py", str(vo), "--out", str(d / "vo.m4a")]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(vb, "tts_line", side_effect=fake_tts):
            with self.assertRaises(_ReachedTts):
                vb.main()
        self.assertEqual(len(calls), 1,
                         "无既有录音却没走到 TTS 合成（f=None 崩在判空前，属 P0 回归）")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestVoBuildPerLine(unittest.TestCase):
    """方案A：vo_build 逐句声音属性——voice/emotion/speed 透传到 media_gen tts 命令。"""

    def test_tts_line_passes_voice_emotion_speed(self):
        import vo_build as vb
        out = Path(tempfile.mkdtemp()) / "L01.mp3"
        with mock.patch.object(vb.subprocess, "call", return_value=0) as call:
            vb.tts_line("文本", out, "音色A", 0.9, "激昂")
        argv = call.call_args[0][0]
        idx = argv.index("--voice"); self.assertEqual(argv[idx + 1], "音色A")
        idx = argv.index("--emotion"); self.assertEqual(argv[idx + 1], "激昂")
        idx = argv.index("--speed"); self.assertEqual(argv[idx + 1], "0.9")

    def test_tts_line_omits_defaults(self):
        """无 emotion/默认 speed 时不传多余参数（保持旧命令形状）。"""
        import vo_build as vb
        out = Path(tempfile.mkdtemp()) / "L02.mp3"
        with mock.patch.object(vb.subprocess, "call", return_value=0) as call:
            vb.tts_line("文本", out, "", 1.0, "")
        argv = call.call_args[0][0]
        self.assertNotIn("--voice", argv)
        self.assertNotIn("--emotion", argv)
        self.assertNotIn("--speed", argv)


class TestVoFit(unittest.TestCase):
    """A1' VO-镜头对账：vo_build fit 子命令的纯函数 fit_report。

    seam：fit_report(vo_lines_dict, clip_durs: dict[shot_id->秒], total) -> list[dict]
    输出每镜对账行：shot/vo 段数/vo 需求时长(含 gap)/clip 时长/差值/verdict/建议。
    """

    def _lines(self):
        return {"lines": [
            {"id": "L01", "shot": "S01", "at": 0.8, "text": "a"},
            {"id": "L02", "shot": "S01", "at": 2.0, "text": "b"},
            {"id": "L03", "shot": "S02", "at": 4.0, "text": "c"},
        ]}

    def test_fit_report_basic(self):
        import vo_build as vb
        rows = vb.fit_report(self._lines(), {"S01": 3.5, "S02": 2.0})
        by = {r["shot"]: r for r in rows}
        # S01: VO 0.8~3.0（两句）→ 需求约 2.2s+句尾 gap；clip 3.5s 富余
        self.assertEqual(by["S01"]["clip"], 3.5)
        self.assertIn(by["S01"]["verdict"], ("ok", "warn"))
        # S02: VO 4.0 起，句长未知（无 _dur 时用文本估不了）→ 对账按 0 处理应报 ok
        self.assertEqual(by["S02"]["clip"], 2.0)

    def test_fit_report_with_real_durs(self):
        """句级 _dur 已知时精确对账：S02 的 VO 段比 clip 长要报 warn。"""
        import vo_build as vb
        data = self._lines()
        data["lines"][2]["_dur"] = 2.6   # L03 实测 2.6s
        rows = vb.fit_report(data, {"S01": 3.5, "S02": 2.0})
        by = {r["shot"]: r for r in rows}
        self.assertEqual(by["S02"]["verdict"], "warn", "VO 2.6s > clip 2.0s 必须报 warn")
        self.assertIn("建议", by["S02"]["advice"])

    def test_fit_report_missing_clip(self):
        """vo 提到但 clips 缺镜 → 缺失行（verdict=missing），不能静默跳过。"""
        import vo_build as vb
        rows = vb.fit_report(self._lines(), {"S01": 3.5})
        self.assertTrue(any(r["shot"] == "S02" and r["verdict"] == "missing" for r in rows))

    def test_fit_report_clip_without_vo(self):
        """有 clip 无 VO 的镜 → 报 info（不是错误，纯音乐/空镜合法）。"""
        import vo_build as vb
        rows = vb.fit_report(self._lines(), {"S01": 3.5, "S02": 2.0, "S03": 4.0})
        self.assertTrue(any(r["shot"] == "S03" and r["verdict"] == "info" for r in rows))

    def test_fit_report_webp_sentinel(self):
        """静态图片镜（clip_durs 值 -1.0 哨兵）→ info 不 missing：
        webp probe 不到时长不是缺成片（v3.1.16 复盘修复，回归钉死）。"""
        import vo_build as vb
        rows = vb.fit_report(self._lines(), {"S01": -1.0, "S02": -1.0})
        by = {r["shot"]: r for r in rows}
        self.assertEqual(by["S01"]["verdict"], "info", "图片镜不能误报 missing")
        self.assertIsNone(by["S01"]["clip"])
        self.assertEqual(by["S01"]["n_lines"], 2, "有 VO 的图片镜句数照常算")
        self.assertGreater(by["S01"]["vo"], 0, "VO 需求时长给 kenburns --duration 参考")


class TestPick(unittest.TestCase):
    """A3 选优半自动：pick 的纯函数 score_images。
    只排序不拍板——启发式（清晰度/对比度/亮度合理域）给参考，最终人选人做。
    seam：score_images(paths: list[Path]) -> list[dict(path, sharpness, contrast, score)]
    """

    def test_score_images_orders_by_score(self):
        import vo_build  # 确保包上下文可用（不必要但统一 import 风格）
        import postprocess as pp
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            # 两张图：清晰高对比 vs 模糊低对比
            sharp = Image.new("RGB", (64, 64), "white")
            for x in range(0, 64, 4):
                for y in range(64):
                    sharp.putpixel((x, y), (0, 0, 0))
            blurry = Image.new("RGB", (64, 64), (128, 128, 128))
            p1, p2 = Path(td) / "shot_01_1.png", Path(td) / "shot_01_2.png"
            sharp.save(p1); blurry.save(p2)
            rows = pp.score_images([p2, p1])
            self.assertEqual(rows[0]["path"], str(p1), "清晰高对比应排第一")
            self.assertGreater(rows[0]["score"], rows[1]["score"])

    def test_score_images_empty(self):
        import postprocess as pp
        self.assertEqual(pp.score_images([]), [])


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestBgmMix(unittest.TestCase):
    """拓展#1 BGM 床：bgm_filter_chain 纯函数——VO 出现自动压低（闪避）。

    seam：bgm_filter_chain(total: float, duck_db: int = -14) -> str
    返回 ffmpeg filter 片段：BGM 输入循环补齐到 total + 音量基准。
    闪避靠拼接时 sidechaincompress（VO 为 key），此函数只管 BGM 侧准备。
    """

    def test_bgm_chain_basic(self):
        import vo_build as vb
        fc = vb.bgm_filter_chain(60.0)
        self.assertIn("aloop", fc, "BGM 短于成片必须循环")
        self.assertIn("atrim=0:60.000", fc, "必须裁到成片总长")

    def test_bgm_chain_duck_volume(self):
        import vo_build as vb
        fc = vb.bgm_filter_chain(30.0, duck_db=-10)
        self.assertIn("volume=-10dB", fc, "基础音量=闪避目标电平，非闪避时段也保持低配")

    def test_bgm_assembly_order(self):
        """BGM 闪避链结构：sidechain 主输入必须是 BGM（被压方），VO 只当 key；
        且 VO 必须进最终 amix（只输出 BGM 是 bug——复盘中抓到过）。"""
        import vo_build as vb
        segs = vb.bgm_assembly(["[base]", "[l1]"], 2, 60.0, -14)
        joined = ";".join(segs)
        self.assertIn("[bgmprep][vokey]sidechaincompress", joined,
                      "BGM 在前=被压缩方，顺序不能反")
        self.assertIn("[voout][ducked]amix", joined, "VO 必须与压过的 BGM 混进成片")
        self.assertIn("asplit=2[voout][vokey]", joined, "VO 要 split 成出片+触发两路")

    def test_bgm_end_to_end_audio_has_vo(self):
        """内容级验证：BGM 版输出的音频里，旁白时段（1.0~2.5s，440Hz）必须有能量。
        lavfi 造句音频+BGM，跑真实 vo_build 子进程，再 ffmpeg 分段测音量。"""
        import subprocess as sp
        from PIL import Image  # noqa: F401（确保 PIL 可用环境一致）
        import ffmpeg_probe as fpx
        ff = fpx.find_ffmpeg()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "lines").mkdir()
            # L01 @0.5s 440Hz 2s；BGM 220Hz 4s
            sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", "-ar", "48000",
                    str(td / "lines" / "L01.m4a")], check=True)
            sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=4", "-ar", "48000",
                    str(td / "bgm.m4a")], check=True)
            (td / "vo_lines.json").write_text(json.dumps(
                {"acts": [], "lines": [{"id": "L01", "shot": "S01", "at": 0.5, "text": "x"}]}),
                encoding="utf-8")
            vo_build_py = Path(__file__).resolve().parents[1] / "scripts" / "vo_build.py"
            r = sp.run([sys.executable, str(vo_build_py),
                        str(td / "vo_lines.json"), "--out", str(td / "vo.m4a"),
                        "--total", "4.0", "--skip-tts", "--bgm", str(td / "bgm.m4a")],
                       capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONUTF8": "1"})
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            out = td / "vo.m4a"
            self.assertTrue(out.exists() and out.stat().st_size > 1000)

            def seg_mean(start: str, dur: str) -> float:
                """分段均方根电平（dB）。注意 -ss 必须在 -i 前（输入侧 seek）：
                放在 -i 后是输出侧 seek，ffmpeg 只在 mux 层丢帧，volumedetect
                仍吃全量样本，测出的是整文件均值——静默段会假响（实测差 67dB）。"""
                q = sp.run([ff, "-ss", start, "-t", dur, "-i", str(out),
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
                import re as _re
                m = _re.search(r"mean_volume:\s*(-?[\d.]+) dB", q.stderr)
                self.assertIsNotNone(m, q.stderr[-300:])
                return float(m.group(1))

            # VO 窗口（1.0~2.5s，440Hz 旁白+BGM）必须显著高于纯 BGM 段（3.0~3.9s）。
            # 只测绝对电平会被"输出里只剩 BGM"骗过——BGM 本身也是 -14dB 级的信号。
            vo_seg, bgm_only = seg_mean("1.0", "1.5"), seg_mean("3.0", "0.9")
            self.assertGreater(vo_seg - bgm_only, 6.0,
                               f"VO 时段 {vo_seg}dB vs 纯BGM段 {bgm_only}dB，差 <6dB"
                               "——旁白没混进成片（v3.1.16 复盘 bug 复发）")


class TestTtsFailover(unittest.TestCase):
    """方案A：TTS 多 key 自动 failover——key#1 挂 → key#2 顶（base/model/voice 全切）；全挂 die(3)。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.out = Path(self._td.name) / "v.m4a"

    def tearDown(self):
        self._td.cleanup()

    def _env(self):
        return {
            "MEDIA_TTS_1_KEY": "k1", "MEDIA_TTS_1_BASE": "https://alpha/v1",
            "MEDIA_TTS_1_MODEL": "cosy", "MEDIA_TTS_1_VOICE": "v-alex",
            "MEDIA_TTS_2_KEY": "k2", "MEDIA_TTS_2_BASE": "https://beta/v1",
            "MEDIA_TTS_2_MODEL": "tts-1", "MEDIA_TTS_2_VOICE": "zh-CN-XiaoxiaoNeural",
        }

    def _fake_urlopen(self, reqs):
        """key#1 请求抛 OSError，key#2 返回音频；顺带记录请求 URL 与 body。"""
        def fake(req, timeout=300):
            reqs.append((req.full_url, json.loads(req.data)))
            if "alpha" in req.full_url:
                raise OSError("conn refused")
            class Resp:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return b"audio-data" + b"x" * 118
            return Resp()
        return fake

    def test_failover_second_key_with_own_voice(self):
        """降级必须连音色一起切：key#2 用 MEDIA_TTS_2_VOICE（Edge 兜底自动换女声）。"""
        import argparse
        reqs = []
        ns = argparse.Namespace(text="你好世界", text_file="", out=str(self.out),
                                voice="", emotion="", speed=1.0)
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with mock.patch.object(mg.urllib.request, "urlopen",
                                   side_effect=self._fake_urlopen(reqs)):
                mg.cmd_tts(ns)
        self.assertGreaterEqual(len(reqs), 2, "key#1 失败必须试 key#2")
        self.assertEqual(reqs[0][0], "https://alpha/v1/audio/speech")
        self.assertEqual(reqs[1][0], "https://beta/v1/audio/speech")
        body2 = reqs[1][1]
        self.assertEqual(body2["model"], "tts-1")
        self.assertEqual(body2["voice"], "zh-CN-XiaoxiaoNeural", "降级音色必须跟随 key")
        self.assertEqual(self.out.read_bytes(), b"audio-data" + b"x" * 118)

    def test_all_fail_dies_3(self):
        import argparse
        reqs = []
        ns = argparse.Namespace(text="x", text_file="", out=str(self.out),
                                voice="", emotion="", speed=1.0)

        def all_fail(req, timeout=300):
            reqs.append(req.full_url)
            raise OSError("down")
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with mock.patch.object(mg.urllib.request, "urlopen", side_effect=all_fail):
                with self.assertRaises(SystemExit) as cm:
                    mg.cmd_tts(ns)
        self.assertEqual(cm.exception.code, 3, "TTS 全挂必须显式退出，不能静默")

    def test_emotion_prefix_plain_function(self):
        self.assertEqual(mg._tts_emotion_prefix("高兴"), "你能用高兴的情感说吗，")
        self.assertEqual(mg._tts_emotion_prefix(""), "")
        self.assertEqual(mg._tts_emotion_prefix("  激昂  "), "你能用激昂的情感说吗，")

    def test_emotion_written_into_input_text(self):
        """情感引导拼进 input（CosyVoice 系官方玩法），不是独立字段。"""
        import argparse
        reqs = []
        ns = argparse.Namespace(text="这是高潮", text_file="", out=str(self.out),
                                voice="", emotion="激昂", speed=1.0)
        with mock.patch.dict(os.environ, self._env(), clear=False):
            with mock.patch.object(mg.urllib.request, "urlopen",
                                   side_effect=self._fake_urlopen(reqs)):
                mg.cmd_tts(ns)
        self.assertTrue(
            reqs[1][1]["input"].startswith("你能用激昂的情感说吗，"),
            "input 必须含情感引导")


class TestAudioTriageDecision(unittest.TestCase):
    """方案B：听诊决策纯函数——机器只粗筛，拍板留人。
    无音轨→yes / 中文人声→no / 英文→yes / 空转录→listen。"""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.at = importlib.import_module("audio_triage")

    def test_no_audio_suggests_voice(self):
        d = self.at.triage_decision(has_audio=False)
        self.assertEqual(d["verdict"], "yes")

    def test_zh_speech_no_voice_needed(self):
        d = self.at.triage_decision(has_audio=True, transcript="晨雾散开，湖面醒了过来。")
        self.assertEqual(d["verdict"], "no", "自带完整中文人声不用补朗读")

    def test_english_speech_suggests_voice(self):
        d = self.at.triage_decision(has_audio=True,
                                    transcript="the morning mist clears over the lake")
        self.assertEqual(d["verdict"], "yes", "非中文人声建议补中文旁白")

    def test_empty_transcript_listen(self):
        d = self.at.triage_decision(has_audio=True, transcript="")
        self.assertEqual(d["verdict"], "listen", "空转录=纯音乐/环境音/噪声，机器不硬判给人听")

    def test_zh_short_fragment_listen(self):
        """只转出一个词（如背景中文广播）不算完整人声——交人听。"""
        d = self.at.triage_decision(has_audio=True, transcript="好")
        self.assertEqual(d["verdict"], "listen", "单字不成人声覆盖，别误判 native")

    def test_verdict_reason_present(self):
        d = self.at.triage_decision(has_audio=False)
        self.assertIn("reason", d)


class TestAudioProfile(unittest.TestCase):
    """方案B：audio_profiles.json 读写（同 watermark/caps 档案模式：实测一次回写）。"""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.at = importlib.import_module("audio_triage")

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        real = self.at.PROFILE_FILE
        self.at.PROFILE_FILE = Path(self._td.name) / "audio_profiles.json"
        self.real = real

    def tearDown(self):
        self.at.PROFILE_FILE = self.real
        self._td.cleanup()

    def test_record_and_load(self):
        self.at.record_profile("model-x", {"verdict": "no", "has_audio": True, "detail": "中文人声"})
        p = self.at.load_profile("model-x")
        self.assertEqual(p["verdict"], "no")

    def test_unknown_pool_empty(self):
        self.assertEqual(self.at.load_profile("nonexistent"), {})


class TestPlanAxis(unittest.TestCase):
    """#6 声音链反向：plan_axis 纯函数——VO 先行，TTS 真实时长反推每镜需求。

    与 fit_report 互为镜像：fit 是"镜定时长 → VO 适配"（对账），
    plan 是"VO 定时长 → 反推镜该多长"（生成 kenburns/出片计划）。
    """

    def test_sequential_at_with_gap(self):
        import vo_build as vb
        items = [
            {"id": "L01", "text": "甲", "dur": 2.0, "shot": "S01"},
            {"id": "L02", "text": "乙", "dur": 3.0, "shot": "S02"},
        ]
        out = vb.plan_axis(items, gap=0.5, pad=0.8)
        self.assertEqual(out["lines"][0]["at"], 0.0)
        self.assertAlmostEqual(out["lines"][1]["at"], 2.5)   # 2.0 + 0.5

    def test_shot_need_aggregates(self):
        """同镜两句：镜需求 = 末句 at+dur − 首句 at + pad。"""
        import vo_build as vb
        items = [
            {"id": "L01", "text": "甲", "dur": 2.0, "shot": "S01"},
            {"id": "L02", "text": "乙", "dur": 1.0, "shot": "S01"},
            {"id": "L03", "text": "丙", "dur": 3.0, "shot": "S02"},
        ]
        out = vb.plan_axis(items, gap=0.5, pad=0.8)
        # S01: L01@0 + 2.0，L02@2.5 + 1.0 → 首句 0 末句 3.5，+0.8 pad = 4.3
        self.assertAlmostEqual(out["shots"]["S01"]["need"], 4.3)
        # S02: L03@4.0 + 3.0 → 需求 3.0 + 0.8 = 3.8
        self.assertAlmostEqual(out["shots"]["S02"]["need"], 3.8)
        self.assertEqual(out["shots"]["S01"]["n_lines"], 2)

    def test_line_without_shot_only_axes(self):
        """无 shot 字段的句子照常排轴（留白/转场旁白），不进镜聚合也不炸。"""
        import vo_build as vb
        items = [
            {"id": "L01", "text": "甲", "dur": 2.0, "shot": "S01"},
            {"id": "L02", "text": "乙", "dur": 1.0},          # 无 shot
            {"id": "L03", "text": "丙", "dur": 2.0, "shot": "S02"},
        ]
        out = vb.plan_axis(items, gap=0.5, pad=0.8)
        self.assertAlmostEqual(out["lines"][2]["at"], 4.0)    # 2.0+0.5+1.0+0.5
        self.assertEqual(set(out["shots"]), {"S01", "S02"})

    def test_total_for_bgm_and_mix(self):
        """末句收尾 + pad 给出成片参考总长（混音/BGM 用）。"""
        import vo_build as vb
        items = [
            {"id": "L01", "text": "甲", "dur": 2.0, "shot": "S01"},
            {"id": "L02", "text": "乙", "dur": 3.0, "shot": "S02"},
        ]
        out = vb.plan_axis(items, gap=0.5, pad=0.8)
        # 末句 at 2.5 + dur 3.0 = 5.5，+pad 0.8 → 总 6.3
        self.assertAlmostEqual(out["total"], 6.3)


class TestPlanAxisWords(unittest.TestCase):
    """#7 词级接线 A：plan_axis 把**相对句首**的词轴转成成片绝对时间。

    word_axis 的词 at 以该句录音自身为原点（0 = 句首）；字幕要的是成片绝对时间。
    转换必须发生在排轴处——那是唯一知道 line.at 的地方。越界必须**可观测**：
    whisper 的锚点插值有可能吐出略超句长的末词，静默放行会让字幕跑出句外。
    """

    def test_words_shifted_to_absolute(self):
        import vo_build as vb
        items = [
            {"id": "L01", "text": "甲乙丙", "dur": 2.0, "shot": "S01",
             "words": [{"w": "甲", "at": 0.0, "dur": 0.6},
                       {"w": "乙", "at": 0.7, "dur": 0.6},
                       {"w": "丙", "at": 1.4, "dur": 0.6}]},
            {"id": "L02", "text": "丁", "dur": 1.0, "shot": "S01",
             "words": [{"w": "丁", "at": 0.1, "dur": 0.5}]},
        ]
        out = vb.plan_axis(items, gap=0.5, pad=0.8)
        w0 = out["lines"][0]["words"]
        self.assertAlmostEqual(w0[0]["at"], 0.0)
        self.assertAlmostEqual(w0[2]["at"], 1.4)
        # L02 at = 2.0 + 0.5 = 2.5 → 句内 0.1 变 2.6
        self.assertAlmostEqual(out["lines"][1]["words"][0]["at"], 2.6)

    def test_no_words_key_when_absent(self):
        """无词轴时输出与旧版一致（不凭空造 words 键）。"""
        import vo_build as vb
        out = vb.plan_axis([{"id": "L01", "text": "甲", "dur": 1.0, "shot": "S01"}])
        self.assertNotIn("words", out["lines"][0])

    def test_words_clamped_inside_line_and_counted(self):
        """词落在句外 → 夹回句内，且必须在 word_stats 里计数（不许静默夹）。"""
        import vo_build as vb
        items = [{"id": "L01", "text": "甲乙", "dur": 1.0, "shot": "S01",
                  "words": [{"w": "甲", "at": 0.2, "dur": 0.4},
                            {"w": "乙", "at": 3.5, "dur": 0.4}]}]
        out = vb.plan_axis(items)
        ws = out["lines"][0]["words"]
        self.assertAlmostEqual(ws[1]["at"], 1.0, msg="越界词必须夹到句尾")
        self.assertGreaterEqual(ws[1]["dur"], 0.02)
        self.assertEqual(out["word_stats"]["clamped"], 1)

    def test_word_stats_counts(self):
        import vo_build as vb
        items = [
            {"id": "L01", "text": "甲", "dur": 1.0, "shot": "S01",
             "words": [{"w": "甲", "at": 0.1, "dur": 0.3}]},
            {"id": "L02", "text": "乙", "dur": 1.0, "shot": "S02"},
        ]
        out = vb.plan_axis(items)
        self.assertEqual(out["word_stats"], {"lines": 1, "words": 1, "clamped": 0})

    def test_blank_and_malformed_words_ignored(self):
        """空词/非 dict 的词轴条目丢弃（whisper 偶有空 token）；不炸也不占位。"""
        import vo_build as vb
        items = [{"id": "L01", "text": "甲", "dur": 1.0, "shot": "S01",
                  "words": [{"w": "  ", "at": 0.1, "dur": 0.3},
                            "垃圾",
                            {"w": "甲", "at": 0.4, "dur": 0.3}]}]
        out = vb.plan_axis(items)
        self.assertEqual([w["w"] for w in out["lines"][0]["words"]], ["甲"])


class TestSplitLineCues(unittest.TestCase):
    """#7 词级接线 B：字幕按**词边界**切行（不是按字符硬切）。

    句级字幕在长句时是一大坨，只能靠 drawtext 自动折行；有词轴就能在词之间断行——
    既不切断词，也不超 max_chars / max_dur。无词轴时保持旧行为（整句一条）。
    """

    def _line(self, **kw):
        d = {"id": "L01", "text": "甲乙丙丁戊己庚辛壬癸", "at": 2.0, "dur": 5.0}
        d.update(kw)
        return d

    def _cj_words(self, text, step=0.4, dur_ratio=0.9, base=2.0):
        """词轴按**成片绝对时间**给——与 vo_lines_at.json 的真实产出一致
        （plan_axis 已把相对句首的词转成绝对；split_line_cues 收到的就是绝对的）。"""
        return [{"w": c, "at": base + i * step, "dur": step * dur_ratio}
                for i, c in enumerate(text)]

    def test_without_words_single_cue(self):
        import vo_build as vb
        cues = vb.split_line_cues(self._line())
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0]["at"], 2.0)
        self.assertAlmostEqual(cues[0]["dur"], 5.0, msg="无词轴时 dur 与旧版一致，不抬升")
        self.assertEqual(cues[0]["text"], "甲乙丙丁戊己庚辛壬癸")

    def test_punctuation_preserved_via_original_text(self):
        """词轴不含标点，但字幕文本要用**原句**：按词定位后从原文取片段。"""
        import vo_build as vb
        line = self._line(text="And so my, fellow", dur=4.0,
                          words=[{"w": "And", "at": 2.0, "dur": 0.4},
                                 {"w": "so", "at": 2.5, "dur": 0.3},
                                 {"w": "my", "at": 2.9, "dur": 0.3},
                                 {"w": "fellow", "at": 3.3, "dur": 0.6}])
        cues = vb.split_line_cues(line, max_width=9, max_dur=99.0, min_dur=0.1)
        self.assertEqual([c["text"] for c in cues], ["And so my,", "fellow"],
                         "逗号必须保留（不能被词拼接吞掉）")

    def test_falls_back_when_words_do_not_match_text(self):
        """转写与输入文本对不上（换词/换语言）→ 回退词拼接，不丢词也不崩。"""
        import vo_build as vb
        line = self._line(text="完全不同的原文", dur=3.0,
                          words=[{"w": "alpha", "at": 2.0, "dur": 0.5},
                                 {"w": "beta", "at": 2.6, "dur": 0.5}])
        cues = vb.split_line_cues(line, max_width=999, min_dur=0.1)
        self.assertEqual([c["text"] for c in cues], ["alpha beta"])

    def test_splits_on_max_width_without_breaking_words(self):
        import vo_build as vb
        line = self._line(text="甲乙丙丁戊己", words=self._cj_words("甲乙丙丁戊己"))
        cues = vb.split_line_cues(line, max_width=6, max_dur=99.0, min_dur=0.1)
        self.assertEqual([c["text"] for c in cues], ["甲乙丙", "丁戊己"],
                         "必须按词边界切，不切断词")
        self.assertAlmostEqual(cues[0]["at"], 2.0)
        self.assertAlmostEqual(cues[1]["at"], 2.0 + 3 * 0.4)

    def test_splits_on_max_dur(self):
        import vo_build as vb
        line = self._line(text="甲乙丙丁",
                          words=[{"w": c, "at": i * 1.0, "dur": 0.9}
                                 for i, c in enumerate("甲乙丙丁")])
        cues = vb.split_line_cues(line, max_width=999, max_dur=2.0, min_dur=0.1)
        self.assertEqual([c["text"] for c in cues], ["甲乙", "丙丁"])

    def test_short_tail_merged_into_previous(self):
        """尾片过短（< min_dur）并回前一条——避免一闪而过的字幕。"""
        import vo_build as vb
        line = self._line(text="甲乙丙",
                          words=[{"w": c, "at": i * 1.0, "dur": 0.9}
                                 for i, c in enumerate("甲乙丙")])
        cues = vb.split_line_cues(line, max_width=4, max_dur=99.0, min_dur=1.5)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "甲乙丙")

    def test_english_words_joined_with_space(self):
        """拉丁词之间必须留空格（否则字幕变成 Andsomy）。"""
        import vo_build as vb
        line = self._line(text="And so my fellow", dur=4.0,
                          words=[{"w": "And", "at": 0.0, "dur": 0.4},
                                 {"w": "so", "at": 0.5, "dur": 0.3},
                                 {"w": "my", "at": 0.9, "dur": 0.3},
                                 {"w": "fellow", "at": 1.3, "dur": 0.6}])
        cues = vb.split_line_cues(line, max_width=9, max_dur=99.0, min_dur=0.1)
        self.assertEqual([c["text"] for c in cues], ["And so my", "fellow"])

    def test_cue_keeps_absolute_word_times(self):
        """每条 cue 带自己的词（绝对时间）——卡拉OK/逐词高亮的接缝。"""
        import vo_build as vb
        line = self._line(text="甲乙", words=self._cj_words("甲乙"))
        cues = vb.split_line_cues(line, max_width=2, min_dur=0.01)
        self.assertEqual([w["w"] for w in cues[0]["words"]], ["甲"])
        self.assertAlmostEqual(cues[0]["words"][0]["at"], 2.0)

    def test_cues_cover_line_and_stay_inside(self):
        """不变量：切开后拼回 == 原句；首条起点 == 句起点；末条不越出句尾。"""
        import vo_build as vb
        line = self._line(text="甲乙丙丁戊", dur=3.0, words=self._cj_words("甲乙丙丁戊", step=0.6))
        cues = vb.split_line_cues(line, max_width=4, max_dur=99.0, min_dur=0.01)
        self.assertAlmostEqual(cues[0]["at"], 2.0)
        last = cues[-1]
        self.assertLessEqual(round(last["at"] + last["dur"], 3), round(5.0 + 0.001, 3))
        self.assertEqual("".join(c["text"] for c in cues), "甲乙丙丁戊")

    def test_cue_dur_never_zero(self):
        """退化词轴（dur 全 0）不许产出 0 长字幕（drawtext 会直接不显示）。"""
        import vo_build as vb
        line = self._line(text="甲", dur=0.0, words=[{"w": "甲", "at": 0.0, "dur": 0.0}])
        cues = vb.split_line_cues(line)
        self.assertGreaterEqual(cues[0]["dur"], 0.02)


class TestPlanWordsWiring(unittest.TestCase):
    """#7 词级接线 C：`plan --words` 把词轴真写进 vo_lines_at.json（内容级，不只看 rc）。"""

    def _args(self, **kw):
        import argparse
        d = dict(lines="", out="", dir="", gap=0.5, pad=0.8, skip_tts=True,
                 voice="", speed=1.0, words=True, word_lang="", word_models_dir="")
        d.update(kw)
        return argparse.Namespace(**d)

    def test_words_persisted_into_vo_lines_at(self):
        import tempfile
        from pathlib import Path as P
        from unittest import mock as mk
        import vo_build as vb
        import word_axis
        with tempfile.TemporaryDirectory() as td:
            td = P(td)
            src = td / "vo_lines.json"
            src.write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲乙", "shot": "S01"}]}), encoding="utf-8")
            fake = td / "L01.m4a"
            fake.write_bytes(b"x")
            with mk.patch.object(vb, "_existing_recording", return_value=fake), \
                 mk.patch.object(vb, "probe", return_value=2.0), \
                 mk.patch.object(word_axis, "transcribe",
                                 return_value={"backend": "whisper", "text": "甲乙",
                                               "words": [{"w": "甲", "at": 0.0, "dur": 0.4},
                                                         {"w": "乙", "at": 0.5, "dur": 0.4}],
                                               "duration": 2.0}):
                with self.assertRaises(SystemExit) as cm:
                    vb.cmd_plan(self._args(lines=str(src), out=str(td / "plan.json")))
            self.assertEqual(cm.exception.code, 0)
            back = json.loads((td / "vo_lines_at.json").read_text(encoding="utf-8"))
            words = back["lines"][0].get("words")
            self.assertTrue(words, "词轴必须落进 vo_lines_at.json（否则字幕仍是句级）")
            self.assertAlmostEqual(words[1]["at"], 0.5)
            plan = json.loads((td / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["word_stats"]["words"], 2)

    def test_words_without_model_dies_with_hint(self):
        """显式要词轴而运行时不具备 → rc=2 + 安装指引（**不静默**给句级轴）。"""
        import contextlib
        import io
        import tempfile
        from pathlib import Path as P
        from unittest import mock as mk
        import vo_build as vb
        with tempfile.TemporaryDirectory() as td:
            td = P(td)
            src = td / "vo_lines.json"
            src.write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲", "shot": "S01"}]}), encoding="utf-8")
            fake = td / "L01.m4a"
            fake.write_bytes(b"x")
            empty = td / "empty_models"
            empty.mkdir()
            err = io.StringIO()
            with mk.patch.object(vb, "_existing_recording", return_value=fake), \
                 mk.patch.object(vb, "probe", return_value=2.0), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    vb.cmd_plan(self._args(lines=str(src), out=str(td / "plan.json"),
                                           word_models_dir=str(empty)))
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("whisper", err.getvalue().lower(),
                          "必须给出词轴模型/依赖的安装指引")


class TestPlanPunctWiring(unittest.TestCase):
    """v4.19：--punct 接进 plan——标点挂回词目 + whisper 静默忽略必须拦截。"""

    def _args(self, **kw):
        import argparse
        d = dict(lines="", out="", dir="", gap=0.5, pad=0.8, skip_tts=True,
                 voice="", speed=1.0, words=False, punct=False, word_lang="",
                 word_backend="", word_models_dir="")
        d.update(kw)
        return argparse.Namespace(**d)

    def _run_plan(self, tmp, transcribe_ret):
        """公共脚手架：1 句稿 + 假录音 + mock 掉 transcribe，返回 (vo_lines_at, plan, err)。"""
        import contextlib
        import io
        import tempfile
        from pathlib import Path as P
        from unittest import mock as mk
        import vo_build as vb
        import word_axis
        with tempfile.TemporaryDirectory() as td:
            td = P(td)
            src = td / "vo_lines.json"
            src.write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲乙", "shot": "S01"}]}), encoding="utf-8")
            fake = td / "L01.m4a"
            fake.write_bytes(b"x")
            err = io.StringIO()
            with mk.patch.object(vb, "_existing_recording", return_value=fake), \
                 mk.patch.object(vb, "probe", return_value=2.0), \
                 mk.patch.object(word_axis, "transcribe",
                                 return_value=transcribe_ret), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    vb.cmd_plan(self._args(lines=str(src), out=str(td / "plan.json"),
                                           **(tmp or {})))
            self.assertEqual(cm.exception.code, 0)
            back = json.loads((td / "vo_lines_at.json").read_text(encoding="utf-8"))
            plan = json.loads((td / "plan.json").read_text(encoding="utf-8"))
            return back, plan, err.getvalue()

    def test_punct_persisted_into_words(self):
        """--punct 生效时：词目带标点写进 vo_lines_at.json（内容级验证）。"""
        back, plan, _ = self._run_plan(
            {"punct": True}, {"backend": "sherpa", "punct": "sherpa", "text": "甲乙。",
                              "words": [{"w": "甲", "at": 0.0, "dur": 0.4},
                                        {"w": "乙。", "at": 0.5, "dur": 0.4}],
                              "duration": 2.0})
        words = back["lines"][0]["words"]
        self.assertEqual([w["w"] for w in words], ["甲", "乙。"],
                         "标点必须挂回词目（否则下游 _cue_text 拿不到带标点文本）")
        self.assertEqual(plan["word_stats"]["words"], 2)

    def test_punct_implies_words(self):
        """--punct 不带 --words 也要跑词轴（标点的挂点是词目，隐含要求）。"""
        called = []

        def fake_transcribe(rec, **kw):
            called.append(kw)
            return {"backend": "sherpa", "punct": "sherpa", "text": "甲乙。",
                    "words": [{"w": "甲", "at": 0.0, "dur": 0.4},
                              {"w": "乙。", "at": 0.5, "dur": 0.4}], "duration": 2.0}

        import tempfile
        from pathlib import Path as P
        from unittest import mock as mk
        import vo_build as vb
        import word_axis
        with tempfile.TemporaryDirectory() as td:
            td = P(td)
            src = td / "vo_lines.json"
            src.write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲乙", "shot": "S01"}]}), encoding="utf-8")
            fake = td / "L01.m4a"
            fake.write_bytes(b"x")
            with mk.patch.object(vb, "_existing_recording", return_value=fake), \
                 mk.patch.object(vb, "probe", return_value=2.0), \
                 mk.patch.object(word_axis, "transcribe", side_effect=fake_transcribe):
                with self.assertRaises(SystemExit) as cm:
                    vb.cmd_plan(self._args(lines=str(src), out=str(td / "plan.json"),
                                           punct=True))
            self.assertEqual(cm.exception.code, 0)
            self.assertTrue(called, "--punct 必须触发词轴（隐含 --words）")
            self.assertTrue(all(kw.get("punctuate") for kw in called),
                            "punctuate=True 必须传给 transcribe")

    def test_punct_on_whisper_dies_loudly(self):
        """whisper 后端静默忽略 punctuate → 必须 die(2)（静默失败主形态防线）。"""
        import contextlib
        import io
        import tempfile
        from pathlib import Path as P
        from unittest import mock as mk
        import vo_build as vb
        import word_axis
        with tempfile.TemporaryDirectory() as td:
            td = P(td)
            src = td / "vo_lines.json"
            src.write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲乙", "shot": "S01"}]}), encoding="utf-8")
            fake = td / "L01.m4a"
            fake.write_bytes(b"x")
            err = io.StringIO()
            with mk.patch.object(vb, "_existing_recording", return_value=fake), \
                 mk.patch.object(vb, "probe", return_value=2.0), \
                 mk.patch.object(word_axis, "transcribe",
                                 return_value={"backend": "whisper", "text": "甲乙",
                                               "words": [{"w": "甲", "at": 0.0, "dur": 0.4},
                                                         {"w": "乙", "at": 0.5, "dur": 0.4}],
                                               "duration": 2.0}), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    vb.cmd_plan(self._args(lines=str(src), out=str(td / "plan.json"),
                                           punct=True))
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("标点", err.getvalue(), "错误信息必须点明标点未生效")
            self.assertIn("sherpa", err.getvalue(), "必须指引切到 sherpa 后端")


class TestPlanWordsWiring2(unittest.TestCase):
    """v4.13 词级接线 C 的补充（归 TestPlanWordsWiring 语义，独立类避免插入错位）。"""

    def _args(self, **kw):
        import argparse
        d = dict(lines="", out="", dir="", gap=0.5, pad=0.8, skip_tts=True,
                 voice="", speed=1.0, words=True, word_lang="", word_backend="",
                 word_models_dir="")
        d.update(kw)
        return argparse.Namespace(**d)

    def test_words_auto_backend_error_passthrough(self):
        """auto 但两个后端都不可用 → transcribe 不抛、只带回 error 字段；
        vo_build 必须把安装指引**递出去**（否则只剩"一句词轴都没拿到"，排查抓瞎）。"""
        import contextlib
        import io
        import tempfile
        from pathlib import Path as P
        from unittest import mock as mk
        import vo_build as vb
        import word_axis
        with tempfile.TemporaryDirectory() as td:
            td = P(td)
            src = td / "vo_lines.json"
            src.write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲", "shot": "S01"}]}), encoding="utf-8")
            fake = td / "L01.m4a"
            fake.write_bytes(b"x")
            err = io.StringIO()
            with mk.patch.object(vb, "_existing_recording", return_value=fake), \
                 mk.patch.object(vb, "probe", return_value=2.0), \
                 mk.patch.object(word_axis, "transcribe",
                                 return_value={"backend": "", "text": "", "words": [],
                                               "duration": 2.0,
                                               "error": "安装指引哨兵XYZ"}), \
                 contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    vb.cmd_plan(self._args(lines=str(src), out=str(td / "plan.json")))
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("安装指引哨兵XYZ", err.getvalue(),
                          "error 里的安装指引必须原样递给用户")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestPlanEndToEnd(unittest.TestCase):
    """#6 plan 端到端：造两段假录音 → --skip-tts 排轴 → 落盘 at/need 正确。"""

    def test_plan_axis_writes_and_aggregates(self):
        import subprocess as sp
        from ffmpeg_probe import find_ffmpeg
        ff = find_ffmpeg()
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "lines").mkdir()
            # L01 2s、L02 1s 同属 S01；L03 3s 属 S02
            for lid, dur in (("L01", 2), ("L02", 1), ("L03", 3)):
                sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={dur}", "-ar", "48000",
                        str(td / "lines" / f"{lid}.m4a")], check=True)
            (td / "vo_lines.json").write_text(json.dumps({"acts": [], "lines": [
                {"id": "L01", "text": "甲", "shot": "S01"},
                {"id": "L02", "text": "乙", "shot": "S01"},
                {"id": "L03", "text": "丙", "shot": "S02"}]}), encoding="utf-8")
            vo_build_py = Path(__file__).resolve().parents[1] / "scripts" / "vo_build.py"
            r = sp.run([sys.executable, str(vo_build_py), "plan",
                        str(td / "vo_lines.json"), "--out", str(td / "plan.json"),
                        "--skip-tts", "--gap", "0.5", "--pad", "0.8"],
                       capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONUTF8": "1"})
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            plan = json.loads((td / "plan.json").read_text(encoding="utf-8"))
            ats = {x["id"]: x["at"] for x in plan["lines"]}
            self.assertAlmostEqual(ats["L01"], 0.0)
            self.assertAlmostEqual(ats["L02"], 2.5)      # 2.0 + 0.5
            self.assertAlmostEqual(ats["L03"], 4.0)      # 2.5 + 1.0 + 0.5
            self.assertAlmostEqual(plan["shots"]["S01"]["need"], 4.3)
            self.assertAlmostEqual(plan["shots"]["S02"]["need"], 3.8)
            # 反向排轴结果必须能喂回正向构建
            at_file = td / "vo_lines_at.json"
            self.assertTrue(at_file.exists())
            back = json.loads(at_file.read_text(encoding="utf-8"))
            self.assertEqual({l["id"]: l["at"] for l in back["lines"]}, ats)




@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestWordCuesEndToEnd(unittest.TestCase):
    """#7 端到端：带词轴的 vo_lines_at.json → subtitles_final.json **必须按词切成多条**。

    防两层假绿：split_line_cues 单测过但 main 没用它（改回句级也照样 rc=0）。
    """

    def test_subtitles_split_on_word_boundaries(self):
        import subprocess as sp
        from ffmpeg_probe import find_ffmpeg
        ff = find_ffmpeg()
        text = "甲乙丙丁戊己庚辛壬癸" * 3          # 30 字，宽 60 > 上限 40 → 必然要切
        words = [{"w": c, "at": round(0.05 + i * 0.09, 3), "dur": 0.08}
                 for i, c in enumerate(text)]
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "lines").mkdir()
            sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=3", "-ar", "48000",
                    str(td / "lines" / "L01.m4a")], check=True)
            (td / "vo_lines_at.json").write_text(json.dumps(
                {"acts": [], "lines": [{"id": "L01", "text": text, "at": 0.0,
                                        "words": words}]}, ensure_ascii=False),
                encoding="utf-8")
            vb = Path(__file__).resolve().parents[1] / "scripts" / "vo_build.py"
            r = sp.run([sys.executable, str(vb), str(td / "vo_lines_at.json"),
                        "--out", str(td / "vo.m4a"), "--skip-tts",
                        "--subs-out", str(td / "subs.json")],
                       capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            subs = json.loads((td / "subs.json").read_text(encoding="utf-8"))
        self.assertGreater(len(subs), 1, "有词轴时字幕必须切成多条（整句一条 = 没接线）")
        self.assertEqual("".join(s["text"] for s in subs), text,
                         "切开后拼回必须等于原句——不切断词")
        for s in subs:
            self.assertIn("words", s, "每条 cue 应带该片词（逐词高亮的接缝）")


class TestMissingAtField(unittest.TestCase):
    """S3：vo_build 正向拼装遇缺 at 字段必须友好报错，不能 KeyError 裸奔。

    v4.7 审计 P1：main() 正向流程 lines[i]["at"] 直下标——手写 vo_lines_at.json
    少个 at 就 KeyError 裸奔 exit 1。校验必须发生在 TTS/录音检查**之前**
    （错误的数据不该开始烧 TTS 额度）。
    """

    def _run(self, data: dict) -> "subprocess.CompletedProcess":
        import subprocess as sp
        vb = Path(__file__).resolve().parents[1] / "scripts" / "vo_build.py"
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "vo_lines_at.json"
            src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return sp.run([sys.executable, str(vb), str(src),
                           "--out", str(Path(td) / "vo.m4a"), "--skip-tts"],
                          capture_output=True, text=True, encoding="utf-8")

    def test_missing_at_dies_before_tts_with_line_id(self):
        r = self._run({"lines": [
            {"id": "L1", "text": "hello", "at": 0.0, "dur": 1.0},
            {"id": "L2", "text": "world", "dur": 1.0}]})        # L2 缺 at
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertIn("L2", r.stderr, "报错必须指认缺字段的行")
        self.assertIn("at", r.stderr)

    def test_valid_lines_pass_validation(self):
        """at 齐全时校验放行（后续因缺录音 die(2)/die(4) 属正常，不是 KeyError）。"""
        r = self._run({"lines": [
            {"id": "L1", "text": "hello", "at": 0.0, "dur": 1.0}]})
        self.assertEqual(r.returncode, 2, r.stderr[-300:])   # 缺录音：die(2)，非 traceback
        self.assertNotIn("KeyError", r.stderr)

if __name__ == "__main__":
    unittest.main(verbosity=2)

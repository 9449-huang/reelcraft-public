# -*- coding: utf-8 -*-
"""postprocess 拼接/QC/水印测试——按模块拆分自 test_media_gen.py。

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



class TestEscapeDrawtext(unittest.TestCase):
    """#11 drawtext 转义：% 进 filter 前必须 \\% 转义。"""

    def test_percent_escaped(self):
        import importlib
        pp = importlib.import_module("postprocess")
        self.assertEqual(pp._escape_drawtext("100% 进度"), "100\\% 进度")

    def test_quote_and_backslash_kept(self):
        import importlib
        pp = importlib.import_module("postprocess")
        self.assertEqual(pp._escape_drawtext("a'b\\c"), "a\\'b\\\\c")


class TestCollectClips(unittest.TestCase):
    """#2/#3 分片收集：自然排序 + .webp 同认。"""

    def _pp(self):
        import importlib
        return importlib.import_module("postprocess")

    def test_natural_order_and_webp_included(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for n in ("S1", "S2", "S10"):
                (d / f"clip_{n}.mp4").write_bytes(b"x")
            (d / "clip_S3.webp").write_bytes(b"x")
            got = [p.name for p in self._pp()._collect_clips(d)]
            self.assertEqual(got, ["clip_S1.mp4", "clip_S2.mp4",
                                   "clip_S3.webp", "clip_S10.mp4"])

    def test_empty_dir_dies(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                self._pp()._collect_clips(Path(td))


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestProbeCodec(unittest.TestCase):
    """#4 一致性签名要含 codec：只比分辨率时 h264 vs vp9 会被误判为可直拼。"""

    def test_codec_parsed(self):
        import importlib
        pp = importlib.import_module("postprocess")
        fake = ("Stream #0:0: Video: h264 (High), yuv420p, 1280x720, 24 fps\n"
                "Stream #0:1: Audio: aac, 48000 Hz\n")
        with mock.patch.object(pp.subprocess, "run",
                               return_value=mock.Mock(stderr=fake)):
            info = pp.probe("dummy.mp4")
        self.assertEqual(info.get("codec"), "h264")
        self.assertEqual(info.get("width"), 1280)


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestQcSeq(unittest.TestCase):
    """优化⑤：跨镜首帧一致性粗检——HSV 直方图相邻 Bhattacharyya 对比（纯函数可测）。"""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.pp = importlib.import_module("postprocess")
        from PIL import Image
        cls.Image = Image
        cls.red = Image.new("RGB", (64, 64), (255, 0, 0))     # H=0 红
        cls.blue = Image.new("RGB", (64, 64), (0, 0, 255))    # H≈240° 蓝
        half = Image.new("RGB", (64, 64), (255, 0, 0))
        for y in range(64):                                    # 左半红右半蓝
            for x in range(32, 64):
                half.putpixel((x, y), (0, 0, 255))
        cls.half = half

    def test_hsv_features_solid_red(self):
        hist, avg_h, s, v = self.pp._hsv_features(self.red)
        nz = [c for c in hist if c > 0]
        self.assertEqual(len(nz), 1, "纯色图直方图应集中在单桶")
        self.assertAlmostEqual(sum(hist), 1.0, places=6, msg="归一化直方图总和=1")
        self.assertLess(avg_h, 10.0, "红色环形均值色相应 ≈0°")
        self.assertAlmostEqual(s, 1.0, places=6)
        self.assertAlmostEqual(v, 1.0, places=6)

    def test_bhattacharyya_bounds(self):
        h_red = self.pp._hsv_features(self.red)[0]
        self.assertAlmostEqual(self.pp._bhattacharyya(h_red, h_red), 1.0, places=6,
                               msg="同分布 → BC=1")
        h_blue = self.pp._hsv_features(self.blue)[0]
        bc = self.pp._bhattacharyya(h_red, h_blue)
        self.assertAlmostEqual(bc, 0.0, places=6, msg="红 vs 蓝 无交集 → BC=0")

    def test_qcseq_decide_warns_on_jump(self):
        """红-红-蓝：前一对 OK，色调跳变对 WARN。"""
        pairs = self.pp.qcseq_decide([("a", self.red), ("b", self.red), ("c", self.blue)])
        self.assertEqual(len(pairs), 2)
        self.assertFalse(pairs[0]["warn"])
        self.assertTrue(pairs[1]["warn"], "红→蓝 色调跳变必须 WARN")
        self.assertEqual((pairs[1]["a"], pairs[1]["b"]), ("b", "c"))

    def test_qcseq_decide_threshold_boundary(self):
        """半红半蓝 vs 纯红：BC=√0.5≈0.707——阈值上下行为分界（BC<阈值才 WARN）。"""
        pairs = self.pp.qcseq_decide([("a", self.red), ("b", self.half)], threshold=0.75)
        self.assertTrue(pairs[0]["warn"], "BC≈0.707 < 0.75 且 ΔH=60°>30° 应 WARN")
        pairs = self.pp.qcseq_decide([("a", self.red), ("b", self.half)], threshold=0.45)
        self.assertFalse(pairs[0]["warn"], "BC≈0.707 ≥ 0.45 不该 WARN")

    def test_qcseq_same_hue_different_value_no_warn(self):
        """回归：正红↔暗红（同色相不同明度）不该 WARN——纯 BC 判据会因 V 桶
        边界抖动得 BC=0 误杀，组合判据（ΔH=0 且 ΔS=0）必须放行。"""
        darkred = self.Image.new("RGB", (64, 64), (139, 0, 0))
        pairs = self.pp.qcseq_decide([("a", self.red), ("b", darkred)])
        self.assertEqual(pairs[0]["dh"], 0.0)
        self.assertFalse(pairs[0]["warn"], "同色相不同明度不是风格跑偏")

    def test_qcseq_color_to_grayscale_warns(self):
        """彩色↔黑白（ΔS≈1）算彩度跳变，应 WARN（防漏报）。"""
        gray = self.Image.new("RGB", (64, 64), (128, 128, 128))
        pairs = self.pp.qcseq_decide([("a", self.red), ("b", gray)])
        self.assertTrue(pairs[0]["warn"], "彩色→黑白是彩度跳变，该提示人眼")

    def test_pipeline_dryrun_includes_qcseq(self):
        """pipeline concat 前默认跑 qcseq（dry-run 下命令出现在 log）。"""
        import subprocess as sp
        d = self.enterContext(tempfile.TemporaryDirectory())
        d = Path(d)
        for n in ("01", "02"):
            (d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}", "t2i_prompt": "x"}), encoding="utf-8")
        (d / "plan.json").write_text(json.dumps({"mode": "stills"}), encoding="utf-8")
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"),
                    str(d), "--dry-run"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("qcseq", r.stdout, "concat 前应默认有 qcseq 粗检命令")

    def test_pipeline_dryrun_no_qcseq_flag(self):
        import subprocess as sp
        d = self.enterContext(tempfile.TemporaryDirectory())
        d = Path(d)
        for n in ("01", "02"):
            (d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}", "t2i_prompt": "x"}), encoding="utf-8")
        (d / "plan.json").write_text(json.dumps({"mode": "stills"}), encoding="utf-8")
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"),
                    str(d), "--dry-run", "--no-qcseq"], capture_output=True, text=True,
                   timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("qcseq", r.stdout, "--no-qcseq 应跳过粗检")


class TestQcseqErrorStops(unittest.TestCase):
    """P2-2 修复：qcseq exit 2（真错误）必须中断 pipeline，只有 exit 1（WARN）放行。"""

    def test_qcseq_exit2_dies(self):
        import argparse
        import pipeline as pp
        d = self.enterContext(tempfile.TemporaryDirectory())
        d = Path(d)
        for n in ("01",):
            (d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S01", "t2i_prompt": "x"}), encoding="utf-8")
        (d / "plan.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        ns = argparse.Namespace(shots=str(d), plan="", stop_after="concat", dry_run=False,
                                workers_image=1, workers_video=1, provider_image="",
                                provider_video="", qcgate=False, qcgate_strict=False,
                                kenburns_dur=5.0, retry_failed=False, no_lint=True,
                                no_qcseq=False, final="", target_res="1280x720", voice="",
                                bgm="", bgm_db=-18.0, subtitles="", slogan="",
                                slogan_position="left", watermark="", watermark_dry_run=False,
                                sound_lines="", sound_voice="", sound_speed=1.0,
                                sound_skip_tts=False, sound_auto_shift=False, sound_gap=0.3)

        def fake_run(cmd, dry, log):
            # 只有 qcseq 调用返回 2（输入错误），其余阶段放行
            if any("qcseq" in str(c) for c in cmd):
                return 2
            return 0

        with mock.patch("pipeline._run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as cm:
                pp.cmd(ns)
        self.assertEqual(cm.exception.code, 2, "qcseq 真错误必须中断，不能当提示放行")

    def test_qcseq_warn_allowed(self):
        import argparse
        import pipeline as pp
        d = self.enterContext(tempfile.TemporaryDirectory())
        d = Path(d)
        for n in ("01",):
            (d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S01", "t2i_prompt": "x"}), encoding="utf-8")
        (d / "plan.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        ns = argparse.Namespace(shots=str(d), plan="", stop_after="concat", dry_run=False,
                                workers_image=1, workers_video=1, provider_image="",
                                provider_video="", qcgate=False, qcgate_strict=False,
                                kenburns_dur=5.0, retry_failed=False, no_lint=True,
                                no_qcseq=False, final="", target_res="1280x720", voice="",
                                bgm="", bgm_db=-18.0, subtitles="", slogan="",
                                slogan_position="left", watermark="", watermark_dry_run=False,
                                sound_lines="", sound_voice="", sound_speed=1.0,
                                sound_skip_tts=False, sound_auto_shift=False, sound_gap=0.3)

        def fake_run(cmd, dry, log):
            if any("qcseq" in str(c) for c in cmd):
                return 1    # WARN：色调跳变提示
            return 0

        with mock.patch("pipeline._run", side_effect=fake_run):
            pp.cmd(ns)      # 不应抛异常（WARN 放行）
        run_log = json.loads((d / "pipeline_run.json").read_text(encoding="utf-8"))
        self.assertEqual(run_log.get("qcseq", {}).get("rc"), 1,
                         "qcseq WARN 结果要落盘供 audit 读")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestXfadePipeline(unittest.TestCase):
    """优化⑨·盲区3：xfade 管线——lavfi 造两段片实跑 concat --xfade，验证转场链路通。
    此前 xfade 只有 plan 透传测试（dry-run 看参数），没有真跑出片验证。"""

    FF = None

    @classmethod
    def setUpClass(cls):
        try:
            import importlib
            fp = importlib.import_module("ffmpeg_probe")
            cls.FF = fp.find_ffmpeg()
        except Exception as e:
            cls.skipReason = f"ffmpeg 不可用: {e}"

    def setUp(self):
        if self.FF is None:
            self.skipTest(self.skipReason)
        self._td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._td.cleanup()

    def test_concat_with_xfade_produces_output(self):
        import subprocess as sp
        d = Path(self._td.name) / "clips"
        d.mkdir()
        for n in ("S1", "S2"):
            p = d / f"clip_{n}.mp4"
            sp.run([self.FF, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24:duration=1",
                    "-c:v", "libx264", "-b:v", "800k", "-pix_fmt", "yuv420p", str(p)],
                   check=True)
        out = Path(self._td.name) / "xfade.mp4"
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "postprocess.py"),
                    "concat", str(d), "--out", str(out),
                    "--target-res", "1280x720", "--xfade", "0.5"],
                   capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, f"xfade concat 失败: {r.stderr[-400:]}")
        self.assertTrue(out.exists() and out.stat().st_size > 0, "xfade 后应有产物")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestFFmpegPipeline(unittest.TestCase):
    """#4 真 ffmpeg 管线冒烟：lavfi testsrc 造片 → 实跑 postprocess.py concat。

    此前 postprocess 的拼接分支只有"读代码觉得对"，没有一条命令能变红。
    这里用合成源（testsrc + sine）零 API 覆盖三条真实路径：
      - 同规格     → -c copy 直切（快路径不破坏）
      - 分辨率不同 → 统一转码（旧逻辑）
      - 同分辨率不同 codec（h264 vs vp9）→ 必须走转码（v3.1.2 #4 修复点：
        旧签名只比分辨率，会把这条误判成可 -c copy 直拼 → 花屏/失败）
      - 带音轨走 filter 分支 → 音轨不丢（v3.1.2 修复点：旧代码写死 a=0）
    另含 #4 QC 硬门禁冒烟：qcgate 好片 PASS / 纯黑片 FAIL / 低分辨率规格 FAIL。
    """

    FF = None

    @classmethod
    def setUpClass(cls):
        try:
            import importlib
            fp = importlib.import_module("ffmpeg_probe")
            cls.FF = fp.find_ffmpeg()
        except Exception as e:
            cls.skipReason = f"ffmpeg 不可用: {e}"

    def setUp(self):
        if self.FF is None:
            self.skipTest(self.skipReason)
        self._td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._td.cleanup()

    def _make_clip(self, name, res, codec="libx264", audio=False, secs=1.0):
        import subprocess as sp
        p = Path(self._td.name) / name
        cmd = [self.FF, "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", f"testsrc=size={res}:rate=24:duration={secs}"]
        if audio:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}"]
        cmd += ["-c:v", codec, "-b:v", "800k", "-pix_fmt", "yuv420p"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"]
        cmd += [str(p)]
        r = sp.run(cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"造片失败 {name}: {r.stderr[-300:]}")
        return p

    def _run_concat(self, clips_dir):
        import subprocess as sp
        import postprocess  # noqa: F401  （确认模块可导入）
        out = Path(self._td.name) / "out.mp4"
        r = sp.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "postprocess.py"),
                    "concat", str(clips_dir), "--out", str(out), "--target-res", "1280x720"],
                   capture_output=True, text=True, timeout=180)
        return r, out

    def _probe(self, p):
        import importlib
        pp = importlib.import_module("postprocess")
        return pp.probe(str(p))

    def test_concat_same_spec_direct_copy(self):
        d = Path(self._td.name) / "clips"
        d.mkdir()
        self._make_clip(d / "clip_S1.mp4", "1280x720")
        self._make_clip(d / "clip_S2.mp4", "1280x720")
        r, out = self._run_concat(d)
        self.assertEqual(r.returncode, 0, f"concat 失败: {r.stderr[-400:]}")
        info = self._probe(out)
        self.assertEqual((info.get("width"), info.get("height")), (1280, 720))
        # 两段 1s → 归一后 ≈2s（±0.6 容差，覆盖 tpad/帧对齐误差）
        self.assertTrue(1.4 <= info.get("duration", 0) <= 2.6,
                        f"时长 {info.get('duration')}s 不像两段拼接")

    def test_concat_mixed_res_reencodes(self):
        d = Path(self._td.name) / "clips"
        d.mkdir()
        self._make_clip(d / "clip_S1.mp4", "1280x720")
        self._make_clip(d / "clip_S2.mp4", "640x360")
        r, out = self._run_concat(d)
        self.assertEqual(r.returncode, 0, f"concat 失败: {r.stderr[-400:]}")
        info = self._probe(out)
        # 重编码分支必出 libx264；若误走 -c copy，混合流会直接报错或产出坏片
        self.assertEqual(info.get("codec"), "h264")
        self.assertEqual((info.get("width"), info.get("height")), (1280, 720))

    def test_concat_mixed_codec_reencodes(self):
        """#4 回归钉死：同分辨率不同编码，必须走转码。
        修复前签名只比 (w,h) → 误判可 -c copy → ffmpeg 直拼 h264+vp9 流。"""
        d = Path(self._td.name) / "clips"
        d.mkdir()
        self._make_clip(d / "clip_S1.mp4", "1280x720", codec="libx264")
        self._make_clip(d / "clip_S2.mp4", "1280x720", codec="libvpx-vp9")
        r, out = self._run_concat(d)
        self.assertEqual(r.returncode, 0, f"concat 失败: {r.stderr[-400:]}")
        info = self._probe(out)
        self.assertEqual(info.get("codec"), "h264",
                         "同分辨率不同 codec 应强制转码，-c copy 直拼不合法")

    def test_concat_filter_branch_keeps_audio(self):
        """#4 回归钉死：filter 重编码分支不能写死 a=0 丢环境音。
        混分辨率（触发 filter 分支）+ 带 sine 音轨 → 输出必须有音轨。"""
        d = Path(self._td.name) / "clips"
        d.mkdir()
        self._make_clip(d / "clip_S1.mp4", "1280x720", audio=True)
        self._make_clip(d / "clip_S2.mp4", "640x360", audio=True)
        r, out = self._run_concat(d)
        self.assertEqual(r.returncode, 0, f"concat 失败: {r.stderr[-400:]}")
        info = self._probe(out)
        self.assertTrue(info.get("audio"),
                        "带音轨的混分辨率拼接后音轨丢失（a=0 写死 bug 复现）")

    # ── #4 QC 硬门禁：机器可判项（黑帧/过曝/静帧/规格）自动判 ──
    def _gate(self, p, *extra):
        import subprocess as sp
        return sp.run([sys.executable,
                       str(Path(__file__).resolve().parents[1] / "scripts" / "postprocess.py"),
                       "qcgate", str(p), *extra],
                      capture_output=True, text=True, timeout=120)

    def test_qcgate_good_passes(self):
        import importlib
        pp = importlib.import_module("postprocess")   # 确认模块可导入
        p = self._make_clip(Path(self._td.name) / "g.mp4", "1280x720", secs=2.0)
        r = self._gate(p)
        self.assertEqual(r.returncode, 0, f"好片应 PASS: {r.stdout}{r.stderr[-200:]}")
        self.assertIn("PASS", r.stdout)

    def test_qcgate_black_fails(self):
        import subprocess as sp
        p = Path(self._td.name) / "b.mp4"
        sp.run([self.FF, "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=black:size=1280x720:rate=24:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
        r = self._gate(p)
        self.assertEqual(r.returncode, 2, f"纯黑片应 FAIL: {r.stdout}{r.stderr[-200:]}")
        self.assertIn("FAIL", r.stdout)

    def test_qcgate_low_res_fails_on_spec(self):
        # 画面是好的（testsrc），但分辨率低于默认下限 1280x720 → 规格项 FAIL
        p = self._make_clip(Path(self._td.name) / "s.mp4", "640x360", secs=1.0)
        r = self._gate(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("分辨率", r.stdout)


class TestWatermarkProfile(unittest.TestCase):
    """优化⑨·盲区2：水印旁线——档案读取 / 缩放逻辑（复用真实 watermark_profiles.json）。"""

    def setUp(self):
        import importlib
        self.dl = importlib.import_module("delogo_watermark")


    def test_clean_profile_refuses(self):
        """clean 档（无水印）→ die 拒绝抹除（防白跑一趟重编码）。"""
        with self.assertRaises(SystemExit):
            self.dl.load_profile("agnes")

    def test_scale_box_same_res_identity(self):
        box = (1133, 571, 54, 56)
        self.assertEqual(self.dl.scale_box(box, 1280, 720, 1280, 720), box)

    def test_scale_box_half_res(self):
        # 640x360：框按比例缩放，不小于 8px
        out = self.dl.scale_box((1133, 571, 54, 56), 1280, 720, 640, 360)
        self.assertEqual(out[0], round(1133 * 640 / 1280))
        self.assertGreaterEqual(out[2], 8)

    def test_scale_box_min_8px(self):
        tiny = self.dl.scale_box((1133, 571, 54, 56), 1280, 720, 80, 45)
        self.assertGreaterEqual(tiny[2], 8)
        self.assertGreaterEqual(tiny[3], 8)


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestWatermarkStopBoost(unittest.TestCase):
    """P2-1 修复：传 --watermark 且 stop_after 不含 watermark → 自动提升 stop + 提示。"""

    def _d(self, mode="stills", watermark="", extra=()):
        import subprocess as sp
        d = self.enterContext(tempfile.TemporaryDirectory())
        d = Path(d)
        for n in ("01", "02"):
            (d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}", "t2i_prompt": "x"}), encoding="utf-8")
        (d / "plan.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
        cmd = [sys.executable,
               str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"),
               str(d), "--dry-run", "--no-lint", *extra]
        if watermark:
            cmd += ["--watermark", watermark, "--watermark-dry-run"]
        r = sp.run(cmd, capture_output=True, text=True, timeout=120)
        return r

    def test_watermark_auto_promotes_stop(self):
        r = self._d(watermark="custom")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("自动把 stop 提升到 watermark", (r.stdout + r.stderr),
                      "给了 --watermark 却不提 stop = 静默跳过，必须提示")
        self.assertIn("delogo_watermark.py", r.stdout, "watermark 阶段命令必须被执行（dry-run 记录）")

    def test_without_watermark_no_promotion(self):
        r = self._d()
        self.assertNotIn("delogo_watermark.py", r.stdout, "没给 --watermark 不该跑水印")
        self.assertNotIn("提升", (r.stdout + r.stderr))


class TestAuditView(unittest.TestCase):
    """优化⑪：audit 进度盘点——出图/出片/失败状态 + 下一步清单。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        for n in ("01", "02", "03"):
            (self.d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}"}), encoding="utf-8")
        (self.d / "plan.json").write_text(
            json.dumps({"mode": "full"}), encoding="utf-8")
        import pipeline as pp
        self.pp = pp

    def tearDown(self):
        self._td.cleanup()

    def _audit(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.pp.audit(self.d)
        return rc, buf.getvalue()

    def test_all_missing(self):
        rc, out = self._audit()
        self.assertEqual(rc, 0)
        self.assertIn("缺首帧", out)
        self.assertIn("images 阶段补 3 镜首帧", out)

    def test_partial_progress(self):
        (self.d / "frames").mkdir()
        (self.d / "frames" / "S01.png").write_bytes(b"x")
        (self.d / "clips").mkdir()
        (self.d / "clips" / "clip_S02.mp4").write_bytes(b"x")
        rc, out = self._audit()
        self.assertIn("出图 1", out)
        self.assertIn("出片 1", out)

    def test_failed_tag_suggests_retry(self):
        (self.d / "frames").mkdir()
        (self.d / "clips").mkdir()
        (self.d / "batch_run.json").write_text(json.dumps(
            {"shots": {"S02": "video key#1 (failed)"}}), encoding="utf-8")
        rc, out = self._audit()
        self.assertIn("--retry-failed", out)


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestUnifiedCli(unittest.TestCase):
    """优化⑩：统一 CLI 入口——media_gen run/audit 转发 pipeline（参数单一事实源）。"""

    def test_run_forward_dry_run(self):
        import subprocess as sp
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: sp.run(["python", "-c", "import shutil,sys;shutil.rmtree(sys.argv[1])", d]))
        Path(d, "plan.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        for n in ("01",):
            Path(d, f"shot_{n}.json").write_text(json.dumps(
                {"shot_id": f"S{n}", "i2v_prompt": "clean prompt words " * 8}),
                encoding="utf-8")
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"),
                    "run", d, "--dry-run", "--no-lint"],
                   capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY-RUN", r.stdout)

    def test_audit_forward(self):
        import subprocess as sp
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: sp.run(["python", "-c", "import shutil,sys;shutil.rmtree(sys.argv[1])", d]))
        Path(d, "plan.json").write_text(json.dumps({"mode": "full"}), encoding="utf-8")
        Path(d, "shot_01.json").write_text(json.dumps({"shot_id": "S01"}), encoding="utf-8")
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"),
                    "audit", d], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[audit]", r.stdout)


class TestSrtParse(unittest.TestCase):
    """srt 导入：parse_srt 纯函数——标准 srt 文本 → 内部 [{at,dur,text}] 时间轴。

    seam：parse_srt(text: str) -> list[dict(at,dur,text)]
    只认 HH:MM:SS,mmm --> HH:MM:SS,mmm 的标准块序（index 行容错缺失/多余）。
    """

    def test_standard_block(self):
        import importlib
        pp = importlib.import_module("postprocess")
        srt = "1\n00:00:01,500 --> 00:00:04,000\n第一句字幕\n\n2\n00:00:05,000 --> 00:00:07,250\n第二句\n"
        subs = pp.parse_srt(srt)
        self.assertEqual(len(subs), 2)
        self.assertAlmostEqual(subs[0]["at"], 1.5)
        self.assertAlmostEqual(subs[0]["dur"], 2.5)
        self.assertEqual(subs[0]["text"], "第一句字幕")

    def test_multiline_text_joined(self):
        import importlib
        pp = importlib.import_module("postprocess")
        srt = "1\n00:00:00,000 --> 00:00:02,000\n两行文本\n拼成一句\n"
        subs = pp.parse_srt(srt)
        self.assertEqual(subs[0]["text"], "两行文本 拼成一句")

    def test_bad_timestamp_skipped(self):
        import importlib
        pp = importlib.import_module("postprocess")
        srt = "1\nnot a timestamp --> 00:00:02,000\n文本\n"
        self.assertEqual(pp.parse_srt(srt), [])

    def test_decimal_seconds_comma_only(self):
        import importlib
        pp = importlib.import_module("postprocess")
        # 毫秒分隔符必须是逗号（标准 srt）；点号分隔不认
        srt = "1\n00:00:01.500 --> 00:00:04,000\nx\n"
        self.assertEqual(pp.parse_srt(srt), [])


class TestSubtitlePreset(unittest.TestCase):
    """字幕样式预设：apply_preset 纯函数 + subtitle_presets.json 档案。

    seam：apply_preset(subs: list[dict], preset: str) -> list[dict]
    未提供样式字段的字幕条目按预设填充（pos/size/fade/fontcolor/box）；
    字幕自带字段优先（逐条覆盖权 > 预设默认）。预设不存在 → ValueError。
    """

    def _subs(self):
        return [{"at": 1.0, "dur": 2.0, "text": "a"},
                {"at": 3.0, "dur": 2.0, "text": "b", "size": 60}]

    def test_preset_fills_defaults(self):
        import importlib
        pp = importlib.import_module("postprocess")
        out = pp.apply_preset(self._subs(), "movie")
        self.assertEqual(out[0]["pos"], "bottom")
        self.assertEqual(out[0]["size"], 40)
        self.assertEqual(out[0]["fontcolor"], "white")
        # 条目自带 size=60 优先于预设 40
        self.assertEqual(out[1]["size"], 60)

    def test_unknown_preset_raises(self):
        import importlib
        pp = importlib.import_module("postprocess")
        with self.assertRaises(ValueError):
            pp.apply_preset(self._subs(), "nope")

    def test_presets_file_valid(self):
        """档案 JSON 合法且含三风格。"""
        import importlib
        pp = importlib.import_module("postprocess")
        p = Path(pp.__file__).parent / "subtitle_presets.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        for k in ("news", "movie", "variety"):
            self.assertIn(k, data)
            self.assertIn("size", data[k])
            self.assertIn("fontcolor", data[k])

    def test_presets_applied_in_drawtext(self):
        """预设的 fontcolor 要真进 drawtext 滤镜串（防只填充不生效）。"""
        import importlib
        pp = importlib.import_module("postprocess")
        preset = json.loads((Path(importlib.import_module("postprocess").__file__).parent
                             / "subtitle_presets.json").read_text(encoding="utf-8"))["news"]
        self.assertIn("fontcolor", preset)
        # news 风格带底框
        self.assertIn("box", preset)


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
class TestSrtBurnEndToEnd(unittest.TestCase):
    """srt 导入 + 预设烧录端到端：lavfi 造 3s 片 → srt 文件 → concat 真跑 → 抽帧验证字幕存在。

    防两层假绿：parse_srt 单测过但 concat 不认 .srt；apply_preset 填了字段但
    drawtext 渲染没用 fontcolor/box（v3.1.16 教训：rc=0 ≠ 内容对）。
    """

    def test_srt_preset_burned(self):
        import subprocess as sp
        import ffmpeg_probe as fpx
        from PIL import Image
        pp = __import__("postprocess", fromlist=["x"])
        ff = fpx.find_ffmpeg()
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            (td / "clips").mkdir()
            sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=24:duration=3",
                    "-pix_fmt", "yuv420p", str(td / "clips" / "clip_01.mp4")],
                   check=True, timeout=60)
            (td / "subs.srt").write_text(
                "1\n00:00:00,200 --> 00:00:02,800\n端到端字幕验证\n",
                encoding="utf-8")
            r = sp.run([sys.executable, str(Path(pp.__file__).parent.parent / "scripts" / "postprocess.py"),
                        "concat", str(td / "clips"), "--out", str(td / "out.mp4"),
                        "--subtitles", str(td / "subs.srt"),
                        "--subtitle-preset", "news"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120,
                       env={**os.environ, "PYTHONUTF8": "1"})
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            out = td / "out.mp4"
            self.assertTrue(out.exists() and out.stat().st_size > 1000)
            # 抽 1s 处的帧：字幕时段内，画面应有大量非 testsrc 原色的白色像素（白字底框）
            fr = td / "frame.png"
            sp.run([ff, "-y", "-loglevel", "error", "-ss", "1.0", "-i", str(out),
                    "-frames:v", "1", str(fr)], check=True, timeout=60)
            with Image.open(fr) as im:
                px = list(im.convert("RGB").getdata())
            whites = sum(1 for p in px if p[0] > 230 and p[1] > 230 and p[2] > 230)
            self.assertGreater(whites, 50, "1s 处应有白色字幕像素——srt 没烧进去")


if __name__ == '__main__':
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""pipeline 编排与导出安全测试——按模块拆分自 test_media_gen.py。

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



class TestResolveWorkers(unittest.TestCase):
    """v4.7.9：plan.json 的并行数必须生效。

    pipeline.py 开头自称"workers / 池顺序 / 水印全部读自 plan.json"，实际只用
    CLI 默认值 3 —— 用户问⑤选的并行数落盘后**零生效**（静默忽略）。"""

    def _args(self, wi=None, wv=None):
        import argparse
        return argparse.Namespace(workers_image=wi, workers_video=wv)

    def test_cli_explicit_wins(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_workers(self._args(5, 7), {"workers_image": 2, "workers_video": 2}),
                         (5, 7))

    def test_plan_used_when_cli_absent(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_workers(self._args(), {"workers_image": 4, "workers_video": 6}),
                         (4, 6))

    def test_legacy_workers_key_covers_both(self):
        """旧字段 workers 兼容为两列同值。"""
        import pipeline as pp
        self.assertEqual(pp.resolve_workers(self._args(), {"workers": 2}), (2, 2))

    def test_fallback_three(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_workers(self._args(), {}), (3, 3))

    def test_bad_plan_value_ignored(self):
        """plan 里塞非法值（0/字符串）→ 忽略回退默认，不能把并行数搞成 0。"""
        import pipeline as pp
        self.assertEqual(pp.resolve_workers(self._args(), {"workers_image": 0, "workers_video": "x"}),
                         (3, 3))


class TestResolveWatermark(unittest.TestCase):
    """v4.7.9：plan.json 的 watermark 必须生效（此前只有 CLI，落盘决策零生效）。"""

    def _args(self, wm=""):
        import argparse
        return argparse.Namespace(watermark=wm)

    def test_cli_wins(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_watermark(self._args("agnes"), {"watermark": "custom"}), "agnes")

    def test_plan_used_when_cli_absent(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_watermark(self._args(), {"watermark": "custom"}), "custom")

    def test_empty_means_off(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_watermark(self._args(), {}), "")


class TestRunForwardsOnlyExplicitWorkers(unittest.TestCase):
    """v4.7.9：media_gen run 未显式给并行数时**不得**转发默认值。

    否则恒真的默认值会覆盖 plan.json 决策（转发链：media_gen run → pipeline → batch）。"""

    def test_no_workers_flags_when_absent(self):
        import argparse
        import media_gen as m
        seen = {}

        def fake_call(cmd):
            seen["cmd"] = cmd
            return 0

        args = argparse.Namespace(shots="x", workers_image=None, workers_video=None)
        with mock.patch.object(m.subprocess, "call", side_effect=fake_call), \
             self.assertRaises(SystemExit):
            m.cmd_run(args)
        self.assertNotIn("--workers-image", seen["cmd"],
                         "未显式给并行数却转发了默认值 → 覆盖 plan.json")
        self.assertNotIn("--workers-video", seen["cmd"])

    def test_explicit_workers_still_forwarded(self):
        import argparse
        import media_gen as m
        seen = {}

        def fake_call(cmd):
            seen["cmd"] = cmd
            return 0

        args = argparse.Namespace(shots="x", workers_image=5, workers_video=None)
        with mock.patch.object(m.subprocess, "call", side_effect=fake_call), \
             self.assertRaises(SystemExit):
            m.cmd_run(args)
        i = seen["cmd"].index("--workers-image")
        self.assertEqual(seen["cmd"][i + 1], "5")


class TestResolvePoolOrder(unittest.TestCase):
    """v4.8.0：plan.json 的 video_pool_order 必须生效。

    此前它只在 mg_status.PLAN_KNOWN_KEYS 里"合法"（plan-check 不报未知键），
    但 pipeline/batch 都不读 → 用户问⑤选的池顺序落盘后**零生效**。
    接入方式：转成 batch 的 `--provider a,b,c`（该参数本就按序取池）。"""

    def _args(self, pv=""):
        import argparse
        return argparse.Namespace(provider_video=pv)

    def test_cli_wins(self):
        import pipeline as pp
        self.assertEqual(
            pp.resolve_pool_order(self._args("agnes,zhipu"), {"video_pool_order": ["custom"]}),
            ["agnes", "zhipu"])

    def test_plan_used_when_cli_absent(self):
        import pipeline as pp
        self.assertEqual(
            pp.resolve_pool_order(self._args(), {"video_pool_order": ["zhipu", "custom"]}),
            ["zhipu", "custom"])

    def test_string_form_accepted(self):
        """plan 手写成逗号串也认（宽容解析，不因格式差异静默失效）。"""
        import pipeline as pp
        self.assertEqual(
            pp.resolve_pool_order(self._args(), {"video_pool_order": "zhipu,custom"}),
            ["zhipu", "custom"])

    def test_empty_means_default(self):
        """不给 → 空列表（batch 走 MEDIA_PRIORITY 默认序，行为不变）。"""
        import pipeline as pp
        self.assertEqual(pp.resolve_pool_order(self._args(), {}), [])

    def test_bad_type_ignored(self):
        import pipeline as pp
        self.assertEqual(pp.resolve_pool_order(self._args(), {"video_pool_order": 123}), [])
        self.assertEqual(pp.resolve_pool_order(self._args(), {"video_pool_order": ["", "  "]}), [])


class TestQcGateAction(unittest.TestCase):
    """门禁退出码 → 动作（纯函数）。

    本仓有两族约定，这里统一消化：
      · 报告族（qcseq / faces）：0=PASS / 1=WARN / >=2=FAIL
      · 门禁族（qcgate / audio-qc）：非 strict 时 WARN 也返 0，>=2=FAIL
    → 共同的硬判据只有 **>=2**；1 是报告性质，默认不拦流程。
    """

    def _act(self, rc):
        import pipeline as pp
        return pp.qc_gate_action("demo", rc)

    def test_pass_is_ok(self):
        self.assertEqual(self._act(0), ("ok", ""))

    def test_warn_is_note_not_die(self):
        act, msg = self._act(1)
        self.assertEqual(act, "note", "WARN 是报告性质，默认不拦流程（要拦用 --*-strict）")
        self.assertIn("WARN", msg)

    def test_fail_dies(self):
        self.assertEqual(self._act(2)[0], "die")

    def test_abnormal_code_dies(self):
        for rc in (3, 4, -1):
            with self.subTest(rc=rc):
                self.assertEqual(self._act(rc)[0], "die", f"异常退出码 {rc} 必须拦")


class TestConcatGatesWired(unittest.TestCase):
    """v4.10.2：concat 前的 audio-qc 与 faces 必须**真的挂进流程**。

    v4.10.0 / v4.10.1 把这两条门禁做出来了，但 pipeline 里出现 0 次——
    `media_gen run` 根本不跑它们（"功能存在 ≠ 功能生效"）。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        for n in ("01", "02"):
            (self.d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}", "t2i_prompt": "x"}), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _plan(self, **kw):
        (self.d / "plan.json").write_text(json.dumps(kw), encoding="utf-8")

    def _dry(self, *extra, env=None):
        import subprocess as sp
        e = dict(os.environ)
        if env:
            e.update(env)
        return sp.run([sys.executable,
                       str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"),
                       str(self.d), "--dry-run", *extra],
                      capture_output=True, text=True, timeout=180, env=e)

    @staticmethod
    def _hits(r, needle):
        return [l for l in r.stdout.splitlines() if l.startswith("+ ") and needle in l]

    def _index(self, r, needle):
        lines = [l for l in r.stdout.splitlines() if l.startswith("+ ")]
        return next((i for i, l in enumerate(lines) if needle in l), -1)

    def test_audio_qc_runs_before_concat(self):
        self._plan(mode="stills")
        r = self._dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self._hits(r, "audio_qc.py")), 1,
                         "concat 前应自动跑一次音频门禁（静音/削波/响度）")
        i_aq = self._index(r, "audio_qc.py")
        i_cc = self._index(r, "postprocess.py concat")
        self.assertGreaterEqual(i_cc, 0, "concat 命令应存在")
        self.assertLess(i_aq, i_cc, "audio-qc 必须排在 concat 之前")

    def test_no_audio_qc_opts_out(self):
        self._plan(mode="stills")
        r = self._dry("--no-audio-qc")
        self.assertEqual(self._hits(r, "audio_qc.py"), [], "--no-audio-qc 应完全跳过")
        self.assertGreaterEqual(self._index(r, "postprocess.py concat"), 0,
                                "跳门禁不该把 concat 也带走")

    def test_audio_qc_strict_is_passed_through(self):
        self._plan(mode="stills")
        r = self._dry("--audio-qc-strict")
        hits = self._hits(r, "audio_qc.py")
        self.assertEqual(len(hits), 1)
        self.assertIn("--strict", hits[0], "--audio-qc-strict 要透传给工具（WARN 升级为拦）")

    def test_no_faces_opts_out(self):
        self._plan(mode="stills")
        r = self._dry("--no-faces")
        self.assertEqual(self._hits(r, "face_consistency.py"), [])

    def test_faces_skipped_when_models_missing(self):
        """缺 cv2/模型 → 跳过**且明说**（不假装通过，也不因可选依赖拦流程）。"""
        self._plan(mode="stills")
        r = self._dry(env={"USERPROFILE": str(self.d / "nohome"),
                           "HOME": str(self.d / "nohome")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._hits(r, "face_consistency.py"), [],
                         "模型不在 → 不该跑 faces")
        self.assertIn("跳过人脸一致性", r.stdout + r.stderr, "跳过必须留下可观测的说明")

    @unittest.skipUnless(
        (Path.home() / ".workbuddy" / "models" / "face_recognition_sface_2021dec.onnx").exists(),
        "需要本地 SFace 模型（~/.workbuddy/models/）")
    def test_faces_runs_before_concat_when_available(self):
        self._plan(mode="stills")
        r = self._dry()
        self.assertEqual(len(self._hits(r, "face_consistency.py")), 1,
                         "有模型时 faces 应在 concat 前跑")
        self.assertLess(self._index(r, "face_consistency.py"),
                        self._index(r, "postprocess.py concat"))

    def test_media_gen_run_forwards_optout_flags(self):
        """门面链路：从 `media_gen.py run` 走时这些开关也必须能传下去
        （否则用户只能用默认，等于白给开关）。正反两面都验，避免"两边都空"的假绿。"""
        import subprocess as sp
        self._plan(mode="stills")
        base = [sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"),
                "run", str(self.d), "--dry-run"]
        on = sp.run(base, capture_output=True, text=True, timeout=180)
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertEqual(len(self._hits(on, "audio_qc.py")), 1,
                         "默认应跑（证明链路是通的，不是在两边都空）")
        off = sp.run(base + ["--no-audio-qc", "--no-faces"],
                     capture_output=True, text=True, timeout=180)
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(self._hits(off, "audio_qc.py"), [],
                         "media_gen run 应把 --no-audio-qc 传下去")
        self.assertEqual(self._hits(off, "face_consistency.py"), [],
                         "media_gen run 应把 --no-faces 传下去")


class TestSubtitleRenderWiring(unittest.TestCase):
    """v4.15：字幕渲染通道的逃生阀必须从两个入口都能碰到。

    默认通道换成了 ASS/libass；一旦它在某台机器上出问题，用户需要
    `--subtitle-render drawtext` 立刻退回旧通道。若这个 flag 只能从
    `postprocess.py concat` 直接给，走 `media_gen run` 的用户就无路可退。

    反面同样要验：**不给 flag 时不许把默认值塞进命令**——否则旁线自己的默认
    将来改了这里也感知不到（v4.9 的 workers 恒真默认值覆盖 plan 就是这毛病）。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        for n in ("01", "02"):
            (self.d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}", "t2i_prompt": "x"}), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _plan(self, **kw):
        (self.d / "plan.json").write_text(json.dumps(kw), encoding="utf-8")

    def _dry(self, *extra):
        import subprocess as sp
        return sp.run([sys.executable,
                       str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"),
                       str(self.d), "--dry-run", *extra],
                      capture_output=True, text=True, timeout=180)

    @staticmethod
    def _concat_line(r):
        hits = [l for l in r.stdout.splitlines()
                if l.startswith("+ ") and "postprocess.py concat" in l]
        return hits[0] if hits else ""

    def test_drawtext_escape_valve_reaches_concat(self):
        self._plan(mode="stills")
        r = self._dry("--subtitle-render", "drawtext")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self._concat_line(r)
        self.assertTrue(line, "concat 命令应存在")
        self.assertIn("--subtitle-render drawtext", line,
                      "逃生阀必须真的传到 postprocess concat")

    def test_default_not_forced_into_command(self):
        """不给 flag 就不许出现——默认由旁线自己决定（防"门面覆盖默认"）。"""
        self._plan(mode="stills")
        r = self._dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--subtitle-render", self._concat_line(r),
                         "未显式指定时不该往命令里塞默认值")

    def test_media_gen_run_forwards_escape_valve(self):
        """门面链路：`media_gen run` 也必须有这条路（否则只能直调 pipeline）。"""
        import subprocess as sp
        self._plan(mode="stills")
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"),
                    "run", str(self.d), "--dry-run",
                    "--subtitle-render", "drawtext"],
                   capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--subtitle-render drawtext", self._concat_line(r),
                      "media_gen run 应把 --subtitle-render 透传到 concat")

    def test_pipeline_rejects_unknown_channel(self):
        """取值必须受约束（拼错时明确报错，而不是静默走默认）。"""
        self._plan(mode="stills")
        r = self._dry("--subtitle-render", "libass")
        self.assertNotEqual(r.returncode, 0, "非法取值应被 argparse 拒绝")
        self.assertIn("invalid choice", (r.stdout or "") + (r.stderr or ""))


class TestExportNonUtf8Guard(unittest.TestCase):
    """v4.7.9：非 UTF-8 文件含私有标记块 → 必须拒绝导出。

    原实现遇到 UnicodeDecodeError 直接 continue（跳过标记块删除）→ 私有内容
    原样进公开仓，而第 5 步自检只查几个关键词，命中不了就静默泄漏。"""

    def _mod(self):
        import importlib.util
        src = Path(__file__).resolve().parents[1] / "scripts" / "export_public.py"
        spec = importlib.util.spec_from_file_location("reelcraft_export", src)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_marker_in_non_utf8_is_rejected(self):
        m = self._mod()
        raw = "# private-begin\n内部密钥=abc\n# private-end\n".encode("gbk")
        err = m.non_utf8_marker_error("notes.md", raw)
        self.assertIsNotNone(err, "非 UTF-8 + 私有标记块必须拒绝导出（拒绝 > 泄漏）")
        self.assertIn("notes.md", err)

    def test_plain_non_utf8_is_allowed(self):
        m = self._mod()
        raw = "中文说明，无标记块\n".encode("gbk")
        self.assertIsNone(m.non_utf8_marker_error("readme.md", raw))


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestPipelineOrchestration(unittest.TestCase):
    """#1 pipeline：只做编排，审美决策读 plan.json，不替用户拍板（dry-run 不烧钱）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        for n in ("01", "02", "03"):
            (self.d / f"shot_{n}.json").write_text(
                json.dumps({"shot_id": f"S{n}", "t2i_prompt": "x"}), encoding="utf-8")

    def tearDown(self):
        self._td.cleanup()

    def _plan(self, **kw):
        (self.d / "plan.json").write_text(json.dumps(kw), encoding="utf-8")

    def _dry(self, *extra):
        import subprocess as sp
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"),
                    str(self.d), "--dry-run", *extra],
                   capture_output=True, text=True, timeout=120)
        return r

    def test_hybrid_only_hero_videos(self):
        self._plan(mode="hybrid", hero_shots=["S1", "S3"])
        r = self._dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        # 真视频只跑 hero 镜（--only S1,S3）；过场镜 S2 走 kenburns
        self.assertIn("--phase videos", r.stdout)
        self.assertIn("--only S1,S3", r.stdout)
        ken = [ln for ln in r.stdout.splitlines() if "kenburns" in ln]
        self.assertEqual(len(ken), 3, "3 镜都应有缓推命令（重点镜真视频在 dry-run 下无 clip）")

    def test_stills_skips_video(self):
        self._plan(mode="stills")
        r = self._dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--phase videos", r.stdout, "stills 档不该出真视频")
        self.assertNotIn("--only", r.stdout)

    def test_full_no_only(self):
        self._plan(mode="full")
        r = self._dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--phase videos", r.stdout)
        self.assertNotIn("--only", r.stdout, "full 档全镜出真视频，不该 --only")
        self.assertNotIn("kenburns", r.stdout, "full 档不该走缓推")

    def test_missing_mode_refuses_not_guess(self):
        """plan.json 缺 mode → 退回 Step 1，绝不默认 full 替用户拍板。"""
        self._plan(workers_video=3)   # 故意不放 mode
        r = self._dry()
        self.assertNotEqual(r.returncode, 0, "缺 mode 必须 die，不能擅自开跑")
        self.assertIn("mode", (r.stderr + r.stdout))

    def test_hybrid_without_hero_refuses(self):
        self._plan(mode="hybrid")     # 忘了 hero_shots
        r = self._dry()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hero_shots", (r.stderr + r.stdout))

    def test_videos_pending_exits_4(self):
        """videos 阶段超时在途（batch exit 4 + batch_run.json 记 timeout-pending）
        → pipeline 必须 die(4) 交还 agent，绝不静默放行进 concat（P0-1）。"""
        import argparse
        import pipeline as pp
        from unittest import mock as _m
        self._plan(mode="full")
        (self.d / "batch_run.json").write_text(json.dumps(
            {"shots": {"S2": "video key#1 (timeout-pending)"}}), encoding="utf-8")
        ns = argparse.Namespace(shots=str(self.d), plan="", stop_after="concat", dry_run=False,
                          workers_image=1, workers_video=1, provider_image="",
                          provider_video="", qcgate=False, qcgate_strict=False,
                          kenburns_dur=5.0, retry_failed=False, no_lint=True, no_qcseq=True, final="",
                          target_res="1280x720", voice="", bgm="", bgm_db=-18.0,
                          subtitles="", slogan="", slogan_position="left",
                                watermark="", watermark_dry_run=False,
                                sound_lines="", sound_voice="", sound_speed=1.0,
                                subtitle_render="",
                                sound_skip_tts=False, sound_auto_shift=False, sound_gap=0.3)
        # images 阶段 rc=0（mock 第 1 次调用）；videos 阶段 rc=4（mock 第 2 次调用）
        with _m.patch("pipeline.subprocess.run",
                      side_effect=[_m.Mock(returncode=0), _m.Mock(returncode=4)]):
            with self.assertRaises(SystemExit) as cm:
                pp.cmd(ns)
        self.assertEqual(cm.exception.code, 4, "有 PENDING 必须 exit 4 交还 agent")

    def test_videos_fail_exits_1(self):
        """videos 阶段真失败（batch exit 1）→ pipeline die(1)，但没 PENDING 时不是 4。"""
        import argparse
        import pipeline as pp
        from unittest import mock as _m
        self._plan(mode="full")
        (self.d / "batch_run.json").write_text(json.dumps(
            {"shots": {"S1": "video key#1 (failed)"}}), encoding="utf-8")
        ns = argparse.Namespace(shots=str(self.d), plan="", stop_after="concat", dry_run=False,
                          workers_image=1, workers_video=1, provider_image="",
                          provider_video="", qcgate=False, qcgate_strict=False,
                          kenburns_dur=5.0, retry_failed=False, no_lint=True, no_qcseq=True, final="",
                          target_res="1280x720", voice="", bgm="", bgm_db=-18.0,
                          subtitles="", slogan="", slogan_position="left",
                                watermark="", watermark_dry_run=False,
                                sound_lines="", sound_voice="", sound_speed=1.0,
                                subtitle_render="",
                                sound_skip_tts=False, sound_auto_shift=False, sound_gap=0.3)
        with _m.patch("pipeline.subprocess.run",
                      side_effect=[_m.Mock(returncode=0), _m.Mock(returncode=1)]):
            with self.assertRaises(SystemExit) as cm:
                pp.cmd(ns)
        self.assertEqual(cm.exception.code, 1)

    def test_sync_frames_matches_manual_naming(self):
        """_sync_frames 命名兜底：schema shot_id="S03" + 手动产物 shots/shot_03.png
        （无 S 前缀）也能同步进 frames/S03.png（P2）。"""
        import pipeline as pp
        self._plan(mode="full")
        frames = self.d / "frames"
        frames.mkdir(exist_ok=True)
        (self.d / "shot_03.png").write_bytes(b"fake-png")
        shots = pp._shots(self.d)
        n = pp._sync_frames(self.d, shots)
        self.assertGreaterEqual(n, 1, "手动产物 shot_03.png 应被同步为 frames/S03.png")
        self.assertTrue((frames / "S03.png").exists())

    def test_videos_lint_blocks_bad_prompt(self):
        """优化②：i2v prompt 含 slop/超词数 → videos 阶段出镜前 die(3)，不烧额度。"""
        import pipeline as pp
        # 改造一个 shot 的 i2v_prompt 为超长 slop 文案
        (self.d / "shot_01.json").write_text(json.dumps(
            {"shot_id": "S01", "i2v_prompt": "a beautiful masterpiece " * 200}),
            encoding="utf-8")
        self._plan(mode="full")
        r = self._dry()
        self.assertEqual(r.returncode, 0, "dry-run 不执行 lint（不烧钱路径不拦）")
        # 非 dry-run：lint 应拦截
        from unittest import mock as _m
        import argparse
        ns = argparse.Namespace(shots=str(self.d), plan="", stop_after="videos",
                                dry_run=False, workers_image=1, workers_video=1,
                                provider_image="", provider_video="", qcgate=False,
                                qcgate_strict=False, kenburns_dur=5.0, retry_failed=False,
                                no_lint=False, no_qcseq=True, final="", target_res="1280x720", voice="",
                                bgm="", bgm_db=-18.0, subtitles="", slogan="",
                                slogan_position="left",
                                watermark="", watermark_dry_run=False,
                                sound_lines="", sound_voice="", sound_speed=1.0,
                                subtitle_render="",
                                sound_skip_tts=False, sound_auto_shift=False, sound_gap=0.3)
        with _m.patch("pipeline.subprocess.run", side_effect=[_m.Mock(returncode=0)]):
            with self.assertRaises(SystemExit) as cm:
                pp.cmd(ns)
        self.assertEqual(cm.exception.code, 3, "lint 未过的镜应退回 Step 3，不得出镜")

    def test_videos_lint_skippable(self):
        """优化②：--no-lint 显式跳过（如历史 shots 目录），不拦。"""
        import pipeline as pp
        (self.d / "shot_01.json").write_text(json.dumps(
            {"shot_id": "S01", "i2v_prompt": "a beautiful masterpiece " * 200}),
            encoding="utf-8")
        self._plan(mode="full")
        from unittest import mock as _m
        import argparse
        ns = argparse.Namespace(shots=str(self.d), plan="", stop_after="videos",
                                dry_run=False, workers_image=1, workers_video=1,
                                provider_image="", provider_video="", qcgate=False,
                                qcgate_strict=False, kenburns_dur=5.0, retry_failed=False,
                                no_lint=True, no_qcseq=True, final="", target_res="1280x720", voice="",
                                bgm="", bgm_db=-18.0, subtitles="", slogan="",
                                slogan_position="left",
                                watermark="", watermark_dry_run=False,
                                sound_lines="", sound_voice="", sound_speed=1.0,
                                subtitle_render="",
                                sound_skip_tts=False, sound_auto_shift=False, sound_gap=0.3)
        # images rc=0 → videos rc=0（放行到底）
        with _m.patch("pipeline.subprocess.run",
                      side_effect=[_m.Mock(returncode=0), _m.Mock(returncode=0)]):
            try:
                pp.cmd(ns)
            except SystemExit as e:
                self.fail(f"--no-lint 不应 die: {e}")

    def test_sound_stage_invokes_vo_build(self):
        """优化④：有 vo_lines.json 时 sound 阶段调 vo_build 并把产物带进 concat。"""
        self._plan(mode="full")
        (self.d / "vo_lines.json").write_text(json.dumps(
            {"acts": [], "lines": [{"id": "l1", "text": "test"}]}), encoding="utf-8")
        r = self._dry("--no-lint")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vo_build.py", r.stdout, "sound 阶段应调 vo_build")
        self.assertIn("vo.m4a", r.stdout, "vo_build 输出 vo.m4a")
        # concat 自动带上 voice/subtitles（sound 产出）
        self.assertIn("--voice", r.stdout)
        self.assertIn("--subtitles", r.stdout)

    def test_sound_absent_skipped(self):
        """优化④：无 vo_lines.json 时不跑 sound（纯画面/自己混音是合法选项）。"""
        self._plan(mode="full")
        r = self._dry("--no-lint")
        self.assertNotIn("vo_build", r.stdout)
        self.assertNotIn("--voice", r.stdout)

    def test_watermark_stage_dry_run_only(self):
        """优化④：--stop-after watermark + --watermark <渠道> → delogo --dry-run 只列档。"""
        self._plan(mode="full")
        r = self._dry("--no-lint", "--stop-after", "watermark",
                      "--watermark", "custom", "--watermark-dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("delogo_watermark.py", r.stdout)
        self.assertIn("--provider custom", r.stdout)
        self.assertIn("--dry-run", r.stdout, "水印 dry-run 应只列待处理档")

    def test_concat_inherits_plan_xfade(self):
        """优化④：plan.json 的 xfade/freeze_last 透传给 concat（schema 已含此二键）。"""
        self._plan(mode="full", xfade=[0.5], freeze_last=1.2)
        r = self._dry("--no-lint")
        self.assertIn("--xfade 0.5", r.stdout)
        self.assertIn("--freeze-last 1.2", r.stdout)

    def test_kenburns_skips_transition_shots(self):
        """v4.6：过渡镜（transition 字段）不走 kenburns 缓推——它等邻居 clip
        出片后由 batch pass2 出真过渡视频，缓推会破坏首尾帧衔接语义。"""
        self._plan(mode="hybrid", hero_shots=["S1"])
        (self.d / "shot_01T.json").write_text(json.dumps({
            "shot_id": "S1T", "transition": {"from": "S1", "to": "S2"},
            "i2v_prompt": "slow morph"}), encoding="utf-8")
        r = self._dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        ken = [ln for ln in r.stdout.splitlines() if "kenburns" in ln and "S1T" in ln]
        self.assertEqual(len(ken), 0, f"过渡镜不该被缓推: {ken}")

    def test_hybrid_transition_not_in_only(self):
        """v4.6：hybrid --only 传 hero 镜列表给 videos 阶段；过渡镜不在 hero 里
        也不该被 concat 前遗忘——它的出片靠用户手工跑 batch（两趟调度），
        pipeline 不替用户拍板（与 hero 选择同级的一等公民决策）。此测试钉住
        --only 只含 hero、不自动附加过渡镜（防止未来悄悄改语义）。"""
        self._plan(mode="hybrid", hero_shots=["S1"])
        (self.d / "shot_01T.json").write_text(json.dumps({
            "shot_id": "S1T", "transition": {"from": "S1", "to": "S2"},
            "i2v_prompt": "slow morph"}), encoding="utf-8")
        r = self._dry()
        self.assertIn("--only S1", r.stdout)


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestExportSafety(unittest.TestCase):
    """P3-2/P3-3：导出目标防护 + 语法校验动态化。"""

    def test_dst_name_guard(self):
        """目标目录名不含 reelcraft_public → 拒绝，防误配误删。"""
        import subprocess as sp
        r = sp.run([sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "export_public.py"),
                    str(Path(tempfile.mkdtemp()) / "somewhere_else")],
                   capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, "非法目标必须拒绝")
        self.assertIn("reelcraft_public", r.stdout + r.stderr)




class TestPipelineVideoArgsPassthrough(unittest.TestCase):
    """S4：pipeline 视频阶段必须把 --negative/--video-size/--video-duration/--qc
    透传给 batch（v4.7 审计 P1：只透传了 provider/only/qcgate，其余用户参数被
    静默丢弃——"以为用了低配 negative，实际是默认值"）。

    用 --dry-run 验证：命令行里必须出现透传参数。plan mode=hybrid 走 --only 路径。
    """

    def _setup(self, td: Path):
        (td / "shots").mkdir()
        (td / "shots" / "plan.json").write_text(json.dumps(
            {"mode": "hybrid", "hero_shots": ["S1"]}), encoding="utf-8")
        (td / "shots" / "shot_01.json").write_text(json.dumps(
            {"shot_id": "S1", "t2i_prompt": "x", "i2v_prompt": "y"}), encoding="utf-8")

    def _run(self, td: Path, extra):
        import subprocess as sp
        pl = Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"
        return sp.run([sys.executable, str(pl), str(td / "shots"), "--dry-run"] + extra,
                      capture_output=True, text=True, encoding="utf-8")

    def test_video_args_visible_in_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup(td)
            r = self._run(td, ["--negative", "my-neg", "--video-size", "1280x720",
                                "--video-duration", "medium", "--qc"])
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            # videos 阶段命令必须带上全部四个参数
            self.assertIn("--negative", r.stdout)
            self.assertIn("my-neg", r.stdout)
            self.assertIn("--video-size 1280x720", r.stdout)
            self.assertIn("--video-duration medium", r.stdout)
            self.assertIn("--qc", r.stdout)

    def test_defaults_not_passed_when_absent(self):
        """未给参数时不追加（不改变 batch 默认行为）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup(td)
            r = self._run(td, [])
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            self.assertNotIn("--negative", r.stdout)
            self.assertNotIn("--video-size", r.stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)

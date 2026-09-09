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


if __name__ == '__main__':
    unittest.main(verbosity=2)

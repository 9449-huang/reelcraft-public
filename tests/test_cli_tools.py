# -*- coding: utf-8 -*-
"""copy/envcheck/clean/lint 等 CLI 工具测试——按模块拆分自 test_media_gen.py。

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



class TestCleanText(unittest.TestCase):
    """#10 copy 清洗：只剥序号形态，数字开头的中文文案不误伤。"""

    _copy_mod = None

    @classmethod
    def _load_copy(cls):
        # scripts/copy.py 与标准库 copy 同名，必须按文件路径加载（避免 sys.modules 缓存撞名）
        if cls._copy_mod is None:
            import importlib.util
            src = Path(__file__).resolve().parents[1] / "scripts" / "copy.py"
            spec = importlib.util.spec_from_file_location("reelcraft_copy", src)
            cls._copy_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls._copy_mod)
        return cls._copy_mod

    def _c(self, t):
        return self._load_copy().clean_text(t)

    def test_strip_arabic_number_dot(self):
        self.assertEqual(self._c("1. 标题"), "标题")

    def test_strip_number_dunhao(self):
        self.assertEqual(self._c("2、副标题"), "副标题")

    def test_strip_cn_parenthesis(self):
        self.assertEqual(self._c("（3）第三条"), "第三条")

    def test_keep_chinese_starting_with_digit(self):
        self.assertEqual(self._c("30年的墨"), "30年的墨")   # 旧实现会误伤成"年的墨"

    def test_mixed_lines(self):
        self.assertEqual(self._c("1. 甲\n乙\n30年的墨"),
                         "甲\n乙\n30年的墨")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestCopyMainEndToEnd(unittest.TestCase):
    """copy.py main() 端到端冒烟：必须真的落盘。

    背景（2026-09-07 复核发现）：#10 重构把 `def clean_text` 以 0 缩进插进了 main 体内，
    main 提前结束、后续调用链变成 return 之后的死代码，CLI 静默 exit 0 不产出任何文案。
    原有测试只 import 了 clean_text 单函数，46 项全绿却完全没发现 —— 必须钉住 main 这一层。
    """

    def test_main_writes_cleaned_file(self):
        import importlib.util
        src = Path(__file__).resolve().parents[1] / "scripts" / "copy.py"
        spec = importlib.util.spec_from_file_location("reelcraft_copy_main", src)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as td:
            outp = Path(td) / "cands.txt"
            saved = dict(os.environ)
            argv = sys.argv
            try:
                os.environ["MEDIA_AGNES_1_KEY"] = "test-key"
                os.environ["MEDIA_AGNES_1_BASE"] = "http://example.invalid/v1"
                sys.argv = ["copy.py", "--brief", "测试", "--count", "3",
                            "--out", str(outp)]
                with mock.patch.object(mod, "chat",
                                       return_value="1. 慢，是一种功夫\n2. 30年的墨\n"):
                    mod.main()
            finally:
                sys.argv = argv
                os.environ.clear()
                os.environ.update(saved)
            self.assertTrue(outp.exists(), "main() 未落盘——函数体被截断了？")
            self.assertEqual(outp.read_text(encoding="utf-8").strip(),
                             "慢，是一种功夫\n30年的墨")


class TestEnvcheck(unittest.TestCase):
    """#65 envcheck：外部环境自检。深模块：run_checks 依赖注入返回结果，不打印不猜。"""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.ec = importlib.import_module("envcheck")

    def test_scan_key_env_gap_detected(self):
        """序号断档（#1 #3 有 #2 缺）必须报出——list_keys 会静默 break，这里不能吞。"""
        env = {"MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://x",
               "MEDIA_AGNES_3_KEY": "k3", "MEDIA_AGNES_3_BASE": "https://x"}
        r = self.ec.scan_key_env("agnes", env=env)
        self.assertEqual(r["ns"], [1, 3])
        self.assertIn(2, r["gaps"], "#2 缺失是断档，要报")
        self.assertTrue(r["usable"])

    def test_scan_key_env_key_without_base(self):
        env = {"MEDIA_AGNES_1_KEY": "k1"}     # 有 KEY 无 BASE
        r = self.ec.scan_key_env("agnes", env=env)
        self.assertEqual(r["missing_base"], [1], "KEY 在 BASE 缺 → 这把 key 不可用")
        self.assertFalse(r["usable"])

    def test_scan_key_env_healthy(self):
        env = {"MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://x",
               "MEDIA_AGNES_2_KEY": "k2", "MEDIA_AGNES_2_BASE": "https://y"}
        r = self.ec.scan_key_env("agnes", env=env)
        self.assertEqual(r["ns"], [1, 2])
        self.assertEqual(r["gaps"], [])
        self.assertEqual(r["missing_base"], [])
        self.assertTrue(r["usable"])

    def test_scan_key_env_base_without_key_ignored(self):
        """只有 BASE 没 KEY：不算断档也不算 missing_base（BASE 是可选配的预置）。"""
        env = {"MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://x",
               "MEDIA_AGNES_4_BASE": "https://orphan"}
        r = self.ec.scan_key_env("agnes", env=env)
        self.assertEqual(r["ns"], [1])
        self.assertEqual(r["gaps"], [], "孤 BASE 不算 key 断档")

    def test_key_value_never_appears_in_results(self):
        """key 本身绝不能进任何 detail/输出（envcheck 常被截图/贴日志）。"""
        env = {"MEDIA_AGNES_1_KEY": "sk-SECRET-value-123", "MEDIA_AGNES_1_BASE": "http://127.0.0.1:9"}
        results = self.ec.run_checks(env=env, prober=lambda h, p, t=2: False)
        blob = repr(results)
        self.assertNotIn("sk-SECRET-value-123", blob, "key 值泄漏进检查结果")

    def test_run_checks_local_service_down_is_fail(self):
        """已配 base 指向 127.0.0.1/localhost → TCP 探测；不通 = fail（本地服务挂了必炸）。"""
        env = {"MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "http://127.0.0.1:59999"}
        results = self.ec.run_checks(env=env, prober=lambda h, p, t=2: False)
        fails = [r for r in results if r["level"] == "fail" and "127.0.0.1" in r["detail"]]
        self.assertTrue(fails, "本地 base 不通必须 fail")

    def test_run_checks_remote_base_not_probed(self):
        """远程 API base 不做 TCP 探测（能力看 caps，网络抖动不当环境故障）。"""
        env = {"MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://api.example.com/v1"}
        probed = []
        results = self.ec.run_checks(env=env,
                                     prober=lambda h, p, t=2: probed.append((h, p)) or True)
        self.assertEqual(probed, [], "远程 base 不该发起探测")
        self.assertTrue(all(r["level"] != "fail" or "api.example.com" not in r["detail"]
                            for r in results))

    def test_summarize_rc_rules(self):
        ok = [{"check": "a", "level": "ok", "detail": "x"}]
        warn = ok + [{"check": "b", "level": "warn", "detail": "y"}]
        fail = warn + [{"check": "c", "level": "fail", "detail": "z"}]
        self.assertEqual(self.ec.summarize(ok)[0], 0)
        self.assertEqual(self.ec.summarize(warn)[0], 0, "warn 不挂（提示性质）")
        self.assertEqual(self.ec.summarize(fail)[0], 1, "有 fail 必须 exit 1")

    def test_caps_corrupt_is_warn(self):
        # 重定向到 tmp：CAPS_FILE 是用户真实文件，测试不许写坏/删除它（本测试曾踩此坑）
        real = self.ec.CAPS_FILE
        self.ec.CAPS_FILE = Path(tempfile.mkdtemp()) / "caps.json"
        try:
            self.ec.CAPS_FILE.write_text("{ broken", encoding="utf-8")
            results = self.ec.check_caps()
            self.assertTrue(any(r["level"] == "warn" for r in results), "坏 caps 是 warn 不是 fail")
        finally:
            self.ec.CAPS_FILE = real


class TestClean(unittest.TestCase):
    """#66 clean：工作区产物治理。默认 scan-only，--yes 移入 .trash（绝不直接删）。"""

    @classmethod
    def setUpClass(cls):
        import pipeline as pl
        cls.pl = pl

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        # 造一套典型工作区
        (self.d / "shots").mkdir()
        (self.d / "shots" / "shot_01.json").write_text("{}", encoding="utf-8")
        (self.d / "plan.json").write_text("{}", encoding="utf-8")
        (self.d / "frames").mkdir()
        (self.d / "frames" / "S01.png").write_bytes(b"x")
        (self.d / "clips").mkdir()
        (self.d / "clips" / "clip_S01.mp4").write_bytes(b"x")
        (self.d / "final.mp4").write_bytes(b"x")
        (self.d / "batch_run.json").write_text("{}", encoding="utf-8")
        (self.d / "qc_frames").mkdir()
        (self.d / "qc_frames" / "f1.png").write_bytes(b"x")

    def tearDown(self):
        self._td.cleanup()

    def test_plan_classifies_regenerable_vs_keep(self):
        plan = self.pl.plan_clean(self.d)
        removed = {str(p.relative_to(self.d)) for p in plan["remove"]}
        kept = {str(p.relative_to(self.d)) for p in plan["keep"]}
        self.assertIn(str(Path("frames") / "S01.png"), removed, "frames 是可再生中间产物")
        self.assertIn(str(Path("clips") / "clip_S01.mp4"), removed)
        self.assertIn(str(Path("qc_frames") / "f1.png"), removed)
        self.assertIn(str(Path("shots") / "shot_01.json"), kept, "shots JSON 是源，绝不清")
        self.assertIn("plan.json", kept)
        self.assertIn("final.mp4", kept)
        self.assertIn("batch_run.json", kept, "账单/断点记录保留")

    def test_plan_is_scan_only(self):
        self.pl.plan_clean(self.d)
        self.assertTrue((self.d / "frames" / "S01.png").exists(), "scan 不许动文件")

    def test_execute_moves_to_trash_not_delete(self):
        plan = self.pl.plan_clean(self.d)
        n = self.pl.execute_clean(plan, self.d)
        self.assertGreaterEqual(n, 3)
        self.assertFalse((self.d / "frames" / "S01.png").exists())
        self.assertTrue((self.d / "final.mp4").exists(), "保留清单不该被移走")
        trash = self.d / ".trash"
        moved = list(trash.rglob("S01.png"))
        self.assertEqual(len(moved), 1, "移除物必须能从 .trash 找回")

    def test_purge_empties_trash(self):
        plan = self.pl.plan_clean(self.d)
        self.pl.execute_clean(plan, self.d)
        self.pl.purge_trash(self.d)
        trash = self.d / ".trash"
        self.assertFalse(trash.exists() and any(trash.iterdir()), "purge 后 .trash 应为空/不存在")


class TestNegativeTemplates(unittest.TestCase):
    """优化⑧：负面模板按题材分档——人物/风景/产品自动选，未命中兜底。"""

    def test_character_detected(self):
        v = mg_core.negative_for_shot({"type": "Character Concept Art"})
        self.assertIn("distorted faces", v)
        self.assertIn("face swap", v)

    def test_landscape_detected(self):
        v = mg_core.negative_for_shot({"subject": "mountain landscape at dawn"})
        self.assertIn("chromatic aberration", v)

    def test_product_detected(self):
        v = mg_core.negative_for_shot({"type": "商品主图", "subject": "陶瓷瓶"})
        self.assertIn("specular blowout", v)

    def test_fallback_generic(self):
        v = mg_core.negative_for_shot({"subject": "ink painting"})
        self.assertEqual(v, mg_core.NEGATIVE_GENERIC)

    def test_explicit_fallback_wins(self):
        v = mg_core.negative_for_shot({"subject": "ink"}, fallback="CUSTOM-X")
        self.assertEqual(v, "CUSTOM-X")


class TestPromptLint(unittest.TestCase):
    """优化②：prompt 静态 lint——词级约束（slop/词数/i2v subject）从 agent 自觉变机器强制。"""

    def setUp(self):
        import prompt_lint
        self.pl = prompt_lint

    def test_hard_slop_hit(self):
        v = self.pl.lint_prompt("a beautiful masterpiece, high quality poster",
                                "t2i", *self.pl.default_lexicon())
        levels = {i["level"] for i in v}
        self.assertIn("FAIL", levels)
        words = {i.get("word") for i in v}
        self.assertTrue({"beautiful", "masterpiece"} <= words, words)

    def test_word_count_range_t2i(self):
        hard, mood = self.pl.default_lexicon()
        too_short = self.pl.lint_prompt("a red circle", "t2i", hard, mood)
        self.assertIn("WARN", {i["level"] for i in too_short})
        long = "word " * 300
        too_long = self.pl.lint_prompt(long, "t2i", hard, mood)
        self.assertIn("FAIL", {i["level"] for i in too_long})

    def test_i2v_word_count_is_strict(self):
        """i2v 40-80 词：超量（写了静态细节）应 FAIL。"""
        hard, mood = self.pl.default_lexicon()
        v = self.pl.lint_prompt("word " * 200, "i2v", hard, mood)
        self.assertIn("FAIL", {i["level"] for i in v})

    def test_i2v_restates_subject_warns(self):
        """i2v 误写 subject（首帧已锁定）：shot 的 subject 字段值 token 出现在 i2v_prompt → WARN。"""
        shot = {"subject": "a black lacquered ink stone with silver edges",
                "i2v_prompt": "The black stone stays still, the brush lifts slowly."}
        hard, mood = self.pl.default_lexicon()
        v = self.pl.lint_shot(shot, "videos", hard, mood)
        levels = {i["level"] for i in v}
        self.assertIn("WARN", levels)
        self.assertTrue(any("subject" in i["msg"] for i in v))

    def test_clean_prompt_passes(self):
        hard, mood = self.pl.default_lexicon()
        t2i = ("A weathered ceramic teacup on a pine table, morning light rakes across "
               "the cracked glaze, dust motes drift through the window beam. The cup's "
               "crack runs from the rim down to the base, edges catching the light.") * 4
        self.assertEqual(self.pl.lint_prompt(t2i, "t2i", hard, mood), [])

    def test_parse_lexicon_md(self):
        """词表解析：md 缺第一节/格式异常回退内置，不崩。"""
        hard, mood = self.pl.parse_lexicon_md(Path("不存在的文件.md"))
        self.assertGreaterEqual(len(hard), len(self.pl.HARD_SLOP))
        self.assertGreaterEqual(len(mood), len(self.pl.MOOD_SLOP))


class TestPromptLintDecisionPath(unittest.TestCase):
    """优化⑨·盲区1：prompt 决策路径——batch 出镜前 lint 的 PASS/FAIL 判定。
    此前只测了 lint 纯函数，没测 batch 侧"FAIL 记 FAIL 不重试不提交"的决策。"""

    def test_pass_batch_snapshot(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            j = Path(td) / "shot_01.json"
            j.write_text(json.dumps({"shot_id": "S01",
                                     "i2v_prompt": "The brush lifts slowly from the "
                                     "inkstone, a single droplet stretches, camera "
                                     "holds still." * 3}), encoding="utf-8")
            self.assertEqual(mg_batch.prompt_lint_snapshot(j, "videos"), "PASS")

    def test_fail_batch_snapshot(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            j = Path(td) / "shot_01.json"
            j.write_text(json.dumps({"shot_id": "S01",
                                     "i2v_prompt": "stunning masterpiece, high quality"}),
                         encoding="utf-8")
            self.assertEqual(mg_batch.prompt_lint_snapshot(j, "videos"), "FAIL")


class TestLintWordCountZh(unittest.TestCase):
    """P2-5 修复：中文按信息密度折算计词（≈1.8 字/词），纯中文不再逐字爆表。"""

    def test_wc_english_unchanged(self):
        import prompt_lint as pl
        self.assertEqual(pl._wc("a cat sits on the windowsill"), 6)

    def test_wc_zh_compressed(self):
        import prompt_lint as pl
        n = pl._wc("今天天气真好我们一起去公园散步")
        self.assertLess(n, 15, "15 个中文字不应按 15 词计")
        self.assertEqual(n, 9)   # ceil(15/1.8)=9

    def test_wc_mixed(self):
        import prompt_lint as pl
        n = pl._wc("a cat 在阳光下 walking across the garden")
        self.assertEqual(n, 9, "英文 6 词 + 中文 4 字 ceil(4/1.8)=3 → 9")

    def test_zh_300_not_fail_over_220(self):
        """纯中文 300 字 t2i：折算 167 词 < 220，不再被逐字计数误杀成 FAIL。"""
        import prompt_lint as pl
        text = "山清水秀的小村庄在晨雾中苏醒" * 20   # 300 字
        issues = pl.lint_prompt(text, "t2i", ["zzzz-no-hit"], [], {})
        self.assertFalse(any(i["level"] == "FAIL" for i in issues), issues)


class TestNegativeWordBoundary(unittest.TestCase):
    """P3-1 修复：英文关键词词边界匹配——"nature" 不再误中 "naturally"。"""

    def test_naturally_not_landscape(self):
        v = mg_core.negative_for_shot({"subject": "naturally occurring crystal"})
        self.assertNotIn("chromatic aberration", v, "naturally 不该触发 landscape 档")

    def test_produce_not_product(self):
        v = mg_core.negative_for_shot({"subject": "produce fresh organic"})
        self.assertNotIn("specular blowout", v, "produce 不该触发 product 档")

    def test_real_word_still_matches(self):
        v = mg_core.negative_for_shot({"subject": "nature documentary b-roll"})
        self.assertIn("chromatic aberration", v, "nature 独立成词仍应命中 landscape")

    def test_zh_keyword_still_substring(self):
        v = mg_core.negative_for_shot({"subject": "风景如画的湖畔"})
        self.assertIn("chromatic aberration", v, "中文关键词保持子串匹配")


class TestReviewFixBatch(unittest.TestCase):
    """v3.1.13 复核修复批：cmd_tts 原子写/voice 优先级/槽位扫描、probe 缓存、
    triage 多 key 扫描、envcheck 空值口径、purge 容错。"""

    def test_scan_tts_slots_dynamic(self):
        """槽位扫描：起始空号跳过、断档容忍、连续 3 空号停（不再封死 19）。"""
        env = {"MEDIA_TTS_2_KEY": "k", "MEDIA_TTS_2_BASE": "https://b",
               "MEDIA_TTS_5_KEY": "k5", "MEDIA_TTS_5_BASE": "https://c"}
        self.assertEqual(mg._scan_tts_slots(env), [2, 5])
        self.assertEqual(mg._scan_tts_slots({}), [])

    def test_scan_tts_slots_empty_value_not_configured(self):
        """KEY="" 是未配置不是已配（与 mg_core.list_keys 口径一致）。"""
        env = {"MEDIA_TTS_1_KEY": "", "MEDIA_TTS_1_BASE": "",
               "MEDIA_TTS_2_KEY": "k", "MEDIA_TTS_2_BASE": "https://b"}
        self.assertEqual(mg._scan_tts_slots(env), [2])

    def test_tts_cli_voice_overrides_env(self):
        """CLI --voice 显式传入必须赢过 env 默认音色。"""
        import argparse
        reqs = []
        ns = argparse.Namespace(text="x", text_file="", out=str(Path(tempfile.mkdtemp()) / "o.mp3"),
                                voice="cli-voice", emotion="", speed=1.0)
        env = {"MEDIA_TTS_1_KEY": "k1", "MEDIA_TTS_1_BASE": "https://alpha/v1",
               "MEDIA_TTS_1_VOICE": "env-voice"}
        resp = type("Resp", (), {"__enter__": lambda s: s, "__exit__": lambda s, *a: False,
                                  "read": lambda s: b"a" * 128})()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(mg.urllib.request, "urlopen",
                                   side_effect=lambda r, timeout=300: reqs.append(r) or resp):
                mg.cmd_tts(ns)
        import json as _j
        self.assertEqual(_j.loads(reqs[0].data)["voice"], "cli-voice")

    def test_tts_all_fail_warns_stale_out(self):
        """全 key 失败时若 out 是旧文件，stderr 必须警告防下游复用。"""
        import argparse, io, contextlib
        out = Path(tempfile.mkdtemp()) / "o.mp3"
        out.write_bytes(b"old-audio")
        ns = argparse.Namespace(text="x", text_file="", out=str(out),
                                voice="", emotion="", speed=1.0)
        env = {"MEDIA_TTS_1_KEY": "k1", "MEDIA_TTS_1_BASE": "https://alpha/v1"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(mg.urllib.request, "urlopen",
                                   side_effect=OSError("down")):
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as cm:
                        mg.cmd_tts(ns)
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("旧文件", err.getvalue())

    def test_probe_cached_by_path(self):
        """同一路径第二次 probe 不再起 ffmpeg（audit/triage 双倍进程的根修）。"""
        import postprocess as pp
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "v.mp4"
            f.write_bytes(b"x")
            calls = []
            with mock.patch.object(pp, "run_capture",
                                   side_effect=lambda c: calls.append(1) or
                                   type("R", (), {"stderr": "Duration: 00:00:05.00"})()):
                pp._PROBE_CACHE.clear()
                a = pp.probe(str(f))
                b = pp.probe(str(f))
                self.assertEqual(len(calls), 1, "第二次必须命中缓存")
                self.assertEqual(a, b)

    def test_triage_scan_tts_slot_finds_n2(self):
        """key 配在 TTS_2 时能扫到（旧版只读 TTS_1 会误报未配置）。"""
        import audio_triage as at
        env = {"MEDIA_TTS_2_KEY": "k", "MEDIA_TTS_2_BASE": "https://sf/v1"}
        self.assertEqual(at._scan_tts_slot(env), (2, "https://sf/v1", "k"))
        self.assertEqual(at._scan_tts_slot({}), (0, "", ""))

    def test_scan_key_env_empty_value_is_gap(self):
        """KEY="" 不算已配：断档口径与 list_keys 一致。"""
        import envcheck as ec
        env = {"MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://x",
               "MEDIA_AGNES_3_KEY": "k3", "MEDIA_AGNES_3_BASE": "https://y"}
        r = ec.scan_key_env("agnes", env=env)
        self.assertEqual(r["ns"], [1, 3])

    def test_purge_trash_skips_locked_file(self):
        """Windows 文件被占用：purge 跳过该文件不炸，其余正常删。"""
        import pipeline as pl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trash = root / ".trash" / "ts"
            trash.mkdir(parents=True)
            (trash / "a.mp4").write_bytes(b"a")
            locked = trash / "b.mp4"
            locked.write_bytes(b"b")
            real_unlink = Path.unlink

            def fake_unlink(self, missing_ok=False):
                if self.name == "b.mp4":
                    raise PermissionError("file in use")
                return real_unlink(self, missing_ok=missing_ok)
            with mock.patch.object(Path, "unlink", fake_unlink):
                n = pl.purge_trash(root)
            self.assertEqual(n, 1, "只删掉没被占用的 a.mp4")
            self.assertTrue(locked.exists(), "被占用文件必须幸存")

    def test_envcheck_badge_ascii_only(self):
        """envcheck 报告 badge 必须 ASCII（裸 cmd 无 PYTHONUTF8 不崩）。"""
        import envcheck as ec
        rc, text = ec.summarize([{"check": "c", "level": "fail", "detail": "d"}])
        self.assertEqual(rc, 1)
        self.assertTrue(all(ord(ch) < 128 for ch in
                            text.split("\n")[0]), "badge 行必须纯 ASCII")

    def test_tts_empty_response_fails_over(self):
        """200 但空体（<64B）不能落盘——必须降级下一把 key。"""
        import argparse
        reqs = []
        ns = argparse.Namespace(text="x", text_file="", out=str(Path(tempfile.mkdtemp()) / "o.mp3"),
                                voice="", emotion="", speed=1.0)
        env = {"MEDIA_TTS_1_KEY": "k1", "MEDIA_TTS_1_BASE": "https://alpha/v1",
               "MEDIA_TTS_2_KEY": "k2", "MEDIA_TTS_2_BASE": "https://beta/v1"}

        def fake(req, timeout=300):
            reqs.append(req.full_url)

            class Resp:
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return b"" if "alpha" in req.full_url else b"real-audio" + b"x" * 128
            return Resp()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(mg.urllib.request, "urlopen", side_effect=fake):
                mg.cmd_tts(ns)
        self.assertEqual(len(reqs), 2, "空体必须触发 failover")
        self.assertEqual(Path(ns.out).read_bytes(), b"real-audio" + b"x" * 128)

    def test_triage_profile_hit_skips_transcribe(self):
        """档案命中（--pool 未 --refresh）直接用结论，不再转录（免烧额度）。"""
        import audio_triage as at
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip_S01.mp4"
            clip.write_bytes(b"x")
            prof = Path(td) / "prof.json"
            with mock.patch.object(at, "PROFILE_FILE", prof):
                at.record_profile("model-x", {"verdict": "no", "has_audio": True,
                                              "detail": "自带中文人声"})
                with mock.patch.object(at, "probe", side_effect=AssertionError("不许 probe")):
                    r = at.triage_one(clip, pool="model-x")
            self.assertEqual(r["verdict"], "no")
            self.assertIn("档案命中", r["reason"])

    def test_triage_refresh_bypasses_profile(self):
        """--refresh 忽略档案强制重测。"""
        import audio_triage as at
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip_S01.mp4"
            clip.write_bytes(b"x")
            prof = Path(td) / "prof.json"
            with mock.patch.object(at, "PROFILE_FILE", prof):
                at.record_profile("model-x", {"verdict": "no", "has_audio": True})
                with mock.patch.object(at, "probe", return_value={}) as pr:
                    r = at.triage_one(clip, pool="model-x", refresh=True)
                pr.assert_called_once()
            self.assertEqual(r["verdict"], "yes", "哑片（probe 无 audio）→ 补")


class TestListShotFiles(unittest.TestCase):
    """分镜 JSON 枚举必须跨平台去重。

    Windows 的文件 glob 大小写不敏感：`glob("S*.json")` 与 `glob("shot_*.json")`
    都会匹配到 `shot_01.json` → 不去重则每镜被算两次（mg_batch 会双跑、
    pipeline 会双缓推）。Linux/macOS 大小写敏感不复现，所以只在 Windows 静默出错。
    本测试在任何平台都应保证"同一文件只出现一次"。
    """

    def test_no_duplicate_across_case_glob(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for n in ("01", "02", "10"):
                (d / f"shot_{n}.json").write_text("{}", encoding="utf-8")
            got = mg_core.list_shot_files(d)
            names = [p.name for p in got]
            self.assertEqual(len(names), len(set(names)), f"有重复: {names}")
            self.assertEqual(names, ["shot_01.json", "shot_02.json", "shot_10.json"],
                             "自然序 + 去重后应恰好每个文件一次")

    def test_s_and_shot_mixed(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "S1.json").write_text("{}", encoding="utf-8")
            (d / "shot_02.json").write_text("{}", encoding="utf-8")
            names = [p.name for p in mg_core.list_shot_files(d)]
            self.assertEqual(names, ["S1.json", "shot_02.json"])


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestBatchLastFrame(unittest.TestCase):
    """过渡镜（半自动方案一）：shot JSON 的 last_frame 字段经 batch --dry-run 透传。

    尾帧缺失时必须报 MISS 跳过（不提交——提交即扣额度）。
    """

    def _run(self, td: Path):
        import subprocess as sp
        mg = Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"
        env = {**os.environ, "PYTHONUTF8": "1",
               "MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://example.invalid/v1"}
        return sp.run([sys.executable, str(mg), "batch", str(td),
                       "--phase", "videos", "--provider", "agnes",
                       "--workers", "1", "--dry-run"],
                      capture_output=True, text=True, encoding="utf-8", env=env)

    def test_last_frame_passed_through(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "frames").mkdir()
            (td / "frames" / "S1.png").write_bytes(b"x")       # 首帧存在
            (td / "next.png").write_bytes(b"y")                 # 尾帧存在
            (td / "shot_01.json").write_text(json.dumps({
                "shot_id": "S1", "t2i_prompt": "x", "i2v_prompt": "y",
                "last_frame": str(td / "next.png")}), encoding="utf-8")
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("--last-frame", r.stdout)
            self.assertIn("next.png", r.stdout)

    def test_missing_last_frame_reports_miss(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "frames").mkdir()
            (td / "frames" / "S1.png").write_bytes(b"x")
            (td / "shot_01.json").write_text(json.dumps({
                "shot_id": "S1", "t2i_prompt": "x", "i2v_prompt": "y",
                "last_frame": str(td / "ghost.png")}), encoding="utf-8")
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("MISS (last_frame", r.stdout)


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestBatchTransition(unittest.TestCase):
    """v4.6 方案二：过渡镜（transition 字段）一等公民——编排理解 DAG 依赖。

    过渡镜 = 独立 shot JSON（如 S01T，natkey 排序天然落在 S01 与 S02 之间），
    带 transition={"from": "S01", "to": "S02"}：无 t2i_prompt（不进 images 阶段）、
    首帧=from 镜 clip 末帧（编排自动抽）、尾帧=to 镜首帧。
    batch videos 两趟调度：pass1 普通镜 → 抽末帧 → pass2 过渡镜。
    缺依赖 = MISS 跳过不提交（提交即扣额度）。
    """

    def _run(self, td: Path, phase="videos", extra=None):
        import subprocess as sp
        mg = Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"
        env = {**os.environ, "PYTHONUTF8": "1",
               "MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://example.invalid/v1"}
        cmd = [sys.executable, str(mg), "batch", str(td),
               "--phase", phase, "--provider", "agnes",
               "--workers", "1", "--dry-run"]
        if extra:
            cmd += extra
        return sp.run(cmd, capture_output=True, text=True,
                      encoding="utf-8", env=env)

    def _setup_shots(self, td: Path):
        """S1 + S2 普通镜 + S1T 过渡镜（依赖 S1 末帧 + S2 首帧）。"""
        (td / "frames").mkdir()
        (td / "clips").mkdir()
        (td / "frames" / "S1.png").write_bytes(b"f1")
        (td / "frames" / "S2.png").write_bytes(b"f2")
        (td / "shot_01.json").write_text(json.dumps({
            "shot_id": "S1", "t2i_prompt": "x", "i2v_prompt": "y"}), encoding="utf-8")
        (td / "shot_02.json").write_text(json.dumps({
            "shot_id": "S2", "t2i_prompt": "x", "i2v_prompt": "y"}), encoding="utf-8")
        (td / "shot_01T.json").write_text(json.dumps({
            "shot_id": "S1T", "transition": {"from": "S1", "to": "S2"},
            "i2v_prompt": "slow morph"}), encoding="utf-8")

    def test_transition_excluded_from_images_phase(self):
        """过渡镜无 t2i_prompt → images 阶段不跑（首帧来自邻居，不是 t2i）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup_shots(td)
            r = self._run(td, phase="images")
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            # 排除方式 = 打 skip 标签、绝不构造生成命令（不烧 t2i 额度）
            self.assertIn("skip (transition)", r.stdout)
            ti_lines = [ln for ln in r.stdout.splitlines() if "S1T" in ln]
            for ln in ti_lines:
                self.assertIn("[skip (transition)]", ln,
                              f"过渡镜 images 阶段只允许 skip，不允许生成命令: {ln}")

    def test_transition_missing_deps_reports_miss(self):
        """依赖未就绪（S1 无 clip）→ 报 MISS 不提交。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup_shots(td)
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("MISS (transition", r.stdout)

    def test_transition_ready_builds_two_pass(self):
        """依赖就绪（S1 有 clip + 抽出末帧）→ 两趟调度：先普通镜后过渡镜，
        过渡镜命令带 --image <抽的末帧> 和 --last-frame <S2 首帧>。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup_shots(td)
            (td / "clips" / "clip_S1.mp4").write_bytes(b"c1")
            # 抽末帧步骤会产出 frames/S1T_seed.png（模拟已抽）
            (td / "frames" / "S1T_seed.png").write_bytes(b"seed")
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("S1T", r.stdout)
            self.assertIn("--last-frame", r.stdout)
            self.assertIn("S1T_seed.png", r.stdout)

    def test_transition_orphan_rejected(self):
        """transition 的 from/to 指向不存在的镜 → die 提示（防静默 MISS 死循环）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "frames").mkdir()
            (td / "shot_01T.json").write_text(json.dumps({
                "shot_id": "S1T", "transition": {"from": "S1", "to": "S9"},
                "i2v_prompt": "x"}), encoding="utf-8")
            r = self._run(td)
            self.assertNotEqual(r.returncode, 0, "孤儿过渡镜必须 die 而非静默 MISS")
            self.assertIn("S9", r.stdout + r.stderr)

    def test_transition_seed_stale_reextracts(self):
        """失效链：from 镜 clip 比 seed 新（邻居重拍）→ 报 STALE 提示重抽。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup_shots(td)
            (td / "clips" / "clip_S1.mp4").write_bytes(b"c1")
            seed = td / "frames" / "S1T_seed.png"
            seed.write_bytes(b"seed")
            # clip 比 seed 新（邻居重拍后）
            import os as _os
            old = seed.stat().st_mtime - 100
            _os.utime(td / "clips" / "clip_S1.mp4", (old + 200, old + 200))
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("STALE", r.stdout)


class TestBuildVideoPayload(unittest.TestCase):
    """v4.7：video payload 构造提为纯函数，钉死三种首尾帧承载方式的差异。

    - Agnes 风 keyframes：extra_body={"image":[首,尾],"mode":"keyframes"}，**不传顶层首帧**
    - custom 池风：首帧走 image_param，尾帧走独立字段名（last_frame_param）
    - 无尾帧：老行为（顶层 image 单帧 i2v）不变
    """

    def _args(self, **kw):
        import argparse
        base = dict(prompt="p", image="", last_frame="", num_frames=121,
                    negative="neg", video_size="", video_duration="")
        base.update(kw)
        return argparse.Namespace(**base)

    def test_agnes_keyframes_uses_extra_body(self):
        info = {"keyframes_style": "extra_body", "supports_num_frames": True,
                "supports_negative": True}
        a = self._args(image="/tmp/first.png", last_frame="/tmp/last.png")
        with mock.patch("media_gen.image_to_uri_shrunk", side_effect=lambda p: f"URL({p})"):
            payload = mg.build_video_payload(info, {}, a, "1024x1024", None, "agnes-video-v2.0")
        self.assertEqual(payload["extra_body"]["mode"], "keyframes")
        self.assertEqual(payload["extra_body"]["image"],
                         ["URL(/tmp/first.png)", "URL(/tmp/last.png)"])
        self.assertNotIn("image", payload, "keyframes 模式不得再传顶层首帧")
        self.assertEqual(payload["num_frames"], 121)
        self.assertEqual(payload["negative_prompt"], "neg")

    def test_keyframes_without_image_dies(self):
        info = {"keyframes_style": "extra_body"}
        a = self._args(image="", last_frame="/tmp/last.png")
        with self.assertRaises(SystemExit) as cm:
            mg.build_video_payload(info, {}, a, "", None, "m")
        self.assertEqual(cm.exception.code, 2, "keyframes 必须双帧齐全")

    def test_custom_pool_last_frame_field(self):
        """custom 池风（可灵系字段名）：首帧 image_param + 尾帧独立字段，老行为不变。"""
        info = {"image_param": "image", "last_frame_param": "tail_image",
                "last_frame_list": False}
        a = self._args(image="/tmp/f.png", last_frame="/tmp/l.png")
        with mock.patch("media_gen.image_to_uri_shrunk", side_effect=lambda p: f"URL({p})"):
            payload = mg.build_video_payload(info, {}, a, "", None, "m")
        self.assertEqual(payload["image"], "URL(/tmp/f.png)")
        self.assertEqual(payload["tail_image"], "URL(/tmp/l.png)")
        self.assertNotIn("extra_body", payload)

    def test_plain_i2v_unchanged(self):
        info = {"image_param": "image", "supports_num_frames": True}
        a = self._args(image="/tmp/f.png")
        with mock.patch("media_gen.image_to_uri_shrunk", side_effect=lambda p: f"URL({p})"):
            payload = mg.build_video_payload(info, {}, a, "", None, "m")
        self.assertEqual(payload["image"], "URL(/tmp/f.png)")
        self.assertNotIn("extra_body", payload)


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestBatchRefImage(unittest.TestCase):
    """角色一致性（v4.7.1）：shot JSON 的 ref_image → batch images 阶段透传 --ref-image。

    意义：v4.7 只加了单命令 `image --ref-image`，批量主流程用不了；本批把它接进 batch。
    相对路径按 shots 根解析（shot JSON 就在 shots/ 下，写 "char_hero.png" 即可）。
    """

    def _run(self, td: Path, phase="images"):
        import subprocess as sp
        mg = Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"
        env = {**os.environ, "PYTHONUTF8": "1",
               "MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://example.invalid/v1"}
        return sp.run([sys.executable, str(mg), "batch", str(td),
                       "--phase", phase, "--provider", "agnes",
                       "--workers", "1", "--dry-run"],
                      capture_output=True, text=True, encoding="utf-8", env=env)

    def test_ref_image_passed_through(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "frames").mkdir()
            (td / "char_hero.png").write_bytes(b"c")          # 角色设定图（shots 根下）
            (td / "shot_01.json").write_text(json.dumps({
                "shot_id": "S1", "t2i_prompt": "x", "i2v_prompt": "y",
                "ref_image": ["char_hero.png"]}), encoding="utf-8")
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("--ref-image", r.stdout)
            self.assertIn("char_hero.png", r.stdout)

    def test_ref_image_string_form(self):
        """字符串单图也支持（不必写成列表）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "frames").mkdir()
            (td / "hero.png").write_bytes(b"c")
            (td / "shot_01.json").write_text(json.dumps({
                "shot_id": "S1", "t2i_prompt": "x", "ref_image": "hero.png"}),
                encoding="utf-8")
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("--ref-image", r.stdout)

    def test_ref_image_missing_reports_miss(self):
        """参考图缺失 → MISS 不提交（提交即扣额度）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "frames").mkdir()
            (td / "shot_01.json").write_text(json.dumps({
                "shot_id": "S1", "t2i_prompt": "x", "ref_image": ["ghost.png"]}),
                encoding="utf-8")
            r = self._run(td)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("MISS (ref_image", r.stdout)


class TestRunShotOnce(unittest.TestCase):
    """pass1（多 worker）与 pass2（过渡镜串行）共用同一执行器 run_shot_once。

    之前两份复制逻辑 → pass2 漏掉 qcgate/qc（locality 崩塌）。本测试钉死：
    ①qcgate 必须被调用 ②结果记录格式与 pass1 一致。
    """

    def _args(self, **kw):
        import argparse
        base = dict(phase="videos", retries=1, no_lint=True, qcgate=False,
                    qcgate_strict=False, qc=False, negative="x",
                    video_size="", video_duration="")
        base.update(kw)
        return argparse.Namespace(**base)

    def _fixture(self, td: Path, create_out=True) -> Path:
        (td / "clips").mkdir()
        out = td / "clips" / "clip_S1.mp4"
        if create_out:
            out.write_bytes(b"x")
        (td / "shot_01.json").write_text("{}", encoding="utf-8")
        return out

    def test_qcgate_runs_and_ok_recorded(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = self._fixture(td)
            results: list[str] = []
            pm: dict[str, str] = {}
            gate = mock.Mock(return_value=mock.Mock(returncode=0, stdout=""))
            with mock.patch("mg_batch.subprocess.call", return_value=0), \
                 mock.patch("mg_batch.run_capture", gate):
                mg_batch.run_shot_once(td / "shot_01.json", "S1", "agnes", 1,
                                       ["python", "x"], out, self._args(qcgate=True),
                                       td / "clips", td / "clips" / "qc",
                                       results, pm, threading.Lock(), 0)
            self.assertEqual(gate.call_count, 1, "qcgate 必须被调用一次")
            gate_cmd = gate.call_args[0][0]
            self.assertIn("qcgate", " ".join(gate_cmd), gate_cmd)
            self.assertIn("OK S1 (agnes key#1)", results)
            self.assertEqual(pm["S1"], "agnes key#1")

    def test_failure_records_fail_and_bumps_streak(self):
        """产物不存在时 rc!=0 → 记 FAIL 并累加连续失败（worker 退场判定用）。"""
        import threading
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = self._fixture(td, create_out=False)
            results: list[str] = []
            pm: dict[str, str] = {}
            with mock.patch("mg_batch.subprocess.call", return_value=2):
                streak = mg_batch.run_shot_once(
                    td / "shot_01.json", "S1", "agnes", 1, ["python", "x"], out,
                    self._args(), td / "clips", td / "clips" / "qc",
                    results, pm, threading.Lock(), 0)
            self.assertEqual(streak, 1, "失败要累加连续失败计数（worker 退场判定用）")
            self.assertTrue(results[0].startswith("FAIL(rc=2)"), results)
            self.assertTrue(pm["S1"].endswith("(failed)"))

    def test_existing_product_upgrades_to_ok(self):
        """断点续跑兜底：rc!=0 但产物已落盘非空 → 视为成功（不误报 FAIL）。"""
        import threading
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = self._fixture(td)          # 产物存在
            results: list[str] = []
            with mock.patch("mg_batch.subprocess.call", return_value=2):
                streak = mg_batch.run_shot_once(
                    td / "shot_01.json", "S1", "agnes", 1, ["python", "x"], out,
                    self._args(), td / "clips", td / "clips" / "qc",
                    results, {}, threading.Lock(), 0)
            self.assertEqual(streak, 0)
            self.assertTrue(results[0].startswith("OK S1"), results)


class TestZhipuVideoPayload(unittest.TestCase):
    """zhipu 风视频 payload 统一走 build_video_payload（原来在 cmd_video 里另起一份）。

    修两类"静默"：① --last-frame / --negative 被无声丢弃 → 产出与预期不符还不报错；
    ② image_url 走未压缩编码 → 大图直传读超时（v4.7 已在 agnes 侧实测过这条上游约束）。
    """

    ZHIPU = {"payload_style": "zhipu", "supports_num_frames": False,
             "supports_negative": False}

    def setUp(self):
        # _IGNORED_WARNED 是模块级"每池只警告一次"去重集合：不清就会跨用例累积，
        # 后面的用例看不到告警（测试互相污染，不是实现问题）
        mg._IGNORED_WARNED.clear()

    def _args(self, **kw):
        import argparse
        base = dict(prompt="p", image="", last_frame="", num_frames=121,
                    negative="", video_size="", video_duration="")
        base.update(kw)
        return argparse.Namespace(**base)

    def test_field_shape(self):
        a = self._args(image="/tmp/f.png")
        with mock.patch("media_gen.image_to_uri_shrunk", side_effect=lambda p: f"S({p})"):
            payload = mg.build_video_payload(self.ZHIPU, {}, a, "1920x1080", 5,
                                             model="cogvideox-flash")
        self.assertEqual(payload["model"], "cogvideox-flash")
        self.assertEqual(payload["prompt"], "p")
        self.assertFalse(payload["with_audio"])
        self.assertEqual(payload["fps"], 30)
        self.assertEqual(payload["size"], "1920x1080")
        self.assertEqual(payload["duration"], 5)

    def test_image_url_uses_shrunk_encoder(self):
        """大图压缩必须覆盖 zhipu 这条 i2v 路径（原用未压缩 image_to_url_or_path）。"""
        a = self._args(image="/tmp/big.png")
        with mock.patch("media_gen.image_to_uri_shrunk",
                        side_effect=lambda p: f"S({p})") as m:
            payload = mg.build_video_payload(self.ZHIPU, {}, a, "1920x1080", 5, model="m")
        self.assertEqual(payload["image_url"], "S(/tmp/big.png)")
        self.assertTrue(m.called)

    def test_last_frame_dies_loudly(self):
        """zhipu 不支持首尾帧 → 必须大声报错，不能静默产出一段"没插值"的视频。"""
        a = self._args(image="/tmp/f.png", last_frame="/tmp/l.png")
        with self.assertRaises(SystemExit) as cm:
            mg.build_video_payload(self.ZHIPU, {}, a, "1920x1080", 5, model="m")
        self.assertEqual(cm.exception.code, 2)

    def test_negative_warns_not_silent(self):
        import contextlib
        import io
        a = self._args(image="/tmp/f.png", negative="blurry")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            payload = mg.build_video_payload(self.ZHIPU, {}, a, "1920x1080", 5, model="m")
        self.assertNotIn("negative_prompt", payload)
        self.assertIn("negative", buf.getvalue().lower(), "被丢弃的参数必须留下痕迹")

    def test_generic_pool_warns_when_negative_unsupported(self):
        import contextlib
        import io
        info = {"supports_negative": False}
        a = self._args(negative="blurry")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            payload = mg.build_video_payload(info, {}, a, "", None, model="m")
        self.assertNotIn("negative_prompt", payload)
        self.assertIn("negative", buf.getvalue().lower())

    def test_warning_fires_once_per_pool(self):
        """batch 逐镜调用 → 同一池的忽略告警不能每镜刷一次（否则刷屏看不见真错误）。"""
        import contextlib
        import io
        info = {"supports_negative": False}
        a = self._args(negative="x")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            for _ in range(3):
                mg.build_video_payload(info, {}, a, "", None, model="m")
        warned = [ln for ln in buf.getvalue().splitlines() if "negative" in ln.lower()]
        self.assertEqual(len(warned), 1, f"同一池的忽略告警应只出现一次，实际 {len(warned)} 次")


class TestStatusTtsSlot(unittest.TestCase):
    """status 必须看到**任意**已配 TTS 槽位（曾只看 MEDIA_TTS_1 → 误报未配置）。"""

    def test_reports_second_slot_when_first_empty(self):
        import argparse
        import contextlib
        import io
        import mg_status
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEDIA_")}
        env["MEDIA_TTS_2_KEY"] = "k2"
        env["MEDIA_TTS_2_BASE"] = "https://tts.invalid/v1"
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(buf):
            mg_status.cmd_status(argparse.Namespace(no_probe=True))
        out = buf.getvalue()
        self.assertIn("tts key#2", out, "key#2 已配却没显示 —— 槽位扫描口径不一致")
        self.assertNotIn("tts: 未配置", out)

    def test_reports_unconfigured_when_no_slot(self):
        import argparse
        import contextlib
        import io
        import mg_status
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEDIA_")}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(buf):
            mg_status.cmd_status(argparse.Namespace(no_probe=True))
        self.assertIn("tts", buf.getvalue())


class TestBuildPollUrl(unittest.TestCase):
    """批四 B：轮询 URL 构造收归 mg_core.build_poll_url 单源。

    此前三处各拼一份（_poll_video_task / _harvest_video / cmd_edit）——
    poll_style path/query 的分叉逻辑散落 = 加新池必漏一处。"""

    def test_query_style_with_param(self):
        info = {"poll_style": "query", "poll_path": "/videos/generations",
                "poll_param": "task_id"}
        self.assertEqual(
            mg_core.build_poll_url("http://b/v1", info, "T1"),
            "http://b/v1/videos/generations?task_id=T1")

    def test_path_style(self):
        info = {"poll_style": "path", "poll_path": "/videos"}
        self.assertEqual(
            mg_core.build_poll_url("http://b/v1", info, "T1"),
            "http://b/v1/videos/T1")

    def test_default_style_is_query(self):
        """未声明 poll_style 的视频池（如 agnes）默认 query 风格——与
        _poll_video_task 旧 else 分支一致，改默认值会直接改坏 agnes 轮询。"""
        self.assertEqual(
            mg_core.build_poll_url("http://b/v1",
                                   {"poll_path": "/agnesapi", "poll_param": "video_id"}, "T9"),
            "http://b/v1/agnesapi?video_id=T9")

    def test_image_side_forced_path(self):
        """image 侧（魔撘 /tasks/{id}）info 无 poll_style，调用方显式 style='path'。"""
        self.assertEqual(
            mg_core.build_poll_url("http://b/v1", {}, "T1",
                                   poll_path="/tasks", style="path"),
            "http://b/v1/tasks/T1")

    def test_poll_path_override_wins(self):
        """harvest 用落盘记录里的 poll_path 覆盖池配置（记录优先）。"""
        info = {"poll_style": "path", "poll_path": "/videos"}
        self.assertEqual(
            mg_core.build_poll_url("http://b/v1", info, "T1", poll_path="/other"),
            "http://b/v1/other/T1")


class TestStatusCapabilityRows(unittest.TestCase):
    """批四 A：status 显示能力档案汇总行（声明/实测/过期一眼可见）。

    caps show 已覆盖明细，但 status 是第一入口——用户看 status 时
    不该对"哪些池真测过 ref_image/keyframes"一无所知。"""

    def _run_status(self):
        import argparse
        import contextlib
        import io
        import mg_status
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mg_status.cmd_status(argparse.Namespace(no_probe=True))
        return buf.getvalue()

    def _caps_load(self, data):
        """patch 真实 mg_caps 模块的 load（cmd_status 延迟 import 拿到同一模块对象）。"""
        import mg_caps
        if isinstance(data, Exception) or callable(data):
            return mock.patch.object(mg_caps, "load", side_effect=data)
        return mock.patch.object(mg_caps, "load", return_value=data)

    def _run_status(self):
        import argparse
        import contextlib
        import io
        import mg_status
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mg_status.cmd_status(argparse.Namespace(no_probe=True))
        return buf.getvalue()

    def test_shows_measured_and_unmeasured(self):
        data = {"custom": {
            "image:cap:ref_image": {"ok": True, "probed_at_ts": time.time()},
        }}
        with self._caps_load(data):
            out = self._run_status()
        self.assertIn("能力", out, "status 缺能力汇总段（批四 A 目标）")
        self.assertIn("ref_image", out)
        self.assertIn("keyframes", out)

    def test_stale_probe_marked(self):
        stale_ts = time.time() - 8 * 86400      # 超 7 天 TTL
        data = {"custom": {
            "image:cap:ref_image": {"ok": True, "probed_at_ts": stale_ts},
        }}
        with self._caps_load(data):
            out = self._run_status()
        self.assertIn("过期", out, "超 TTL 的实测应标「过期」而非冒充有效")

    def test_corrupt_caps_does_not_crash_status(self):
        """caps 档案损坏不能拖垮 status（load 抛异常时静默跳过该段）。"""
        with self._caps_load(RuntimeError("corrupt json")):
            out = self._run_status()          # 不应 raise
        self.assertIn("providers", out)


class TestProbeInnerPollTimeout(unittest.TestCase):
    """v4.7.8：探针外层超时必须**晚于**内层轮询超时。

    否则 media_gen 在落盘前被 run_capture 杀掉 → 已受理的付费任务永久丢失，
    而 `_execute_smoke` 提示的"用 harvest 收割"无记录可收（提示指向不存在的路径）。"""

    def test_inner_timeout_lt_outer(self):
        import mg_caps
        self.assertLess(mg_caps.inner_poll_timeout(300), 300,
                        "内层轮询超时必须小于外层，media_gen 才能先主动落盘")
        self.assertGreaterEqual(mg_caps.inner_poll_timeout(300), 60)
        self.assertGreaterEqual(mg_caps.inner_poll_timeout(120), 60,
                                "外层很短时内层也不能低于下限")

    def test_cap_probe_cmd_carries_poll_timeout(self):
        import mg_caps
        with tempfile.TemporaryDirectory() as td:
            cmd = mg_caps.cap_cmd("agnes", "image", "ref_image",
                                  Path(td) / "o.png", 1, Path(td), timeout=300)
        self.assertIn("--poll-timeout", cmd,
                      "探针命令必须带内层超时，否则任务被外层杀在落盘前")
        self.assertLess(int(cmd[cmd.index("--poll-timeout") + 1]), 300)

    def test_real_smoke_cmd_carries_poll_timeout(self):
        import mg_caps
        seen = {}

        def fake_exec(cmd, out, kind, **kw):
            seen["cmd"] = cmd
            return {"ok": False, "rc": 1}

        with mock.patch.object(mg_caps, "_execute_smoke", side_effect=fake_exec):
            mg_caps._real_smoke("agnes", "video", timeout=300)
        self.assertIn("--poll-timeout", seen["cmd"])


class TestEnvcheckRunnerNone(unittest.TestCase):
    """v4.8.0：runner 异常被吞返回 None → 原 `r.stdout` AttributeError（体检直接崩）。"""

    def test_runner_none_does_not_crash(self):
        import envcheck
        rs = envcheck.run_checks(runner=lambda cmd: None)
        self.assertTrue(rs, "runner 返回 None 时仍应产出体检项（标 warn/skip 而非崩）")


class TestPromptLintAtomicNull(unittest.TestCase):
    """v4.8.0：shot JSON 里 `_atomic: null` → `None.get` AttributeError。"""

    def test_atomic_null_does_not_crash(self):
        import prompt_lint
        lex = Path(__file__).resolve().parents[1] / "references" / "anti-slop-lexicon.md"
        hard, mood = prompt_lint.parse_lexicon_md(lex)
        shot = {"shot_id": "S1", "i2v_prompt": "x", "_atomic": None}
        issues = prompt_lint.lint_shot(shot, "videos", hard, mood)
        self.assertIsInstance(issues, list)


class TestDelogoGeometry(unittest.TestCase):
    """v4.8.0：水印框的分辨率解析与钳制。

    ① 带封面图的 mp4 会先列 mjpeg 缩略图 → 原 `re.search` 取首个匹配拿到 320x240，
       框位整体算错（已实证）；② 钳制用 OR 触发且不校验不变式：框比画面还大时
       钳完仍越界，还打印"已钳制"（假象）。"""

    def _mod(self):
        import importlib.util
        src = Path(__file__).resolve().parents[1] / "scripts" / "delogo_watermark.py"
        spec = importlib.util.spec_from_file_location("reelcraft_delogo", src)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_parse_size_skips_mjpeg_cover(self):
        m = self._mod()
        stderr = ("  Stream #0:0(eng): Video: mjpeg, yuvj420p, 320x240\n"
                  "  Stream #0:1(eng): Video: h264, yuv420p, 1920x1080\n")
        self.assertEqual(m.parse_size(stderr), (1920, 1080),
                         "带封面图应跳过 mjpeg 缩略图取真实视频流分辨率")

    def test_parse_size_plain(self):
        m = self._mod()
        self.assertEqual(m.parse_size("  Stream #0:0: Video: h264, 1280x720\n"), (1280, 720))

    def test_parse_size_none(self):
        m = self._mod()
        self.assertIsNone(m.parse_size("no video line"))

    def test_clamp_box_in_range(self):
        m = self._mod()
        self.assertEqual(m.clamp_box(10, 20, 54, 56, 1280, 720), (10, 20, 54, 56))

    def test_clamp_box_over_edge_stays_inside(self):
        m = self._mod()
        x, y, w, h = m.clamp_box(1260, 700, 54, 56, 1280, 720)
        self.assertLess(x + w, 1280, "钳制后框必须真的在画面内")
        self.assertLess(y + h, 720)
        self.assertGreaterEqual(x, 1)
        self.assertGreaterEqual(y, 1)

    def test_clamp_box_too_big_dies(self):
        m = self._mod()
        with self.assertRaises(SystemExit):
            m.clamp_box(0, 0, 2000, 2000, 1280, 720)


class TestTtsSlotSingleSource(unittest.TestCase):
    """v4.8.0：TTS 槽位枚举单源。

    audio_triage 曾自带 `_scan_tts_slot` 副本，与 mg_core.scan_tts_slots 口径
    不一致（status 说"已配"、triage 说"未配"）。这里断言二者**判定一致**：
    半配槽（只有 BASE 或只有 KEY）不给出，但也不中断后续枚举。"""

    def test_skips_half_configured_but_keeps_scanning(self):
        import audio_triage as at
        import mg_core
        env = {"MEDIA_TTS_2_BASE": "http://half/v1",           # 半配
               "MEDIA_TTS_3_KEY": "k3", "MEDIA_TTS_3_BASE": "http://c/v1"}
        n, base, key = at._scan_tts_slot(env)
        self.assertEqual((n, base, key), (3, "http://c/v1", "k3"),
                         "半配槽不能中断枚举（须与 mg_core.scan_tts_slots 同口径）")
        self.assertIn(n, mg_core.scan_tts_slots(env),
                      "枚举结果必须是 mg_core 槽位表的子集（单源）")

    def test_first_ready_slot_wins(self):
        import audio_triage as at
        env = {"MEDIA_TTS_1_KEY": "k1", "MEDIA_TTS_1_BASE": "http://a/v1",
               "MEDIA_TTS_2_KEY": "k2", "MEDIA_TTS_2_BASE": "http://b/v1"}
        self.assertEqual(at._scan_tts_slot(env), (1, "http://a/v1", "k1"))

    def test_none_ready(self):
        import audio_triage as at
        self.assertEqual(at._scan_tts_slot({}), (0, "", ""))


class TestOnlyWithTransition(unittest.TestCase):
    """批二：--only × 过渡镜兼容（hybrid 核心场景）。

    过渡镜是两镜之间的"桥"：邻居在本次运行 → pass2 接上；已有历史产物 → skip (exists)；
    from 镜带不动（不在 --only 且无历史产物）→ 排除并提示，不留在计划里刷 MISS。"""

    def _run(self, td: Path, only: str):
        import subprocess as sp
        mg = Path(__file__).resolve().parents[1] / "scripts" / "media_gen.py"
        env = {**os.environ, "PYTHONUTF8": "1",
               "MEDIA_AGNES_1_KEY": "k1", "MEDIA_AGNES_1_BASE": "https://example.invalid/v1"}
        cmd = [sys.executable, str(mg), "batch", str(td), "--phase", "videos",
               "--provider", "agnes", "--workers", "1", "--dry-run",
               "--only", only]
        return sp.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)

    def _setup(self, td: Path, s1_clip: bool = False):
        """S1/S2 普通镜 + S1T 过渡镜（JSON 永远齐全——孤儿依赖该 die 是另一条防线）。"""
        (td / "frames").mkdir()
        (td / "clips").mkdir()
        (td / "frames" / "S1.png").write_bytes(b"f1")
        (td / "frames" / "S2.png").write_bytes(b"f2")
        (td / "shot_01.json").write_text(json.dumps(
            {"shot_id": "S1", "t2i_prompt": "x", "i2v_prompt": "y"}), encoding="utf-8")
        (td / "shot_02.json").write_text(json.dumps(
            {"shot_id": "S2", "t2i_prompt": "x", "i2v_prompt": "y"}), encoding="utf-8")
        (td / "shot_01T.json").write_text(json.dumps(
            {"shot_id": "S1T", "transition": {"from": "S1", "to": "S2"},
             "i2v_prompt": "m"}), encoding="utf-8")
        if s1_clip:
            (td / "clips" / "clip_S1.mp4").write_bytes(b"c1")

    def test_only_keeps_adjacent_transition(self):
        """邻居在本次 --only 里 → 过渡镜保留（hybrid 核心场景）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup(td)
            r = self._run(td, "S1,S2")
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("S1T", r.stdout, "--only 把过渡镜整个滤掉了——hybrid 出不了过渡")

    def test_only_keeps_transition_with_from_history(self):
        """from 镜不在本次运行但有历史 clip → seed 能从历史产物抽，过渡镜保留。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup(td, s1_clip=True)
            r = self._run(td, "S2")
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("S1T", r.stdout, "from 有历史产物就能抽 seed，不该丢")

    def test_only_drops_unrunnable_transition_loudly(self):
        """from 镜不在 --only 且无任何历史产物 → 排除并提示（不留在计划里刷 MISS）。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._setup(td)
            r = self._run(td, "S2")
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertNotIn("S1T ->", r.stdout, "带不动的过渡镜不该出现在计划里")
            self.assertIn("S1T", r.stderr, "排除必须留痕，不能无声")




class TestCleanIntermediateFiles(unittest.TestCase):
    """S5：concat 的 _norm/_mixed/_with_text 中间文件必须纳入 clean 治理面。

    v4.7 审计 P2：这些可再生中间产物会留在工作区越积越多，clean 只认
    frames/clips 等目录，散落在 clips/ 里的 _norm.mp4 等文件扫不到。
    """

    def test_norm_mixed_in_clean_plan(self):
        import pipeline
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "clips").mkdir()
            (td / "clips" / "_norm.mp4").write_bytes(b"n")
            (td / "clips" / "_mixed.mp4").write_bytes(b"m")
            (td / "clips" / "clip_S1.mp4").write_bytes(b"c")
            plan = pipeline.plan_clean(td)
            removed = [p.name for p in plan["remove"]]
            self.assertIn("_norm.mp4", removed, "_norm.mp4 应属可再生中间产物")
            self.assertIn("_mixed.mp4", removed, "_mixed.mp4 应属可再生中间产物")
            self.assertIn("clip_S1.mp4", removed, "clip 产物本来就在治理面")

if __name__ == '__main__':
    unittest.main(verbosity=2)

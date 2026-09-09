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


if __name__ == '__main__':
    unittest.main(verbosity=2)

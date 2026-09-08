# -*- coding: utf-8 -*-
"""reelcraft 最小测试集（unittest，标准库零依赖）。

只测纯逻辑函数，不发任何网络请求、不碰真实 ~/.workbuddy 状态。
运行：python -m unittest discover tests -v   （在 skill 根目录）
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
import media_gen as mg  # noqa: E402
import mg_core  # noqa: E402
import mg_batch  # noqa: E402


class TestLoadEnvFile(unittest.TestCase):
    """_load_env_file 解析：含行内注释容错（2026-09-05 修过的 bug，回归钉死）。"""

    def _parse(self, content: str):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "k.env"
            p.write_text(content, encoding="utf-8")
            saved = dict(mg_core.os.environ)
            try:
                mg_core.os.environ.pop("MEDIA_TEST_1_KEY", None)
                mg_core._load_env_file(p)
                return mg_core.os.environ.get("MEDIA_TEST_1_KEY")
            finally:
                for k in list(mg_core.os.environ):
                    if k not in saved:
                        mg_core.os.environ.pop(k, None)

    def test_plain_value(self):
        self.assertEqual(self._parse('export MEDIA_TEST_1_KEY="abc"\n'), "abc")

    def test_inline_comment_tolerated(self):
        self.assertEqual(
            self._parse('export MEDIA_TEST_1_KEY="high"   # 主流档：注释\n'), "high")

    def test_comment_line_skipped(self):
        self.assertIsNone(self._parse('# 全是注释\n\n'))

    def test_no_quote_value_skipped(self):
        self.assertIsNone(self._parse('export MEDIA_TEST_1_KEY=abc\n'))

    def test_does_not_override_existing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "k.env"
            p.write_text('export MEDIA_TEST_1_KEY="from-file"\n', encoding="utf-8")
            saved = dict(mg_core.os.environ)
            try:
                mg_core.os.environ["MEDIA_TEST_1_KEY"] = "already-set"
                mg_core._load_env_file(p)
                self.assertEqual(mg_core.os.environ["MEDIA_TEST_1_KEY"], "already-set")
            finally:
                for k in list(mg_core.os.environ):
                    if k not in saved:
                        mg_core.os.environ.pop(k, None)


class TestAbsUrl(unittest.TestCase):
    """相对路径产物按 base origin 补全（LTX Bridge 风格）。"""

    def test_relative_path_completed(self):
        self.assertEqual(
            mg_core._abs_url("/files/x.webp", "http://127.0.0.1:8000/v1"),
            "http://127.0.0.1:8000/files/x.webp")

    def test_absolute_url_untouched(self):
        self.assertEqual(mg_core._abs_url("https://a.com/v.mp4", "http://b.com/v1"),
                         "https://a.com/v.mp4")

    def test_empty_untouched(self):
        self.assertEqual(mg_core._abs_url("", "http://b.com/v1"), "")

    def test_none_untouched(self):
        self.assertIsNone(mg_core._abs_url(None, "http://b.com/v1"))

    def test_base_without_scheme_untouched(self):
        self.assertEqual(mg_core._abs_url("/files/x.webp", "127.0.0.1:8000"), "/files/x.webp")


class TestFinalOut(unittest.TestCase):
    """产物后缀与 --out 不符时按实际后缀存。"""

    def test_mp4_out_gets_webp(self):
        self.assertEqual(mg_core._final_out("C:/x/clip.mp4", "http://h/f/a.webp"),
                         "C:/x/clip.webp")

    def test_matching_suffix_untouched(self):
        self.assertEqual(mg_core._final_out("C:/x/clip.mp4", "http://h/f/a.mp4"),
                         "C:/x/clip.mp4")

    def test_url_without_suffix_untouched(self):
        self.assertEqual(mg_core._final_out("C:/x/clip.mp4", "http://h/f/download"),
                         "C:/x/clip.mp4")


class TestInsertSuffix(unittest.TestCase):
    def test_adds_suffix(self):
        # Windows Path 会规范分隔符，用 Path 比较做平台无关断言
        self.assertEqual(Path(mg_core._insert_suffix("C:/x/S01.png", "_2")),
                         Path("C:/x/S01_2.png"))


@unittest.skip("LTX Bridge 测试暂时停用：桥已拆分 build_workflow→build_workflow_ltx/wan 且加了 "
               "@app.on_event，本 mock 面无法跟上（算力机离线时 E:\\LTXBridge 也非最新）。"
               "需改为黑盒契约测试（起服务打真实 /v1/videos），勿再引外部桥内部实现。")
class TestLTXBridgeWorkflow(unittest.TestCase):
    """LTX Bridge（E:\\LTXBridge）i2v/t2v workflow 连线回归。
    bridge 在 skill 仓之外，不存在时跳过；import 前 stub 掉第三方依赖。"""

    BRIDGE = Path(r"E:\LTXBridge\bridge_server.py")

    def _load_bs(self):
        if not self.BRIDGE.exists():
            self.skipTest("LTX Bridge 不在本机")
        import types
        for name in ("httpx", "uvicorn"):
            if name not in sys.modules:
                sys.modules[name] = types.ModuleType(name)
        sys.modules["uvicorn"].run = lambda *a, **k: None
        if "fastapi" not in sys.modules:
            fa = types.ModuleType("fastapi")
            fa.FastAPI = lambda **k: types.SimpleNamespace(
                get=lambda *a, **k: (lambda f: f), post=lambda *a, **k: (lambda f: f))
            fa.HTTPException = type("HTTPException", (Exception,), {})
            sys.modules["fastapi"] = fa
            exc = types.ModuleType("fastapi.exceptions")
            exc.HTTPException = type("HTTPException", (Exception,), {})
            sys.modules["fastapi.exceptions"] = exc
            resp = types.ModuleType("fastapi.responses")
            resp.FileResponse = object
            resp.JSONResponse = object
            sys.modules["fastapi.responses"] = resp
        if "pydantic" not in sys.modules:
            pyd = types.ModuleType("pydantic")
            pyd.BaseModel = type("BaseModel", (), {})
            pyd.Field = lambda *a, **k: None
            sys.modules["pydantic"] = pyd
        import importlib.util
        spec = importlib.util.spec_from_file_location("bridge_server", self.BRIDGE)
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        return bs

    def test_t2v_branch(self):
        bs = self._load_bs()
        wf, _ = bs.build_workflow("a cat", image_name="")
        self.assertEqual(wf["6"]["class_type"], "EmptyLTXVLatentVideo")
        self.assertEqual(wf["7"]["inputs"]["latent_image"], ["6", 0])
        self.assertEqual(wf["7"]["inputs"]["cfg"], 2.0)

    def test_i2v_branch(self):
        bs = self._load_bs()
        wf, _ = bs.build_workflow("a cat", image_name="start.png")
        self.assertEqual(wf["10"]["class_type"], "LoadImage")
        self.assertEqual(wf["6"]["class_type"], "LTXVImgToVideo")
        self.assertEqual(wf["6"]["inputs"]["image"], ["10", 0])
        self.assertEqual(wf["6"]["inputs"]["vae"], ["1", 2])
        self.assertEqual(wf["7"]["inputs"]["positive"], ["6", 0])
        self.assertEqual(wf["7"]["inputs"]["negative"], ["6", 1])
        self.assertEqual(wf["7"]["inputs"]["latent_image"], ["6", 2])

    def test_i2v_img_compression_passthrough(self):
        bs = self._load_bs()
        wf, _ = bs.build_workflow("a cat", image_name="s.png", img_compression=50.0)
        self.assertEqual(wf["6"]["inputs"]["img_compression"], 50.0)

    def test_t2v_not_polluted_by_i2v(self):
        bs = self._load_bs()
        bs.build_workflow("x", image_name="a.png")
        wf, _ = bs.build_workflow("x")
        self.assertNotIn("10", wf)
        self.assertEqual(wf["6"]["class_type"], "EmptyLTXVLatentVideo")


class TestInterleaveByPool(unittest.TestCase):
    """混编交错分配：模型分布均匀（2026-09-05 跨池 batch 的核心逻辑）。"""

    def _key(self, n):
        return {"n": n}

    def test_even_interleave(self):
        spec = [("agnes", self._key(1)), ("agnes", self._key(2)), ("agnes", self._key(3)),
                ("custom", self._key(1)), ("custom", self._key(2))]
        out = mg_core._interleave_by_pool(spec, ["agnes", "custom"])
        pools = [p for p, _ in out]
        self.assertEqual(pools, ["agnes", "custom", "agnes", "custom", "agnes"])

    def test_three_pools(self):
        spec = [("a", self._key(1)), ("a", self._key(2)),
                ("b", self._key(1)), ("c", self._key(1))]
        out = mg_core._interleave_by_pool(spec, ["a", "b", "c"])
        pools = [p for p, _ in out]
        self.assertEqual(pools[0], "a")
        self.assertEqual(pools[1], "b")
        self.assertEqual(pools[2], "c")

    def test_no_pool_lost(self):
        spec = [(f"p{i}", self._key(1)) for i in range(5)]
        out = mg_core._interleave_by_pool(spec, [f"p{i}" for i in range(5)])
        self.assertEqual(len(out), 5)


class TestVideoDefaultSize(unittest.TestCase):
    """每池默认分辨率必须在该池 sizes 白名单内（2026-09-06 智谱回归钉死：
    CLI --video-size 共享默认曾误设为 1280x720，智谱白名单无此项导致 rc=2）。"""

    def test_zhipu_default_in_whitelist(self):
        info = mg_core.PROVIDERS["zhipu"]["models"]["video"]
        self.assertIn(info["default_size"], info["sizes"])

    def test_every_sized_pool_has_valid_default(self):
        """凡声明了 sizes 白名单的视频池，都必须配 default_size 且落在白名单内。"""
        for pool, pinfo in mg_core.PROVIDERS.items():
            info = pinfo.get("models", {}).get("video")
            if not info or "sizes" not in info:
                continue
            self.assertTrue(info.get("default_size"),
                            f"{pool} 声明 sizes 却未配 default_size")
            self.assertIn(info["default_size"], info["sizes"],
                          f"{pool} 的 default_size 不在自己的 sizes 白名单内")


class TestPendingTask(unittest.TestCase):
    """pending task 落盘/弹出（STATE_FILE 隔离到临时目录）。"""

    def setUp(self):
        self._orig_state = mg_core.STATE_FILE
        self._td = tempfile.TemporaryDirectory()
        mg_core.STATE_FILE = Path(self._td.name) / "state.json"

    def tearDown(self):
        mg_core.STATE_FILE = self._orig_state
        self._td.cleanup()

    def test_save_and_pop(self):
        mg_core._save_pending_task("custom", "tid1", "C:/x/out.mp4", "http://b/v1")
        rec = mg_core._pop_pending_task("tid1")
        self.assertEqual(rec["pool"], "custom")
        self.assertEqual(rec["out"], "C:/x/out.mp4")
        self.assertIsNone(mg_core._pop_pending_task("tid1"))  # 弹出后即删

    def test_kind_field_defaults_video(self):
        mg_core._save_pending_task("custom", "tid2", "C:/x/o.mp4", "http://b/v1")
        s = json.loads(mg_core.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(s["pending_tasks"]["tid2"]["kind"], "video")

    def test_kind_image(self):
        mg_core._save_pending_task("custom", "tid3", "C:/x/o.png", "http://b/v1", kind="image")
        s = json.loads(mg_core.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(s["pending_tasks"]["tid3"]["kind"], "image")


class TestImageTimeoutPending(unittest.TestCase):
    """image 异步轮询超时 → 落盘 pending + exit 4（不重试不丢额度）。"""

    def setUp(self):
        self._orig_state = mg_core.STATE_FILE
        self._td = tempfile.TemporaryDirectory()
        mg_core.STATE_FILE = Path(self._td.name) / "state.json"

    def tearDown(self):
        mg_core.STATE_FILE = self._orig_state
        self._td.cleanup()

    def test_timeout_exits_4_and_persists(self):
        key = {"key": "k", "base": "http://b/v1", "poll": "http://b/v1", "n": 1,
               "roles": {"image"}, "pool": "custom"}
        resp = {"task_id": "T123"}
        counter = {"t": 0}

        def fake_time():
            counter["t"] += 100      # 每次查询推进 100s → 约 4 轮后超过 300s 截止
            return counter["t"]

        with mock.patch.object(mg.time, "sleep"), \
             mock.patch.object(mg.urllib.request, "urlopen",
                               side_effect=Exception("simulated timeout")), \
             mock.patch.object(mg.time, "time", side_effect=fake_time):
            with self.assertRaises(SystemExit) as cm:
                mg_core._resolve_async_task(key, resp, "/tasks",
                                       out="C:/x/S01.png", pool="custom")
            self.assertEqual(cm.exception.code, 4)
        s = json.loads(mg_core.STATE_FILE.read_text(encoding="utf-8"))
        rec = s["pending_tasks"]["T123"]
        self.assertEqual(rec["kind"], "image")
        self.assertEqual(rec["out"], "C:/x/S01.png")
        self.assertEqual(rec["poll_path"], "/tasks")


def _cp_worker_save_pending(tmpdir, i):
    """跨进程并发写 pending 的 worker（multiprocessing spawn 顶层函数）。"""
    import mg_core
    mg_core.STATE_FILE = Path(tmpdir) / "state.json"
    mg_core._save_pending_task("custom", f"tid{i}", f"C:/x/out{i}.mp4", "http://b/v1")


class TestCrossProcessState(unittest.TestCase):
    """#1/#6 跨进程竞态回归：state 并发读改写不丢记录（文件锁+原子写）。
    旧实现（threading.Lock + 整文件覆盖写）在 batch 每镜独立 subprocess 场景
    会互相覆盖——pending 记录丢失意味着慢任务出片后无人收割、重跑重复扣费。"""

    def test_concurrent_pending_tasks_survive(self):
        import multiprocessing as mp
        td = tempfile.TemporaryDirectory()
        try:
            ctx = mp.get_context("spawn")
            procs = [ctx.Process(target=_cp_worker_save_pending, args=(td.name, i))
                     for i in range(12)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(60)
            saved, mg_core.STATE_FILE = mg_core.STATE_FILE, Path(td.name) / "state.json"
            try:
                s = mg_core._load_state()
            finally:
                mg_core.STATE_FILE = saved
            self.assertEqual(len(s.get("pending_tasks", {})), 12,
                             "并发写 pending 有丢失（state 跨进程竞态回归）")
        finally:
            td.cleanup()

    def test_throttle_persists_and_second_call_waits(self):
        """#6 节流落盘：第二次同 tag 调用应等待（时间戳跨进程共享）。"""
        td = tempfile.TemporaryDirectory()
        saved = mg_core.THROTTLE_FILE
        mg_core.THROTTLE_FILE = Path(td.name) / "throttle.json"
        try:
            with mock.patch.object(mg_core.time, "sleep") as ms:
                mg_core.video_throttle(60, "agnes_key1")   # 首次：ts 空 → 不等待
                mg_core.video_throttle(60, "agnes_key1")   # 第二次：应等待 ~1s
            ms.assert_called_once()                          # 恰好 sleep 一次
            ts = json.loads(mg_core.THROTTLE_FILE.read_text(encoding="utf-8"))
            self.assertIn("agnes_key1", ts)                  # 时间戳已落盘（跨进程可见）
            self.assertGreater(ts["agnes_key1"], mg_core.time.time() - 1)
        finally:
            mg_core.THROTTLE_FILE = saved
            td.cleanup()


class TestNatSort(unittest.TestCase):
    """#2 自然排序：数字段按数值比较（S2 < S10），字典序会错位。"""

    def test_shot_order(self):
        paths = ["S10.json", "S2.json", "S1.json", "S11.json"]
        got = sorted(paths, key=mg_core.natkey)
        self.assertEqual(got, ["S1.json", "S2.json", "S10.json", "S11.json"])

    def test_clip_order(self):
        paths = ["clip_S10.mp4", "clip_S2.mp4", "clip_S1.mp4"]
        got = sorted(paths, key=mg_core.natkey)
        self.assertEqual(got, ["clip_S1.mp4", "clip_S2.mp4", "clip_S10.mp4"])

    def test_padding_zero_still_ok(self):
        paths = ["S01.json", "S02.json", "S10.json"]
        self.assertEqual(sorted(paths, key=mg_core.natkey),
                         sorted(paths))   # 补零时自然序与字典序一致


class TestExtractVideoUrl(unittest.TestCase):
    """#9 URL 提取：data 为 list 的分支（此前仅认 dict 漏检）。"""

    def _e(self, st):
        return mg_core._extract_video_url(st)

    def test_data_list(self):
        self.assertEqual(self._e({"data": [{"url": "http://x/v.mp4"}]}),
                         "http://x/v.mp4")

    def test_data_list_video_url_key(self):
        self.assertEqual(self._e({"data": [{"video_url": "http://x/v.mp4"}]}),
                         "http://x/v.mp4")

    def test_data_dict_still_works(self):
        self.assertEqual(self._e({"data": {"url": "http://x/v.mp4"}}),
                         "http://x/v.mp4")

    def test_video_result_list(self):
        self.assertEqual(self._e({"video_result": [{"url": "http://x/v.mp4"}]}),
                         "http://x/v.mp4")

    def test_no_http_returns_none(self):
        self.assertIsNone(self._e({"data": [{"url": "rel/path.mp4"}]}))


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


class TestFileLock(unittest.TestCase):
    """_FileLock：退出必须释放句柄（争用降级路径曾泄漏 fd，且静默无锁）。"""

    def test_fd_closed_after_exit(self):
        with tempfile.TemporaryDirectory() as td:
            lk = mg_core._FileLock(Path(td) / "s.json")
            with lk:
                self.assertIsNotNone(lk.fd)
                self.assertFalse(lk.fd.closed)
            self.assertTrue(lk.fd.closed, "退出后句柄未关闭 → 泄漏")

    def test_reacquirable(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            for _ in range(3):
                with mg_core._FileLock(p):
                    pass        # 反复获取/释放不应卡死或抛错


class TestDownloadCleanup(unittest.TestCase):
    """#7 _download：下载中断必须清掉 .dl 残骸，别在工作区堆垃圾。"""

    def test_no_leftover_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "a.mp4"
            resp = mock.MagicMock()
            resp.read.side_effect = [b"half", OSError("boom")]
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: False
            with mock.patch.object(mg_core.urllib.request, "urlopen", return_value=resp):
                with self.assertRaises(OSError):
                    mg_core._download("http://x/y.mp4", str(out))
            self.assertEqual(list(Path(td).glob("*.dl*")), [])
            self.assertFalse(out.exists(), "失败的下载不应留下成品")


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


class TestCapsSource(unittest.TestCase):
    """#2 能力单源：声明是候选，实测（未过期）才是权威。"""

    def setUp(self):
        import importlib
        self.caps = importlib.import_module("mg_caps")
        self._td = tempfile.TemporaryDirectory()
        self.real = self.caps.CAPS_FILE
        self.caps.CAPS_FILE = Path(self._td.name) / "caps.json"

    def tearDown(self):
        self.caps.CAPS_FILE = self.real
        self._td.cleanup()

    def test_declared_when_never_measured(self):
        v = self.caps.effective("agnes", "image")
        self.assertEqual(v["source"], "declared")
        self.assertIsNone(v["measured"])

    def test_measured_wins(self):
        self.caps.record("agnes", "image", {
            "method": "real-smoke", "ok": True, "probed_at_ts": time.time(),
            "actual_res": "1312x736"})
        v = self.caps.effective("agnes", "image")
        self.assertEqual(v["source"], "measured")
        self.assertEqual(v["measured"]["actual_res"], "1312x736")

    def test_models_guess_is_not_authoritative(self):
        """/models 猜测不能当权威——它只是比声明多了一点线索。"""
        self.caps.record("agnes", "image", {
            "method": "models-guess", "ok": True, "probed_at_ts": time.time()})
        self.assertEqual(self.caps.effective("agnes", "image")["source"], "declared")

    def test_stale_falls_back_to_declared(self):
        self.caps.record("agnes", "image", {
            "method": "real-smoke", "ok": True,
            "probed_at_ts": time.time() - self.caps.CAPS_TTL - 1})
        v = self.caps.effective("agnes", "image")
        self.assertTrue(v["stale"])
        self.assertEqual(v["source"], "declared", "过期实测不该继续当权威")

    def test_clear(self):
        # record 必须带 probed_at_ts：缺省 0 会被 effective() 判为过期（>7 天）→ 回落声明
        self.caps.record("agnes", "image",
                         {"method": "real-smoke", "ok": True, "probed_at_ts": time.time()})
        self.caps.record("agnes", "video",
                         {"method": "real-smoke", "ok": True, "probed_at_ts": time.time()})
        self.assertEqual(self.caps.clear("agnes", "video"), 1)
        self.assertEqual(self.caps.effective("agnes", "video")["source"], "declared")
        self.assertEqual(self.caps.effective("agnes", "image")["source"], "measured")

    def test_corrupt_file_degrades_to_declared(self):
        self.caps.CAPS_FILE.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.caps.load(), {})
        self.assertEqual(self.caps.effective("agnes", "image")["source"], "declared")

    def test_models_guess_readable_from_effective(self):
        """写读 key 对齐：probe 非 --real 写 f"{kind}:models"，effective/show 必须能读回。
        此前只读 kind 键 → 探测结果落盘即读不回（死数据 + show 永远显示"未测过"）。"""
        self.caps.record("agnes", "video:models", {
            "method": "models-guess", "ok": True, "probed_at_ts": time.time(),
            "total_models": 12, "guess": {"video": ["agnes-video-v2.0"]}})
        v = self.caps.effective("agnes", "video")
        self.assertIsNotNone(v["measured"], "effective 应能读回 models-guess 记录")
        self.assertEqual(v["measured"]["method"], "models-guess")
        self.assertEqual(v["source"], "declared", "探测仍是线索，不因可读就变权威")

    def test_clear_removes_both_keys(self):
        """clear --kind video 要连 f"video:models" 一起清，不能清了实测还留线索。"""
        self.caps.record("agnes", "video",
                         {"method": "real-smoke", "ok": True, "probed_at_ts": time.time()})
        self.caps.record("agnes", "video:models",
                         {"method": "models-guess", "ok": True, "probed_at_ts": time.time()})
        self.assertEqual(self.caps.clear("agnes", "video"), 2)
        self.assertIsNone(self.caps.effective("agnes", "video")["measured"])


class TestCapsDocSync(unittest.TestCase):
    """优化⑥：能力元数据文档单源化——md 快照区块是 .media_caps.json 的生成物。
    caps sync 重写区块（区块外文字不碰）/ --check 做 drift 校验。"""

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.caps = importlib.import_module("mg_caps")

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.doc = Path(self._td.name) / "model-capabilities.md"
        self.real_caps_file = self.caps.CAPS_FILE
        self.caps.CAPS_FILE = Path(self._td.name) / "caps.json"   # load() 读这里

    def tearDown(self):
        self.caps.CAPS_FILE = self.real_caps_file
        self._td.cleanup()

    def _record_real_ok(self, res="1088x832"):
        self.caps.record("agnes", "video", {
            "method": "real-smoke", "ok": True, "probed_at": "2026-09-07 08:00:00",
            "probed_at_ts": time.time(), "model": "agnes-video-v2.0",
            "actual_res": res, "duration_s": 5.0, "elapsed_s": 135,
            "requested_size": "1024x576"})

    def test_render_unmeasured_rows(self):
        block = self.caps.render_snapshot({})
        self.assertIn(self.caps.AUTO_BEGIN, block)
        self.assertIn("| agnes | video | 声明 |", block, "未测池出声明行")
        self.assertIn("未测", block)

    def test_render_real_smoke_row_with_drift_note(self):
        self._record_real_ok()
        block = self.caps.render_snapshot(self.caps.load())
        self.assertIn("| agnes | video | 实测 | ✅ |", block)
        self.assertIn("1088x832 / 5.0s", block)
        self.assertIn("请求 1024x576 ≠ 实测 1088x832", block, "上游不按请求出片的实测差异要进文档")

    def test_render_stale_marked(self):
        self.caps.record("agnes", "video", {
            "method": "real-smoke", "ok": True,
            "probed_at_ts": time.time() - self.caps.CAPS_TTL - 10})
        block = self.caps.render_snapshot(self.caps.load(),
                                          now=time.time())
        self.assertIn("已过期", block)

    def test_sync_write_and_check_roundtrip(self):
        self.doc.write_text("# 模型能力表\n\n正文结论保持不动。\n", encoding="utf-8")
        self._record_real_ok()
        self.assertEqual(self.caps.caps_sync(self.doc, check_only=False), 0)
        text = self.doc.read_text(encoding="utf-8")
        self.assertIn("正文结论保持不动", text, "区块外叙述文字不许被机器碰")
        self.assertIn("caps-auto-begin", text)
        # 一致 → check 过
        self.assertEqual(self.caps.caps_sync(self.doc, check_only=True), 0)
        # 篡改区块 → drift
        self.doc.write_text(text.replace("1088x832", "9999x1"), encoding="utf-8")
        self.assertEqual(self.caps.caps_sync(self.doc, check_only=True), 1)

    def test_sync_replaces_existing_block_not_append(self):
        """二次 sync 必须原地替换区块，不能无限追加。"""
        self.doc.write_text("HEAD\n", encoding="utf-8")
        self.caps.record("agnes", "image", {
            "method": "models-guess", "ok": True, "probed_at_ts": time.time(),
            "total_models": 3, "guess": {"image": ["x"]}})
        self.caps.caps_sync(self.doc, check_only=False)
        first = self.doc.read_text(encoding="utf-8")
        self.caps.caps_sync(self.doc, check_only=False)
        second = self.doc.read_text(encoding="utf-8")
        self.assertEqual(first, second, "同一份实测 sync 两次结果必须幂等")
        self.assertEqual(second.count("caps-auto-begin"), 1)

    def test_sync_guess_row_is_not_authoritative(self):
        self.caps.record("agnes", "video:models", {
            "method": "models-guess", "ok": True, "probed_at_ts": time.time(),
            "guess": {"video": ["agnes-video-v2.0"]}})
        block = self.caps.render_snapshot(self.caps.load())
        self.assertIn("探测·仅参考", block, "/models 猜测不得标成实测权威")


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


class TestFindExistingProduct(unittest.TestCase):
    """断点续跑 helper（#3 补漏）：找"这镜算不算已出片"的落点——认 .webp 兄弟产物，
    避免上游把 .mp4 落成 .webp 后单命令重跑误判未完成 → 重复提交扣费。"""

    def _tdir(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Path(td.name)

    def test_exact_out_found(self):
        d = self._tdir()
        p = d / "clip_01.mp4"
        p.write_bytes(b"x" * 10)
        self.assertEqual(mg_core.find_existing_product(str(p)), str(p))

    def test_webp_sibling_found(self):
        """out 是 .mp4 但产物是 .webp：必须识别为已出片（#3 单命令漏网点）。"""
        d = self._tdir()
        (d / "clip_01.webp").write_bytes(b"x" * 10)
        found = mg_core.find_existing_product(str(d / "clip_01.mp4"))
        self.assertTrue(found.endswith(".webp"))

    def test_empty_file_not_counted(self):
        d = self._tdir()
        p = d / "clip_01.mp4"
        p.write_bytes(b"")      # 空文件≠已出片，断点续跑应继续生成
        self.assertEqual(mg_core.find_existing_product(str(p)), "")

    def test_mp4_sibling_found_when_out_is_webp(self):
        d = self._tdir()
        (d / "clip_01.mp4").write_bytes(b"x" * 10)
        found = mg_core.find_existing_product(str(d / "clip_01.webp"))
        self.assertEqual(found, str(d / "clip_01.mp4"))

    def test_none_found(self):
        d = self._tdir()
        self.assertEqual(mg_core.find_existing_product(str(d / "clip_01.mp4")), "")


class TestBatchExitCode(unittest.TestCase):
    """batch 退出码 → pipeline 决策契约：
    0=全成功；1=有 FAIL；4=有 PENDING（超时在途，必须 exit 4 交还 agent 问三选）。"""

    def test_all_ok_exit_0(self):
        self.assertEqual(mg_batch.batch_exit_code(["OK A (agnes key#1)", "skip B"]), 0)

    def test_fail_exit_1(self):
        self.assertEqual(mg_batch.batch_exit_code(
            ["OK A", "FAIL(rc=2) B [video key#1]"]), 1)

    def test_pending_exit_4(self):
        """全 PENDING（纯超时场景）：此前 exit 0 → pipeline 静默放行。
        任务已受理在途，重试=重复扣费，必须 4 让 pipeline 交还 agent。"""
        self.assertEqual(mg_batch.batch_exit_code(
            ["PENDING B (任务已落盘，跑 harvest 收割)"]), 4)

    def test_pending_takes_precedence_over_fail(self):
        # 时序不确定：同一轮既可能 FAIL 也可能 PENDING，只要在途就必须交还 agent
        self.assertEqual(mg_batch.batch_exit_code(
            ["FAIL(rc=5) A", "PENDING B (超时)"]), 4)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)


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

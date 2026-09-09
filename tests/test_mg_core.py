# -*- coding: utf-8 -*-
"""mg_core 路由/熔断/env/caps 测试——按模块拆分自 test_media_gen.py。

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


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
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


class TestLedgerAppend(unittest.TestCase):
    """#5 成本账本：ledger_append 文件行为（JSONL 追加 + 自动建目录）。"""

    def test_append_writes_jsonl_lines(self):
        import mg_core
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "ledger.jsonl"     # 父目录不存在
            mg_core.ledger_append(p, {"provider": "agnes", "op": "image", "ok": True})
            mg_core.ledger_append(p, {"provider": "zhipu", "op": "video", "ok": False})
            rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["provider"], "agnes")
            self.assertTrue(rows[0]["ok"])
            self.assertFalse(rows[1]["ok"])

    def test_append_fills_ts_and_ms(self):
        """entry 缺 ts/ms 时自动补（挂点处少写样板）。"""
        import mg_core
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.jsonl"
            mg_core.ledger_append(p, {"provider": "agnes", "op": "tts", "ok": True})
            row = json.loads(p.read_text(encoding="utf-8").strip())
            self.assertIn("ts", row)
            self.assertIn("ms", row)


class TestLedgerSummarize(unittest.TestCase):
    """#5 成本账本：ledger_summarize 聚合纯函数。"""

    def _row(self, ts, provider="agnes", op="image", ok=True, ms=1000):
        return {"ts": ts, "provider": provider, "op": op, "ok": ok, "ms": ms,
                "key": 1, "err": ""}

    def test_summary_counts_and_wasted(self):
        import mg_core
        rows = [
            self._row("2026-09-09T10:00:00", ok=True),
            self._row("2026-09-09T10:01:00", ok=True, op="video", ms=9000),
            self._row("2026-09-09T10:02:00", ok=False, provider="zhipu", op="video"),
            self._row("2026-09-08T09:00:00", ok=True, provider="zhipu"),
        ]
        s = mg_core.ledger_summarize(rows)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["ok"], 3)
        self.assertEqual(s["fail"], 1)
        self.assertEqual(s["wasted"], 1, "失败调用=白烧的额度")
        self.assertEqual(s["by_provider"]["agnes"]["total"], 2)
        self.assertEqual(s["by_provider"]["zhipu"]["fail"], 1)
        self.assertEqual(s["by_op"]["video"]["total"], 2)
        self.assertEqual(s["by_day"]["2026-09-09"]["total"], 3)

    def test_days_filter_drops_old(self):
        import mg_core
        rows = [
            self._row("2026-09-09T10:00:00"),
            self._row("2026-08-01T10:00:00"),          # 一个月前
        ]
        s = mg_core.ledger_summarize(rows, days=7)
        self.assertEqual(s["total"], 1, "days=N 只统计最近 N 天")

    def test_bad_rows_skipped(self):
        """坏行（缺字段/时间戳非法）跳过不炸——JSONL 是 append-only，历史脏数据必须容错。"""
        import mg_core
        rows = [
            self._row("2026-09-09T10:00:00"),
            {"provider": "x"},                          # 缺 ts
            {"ts": "not-a-date", "provider": "x", "op": "image", "ok": True},
            None,                                       # 完全坏行
        ]
        s = mg_core.ledger_summarize(rows)
        self.assertEqual(s["total"], 1)


class TestLedgerHook(unittest.TestCase):
    """#5 挂点：call_with_failover 成功/失败都要落账（账本空 = 挂点没生效）。"""

    def setUp(self):
        import mg_core
        self._td = tempfile.TemporaryDirectory()
        self.ledger = Path(self._td.name) / "ledger.jsonl"
        self._orig = mg_core.LEDGER_FILE
        self._orig_state = mg_core.STATE_FILE
        mg_core.LEDGER_FILE = self.ledger
        # state 也必须重定向：真实 state 里可能留着上次跑的 key 冷却条目，
        # 会让 failover 全部跳过 → 报 "所有 key 失败: None" 的假故障
        mg_core.STATE_FILE = Path(self._td.name) / "state.json"
        self._env = dict(os.environ)
        os.environ["MEDIA_AGNES_1_KEY"] = "k1"
        os.environ["MEDIA_AGNES_1_BASE"] = "https://example.invalid/v1"

    def tearDown(self):
        import mg_core
        mg_core.LEDGER_FILE = self._orig
        mg_core.STATE_FILE = self._orig_state
        os.environ.clear()
        os.environ.update(self._env)
        self._td.cleanup()

    def _rows(self):
        return [json.loads(x) for x in self.ledger.read_text(encoding="utf-8").splitlines()
                if x.strip()]

    def test_success_logged(self):
        import mg_core
        mg_core.call_with_failover("agnes", lambda k: {"ok": 1}, kind="image")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["op"], "image")
        self.assertEqual(rows[0]["provider"], "agnes")

    def test_failure_logged(self):
        """失败也要记——那才是"白烧的额度"，只看成功数发现不了问题。"""
        import mg_core
        def bad(k):
            raise PermissionError("401 invalid key")
        with self.assertRaises(mg_core.AllKeysFailed):
            mg_core.call_with_failover("agnes", bad, kind="video")
        rows = self._rows()
        self.assertTrue(rows)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("401", rows[0]["err"])
        self.assertEqual(rows[0]["op"], "video")


if __name__ == '__main__':
    unittest.main(verbosity=2)

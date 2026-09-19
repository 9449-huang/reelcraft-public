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


class TestPollTasks(unittest.TestCase):
    """批四 B：轮询循环骨架收归 mg_core.poll_tasks 生成器。

    此前三份手写 while 循环（_resolve_async_task / _poll_video_task /
    cmd_edit）+ 两份收割单查（_harvest_image/_video），语义差异（interval/
    headers/终态集合/超时动作）留在调用方，只共享骨架——网络语义不动。"""

    def _poll_result(self, states, **kw):
        """跑 poll_tasks，返回 yield 出的 (st, err) 序列与 urlopen 调用记录。

        始终用假时钟（每查一次推进 100s），deadline 才不会真等。"""
        t = {"v": 0}
        clock = lambda: (t.__setitem__("v", t["v"] + 100), t["v"])[1]  # noqa: E731
        it = mg_core.poll_tasks(
            "http://b/v1/tasks/T1",
            headers={"Authorization": "Bearer k"},
            interval=kw.get("interval", 0),
            deadline=kw.get("deadline", 10_000),
            timeout=kw.get("timeout", 5),
            clock=clock,
        )
        got, errs, calls = [], [], []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            s = states.pop(0)
            if isinstance(s, Exception):
                raise s
            import io as _io
            return _io.BytesIO(json.dumps(s).encode())

        with mock.patch.object(mg_core.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            for st, err in it:
                got.append((st, err))
                errs.append(err)
                if not states:
                    break          # 状态序列耗尽即停（不测 deadline 的场景）
        return got, errs, calls

    def test_yields_states_until_success(self):
        got, errs, calls = self._poll_result(
            [{"task_status": "RUNNING"}, {"task_status": "SUCCEED",
                                          "output_images": ["http://x/1.png"]}])
        self.assertEqual(len(got), 2)
        self.assertEqual(errs, [None, None])
        self.assertTrue(all(c == "http://b/v1/tasks/T1" for c in calls))

    def test_network_error_yields_none_state_with_err(self):
        """轮询异常不再 while 内 continue 吞掉，而是 yield (None, err)——
        调用方决定继续/终止（语义差异留在调用方，骨架不擅自决策）。"""
        got, errs, _ = self._poll_result(
            [OSError("net down"),
             {"task_status": "SUCCEED", "output_images": ["http://x/1.png"]}])
        self.assertEqual(errs[0], "net down")
        self.assertIsNone(got[0][0])
        self.assertIsNotNone(got[1][0])

    def test_deadline_stops_generator(self):
        t = {"v": 0}
        clock = lambda: (t.__setitem__("v", t["v"] + 100), t["v"])[1]  # noqa: E731
        got, errs, _ = self._poll_result(
            [{"task_status": "RUNNING"}, {"task_status": "RUNNING"},
             {"task_status": "RUNNING"}], deadline=250)
        self.assertLess(len(got), 3, "超 deadline 后生成器应停止 yield")

    def test_interval_sleep_called(self):
        """interval 语义：每轮 yield 后 sleep(interval)（cmd_edit 用 args.wait）。"""
        t = {"v": 0}
        clock = lambda: (t.__setitem__("v", t["v"] + 100), t["v"])[1]  # noqa: E731
        sleeps = []
        with mock.patch.object(mg_core.time, "sleep", side_effect=sleeps.append):
            it = mg_core.poll_tasks(
                "http://b/v1/tasks/T1", headers={}, interval=7,
                deadline=1000, timeout=5, clock=clock)
            list(it)   # 消费到底
        self.assertTrue(sleeps and all(s == 7 for s in sleeps))


class TestFetchTaskState(unittest.TestCase):
    """批四 B：收割单次查询收归 mg_core.fetch_task_state。"""

    def test_fetch_parses_json(self):
        import io as _io
        resp = _io.BytesIO(json.dumps({"task_status": "SUCCEED",
                                       "output_images": ["u"]}).encode())

        def fake_urlopen(req, timeout=None):
            assert timeout == 30
            return resp

        with mock.patch.object(mg_core.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            st, err = mg_core.fetch_task_state(
                "http://b/v1/tasks/T1",
                {"Authorization": "Bearer k"}, timeout=30)
        self.assertIsNone(err)
        self.assertEqual(st["task_status"], "SUCCEED")
        self.assertEqual(st["output_images"], ["u"])

    def test_fetch_error_returns_none_state(self):
        """查询异常 → (None, err)——调用方拿 err 打日志（harvest 原语义保留）。"""
        with mock.patch.object(mg_core.urllib.request, "urlopen",
                               side_effect=OSError("boom")):
            st, err = mg_core.fetch_task_state("http://b/v1/tasks/T1", {})
        self.assertIsNone(st)
        self.assertEqual(err, "boom")


class TestHarvestPending(unittest.TestCase):
    """harvest 收割必须能跑通。

    v4.7.7 修 P0：mg_batch 用了 `mg_core.build_poll_url` / `mg_core.fetch_task_state`，
    但该文件只有 `from mg_core import (...)`、没有 `import mg_core` → 一跑就
    `NameError: name 'mg_core' is not defined`。328 测试全绿没抓到，因为
    harvest 此前**零行为测试**（本类补上这个盲区）。"""

    def setUp(self):
        self._orig = mg_core.STATE_FILE
        self._td = tempfile.TemporaryDirectory()
        mg_core.STATE_FILE = Path(self._td.name) / "state.json"

    def tearDown(self):
        mg_core.STATE_FILE = self._orig
        self._td.cleanup()

    def test_harvest_image_downloads_and_pops(self):
        import argparse
        out = str(Path(self._td.name) / "S01.png")
        mg_core._save_pending_task("custom", "T1", out, "http://b/v1",
                                   kind="image", poll_path="/tasks")
        key = {"key": "k", "base": "http://b/v1", "poll": "http://b/v1", "n": 1}
        seen = {}

        def fake_fetch(url, headers, timeout=30):
            seen["url"] = url
            return {"task_status": "SUCCEED",
                    "output_images": ["http://b/v1/files/1.png"]}, None

        with mock.patch.object(mg_batch, "list_keys", return_value=[key]), \
             mock.patch.object(mg_core, "fetch_task_state", side_effect=fake_fetch), \
             mock.patch.object(mg_batch, "_download") as dl:
            mg_batch.cmd_harvest(argparse.Namespace())
        self.assertIn("/tasks/T1", seen.get("url", ""), "harvest 未按 poll_path 构造 URL")
        dl.assert_called_once()
        self.assertEqual(mg_core._load_state().get("pending_tasks"), {},
                         "收割成功后 pending 记录必须移除")

    def test_harvest_video_uses_pool_poll_style(self):
        """视频侧 URL 走池的 poll_style：agnes 未声明 poll_style → query 风格
        （旧版硬编码 path 拼接会拼错，与轮询侧同源后消灭该不一致）。"""
        import argparse
        mg_core._save_pending_task("agnes", "V1", str(Path(self._td.name) / "S02.mp4"),
                                   "http://b/v1")
        key = {"key": "k", "base": "http://b/v1", "poll": "http://b/v1", "n": 1}
        seen = {}

        def fake_fetch(url, headers, timeout=30):
            seen["url"] = url
            return {"task_status": "RUNNING"}, None

        with mock.patch.object(mg_batch, "list_keys", return_value=[key]), \
             mock.patch.object(mg_core, "fetch_task_state", side_effect=fake_fetch):
            mg_batch.cmd_harvest(argparse.Namespace())
        self.assertIn("video_id=V1", seen.get("url", ""),
                      "agnes 无 poll_style → 应按 query 风格构造（video_id=V1）")


class TestPendingKeyNumber(unittest.TestCase):
    """v4.7.8：落盘记录必须存 key 序号。

    原来只存 pool/base，续等（`--wait-task`）固定取 `list_keys()[0]`——多 key 池
    （agnes 3 把 key 跨不同域名/账号）由 key#2 提交的任务，用 key#1 轮询必失败
    （401/404 循环到 deadline），任务永远收不回。"""

    def setUp(self):
        self._orig = mg_core.STATE_FILE
        self._td = tempfile.TemporaryDirectory()
        mg_core.STATE_FILE = Path(self._td.name) / "state.json"

    def tearDown(self):
        mg_core.STATE_FILE = self._orig
        self._td.cleanup()

    def test_pending_records_key_n(self):
        mg_core._save_pending_task("agnes", "V9", "C:/x/o.mp4", "http://b/v1", key_n=2)
        rec = mg_core._load_state()["pending_tasks"]["V9"]
        self.assertEqual(rec.get("key_n"), 2, "落盘必须记 key 序号（续等要靠它选对 key）")

    def test_wait_uses_recorded_key(self):
        import argparse
        mg_core._save_pending_task("agnes", "V9", "C:/x/o.mp4", "http://b/v1", key_n=2)
        keys = [{"key": "k1", "base": "b", "poll": "b", "n": 1},
                {"key": "k2", "base": "b", "poll": "b", "n": 2}]
        args = argparse.Namespace(wait_task="V9", out="C:/x/o.mp4", wait=1,
                                  poll_timeout=None, provider="agnes")
        with mock.patch.object(mg_core, "list_keys", return_value=keys), \
             mock.patch.object(mg_core, "_poll_video_task") as poll:
            mg_core._wait_existing_task(args)
        self.assertEqual(poll.call_args[0][2]["n"], 2,
                         "续等必须用落盘记录的 key 序号，而不是 keys[0]")

    def test_wait_falls_back_when_key_gone(self):
        """记录里的 key 已从 env 移除 → 退回 keys[0]（不能因为找不到就崩）。"""
        import argparse
        mg_core._save_pending_task("agnes", "V9", "C:/x/o.mp4", "http://b/v1", key_n=7)
        keys = [{"key": "k1", "base": "b", "poll": "b", "n": 1}]
        args = argparse.Namespace(wait_task="V9", out="C:/x/o.mp4", wait=1,
                                  poll_timeout=None, provider="agnes")
        with mock.patch.object(mg_core, "list_keys", return_value=keys), \
             mock.patch.object(mg_core, "_poll_video_task") as poll:
            mg_core._wait_existing_task(args)
        self.assertEqual(poll.call_args[0][2]["n"], 1)


class TestKeyLevelStyleEnv(unittest.TestCase):
    """v4.7.8：报错提示让用户配 `_REF_IMAGE_STYLE=extra_body` —— 该 env 必须真被读。

    此前 `list_keys` 不读这三个 env（STYLE/MAX/KEYFRAMES_STYLE），而
    `ref_image_supported`/`keyframes_supported` 有 key 级覆盖分支 → key 级形同虚设，
    照提示配完仍然 die（自指死循环）。"""

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEDIA_")}
        env.update(extra)
        return env

    def test_list_keys_reads_style_env(self):
        env = self._env(MEDIA_CUSTOM_1_KEY="k", MEDIA_CUSTOM_1_BASE="http://b/v1",
                        MEDIA_CUSTOM_1_REF_IMAGE_STYLE="extra_body",
                        MEDIA_CUSTOM_1_REF_IMAGE_MAX="2",
                        MEDIA_CUSTOM_1_KEYFRAMES_STYLE="extra_body")
        with mock.patch.dict(os.environ, env, clear=True):
            keys = mg_core.list_keys("custom", required=False)
        self.assertTrue(keys, "env 配了 key 却枚举不到")
        k = keys[0]
        self.assertEqual(k.get("ref_image_style"), "extra_body",
                         "list_keys 必须读 _REF_IMAGE_STYLE（否则提示自指死循环）")
        self.assertEqual(mg_core.ref_image_supported({}, k)["max"], 2,
                         "_REF_IMAGE_MAX 必须是数字语义（字符串会与 len() 比较出错）")
        self.assertTrue(mg_core.keyframes_supported({}, k),
                        "list_keys 必须读 _KEYFRAMES_STYLE")

    def test_key_level_env_makes_pool_candidate(self):
        """池模板未声明、但 key 级 env 声明了 → 该池仍应能作候选（否则提示自指死循环）。"""
        env = self._env(MEDIA_CUSTOM_1_KEY="k", MEDIA_CUSTOM_1_BASE="http://b/v1",
                        MEDIA_CUSTOM_1_REF_IMAGE_STYLE="extra_body")
        with mock.patch.dict(os.environ, env, clear=True):
            sup = mg_core.ref_image_candidates("custom", {})
        self.assertIsNotNone(sup, "key 级 env 声明未让该池成为候选——提示就是自指的")
        self.assertEqual(sup["max"], 4)

    def test_pool_level_declaration_still_wins(self):
        info = mg_core.PROVIDERS["agnes"]["models"]["image"]
        sup = mg_core.ref_image_candidates("agnes", info)
        self.assertIsNotNone(sup)
        self.assertEqual(sup["style"], "extra_body")


class TestCostGate(unittest.TestCase):
    """v4.9.0：批量前的调用次数预估与额度门禁。

    ledger 目前只有**事后**记账（ledger_summarize）——跑之前不知道要烧多少次调用。
    免费池的"成本"就是额度：一次误跑（如忘了 --only，全量 60 镜 × 每镜 3 次上限）
    要等跑完才知道。这里补**事前**预估 + 阈值拦截。"""

    def test_estimate_basic(self):
        est = mg_batch.estimate_calls(10, 0)
        self.assertEqual((est["shots"], est["max_calls"]), (10, 10))

    def test_estimate_with_retries(self):
        est = mg_batch.estimate_calls(10, 2)
        self.assertEqual(est["max_calls"], 30, "每镜 1 次 + 最多重试 2 次 = 上限 3 次")

    def test_estimate_negative_clamped(self):
        """非法输入不得算出负数上限（负数会被 `>` 判定绕过门禁）。"""
        self.assertEqual(mg_batch.estimate_calls(-5, -1)["max_calls"], 0)

    def test_budget_no_limit_never_blocks(self):
        self.assertIsNone(mg_batch.budget_error(mg_batch.estimate_calls(10, 2), 0))

    def test_budget_within_limit_passes(self):
        self.assertIsNone(mg_batch.budget_error(mg_batch.estimate_calls(10, 2), 30))

    def test_budget_blocks_over_limit(self):
        err = mg_batch.budget_error(mg_batch.estimate_calls(10, 2), 29)
        self.assertIsNotNone(err, "预估 30 > 门禁 29 必须拦")
        self.assertIn("30", err)
        self.assertIn("29", err)


class TestCharacterBible(unittest.TestCase):
    """v4.9.0：角色圣经（漫剧方向第一步）。

    角色级连续性（跨几十镜引用同一角色）不该靠每镜手抄参考图路径——
    `<shots>/characters.json` 定义一次，shot 只写 `characters: ["hero"]`。"""

    def test_merge_refs_from_characters(self):
        bible = {"characters": {"hero": {"ref_images": ["a.png", "b.png"]}}}
        refs, miss = mg_core.merge_character_refs({"characters": ["hero"]}, bible)
        self.assertEqual(refs, ["a.png", "b.png"])
        self.assertEqual(miss, [])

    def test_merge_dedup_with_own_ref(self):
        bible = {"characters": {"hero": {"ref_images": ["a.png"]}}}
        refs, _ = mg_core.merge_character_refs(
            {"characters": ["hero"], "ref_image": ["a.png", "c.png"]}, bible)
        self.assertEqual(refs, ["a.png", "c.png"], "与角色图重复的自带图要去重")

    def test_missing_character_reported(self):
        refs, miss = mg_core.merge_character_refs({"characters": ["ghost"]}, {"characters": {}})
        self.assertEqual(refs, [])
        self.assertEqual(miss, ["ghost"], "查不到的角色 id 必须报出来（不能静默丢一致性）")

    def test_no_bible_falls_back_to_own_ref(self):
        refs, miss = mg_core.merge_character_refs({"ref_image": ["x.png"]}, {})
        self.assertEqual(refs, ["x.png"])
        self.assertEqual(miss, [], "没有角色圣经时行为与旧版一致（零回归）")

    def test_string_forms_accepted(self):
        bible = {"characters": {"hero": {"ref_images": ["a.png"]}}}
        refs, _ = mg_core.merge_character_refs({"characters": "hero", "ref_image": "z.png"}, bible)
        self.assertEqual(refs, ["a.png", "z.png"], "字符串形式（手写 JSON 常见）也要认")

    def test_load_bible_tolerant(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(mg_core.load_character_bible(td), {}, "缺失 → 空")
            (Path(td) / "characters.json").write_text("{bad json", encoding="utf-8")
            self.assertEqual(mg_core.load_character_bible(td), {}, "损坏不能让 batch 崩")
            (Path(td) / "characters.json").write_text('["not a dict"]', encoding="utf-8")
            self.assertEqual(mg_core.load_character_bible(td), {}, "非 dict 一律当空")


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
        """days=N 只统计最近 N 天。

        用**相对当前时间**造数据：原先硬编码 "2026-09-09" 并断言 7 天内命中，
        等于给测试装了颗时间炸弹——到了 2026-09-16 之后必然失败（门禁无辜变红）。
        """
        import datetime as _dt
        import mg_core
        now = _dt.datetime.now()
        fresh = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        old = (now - _dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        s = mg_core.ledger_summarize([self._row(fresh), self._row(old)], days=7)
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


class TestMixedAudioConcat(unittest.TestCase):
    """复测发现的真 P0（2026-09）：concat 曾用 `all(audio)` 判定整卷音轨。

    hybrid 模式（真视频带环境音 + kenburns 静帧片段无音轨）是常态，`all` 为 False
    → 整卷音轨被静默丢弃、rc=0 无声成片。修法：区分 none/all/mixed，mixed 给无音轨
    分片补等长静音。
    """

    def test_audio_plan(self):
        import postprocess as pp
        a = {"audio": True}
        n = {"audio": False}
        self.assertEqual(pp.audio_plan([]), "none")
        self.assertEqual(pp.audio_plan([n, n]), "none")
        self.assertEqual(pp.audio_plan([a, a]), "all")
        self.assertEqual(pp.audio_plan([a, n]), "mixed", "混编必须识别为 mixed 而非 none")
        self.assertEqual(pp.audio_plan([n, a, n]), "mixed")
        self.assertEqual(pp.audio_plan([{}, {}]), "none", "缺 audio 字段按无音轨")

    def test_audio_src_pads_silence(self):
        import postprocess as pp
        with_audio = pp.audio_src(True, 0, 3.0)
        self.assertIn("[0:a]", with_audio)
        silent = pp.audio_src(False, 2, 4.5)
        self.assertIn("anullsrc", silent, "无音轨分片必须补静音，否则混编丢整体音轨")
        self.assertIn("atrim=0:4.500", silent)
        self.assertIn("[a2]", silent)

    def test_audio_src_zero_duration_falls_back(self):
        """时长探不到（0/None）时用兜底值，不能生成 atrim=0:0（零长静音）。"""
        import postprocess as pp
        out = pp.audio_src(False, 1, 0.0)
        self.assertNotIn("atrim=0:0.000", out)


class TestBlockedShotsVisible(unittest.TestCase):
    """复测发现的真 P0（2026-09）：make_cmd 报 MISS/STALE 的镜曾被当 "skip" 吞掉。

    后果：成片静默缺一段、batch_run.json 无记录、--retry-failed 永远抓不到。
    修法：skip 分 done/blocked 两类，blocked 进结果与账单并计入非零退出码。
    """

    def test_skip_kind(self):
        import mg_batch
        self.assertEqual(mg_batch.skip_kind("skip (exists)"), "done")
        self.assertEqual(mg_batch.skip_kind("skip (transition)"), "done")
        self.assertEqual(mg_batch.skip_kind("MISS (no frame)"), "blocked")
        self.assertEqual(mg_batch.skip_kind("MISS (transition S1T: seed 未抽)"), "blocked")
        self.assertEqual(mg_batch.skip_kind("STALE (transition S1T: ...)"), "blocked")

    def test_record_skip_marks_blocked(self):
        import mg_batch
        results, pm = [], {}
        self.assertFalse(mg_batch.record_skip(results, pm, "S1", "skip (exists)"))
        self.assertEqual(results, ["skip S1"])
        self.assertNotIn("S1", pm, "已完成跳过不进账单")
        self.assertTrue(mg_batch.record_skip(results, pm, "S2", "MISS (no frame)"))
        self.assertTrue(results[-1].startswith("MISS S2"), results)
        self.assertEqual(pm["S2"], "blocked")

    def test_exit_code_counts_miss_as_failure(self):
        """缺镜必须非零退出——否则 pipeline 带着缺口继续 concat。"""
        import mg_batch
        self.assertEqual(mg_batch.batch_exit_code(["skip A"]), 0)
        self.assertEqual(mg_batch.batch_exit_code(["OK A", "skip B"]), 0)
        self.assertEqual(mg_batch.batch_exit_code(["MISS B [MISS (no frame)]"]), 1)
        # PENDING 优先（超时在途：重试=重复扣费，必须交还 agent）
        self.assertEqual(mg_batch.batch_exit_code(
            ["PENDING A (任务已落盘)", "MISS B [MISS (no frame)]"]), 4)


class TestLastFrameFields(unittest.TestCase):
    """过渡镜（半自动方案一）：build_last_frame_fields 从池配置+key env 解析尾帧字段。

    哲学沿用 image_param/image_list：字段名可配（各家叫 tail_image/lastFrame/…），
    未配置 = 该池不支持首尾帧双条件（返回 None，调用方不传）。
    """

    def test_no_config_returns_none(self):
        import mg_core
        self.assertIsNone(mg_core.build_last_frame_fields({}, {}))

    def test_pool_level_config(self):
        import mg_core
        info = {"last_frame_param": "tail_image", "last_frame_list": False}
        out = mg_core.build_last_frame_fields(info, {})
        self.assertEqual(out, {"tail_image": None})   # 字段名确定，值由调用方填

    def test_list_style(self):
        import mg_core
        info = {"last_frame_param": "last_frame_urls", "last_frame_list": True}
        out = mg_core.build_last_frame_fields(info, {})
        self.assertEqual(out, {"last_frame_urls": None})

    def test_key_env_override(self):
        """同一池不同 key 支持不同字段名（key 级覆盖，env MEDIA_<P>_n_LAST_FRAME_PARAM）。"""
        import mg_core
        info = {"last_frame_param": "tail_image"}
        key = {"n": 2, "last_frame_param": "endFrame"}    # key 级覆盖
        out = mg_core.build_last_frame_fields(info, key)
        self.assertEqual(out, {"endFrame": None})

    def test_default_field_name(self):
        """只声明支持不指定名 → 默认 last_frame（common naming，可被覆盖）。"""
        import mg_core
        out = mg_core.build_last_frame_fields({"last_frame": True}, {})
        self.assertEqual(out, {"last_frame": None})


class TestAgnesRefImageAndKeyframes(unittest.TestCase):
    """v4.7（实测驱动）：Agnes 免费池支持「多图参考」与「首尾帧 keyframes」。

    实测证据（_probe_agnes/）：
      - extra_body.image=[...] → 走 /images/i2i/，角色特征保留（真图生图）
      - 顶层 image → 走 /images/t2i/，**静默忽略无报错**（退化成文生图）
      - 视频 extra_body={"image":[首,尾],"mode":"keyframes"} → 真插值（末帧≈第二关键帧）
    """

    def test_agnes_declares_capabilities(self):
        """池能力声明是单一真源：payload 分支由声明驱动，不硬编码池名。"""
        import mg_core
        v = mg_core.PROVIDERS["agnes"]["models"]["video"]
        i = mg_core.PROVIDERS["agnes"]["models"]["image"]
        self.assertEqual(v.get("keyframes_style"), "extra_body")
        self.assertEqual(i.get("ref_image_style"), "extra_body")
        self.assertEqual(i.get("ref_image_max"), 4)

    def test_keyframes_supported(self):
        import mg_core
        self.assertTrue(mg_core.keyframes_supported({"keyframes_style": "extra_body"}, {}))
        self.assertTrue(mg_core.keyframes_supported({}, {"keyframes_style": "extra_body"}))
        self.assertFalse(mg_core.keyframes_supported({}, {}))
        self.assertFalse(mg_core.keyframes_supported({"last_frame_param": "tail"}, {}))

    def test_ref_image_supported(self):
        import mg_core
        sup = mg_core.ref_image_supported({"ref_image_style": "extra_body"})
        self.assertEqual(sup, {"style": "extra_body", "max": 4})
        self.assertIsNone(mg_core.ref_image_supported({}))
        self.assertEqual(
            mg_core.ref_image_supported({"ref_image_style": "extra_body",
                                         "ref_image_max": 2})["max"], 2)

    def test_build_image_body_refs_go_extra_body(self):
        """参考图必须放 extra_body；顶层出现 image 会被上游静默忽略（坑）。"""
        import mg_core
        body = mg_core.build_image_body("m1", "p", "1024x1024", refs=["u1", "u2"])
        self.assertNotIn("image", body, "顶层 image 会被静默忽略，绝不能出现")
        self.assertEqual(body["extra_body"]["image"], ["u1", "u2"])
        self.assertEqual(body["extra_body"]["response_format"], "url")

    def test_build_image_body_plain_t2i(self):
        import mg_core
        body = mg_core.build_image_body("m1", "p", "1024x1024")
        self.assertEqual(body["model"], "m1")
        self.assertEqual(body["prompt"], "p")
        self.assertNotIn("extra_body", body)


class TestImageToUriShrunk(unittest.TestCase):
    """大图直传上游会读超时（实测 960KB→失败 / 220KB→18s 成功）。

    编码器必须在**超阈值时**才改编码：小文件零改动（防回归），大文件转 JPEG 保尺寸。
    """

    def test_missing_path_passthrough(self):
        import mg_core
        self.assertEqual(mg_core.image_to_uri_shrunk("/no/such/file.png"),
                         "/no/such/file.png")

    def test_url_passthrough(self):
        """URL（非本地文件）原样返回——Agnes 回传的 url 走这条。"""
        import mg_core
        u = "https://example.invalid/x.png"
        self.assertEqual(mg_core.image_to_uri_shrunk(u), u)

    def test_small_file_uses_original_mime(self):
        """小文件走原路径：mime 保持 image/png（不因压缩逻辑改变既有行为）。"""
        import mg_core
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "small.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1000)
            out = mg_core.image_to_uri_shrunk(str(p))
            self.assertTrue(out.startswith("data:image/png;base64,"))

    def test_large_png_becomes_smaller_jpeg(self):
        """大 PNG → JPEG，且体积显著下降（否则等于没修）。"""
        import mg_core
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("无 PIL")
        import random
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "big.png"
            im = Image.new("RGB", (1024, 1024))
            px = im.load()
            random.seed(1)
            for y in range(1024):
                for x in range(1024):
                    px[x, y] = (random.randint(0, 255), random.randint(0, 255),
                                random.randint(0, 255))
            im.save(p)          # 噪声图压缩率低 → 体积大
            if p.stat().st_size <= 450_000:
                self.skipTest("噪声图不够大，跳过")
            out = mg_core.image_to_uri_shrunk(str(p))
            self.assertTrue(out.startswith("data:image/jpeg;base64,"))
            self.assertLess(len(out), p.stat().st_size,
                            "JPEG 编码后应比原 PNG 小（否则没达到省体积目的）")


class TestNetworkErrorClassification(unittest.TestCase):
    """http_call 的重试边界（P1：读超时被通用 except 捕获 → 重提 = 重复扣费）。

    判据：POST 请求**已经发出去**之后才失败 → 服务端可能已受理 → 不能重试、不能换 key。
    连接建立前就失败（DNS/拒绝连接）→ 肯定没提交 → 照旧重试。
    """

    def test_post_read_timeout_is_uncertain(self):
        import socket
        import mg_core
        for exc in (socket.timeout("read timed out"), TimeoutError("timed out")):
            self.assertEqual(mg_core.classify_network_error(exc, "POST"), "uncertain",
                             f"{type(exc).__name__} 已发出后超时，不得重试")

    def test_get_read_timeout_is_retryable(self):
        """GET 幂等：保留原有重试韧性，别把超时一刀切成不可重试。"""
        import socket
        import mg_core
        self.assertEqual(mg_core.classify_network_error(socket.timeout("t"), "GET"), "retry")

    def test_connection_refused_is_retryable_even_on_post(self):
        """连接被拒 = 请求从未送达 → 重试安全（不能过度保守拖垮可用性）。"""
        import mg_core
        self.assertEqual(mg_core.classify_network_error(ConnectionRefusedError("refused"),
                                                        "POST"), "retry")

    def test_urlerror_wrapping_timeout_is_uncertain(self):
        """urlopen 抛的是 URLError 包着 timeout —— 必须拆开看 reason。"""
        import socket
        import urllib.error
        import mg_core
        e = urllib.error.URLError(socket.timeout("read timed out"))
        self.assertEqual(mg_core.classify_network_error(e, "POST"), "uncertain")

    def test_urlerror_wrapping_dns_is_retryable(self):
        import socket
        import urllib.error
        import mg_core
        e = urllib.error.URLError(socket.gaierror("name resolution failed"))
        self.assertEqual(mg_core.classify_network_error(e, "POST"), "retry")

    def test_remote_disconnected_on_post_is_uncertain(self):
        """连接被对端掐断（读到一半断）→ 也无法确认未受理。"""
        import http.client
        import mg_core
        self.assertEqual(mg_core.classify_network_error(http.client.RemoteDisconnected("x"),
                                                        "POST"), "uncertain")

    def test_plain_exception_defaults_to_retry(self):
        import mg_core
        self.assertEqual(mg_core.classify_network_error(ValueError("weird"), "POST"), "retry")


class TestHttpCallNoDuplicateSubmit(unittest.TestCase):
    """http_call 行为级：POST 读超时只发一次（原来会重试 3 次 = 提交 3 次）。"""

    def test_post_read_timeout_submits_once(self):
        import socket
        import mg_core
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(req)
            raise socket.timeout("read timed out")

        with mock.patch.object(mg_core.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(mg_core.time, "sleep"):
            with self.assertRaises(mg_core.RequestUncertain):
                mg_core.http_call("POST", "https://x.invalid/v1/videos", {}, {"a": 1}, timeout=10)
        self.assertEqual(len(sent), 1, "读超时后又重提了一次 —— 每次重提都是一笔新费用")

    def test_post_server_error_still_retried(self):
        """5xx = 服务端明确拒绝，没产生任务 → 保持重试（别误伤可用性）。"""
        import urllib.error
        import mg_core
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(req)
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

        with mock.patch.object(mg_core.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(mg_core.time, "sleep"):
            with self.assertRaises(SystemExit):
                mg_core.http_call("POST", "https://x.invalid/v1/videos", {}, {"a": 1},
                                  timeout=10, max_retry=2)
        self.assertEqual(len(sent), 2, "503 是明确失败，应保持原重试次数")


class TestRequestUncertainStopsFailover(unittest.TestCase):
    """RequestUncertain 不得触发换 key：换 key = 第二次提交 = 第二次扣费。"""

    def setUp(self):
        import mg_core
        self._td = tempfile.TemporaryDirectory()
        self._orig_ledger = mg_core.LEDGER_FILE
        self._orig_state = mg_core.STATE_FILE
        mg_core.LEDGER_FILE = Path(self._td.name) / "ledger.jsonl"
        mg_core.STATE_FILE = Path(self._td.name) / "state.json"
        self._env = dict(os.environ)
        os.environ["MEDIA_AGNES_1_KEY"] = "k1"
        os.environ["MEDIA_AGNES_1_BASE"] = "https://example.invalid/v1"
        os.environ["MEDIA_AGNES_2_KEY"] = "k2"
        os.environ["MEDIA_AGNES_2_BASE"] = "https://example.invalid/v1"

    def tearDown(self):
        import mg_core
        mg_core.LEDGER_FILE = self._orig_ledger
        mg_core.STATE_FILE = self._orig_state
        os.environ.clear()
        os.environ.update(self._env)
        self._td.cleanup()

    def test_no_failover_on_uncertain(self):
        import mg_core
        tried = []

        def bad(k):
            tried.append(k["n"])
            raise mg_core.RequestUncertain("POST 超时，可能已受理")

        with self.assertRaises(mg_core.RequestUncertain):
            mg_core.call_with_failover("agnes", bad, kind="video")
        self.assertEqual(tried, [1], "不确定状态换 key 重提了 —— 会重复扣费")

    def test_uncertain_is_recorded_in_ledger(self):
        import mg_core

        def bad(k):
            raise mg_core.RequestUncertain("POST 超时")

        with self.assertRaises(mg_core.RequestUncertain):
            mg_core.call_with_failover("agnes", bad, kind="video")
        rows = [json.loads(x) for x in
                mg_core.LEDGER_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertTrue(rows, "不确定性失败也必须落账（可能是笔已产生的费用）")
        self.assertFalse(rows[0]["ok"])


class TestTtsSlotScan(unittest.TestCase):
    """TTS 槽位扫描必须只有**一处**实现。

    mg_status 曾硬编码只探 MEDIA_TTS_1_*，而 cmd_tts 扫全部槽位 →
    key#1 空缺时 status 误报"tts 未配置"，把人指去改一个本来没问题的配置。
    """

    def test_contiguous_slots(self):
        import mg_core
        env = {"MEDIA_TTS_1_KEY": "k", "MEDIA_TTS_1_BASE": "b",
               "MEDIA_TTS_2_KEY": "k2", "MEDIA_TTS_2_BASE": "b2"}
        self.assertEqual(mg_core.scan_tts_slots(env), [1, 2])

    def test_hole_after_first_is_tolerated(self):
        import mg_core
        env = {"MEDIA_TTS_1_KEY": "k", "MEDIA_TTS_1_BASE": "b",
               "MEDIA_TTS_3_KEY": "k3", "MEDIA_TTS_3_BASE": "b3"}
        self.assertEqual(mg_core.scan_tts_slots(env), [1, 3])

    def test_starting_hole_is_tolerated(self):
        env = {"MEDIA_TTS_2_KEY": "k", "MEDIA_TTS_2_BASE": "b"}
        import mg_core
        self.assertEqual(mg_core.scan_tts_slots(env), [2])

    def test_empty_env(self):
        import mg_core
        self.assertEqual(mg_core.scan_tts_slots({}), [])

    def test_single_source_of_truth(self):
        """两处调用必须共用同一实现，不允许各写一份（语义漂移的根源）。"""
        import mg_core
        import media_gen
        self.assertIs(media_gen._scan_tts_slots, mg_core.scan_tts_slots)
class TestCapsCapabilityProbe(unittest.TestCase):
    """能力级真探针（批二）：池级冒烟只证"能出片"，不证"参考图真锁脸 / 首尾帧真插值"。

    v4.7 的教训：顶层 image 被静默忽略、大图直传读超时——这类只有真跑才知道。
    把"实测过"从池级延伸到能力级：caps probe <pool> --kind <k> --cap <c> --real。
    """

    def setUp(self):
        import mg_caps
        self._td = tempfile.TemporaryDirectory()
        self._orig = mg_caps.CAPS_FILE
        mg_caps.CAPS_FILE = Path(self._td.name) / "caps.json"

    def tearDown(self):
        import mg_caps
        mg_caps.CAPS_FILE = self._orig
        self._td.cleanup()

    def test_unknown_cap_dies(self):
        import mg_caps
        with self.assertRaises(SystemExit) as cm:
            mg_caps.cap_cmd("agnes", "image", "bogus", Path("o.png"), 1, Path("."))
        self.assertEqual(cm.exception.code, 2)

    def test_undeclared_cap_refuses_to_burn_quota(self):
        """zhipu 未声明 keyframes → 探针拒绝（跑了也是白烧一次额度）。"""
        import mg_caps
        with self.assertRaises(SystemExit) as cm:
            mg_caps.cap_cmd("zhipu", "video", "keyframes", Path("o.mp4"), 1, Path("."))
        self.assertEqual(cm.exception.code, 2)

    def test_ref_image_cmd_builds_fixture_and_flag(self):
        import mg_caps
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cmd = mg_caps.cap_cmd("agnes", "image", "ref_image", tmp / "o.png", 1, tmp)
            self.assertIn("--ref-image", cmd)
            ref = Path(cmd[cmd.index("--ref-image") + 1])
            self.assertTrue(ref.exists() and ref.stat().st_size > 0,
                            "fixture 图要现场生成（小图，规避大图超时约束）")

    def test_keyframes_cmd_passes_both_frames(self):
        import mg_caps
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cmd = mg_caps.cap_cmd("agnes", "video", "keyframes", tmp / "o.mp4", 1, tmp)
            self.assertIn("--image", cmd)
            self.assertIn("--last-frame", cmd)

    def test_capability_view_declared_vs_measured(self):
        import time as _t
        import mg_caps
        rows = {r["cap"]: r for r in mg_caps.capability_view("agnes")}
        self.assertTrue(rows["ref_image"]["declared"], "agnes 应声明支持 ref_image")
        self.assertTrue(rows["keyframes"]["declared"], "agnes 应声明支持 keyframes")
        self.assertIsNone(rows["ref_image"]["measured"], "没测过就没有实测值")
        mg_caps.record("agnes", "video:cap:keyframes",
                       {"ok": True, "method": "cap-smoke", "probed_at_ts": _t.time()})
        rows = {r["cap"]: r for r in mg_caps.capability_view("agnes")}
        self.assertTrue(rows["keyframes"]["measured"]["ok"], "记录后实测即权威")

    def test_probe_cli_records_under_cap_key(self):
        import argparse
        import mg_caps
        with mock.patch.object(mg_caps, "_cap_smoke",
                               return_value={"ok": True, "elapsed_s": 1.0}) as sm:
            with self.assertRaises(SystemExit) as cm:
                mg_caps.cmd_caps(argparse.Namespace(
                    caps_cmd="probe", pool="agnes", kind="video", cap="keyframes",
                    real=True, model="", size="", image="", timeout=10, pin_key=1))
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(sm.call_args.args[2], "keyframes")
        s = json.loads(mg_caps.CAPS_FILE.read_text(encoding="utf-8"))
        self.assertIn("video:cap:keyframes", s.get("agnes", {}),
                      "能力实测必须落在独立的 cap 键下（不覆盖池级冒烟）")


class TestDownloadLocalPath(unittest.TestCase):
    """S1：轮询路径的产物消费必须支持 local_path（本地桥接风上游）。

    v4.7 审计 P1：_extract_video_url 认得 local_path 并原样返回，但 _download
    对它 urlopen -> ValueError（urlopen 不吃 Windows 盘符路径）。同步路径有
    shutil.copyfile 保护（#10），轮询路径没有——同一条链路两种行为。
    """

    def test_download_copies_local_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "made.mp4"
            src.write_bytes(b"FAKEVID")
            out = tmp / "out.mp4"
            mg_core._download(str(src), out)
            self.assertTrue(out.exists() and out.read_bytes() == b"FAKEVID")

    def test_download_local_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.mp4"
            with self.assertRaises(Exception):
                mg_core._download(str(Path(td) / "no_such.mp4"), out)
            self.assertFalse(out.exists())
            self.assertFalse(list(Path(td).glob("*.dl.*")),
                             "失败必须清掉 .dl 残骸")


class TestCmdEditValidation(unittest.TestCase):
    """S2（核验后修正）：cmd_edit 无输入图的退出码契约。

    审计初判"无 key 裸奔 exit 1"经真机复核为**误报**——key 从
    ~/.workbuddy/media_keys.env 文件加载（非进程 env），list_keys(required=True)
    已 die(2) 带指引。真正要钉死的是：缺输入图 die(2) 不 traceback。
    """

    def test_missing_input_image_dies_not_traceback(self):
        import subprocess as sp
        mg = Path(mg_core.__file__).parent / "media_gen.py"
        env = {**os.environ, "PYTHONUTF8": "1"}
        r = sp.run([sys.executable, str(mg), "edit", "--image", "no/such.png",
                    "--prompt", "x", "--out", "o.png"],
                   capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(r.returncode, 2, r.stderr[-300:])
        self.assertNotIn("Traceback", r.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
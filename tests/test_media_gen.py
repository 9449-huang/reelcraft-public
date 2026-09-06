# -*- coding: utf-8 -*-
"""reelcraft 最小测试集（unittest，标准库零依赖）。

只测纯逻辑函数，不发任何网络请求、不碰真实 ~/.workbuddy 状态。
运行：python -m unittest discover tests -v   （在 skill 根目录）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import media_gen as mg  # noqa: E402
import mg_core  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

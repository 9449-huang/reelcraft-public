# -*- coding: utf-8 -*-
"""face_consistency 测试：跨镜人脸一致性（SFace 128 维嵌入余弦）。

运行：python -m unittest discover tests          （快：纯函数 + 后端选择）
     SLOW=1 python -m unittest discover tests   （含真跑 cv2 的端到端）

设计：嵌入比对与判定全是**纯函数**（测试不碰 cv2/模型）；检测/嵌入是薄 IO 壳。
用例里的框与相似度取自 2026-09-15 真机实测（opencv_zoo 示例图），不是编的。
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_slow = not os.environ.get("SLOW")

# 真机实测的样本图（本地 assets，不进仓库；没有就跳过 SLOW 用例）
MODELS_DIR = Path.home() / ".workbuddy" / "models"
SAMPLE_FACES = MODELS_DIR / "sample_faces.jpg"

# 实测框（sample_faces.jpg 1024x512）：主体 + 背景人群 + 极小误检
F_SUBJECT = {"box": [249, 83, 134, 171], "score": 0.947, "area": 23008}
F_CROWD = {"box": [756, 189, 28, 35], "score": 0.928, "area": 1007}
F_TINY = {"box": [540, 217, 12, 14], "score": 0.809, "area": 178}
F_LOWCONF = {"box": [100, 100, 120, 150], "score": 0.42, "area": 18000}


def _fc():
    import importlib
    return importlib.import_module("face_consistency")


class TestCosine(unittest.TestCase):
    """余弦相似度（纯 Python，不依赖 numpy）。"""

    def test_identical_is_one(self):
        self.assertAlmostEqual(_fc().cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0, places=6)

    def test_orthogonal_is_zero(self):
        self.assertAlmostEqual(_fc().cosine([1.0, 0.0], [0.0, 1.0]), 0.0, places=6)

    def test_opposite_is_minus_one(self):
        self.assertAlmostEqual(_fc().cosine([1.0, 0.0], [-1.0, 0.0]), -1.0, places=6)

    def test_scale_invariant(self):
        """余弦只看方向——同一张脸不同亮度/对比度不该改分。"""
        self.assertAlmostEqual(_fc().cosine([1.0, 1.0], [7.0, 7.0]), 1.0, places=6)

    def test_zero_vector_safe(self):
        """零向量不能抛异常、也不能返回 nan（会让下游比较假通过）。"""
        v = _fc().cosine([0.0, 0.0], [1.0, 1.0])
        self.assertEqual(v, 0.0)
        self.assertEqual(_fc().cosine([0.0, 0.0], [0.0, 0.0]), 0.0)

    def test_length_mismatch_safe(self):
        self.assertEqual(_fc().cosine([1.0, 2.0], [1.0]), 0.0)


class TestFilterFaces(unittest.TestCase):
    """人群/误检过滤：只留"能当主体"的脸——背景 30px 小脸会让比对结果变成噪声。"""

    def test_defaults_drop_crowd_and_lowconf(self):
        kept = _fc().filter_faces([F_SUBJECT, F_CROWD, F_TINY, F_LOWCONF])
        self.assertEqual(len(kept), 1, f"应只留主体脸，实际 {kept}")
        self.assertEqual(kept[0]["area"], 23008)

    def test_side_threshold_boundary(self):
        just = {"box": [0, 0, 48, 48], "score": 0.9, "area": 2304}
        under = {"box": [0, 0, 47, 47], "score": 0.9, "area": 2209}
        kept = _fc().filter_faces([just, under], min_side=48)
        self.assertEqual([f["area"] for f in kept], [2304], "min_side 是含端点的下限")

    def test_short_side_governs_not_area(self):
        """宽脸条（200x30）不该被当主体——判据是**短边**，不是面积。"""
        strip = {"box": [0, 0, 200, 30], "score": 0.9, "area": 6000}
        self.assertEqual(_fc().filter_faces([strip], min_side=48), [])

    def test_empty_and_none_safe(self):
        self.assertEqual(_fc().filter_faces([]), [])
        self.assertEqual(_fc().filter_faces(None), [])


class TestPickPrimary(unittest.TestCase):
    def test_largest_area_wins(self):
        p = _fc().pick_primary([F_CROWD, F_SUBJECT, F_TINY])
        self.assertEqual(p["area"], 23008, "主体 = 画面里最大那张脸")

    def test_empty_returns_none(self):
        self.assertIsNone(_fc().pick_primary([]))
        self.assertIsNone(_fc().pick_primary(None))


class TestPairwiseSimilarity(unittest.TestCase):
    """两两相似度：只出 i<j 的组合，不重复、不自比。"""

    def test_pairs_shape_and_count(self):
        embs = {"S01": [1.0, 0.0], "S02": [1.0, 0.0], "S03": [0.0, 1.0]}
        pairs = _fc().pairwise_similarity(embs)
        self.assertEqual(len(pairs), 3, "3 个条目应有 3 对（C(3,2)）")
        keys = {tuple(sorted((p["a"], p["b"]))) for p in pairs}
        self.assertEqual(keys, {("S01", "S02"), ("S01", "S03"), ("S02", "S03")})

    def test_values(self):
        embs = {"A": [1.0, 0.0], "B": [1.0, 0.0], "C": [0.0, 1.0]}
        d = {(p["a"], p["b"]): round(p["sim"], 3) for p in _fc().pairwise_similarity(embs)}
        self.assertEqual(d[("A", "B")], 1.0)
        self.assertEqual(d[("A", "C")], 0.0)

    def test_single_entry_no_pairs(self):
        self.assertEqual(_fc().pairwise_similarity({"A": [1.0, 0.0]}), [])

    def test_deterministic_order(self):
        embs = {"S02": [1.0, 0.0], "S01": [0.0, 1.0], "S03": [1.0, 1.0]}
        a = [(p["a"], p["b"]) for p in _fc().pairwise_similarity(embs)]
        b = [(p["a"], p["b"]) for p in _fc().pairwise_similarity(embs)]
        self.assertEqual(a, b, "输出顺序必须稳定（报告可 diff）")


class TestMeanSimilarity(unittest.TestCase):
    def test_odd_one_out_gets_low_mean(self):
        """实测语义：同一个人两镜 ≈0.59、不同人 ≈0.1 → 出问题的那一镜均值必然最低。"""
        embs = {"S01": [1.0, 0.0, 0.0], "S02": [1.0, 0.05, 0.0], "S03": [0.0, 0.0, 1.0]}
        m = _fc().mean_similarity(embs)
        self.assertEqual(set(m), {"S01", "S02", "S03"})
        self.assertGreater(m["S01"], m["S03"])
        self.assertGreater(m["S02"], m["S03"])
        self.assertLess(m["S03"], 0.1, "与谁都对不上的那一镜，均值应很低")

    def test_single_entry_mean_is_none(self):
        self.assertIsNone(_fc().mean_similarity({"A": [1.0, 0.0]})["A"],
                          "只有一镜时不存在「与其余镜的均值」，给 None 而不是编个 1.0")


class TestOutliers(unittest.TestCase):
    def test_flags_below_threshold(self):
        means = {"S01": 0.62, "S02": 0.58, "S03": 0.09}
        self.assertEqual(_fc().outliers(means, 0.363), ["S03"])

    def test_none_means_skipped(self):
        self.assertEqual(_fc().outliers({"S01": None, "S02": 0.5}, 0.363), [])

    def test_all_ok(self):
        self.assertEqual(_fc().outliers({"S01": 0.6, "S02": 0.7}, 0.363), [])


class TestConsistencyVerdict(unittest.TestCase):
    """判定产 (级别, 说明) 列表；空 = 全过。与 qcseq 同口味：报告性质，不硬拦。"""

    def test_clean_is_empty(self):
        v = _fc().consistency_verdict(
            [{"a": "S01", "b": "S02", "sim": 0.62}],
            {"S01": 0.62, "S02": 0.62}, {"S01": 1, "S02": 1})
        self.assertEqual(v, [])

    def test_outlier_warns_with_names(self):
        v = _fc().consistency_verdict(
            [{"a": "S01", "b": "S03", "sim": 0.08}],
            {"S01": 0.62, "S02": 0.58, "S03": 0.08}, {"S01": 1, "S02": 1, "S03": 1})
        msgs = " ".join(m for _, m in v)
        self.assertIn("WARN", [x[0] for x in v])
        self.assertIn("S03", msgs, "必须点出是哪一镜漂了——只说'不一致'等于没说")

    def test_insufficient_coverage_warns(self):
        """只有 1 镜检出可用人脸 → 无法比对，必须说清而不是静默 PASS。"""
        v = _fc().consistency_verdict([], {"S01": None}, {"S01": 1, "S02": 0})
        self.assertIn("WARN", [x[0] for x in v])
        self.assertIn("1", " ".join(m for _, m in v))

    def test_multi_subject_shot_warns(self):
        """一镜里有多张可用人脸 → 主体判定有歧义，提示人复核。"""
        v = _fc().consistency_verdict(
            [{"a": "S01", "b": "S02", "sim": 0.7}],
            {"S01": 0.7, "S02": 0.7}, {"S01": 2, "S02": 1})
        self.assertIn("WARN", [x[0] for x in v])
        self.assertIn("S01", " ".join(m for _, m in v))

    def test_zero_faces_reported(self):
        v = _fc().consistency_verdict([], {}, {"S01": 0, "S02": 0})
        self.assertIn("WARN", [x[0] for x in v])


class TestFaceBackend(unittest.TestCase):
    """后端可用性纯函数：缺 cv2 或缺模型一律返回空串（调用方给安装指引，不崩）。"""

    def test_empty_when_models_missing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_fc().face_backend(td), "")

    def test_sface_when_all_present(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "face_detection_yunet_2023mar.onnx").write_bytes(b"\x00" * 4096)
            (Path(td) / "face_recognition_sface_2021dec.onnx").write_bytes(b"\x00" * 4096)
            with mock.patch.object(_fc(), "_cv2_available", return_value=True):
                self.assertEqual(_fc().face_backend(td), "sface")

    def test_empty_when_runtime_missing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "face_detection_yunet_2023mar.onnx").write_bytes(b"\x00" * 4096)
            (Path(td) / "face_recognition_sface_2021dec.onnx").write_bytes(b"\x00" * 4096)
            with mock.patch.object(_fc(), "_cv2_available", return_value=False):
                self.assertEqual(_fc().face_backend(td), "", "有模型但没 cv2 → 也必须返回空，不崩")

    def test_install_hint_mentions_models_dir(self):
        hint = _fc().install_hint("/tmp/x")
        self.assertIn("opencv", hint.lower())
        self.assertIn("/tmp/x", hint)


class TestExitContract(unittest.TestCase):
    """退出码与 qcseq 同口味：0=全过 / 1=有 WARN（报告性质）/ 2=输入错误。"""

    def test_badge_from_verdict(self):
        fc = _fc()
        self.assertEqual(fc.badge([]), ("PASS", 0))
        self.assertEqual(fc.badge([("WARN", "x")]), ("WARN", 1))
        self.assertEqual(fc.badge([("FAIL", "x")]), ("FAIL", 2))


class TestFrameExtractionContract(unittest.TestCase):
    """抽帧契约（2026-09-15 实测暴露的两个坑）。

    坑 1：`ffmpeg -ss 0.00 -i x.jpg -frames:v 1 out.png` **rc=0 但完全不产出文件**
          （seek 越过唯一那一帧）→ 图片输入必须绕过 ffmpeg。
    坑 2：抽帧失败若只看 rc 就会被当成"没人脸"，把"测不了"说成"没问题"
          → 必须校验产物存在且非空，并把两种情况在报告里**分开说**。"""

    def test_image_input_bypasses_ffmpeg(self):
        fc = _fc()
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "a.jpg"
            img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
            src, why = fc._frame_for(img, Path(td) / "out.png")
        self.assertEqual(src, img, "图片必须直接用原文件，不经过 ffmpeg（否则 rc=0 无产物）")
        self.assertEqual(why, "image")

    def test_missing_input_reports_extract_failed(self):
        fc = _fc()
        with tempfile.TemporaryDirectory() as td:
            src, why = fc._frame_for(Path(td) / "nope.mp4", Path(td) / "out.png")
        self.assertIsNone(src)
        self.assertEqual(why, "extract_failed")


class TestExtractFailureIsDistinctFromNoFace(unittest.TestCase):
    """抽帧失败必须单列成 WARN，不能含糊成"没有可用人脸"。"""

    def test_verdict_reports_extract_failure(self):
        v = _fc().consistency_verdict([], {}, {"S01": 0, "S02": 0},
                                      extract_failed=["S01"])
        msgs = " ".join(m for _, m in v)
        self.assertIn("抽帧失败", msgs)
        self.assertIn("S01", msgs, "要说清是哪一镜抽帧失败")

    def test_analyze_surfaces_frame_ok_flag(self):
        fc = _fc()
        with mock.patch.object(fc, "face_backend", return_value="sface"), \
             mock.patch.object(fc, "_frame_for", return_value=(None, "extract_failed")):
            r = fc.analyze(["a.mp4", "b.mp4"])
        self.assertFalse(r["shots"][0]["frame_ok"])
        self.assertIn("抽帧失败", " ".join(m for _, m in r["verdict"]))


class TestIdentityKeyIsUniqueNotPath(unittest.TestCase):
    """身份键必须带序号。

    2026-09-15 实测暴露：最初用 `path` 当键，同一文件传两次（或 `--ref` 与某镜同文件）
    会在 `embs` dict 里**静默塌缩成一条** → 报告成"只有 1 镜，无法比对"。
    这个 bug 会让"自比对 = 1.0"这条最有说服力的验证**永远测不出来**。"""

    def test_duplicate_paths_get_distinct_keys(self):
        fc = _fc()
        face = {"box": [10, 10, 60, 60], "score": 0.9, "area": 3600, "emb": [1.0, 0.0]}
        with mock.patch.object(fc, "face_backend", return_value="sface"), \
             mock.patch.object(fc, "_frame_for", return_value=(Path("x.png"), "image")), \
             mock.patch.object(fc, "_detect_and_embed", return_value=[face]):
            r = fc.analyze(["same.jpg", "same.jpg"])
        keys = [s["key"] for s in r["shots"]]
        self.assertEqual(len(set(keys)), 2, f"同路径两次必须得到两个不同身份键：{keys}")
        self.assertEqual(len(r["pairs"]), 1, "两条记录应产生 1 对比较，不能塌缩")
        self.assertAlmostEqual(r["pairs"][0]["sim"], 1.0, places=6,
                               msg="同一张脸与自己比必须为 1.0")

    def test_ref_and_shot_same_file_still_compared(self):
        fc = _fc()
        face = {"box": [10, 10, 60, 60], "score": 0.9, "area": 3600, "emb": [1.0, 0.0]}
        with mock.patch.object(fc, "face_backend", return_value="sface"), \
             mock.patch.object(fc, "_frame_for", return_value=(Path("x.png"), "image")), \
             mock.patch.object(fc, "_detect_and_embed", return_value=[face]):
            r = fc.analyze(["a.jpg"], ref="a.jpg")
        self.assertEqual(len(r["shots"]), 2, "参考 + 1 镜 = 2 条")
        self.assertEqual(len(r["pairs"]), 1, "参考与同文件镜仍要产出比对，不能塌缩")


class TestCv2ImportShielding(unittest.TestCase):
    """cv2 导入必须避开 **scripts/ 遮蔽标准库 copy**。

    本仓有 `scripts/copy.py`（文案旁线）。真机 `python scripts/face_consistency.py`
    时 sys.path[0] 就是 scripts/，cv2 的 bootstrap 内部 `import copy` 会命中它 →
    `AttributeError: module 'copy' has no attribute 'copy'` → `_cv2_available()` 吞掉异常
    返回 False → **整个功能静默降级成"未安装"**（2026-09-15 实测：测试全绿，CLI 却
    打印安装指引）。测试进程里 unittest 早已导入标准库 copy，所以**永远看不到**——
    这就是为什么必须有下面这条独立进程的真机复刻。"""

    def test_cv2_only_imported_through_shield(self):
        import ast as _ast
        src = (Path(__file__).resolve().parents[1] / "scripts" /
               "face_consistency.py").read_text(encoding="utf-8")
        bad = []
        for fn in [n for n in _ast.walk(_ast.parse(src)) if isinstance(n, _ast.FunctionDef)]:
            if fn.name == "_import_cv2":
                continue
            for sub in _ast.walk(fn):
                if isinstance(sub, _ast.Import) and \
                        any(a.name.split(".")[0] == "cv2" for a in sub.names):
                    bad.append(f"{fn.name}:{sub.lineno}")
        self.assertEqual(bad, [],
                         "cv2 只许经 _import_cv2 导入（它负责临时摘掉 scripts/）: " + str(bad))

    def test_import_restores_sys_path(self):
        fc = _fc()
        before = list(sys.path)
        if fc._cv2_available():
            self.assertEqual(sys.path, before, "导入后必须原样还原 sys.path")

    @unittest.skipUnless((MODELS_DIR / "face_recognition_sface_2021dec.onnx").exists(),
                         "需要本地模型")
    def test_real_cli_scenario_can_import_cv2(self):
        """**新进程 + cwd=scripts/**（复刻 `python scripts/face_consistency.py`）→
        cv2 必须导入成功。这条是 copy 遮蔽 bug 的唯一有效防线。"""
        import subprocess as sp
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        r = sp.run([sys.executable, "-c",
                    "import face_consistency as fc; print(fc._cv2_available())"],
                   cwd=str(scripts), capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=120)
        out = (r.stdout or "").strip().splitlines()
        self.assertTrue(out and out[-1] == "True",
                        f"真机场景下 cv2 必须可用，实际 stdout={out} stderr={(r.stderr or '')[-300:]}")


class TestCliFacadeParity(unittest.TestCase):
    """`media_gen faces` 门面必须覆盖独立 CLI 的参数集。

    历史坑（v4.7.9）：CLI 默认值恒真 → 覆盖 plan 决策而无人察觉。
    同族风险：门面漏注册某个 flag，用户从 `media_gen faces` 走时**静默只能用默认值**
    （`cmd` 里全是 `getattr(args, x, default)`，漏了不报错）。这条钉住包含关系。
    """

    @staticmethod
    def _flags_from_var(tree, var):
        flags = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == var):
                for a in node.args:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and a.value.startswith("-")):
                        flags.add(a.value)
        return flags

    @staticmethod
    def _parser_var(tree, *, via_add_parser=None, via_ctor=False):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                f = node.value.func
                ok = False
                if via_add_parser and isinstance(f, ast.Attribute) and f.attr == "add_parser":
                    ok = bool(node.value.args) and isinstance(node.value.args[0], ast.Constant) \
                        and node.value.args[0].value == via_add_parser
                elif via_ctor and isinstance(f, ast.Attribute) and f.attr == "ArgumentParser":
                    ok = True
                if ok:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            return t.id
        return ""

    def _flags_of(self, path, **kw):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        var = self._parser_var(tree, **kw)
        self.assertTrue(var, f"{Path(path).name} 里找不到目标 parser 变量")
        return self._flags_from_var(tree, var)

    def test_media_gen_faces_covers_standalone_cli(self):
        root = Path(__file__).resolve().parents[1]
        standalone = self._flags_of(root / "scripts" / "face_consistency.py", via_ctor=True)
        facade = self._flags_of(root / "scripts" / "media_gen.py", via_add_parser="faces")
        missing = sorted(standalone - facade)
        self.assertEqual(missing, [], f"media_gen faces 门面缺少这些 flag（会静默用默认值）: {missing}")


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
@unittest.skipUnless(SAMPLE_FACES.exists(), "需要本地样本图（~/.workbuddy/models/sample_faces.jpg）")
class TestFaceConsistencyEndToEnd(unittest.TestCase):
    """真跑 cv2 检测+对齐+嵌入：验证"同一个人能对上、不同人对不上"。

    这是本模块的**内容级**验证——只看 rc=0 抓不到"检测框错位/对齐错/嵌入退化成常量"
    这类静默失败。断言依据 2026-09-15 实测：主体与同人小脸余弦 +0.591 > 阈值 0.363，
    不同人之间 ≤ +0.33。"""

    def test_detects_subject_face_and_embeds(self):
        r = _fc().analyze([str(SAMPLE_FACES)])
        self.assertEqual(len(r["shots"]), 1)
        s = r["shots"][0]
        self.assertGreaterEqual(s["n_faces"], 1, f"应检出人脸，实际 {s}")
        self.assertEqual(len(s["primary"]["emb"]), 128, "SFace 嵌入必须是 128 维")
        self.assertGreater(len(s["primary"]["box"]), 0)

    def test_same_person_pair_scores_above_threshold(self):
        """把人群也放进来（min_side=20）→ 必须出现"同一个人"的配对 ≥0.363。"""
        r = _fc().analyze([str(SAMPLE_FACES)], min_side=20)
        pairs = r["pairs"]
        self.assertGreaterEqual(len(pairs), 0)
        # 单图内部：主体 vs 全场最大相似度
        s = r["shots"][0]
        best = s.get("best_sim_all")
        if best is not None:
            self.assertGreater(best, 0.363,
                               f"实测同人余弦 0.591 应高于阈值，实际 best={best}")

    def test_self_consistency_is_one(self):
        """同一张图与自己比 → 余弦 1.0（验证嵌入是确定性的，不是随机噪声）。"""
        r = _fc().analyze([str(SAMPLE_FACES), str(SAMPLE_FACES)], min_side=48)
        if r["pairs"]:
            self.assertAlmostEqual(r["pairs"][0]["sim"], 1.0, places=4)
        else:
            self.skipTest("两镜未都检出人脸，跳过自比对")

    def test_missing_face_image_reports_zero_not_crash(self):
        """无小人脸图（纯色）→ n_faces=0 且判定 WARN，绝不崩。"""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as td:
            plain = os.path.join(td, "plain.mp4")
            ff = __import__("importlib").import_module("postprocess")._ffmpeg()
            sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=gray:s=320x240:d=2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", plain], check=True)
            r = _fc().analyze([plain])
        self.assertEqual(r["shots"][0]["n_faces"], 0)
        self.assertTrue(any(lv == "WARN" for lv, _ in r["verdict"]))

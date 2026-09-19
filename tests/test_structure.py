# -*- coding: utf-8 -*-
"""结构性防线测试——补 AST 校验与 dry-run 都覆盖不到的盲区。

由来（血的教训，2026-09）：
  1. **def 截断事故 ×4**：把模块级函数插进另一个函数体中间，AST 仍然合法，
     但后半段变成死代码 / 另一个函数的身体 → 运行时静默失效。AST 只保语法不保结构。
  2. **子进程入口指错**：mg_batch 从 media_gen 拆出后无 `__main__`，而 worker
     子命令用 `Path(__file__)` 构造 → 真实 batch 每镜静默 exit 0、全队记 OK 但零生成。
     现有测试全是 dry-run，从没抓到。

本文件用"结构快照 + 静态引用检查"把这两类事故变成红灯。
运行：python -m unittest discover tests          （快：结构快照与静态检查）
     SLOW=1 python -m unittest discover tests   （含 CLI --help 冒烟）
"""
import ast
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_slow = not os.environ.get("SLOW")

# 关键模块的**顶层函数清单快照**——结构被人为破坏（函数消失/被合并进别的函数）时红灯。
# 变更流程：确认结构无误（top-level 函数都能在文件里独立找到）→ 更新此表。
_MANIFEST = {
    'mg_core.py': [
        '_abs_url', '_atomic_write_json', '_cooldown_update', '_download', '_download_image',
        '_extract_video_url', '_ffmpeg', '_final_out', '_gen_image_once', '_image_rpm_for',
        '_insert_suffix', '_interleave_by_pool', '_kw_hit', '_load_env_file', '_load_state',
        '_poll_video_task', '_pop_pending_task', '_print_timeout_menu', '_priority_pools',
        '_resolve_async_task', '_save_pending_task', '_uncertain_causes', '_update_state',
        '_wait_existing_task', 'build_image_body', 'build_last_frame_fields',
        'build_poll_url', 'call_with_failover', 'classify_network_error', 'die',
        'fetch_task_state', 'find_existing_product', 'find_product', 'http_call',
        'image_to_uri_shrunk', 'image_to_url_or_path', 'is_transition', 'key_mask',
        'keyframes_supported', 'ledger_append', 'ledger_read', 'ledger_summarize',
        'list_keys', 'list_shot_files', 'load_character_bible', 'merge_character_refs',
        'natkey', 'negative_for_shot',
        'poll_tasks', 'pools_for_role', 'ref_image_candidates', 'ref_image_supported',
        'run_capture', 'scan_tts_slots', 'video_throttle',
    ],
    'mg_batch.py': [
        '_harvest_image', '_harvest_video', 'batch_exit_code', 'budget_error', 'cmd_batch',
        'cmd_harvest', 'estimate_calls', 'make_cmd', 'prompt_lint_snapshot', 'record_skip',
        'run_shot_once', 'skip_kind',
    ],
    'media_gen.py': [
        '_tts_emotion_prefix', '_warn_ignored_params', 'build_video_payload', 'cmd_audit',
        'cmd_clean', 'cmd_edit', 'cmd_envcheck', 'cmd_image', 'cmd_ledger', 'cmd_run',
        'cmd_tts', 'cmd_video', 'main',
    ],
    'pipeline.py': [
        '_run', '_shots', '_sync_frames', 'audit', 'cmd', 'cmd_clean', 'die', 'execute_clean',
        'main', 'plan_clean', 'purge_trash', 'qc_gate_action', 'resolve_pool_order',
        'resolve_watermark', 'resolve_workers',
    ],
    'postprocess.py': [
        '_bhattacharyya', '_collect_clips', '_escape_drawtext', '_frame_stats',
        '_hsv_features', '_webp_frames', 'apply_preset', 'audio_plan', 'audio_src',
        'cmd_check', 'cmd_concat', 'cmd_extract', 'cmd_kenburns', 'cmd_kenburns_all',
        'cmd_pick', 'cmd_qcgate', 'cmd_qcseq', 'cmd_stylegrid', 'cmd_webp2mp4',
        'compare_frames', 'die', 'duration_verdict', 'find_font', 'load_presets', 'main',
        'parse_srt', 'probe', 'qc_times', 'qcseq_decide', 'run', 'score_images',
    ],
    'vo_build.py': [
        '_existing_recording', 'bgm_assembly', 'bgm_filter_chain', 'cmd_fit',
        'cmd_plan', 'die', 'fit_report', 'main', 'plan_axis', 'probe', 'tts_line',
    ],
    'mg_status.py': [
        '_probe_models', 'cmd_last_frame', 'cmd_plan_check', 'cmd_qc', 'cmd_status',
        'fmt_capability_rows',
    ],
    # 侧车脚本（v4.10.1 起纳入）——此前只覆盖 7 个核心脚本，
    # 结构保护对旁线形同虚设（face_consistency 的一次 def 截断就是在这块盲区里发生的）
    'audio_qc.py': [
        'parse_silencedetect', 'parse_volumedetect', 'parse_astats', '_sil_dur',
        'qc_verdict', 'speech_from_silences', 'probs_to_segments', 'speech_summary',
        '_onnx_available', 'vad_backend', 'probe_duration', '_run_silencedetect',
        '_run_volumedetect', '_run_astats', '_read_pcm16k', '_silero_probs',
        'detect_speech', 'analyze', '_badge', '_targets',
        'cmd', 'main',
    ],
    'audio_triage.py': [
        '_scan_tts_slot', 'triage_decision', 'load_profile', 'record_profile',
        '_extract_sample', '_transcribe', '_local_vad', 'triage_one',
        'cmd', 'main',
    ],
    'copy.py': [
        'clean_text', 'chat', 'main',
    ],
    'delogo_watermark.py': [
        'die', 'load_profile', 'parse_size', 'clamp_box',
        'probe_size', 'scale_box', 'main',
    ],
    'envcheck.py': [
        '_result', 'scan_key_env', 'check_key_env', 'check_runtime',
        '_find_font_quiet', '_tcp_ok', 'check_local_services', 'check_caps',
        'run_checks', 'summarize', 'main',
    ],
    'export_public.py': [
        'remove_block', 'non_utf8_marker_error', 'main',
    ],
    'face_consistency.py': [
        'cosine', 'filter_faces', 'pick_primary', 'pairwise_similarity',
        'mean_similarity', 'outliers', 'consistency_verdict', 'badge',
        '_import_cv2', '_cv2_available', 'face_backend', 'install_hint',
        '_frame_for', 'analyze', '_detect_and_embed', '_targets',
        'cmd', 'main',
    ],
    'ffmpeg_probe.py': [
        'find_ffmpeg',
    ],
    'mg_caps.py': [
        'load', '_write', 'record', 'clear',
        'effective', '_measure', '_real_smoke', '_execute_smoke',
        'cap_declared', '_tiny_png', 'inner_poll_timeout', 'cap_cmd',
        'capability_view', '_cap_smoke', '_models_smoke', '_fmt_view',
        'cmd_caps', 'build_parser', '_snapshot_row', 'render_snapshot',
        '_all_rows', '_extract_block', 'caps_sync',
    ],
    'prompt_lint.py': [
        'default_lexicon', 'parse_lexicon_md', '_wc', 'lint_prompt',
        '_significant_tokens', 'lint_shot', 'cmd', 'main',
    ],
}


def _top_level_funcs(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(n.name for n in tree.body if isinstance(n, ast.FunctionDef))


class TestScriptStructure(unittest.TestCase):
    """所有脚本可解析 + 无重复顶层函数名（重复=某函数被覆盖，静默失效）。"""

    def test_all_scripts_parse(self):
        for f in sorted(SCRIPTS.glob("*.py")):
            with self.subTest(script=f.name):
                try:
                    ast.parse(f.read_text(encoding="utf-8"))
                except SyntaxError as e:
                    self.fail(f"{f.name} 语法错误: {e}")

    def test_no_duplicate_top_level_functions(self):
        for f in sorted(SCRIPTS.glob("*.py")):
            names = _top_level_funcs(f)
            dups = sorted({n for n in names if names.count(n) > 1})
            with self.subTest(script=f.name):
                self.assertEqual(dups, [], f"{f.name} 顶层函数重名（后者会覆盖前者）: {dups}")

    def test_top_level_manifest(self):
        """结构快照：顶层函数清单与基线一致。

        破坏方式对照——函数被插进别的函数体中间时，它会从 top-level 清单里**消失**
        （变成嵌套 def），本测试立刻红灯。这是 AST 校验（只看语法）抓不到的那一类。
        """
        for fname, expected in _MANIFEST.items():
            with self.subTest(script=fname):
                got = _top_level_funcs(SCRIPTS / fname)
                missing = sorted(set(expected) - set(got))
                added = sorted(set(got) - set(expected))
                self.assertEqual(
                    (missing, added), ([], []),
                    f"{fname} 顶层函数清单变了。\n"
                    f"  消失（疑似被截断/嵌套进别的函数）: {missing}\n"
                    f"  新增: {added}\n"
                    f"  → 先核对结构无误，再更新 tests/test_structure.py 的 _MANIFEST")

    def test_manifest_covers_every_script(self):
        """快照必须覆盖 scripts/ 下**每一个**脚本。

        否则新加的旁线天然免检——`def` 截断/函数被嵌套这类事故照样能溜过去
        （v4.10.1 起补全：此前 10 个侧车脚本无保护）。
        """
        on_disk = {f.name for f in SCRIPTS.glob("*.py")}
        uncovered = sorted(on_disk - set(_MANIFEST))
        self.assertEqual(
            uncovered, [],
            f"这些脚本没有结构快照（新增脚本需登记到 _MANIFEST）: {uncovered}")
        stale = sorted(set(_MANIFEST) - on_disk)
        self.assertEqual(stale, [], f"_MANIFEST 里有已不存在的脚本: {stale}")


class TestSubprocessEntryTargets(unittest.TestCase):
    """子进程入口引用必须存在且带 `__main__` 守卫（mg_batch 静默空跑事故的防线）。"""

    _PAT = re.compile(r'\.parent\s*/\s*"([A-Za-z_][A-Za-z0-9_]*\.py)"')

    def test_referenced_scripts_exist_and_have_main(self):
        refs: dict[str, set[str]] = {}
        for f in sorted(SCRIPTS.glob("*.py")):
            for m in self._PAT.finditer(f.read_text(encoding="utf-8")):
                refs.setdefault(m.group(1), set()).add(f.name)
        self.assertTrue(refs, "没扫到任何子进程入口引用？检查正则是否失效")
        for target, sources in sorted(refs.items()):
            with self.subTest(target=target):
                tp = SCRIPTS / target
                self.assertTrue(tp.exists(), f"{target} 不存在（被 {sources} 引用）")
                body = tp.read_text(encoding="utf-8")
                self.assertIn(
                    'if __name__', body,
                    f"{target} 被 {sources} 当子进程入口调用，但没有 `__main__` 守卫——"
                    f"子进程会静默 exit 0、什么都不做（历史 P0 事故）")


@unittest.skipUnless(not _slow, 'slow: SLOW=1 启用')
class TestCliHelpSmoke(unittest.TestCase):
    """CLI 入口冒烟：带 argparse 的脚本跑 `--help` 必须正常退出。

    覆盖"import 期就炸/argparse 装配错"这类只在真进程里暴露的问题。
    """

    def test_help_exits_zero(self):
        checked = 0
        for f in sorted(SCRIPTS.glob("*.py")):
            src = f.read_text(encoding="utf-8")
            if "if __name__" not in src or "argparse" not in src:
                continue
            with self.subTest(script=f.name):
                r = subprocess.run([sys.executable, str(f), "--help"],
                                   capture_output=True, text=True, timeout=60,
                                   encoding="utf-8")
                self.assertEqual(r.returncode, 0,
                                 f"{f.name} --help rc={r.returncode}\n{(r.stderr or '')[-400:]}")
                self.assertIn("usage", (r.stdout or "").lower(), f.name)
            checked += 1
        self.assertGreaterEqual(checked, 5, "冒烟覆盖太少，检查过滤条件")




class TestConceptSingleSource(unittest.TestCase):
    """概念必须单源：is_transition / find_product 只能在 mg_core 定义。

    此前过渡镜判定 mg_batch 一份 + pipeline 内联两处、clip 查找两份实现——
    概念散落 = 加能力必漏一处（v4.6 def 截断事故的同源土壤）。"""

    _SHARED = {"is_transition", "find_product"}

    def test_shared_concepts_defined_only_in_core(self):
        hits = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name == "mg_core.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in tree.body:
                if isinstance(n, ast.FunctionDef) and n.name in self._SHARED:
                    hits.append(f"{f.name}:{n.lineno}:{n.name}")
        self.assertEqual(hits, [], "共享概念被本地重定义（改用 mg_core 的）: " + str(hits))

    def test_no_legacy_inline_names(self):
        """旧内联名（_is_transition/_find_clip/_find_dep_clip）不许在任何脚本复活。"""
        banned = {"_is_transition", "_find_clip", "_find_dep_clip"}
        hits = []
        for f in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in tree.body:
                if isinstance(n, ast.FunctionDef) and n.name in banned:
                    hits.append(f"{f.name}:{n.lineno}:{n.name}")
        self.assertEqual(hits, [], "旧名复活（用 mg_core.is_transition / find_product）: " + str(hits))


class TestNoMutableArgparseDefaults(unittest.TestCase):
    """add_argument 的 default=[] / default={} 是可变默认值坑。

    同进程多次 parse（或复用 parser 的场景）列表会跨调用累积——
    v4.7 的 --ref-image 就带过这个雷。AST 扫描防复发。"""

    def test_no_mutable_defaults_in_argparse(self):
        bad = []
        for f in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_argument"):
                    for kw in node.keywords:
                        if kw.arg == "default" and isinstance(kw.value, (ast.List, ast.Dict)):
                            bad.append(f"{f.name}:{node.lineno}")
        self.assertEqual(bad, [], "argparse 可变默认值（改成 default=None，消费侧 `or []`）: " + str(bad))


class TestPollingSingleSource(unittest.TestCase):
    """轮询循环单源（批四 B）：while 循环体内不许出现 urlopen。

    三份手写轮询 while（_resolve_async_task/_poll_video_task/cmd_edit）曾各拼
    URL、各判终态、各设超时——加新池必漏一处。收归 mg_core.poll_tasks 后，
    新增池/新调用方若再手写 while+urlopen 轮询，这里红灯。
    TTS 的重试在 for 内（非轮询）、copy/audio_triage 单次调用——不误伤。"""

    def test_no_urlopen_inside_while_outside_core(self):
        bad = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name == "mg_core.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for wh in [n for n in ast.walk(tree) if isinstance(n, ast.While)]:
                for node in ast.walk(wh):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "urlopen"):
                        bad.append(f"{f.name}:{node.lineno}")
                        break
        self.assertEqual(
            bad, [],
            "while 循环内手写 urlopen 轮询（改用 mg_core.poll_tasks / "
            "fetch_task_state，网络语义留在调用方）: " + str(bad))


class TestNoUnimportedModuleUse(unittest.TestCase):
    """用了模块名却没 import —— 静默炸弹（v4.7.7 P0 的根因防线）。

    `mg_batch.py` 调了 `mg_core.build_poll_url(...)`，但该文件只有
    `from mg_core import (...)`、没有 `import mg_core` → 一跑 harvest 就
    NameError。328 个测试全绿没抓到（那条路径零行为测试）。
    本守卫静态扫描：对每个脚本名 X，若出现 `X.attr` 属性访问却没 `import X` → 红灯。"""

    _MODULES = {p.stem for p in SCRIPTS.glob("*.py")}

    def test_no_module_attr_without_import(self):
        bad = []
        for f in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            imported = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for al in n.names:
                        imported.add((al.asname or al.name).split(".")[0])
            used = set()
            for n in ast.walk(tree):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                        and n.value.id in self._MODULES and n.value.id not in imported):
                    used.add(n.value.id)
            if used:
                bad.append(f"{f.name}: {sorted(used)}")
        self.assertEqual(
            bad, [],
            "用了本仓模块名却没 import（运行时 NameError 静默炸弹）: " + str(bad))


class TestSharedConstantsSingleSource(unittest.TestCase):
    """共享常量/实现单源（v4.7.9 收编）。

    - 产物白名单字面量 `(".mp4", ".webp")` 只许出现在 mg_core（PRODUCT_EXTS）：
      复刻一份 → 加新后缀时漏改一处 → 断点续跑误判缺片 → 重复扣费。
    - env 加载实现只许在 mg_core（copy.py 曾自带副本且正则已漂移：
      `="(.*)"$` 要求行尾收引号，带行内注释的 key 行被静默跳过）。"""

    def test_product_exts_literal_only_in_core(self):
        bad = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name == "mg_core.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, (ast.Tuple, ast.List)) and len(n.elts) == 2:
                    vals = sorted(e.value for e in n.elts
                                  if isinstance(e, ast.Constant) and isinstance(e.value, str))
                    if vals == [".mp4", ".webp"]:
                        bad.append(f"{f.name}:{n.lineno}")
        self.assertEqual(bad, [], "产物后缀白名单被复刻（改用 mg_core.PRODUCT_EXTS）: " + str(bad))

    def test_shared_impls_not_redefined(self):
        """共享实现只许在 mg_core：`_load_env_file`（副本正则已漂移）、`_ffmpeg`（副本缓存已漂移）。"""
        banned = {"_load_env_file", "_ffmpeg"}
        bad = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name == "mg_core.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in tree.body:
                if isinstance(n, ast.FunctionDef) and n.name in banned:
                    bad.append(f"{f.name}:{n.lineno}:{n.name}")
        self.assertEqual(bad, [], "共享实现被本地重定义（改用 mg_core 的）: " + str(bad))


class TestWorkersDefaultNotMaskingPlan(unittest.TestCase):
    """并行数 CLI 的 default 必须是 None（v4.7.9）。

    转发链 `media_gen run → pipeline → batch` 里，恒真的默认值（3）会**覆盖**
    plan.json 的决策（问⑤落盘的 workers_image/video）。手搓 Namespace 的行为
    测试测不到这一层（它测的是转发逻辑），所以用 AST 钉死。"""

    _FLAGS = ("--workers-image", "--workers-video")

    def test_workers_default_is_none(self):
        bad = []
        for name in ("media_gen.py", "pipeline.py"):
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "add_argument"):
                    continue
                if not n.args or not isinstance(n.args[0], ast.Constant):
                    continue
                if n.args[0].value not in self._FLAGS:
                    continue
                for kw in n.keywords:
                    if kw.arg == "default" and not (
                            isinstance(kw.value, ast.Constant) and kw.value.value is None):
                        bad.append(f"{name}:{n.lineno} {n.args[0].value}")
        self.assertEqual(bad, [], "并行数 default 必须 None（否则覆盖 plan.json 决策）: " + str(bad))


class TestOptionalDepsNotTopLevel(unittest.TestCase):
    """可选重依赖（onnxruntime/numpy/torch/cv2…）不得在**模块顶层** import。

    这是"优雅降级"的前提：若顶层 import onnxruntime，则没装它的机器上整个模块
    import 就炸——`vad_backend()` 想回退 ffmpeg 都来不及执行。所有重依赖必须
    写在函数体内按需 import。
    """

    BANNED = {"onnxruntime", "numpy", "torch", "cv2", "scipy", "soundfile",
              "librosa", "transformers", "diffusers"}

    @staticmethod
    def _top_level_imports(path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out = []
        for n in tree.body:                       # 只看顶层语句（不进函数）
            if isinstance(n, ast.Import):
                out += [(a.name.split(".")[0], n.lineno) for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                out.append(((n.module or "").split(".")[0], n.lineno))
        return out

    def test_no_top_level_heavy_import(self):
        bad = []
        for f in sorted(SCRIPTS.glob("*.py")):
            for name, lineno in self._top_level_imports(f):
                if name in self.BANNED:
                    bad.append(f"{f.name}:{lineno} import {name}")
        self.assertEqual(
            bad, [],
            "可选重依赖必须函数内延迟 import（否则缺依赖时模块直接 import 失败，"
            "优雅降级无从谈起）: " + str(bad))

    def test_silero_path_is_really_implemented(self):
        """反空壳：audio_qc 必须真有**模型推理调用**（`InferenceSession`），
        否则 `vad_backend()` 声称支持 silero 却永远是空壳/假概率。

        ⚠️ 判据必须是「有推理调用」，不能只是「有 onnxruntime import」——
        `_onnx_available()` 里也有 import，弱判据会被它满足而对空壳放行
        （此条由受控破坏验证当场发现）。"""
        tree = ast.parse((SCRIPTS / "audio_qc.py").read_text(encoding="utf-8"))
        hit = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for sub in ast.walk(fn):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "InferenceSession"):
                    hit.append(fn.name)
        self.assertTrue(
            hit, "audio_qc 里找不到 InferenceSession 调用 → silero 后端是空壳（假概率）")


class TestShrunkEncoderCoverage(unittest.TestCase):
    """输入图编码必须走 image_to_uri_shrunk（大图直传会读超时，v4.7 实测 960KB→2m23s）。

    裸编码器 image_to_url_or_path 只允许在 mg_core 内部出现（作为 shrunk 的小文件路径）；
    新增池/新增参数时若绕过它，这里红灯。"""

    def test_raw_encoder_not_called_outside_core(self):
        hits = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name == "mg_core.py":
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "image_to_url_or_path"):
                    hits.append(f"{f.name}:{node.lineno}")
        self.assertEqual(hits, [], "绕过压缩编码的输入图调用: " + str(hits))


if __name__ == '__main__':
    unittest.main(verbosity=2)

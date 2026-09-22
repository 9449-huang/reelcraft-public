# -*- coding: utf-8 -*-
"""dialogue.py（剧本/对白层）单元测试——批十四。

纯函数全覆盖 + CLI 壳（check/out 两路）+ 防线（空剧本 die / 坏角色表 die /
未映射角色必须报告，不静默全默认）。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dialogue as dl


class TestParseDialogue(unittest.TestCase):
    """parse_dialogue 纯函数：剧本体 → 结构化。"""

    def test_basic_role_line(self):
        r = dl.parse_dialogue("阿明：今天天气不错。")
        self.assertEqual(len(r["lines"]), 1)
        self.assertEqual(r["lines"][0]["speaker"], "阿明")
        self.assertEqual(r["lines"][0]["text"], "今天天气不错。")
        self.assertFalse(r["warnings"])

    def test_english_colon_fallback(self):
        """半角冒号（英文输入法）也要认——中文剧本常见坑。"""
        r = dl.parse_dialogue("小雨:是啊。")
        self.assertEqual(r["lines"][0], {"speaker": "小雨", "text": "是啊。"})

    def test_emotion_annotation(self):
        """角色名后的（激昂）注记 → 该句 emotion，不混进台词。"""
        r = dl.parse_dialogue("阿明（激昂）：太棒了！")
        ln = r["lines"][0]
        self.assertEqual(ln["speaker"], "阿明")
        self.assertEqual(ln["text"], "太棒了！")
        self.assertEqual(ln["emotion"], "激昂")

    def test_mid_sentence_parens_untouched(self):
        """句**中**括号不是情感注记（"他（终于）醒了"必须原样保留）。"""
        r = dl.parse_dialogue("阿明：他（终于）醒了。")
        self.assertEqual(r["lines"][0]["text"], "他（终于）醒了。")
        self.assertNotIn("emotion", r["lines"][0])

    def test_comment_and_acts(self):
        """# 注释行进 acts（去井号）；纯分隔注释丢弃；[空镜] 行进 acts。"""
        r = dl.parse_dialogue("# 场一：公园\n# ---\n[空镜：公园全景]\n阿明：走吧")
        self.assertEqual(r["acts"], ["场一：公园", "[空镜：公园全景]"])
        self.assertEqual(len(r["lines"]), 1)

    def test_shot_marker(self):
        """行首 [S01] = 该句所属镜（喂 plan 反推镜时长用）。"""
        r = dl.parse_dialogue("[S01] 阿明：走吧\n[S02] 小雨：好。")
        self.assertEqual(r["lines"][0]["shot"], "S01")
        self.assertEqual(r["lines"][1]["shot"], "S02")

    def test_bare_line_is_narration(self):
        """无角色名的行 = 旁白体台词（不丢弃、不警告）。"""
        r = dl.parse_dialogue("夜幕降临，城市安静下来。")
        self.assertEqual(r["lines"][0]["speaker"], "旁白")
        self.assertEqual(r["lines"][0]["text"], "夜幕降临，城市安静下来。")

    def test_empty_colon_role_warns(self):
        """冒号前为空 → 进 warnings（不静默丢行）。"""
        r = dl.parse_dialogue("：台词内容")
        self.assertEqual(r["lines"], [])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("冒号前为空", r["warnings"][0])

    def test_empty_text_is_warning_not_line(self):
        """角色后没台词（"阿明："）→ 解析成空台词行前必须警告还是收？
        台词空 = 这行没内容 → 与冒号前为空同类，收进 warnings。"""
        r = dl.parse_dialogue("阿明：")
        # 现实现：rest 为空 strip 后空串，无 emotion，text=""
        # → 空台词行也该进 warnings（不能产出 text="" 的台词喂 TTS）
        if r["lines"] and not r["lines"][0]["text"]:
            self.fail("空台词不该产出（喂 TTS 会合成空音频）")

    def test_order_preserved(self):
        r = dl.parse_dialogue("甲：一\n乙：二\n甲：三")
        self.assertEqual([l["speaker"] for l in r["lines"]], ["甲", "乙", "甲"])


class TestAssignVoices(unittest.TestCase):
    """assign_voices 纯函数：角色表 → 音色映射。"""

    def test_mapped_and_unmapped(self):
        lines = [{"speaker": "阿明", "text": "一"}, {"speaker": "小雨", "text": "二"}]
        out, unmapped = dl.assign_voices(lines, {"阿明": "tongtong"}, default_voice="")
        self.assertEqual(out[0]["voice"], "tongtong")
        self.assertEqual(unmapped, ["小雨"])
        self.assertNotIn("voice", out[1], "未映射且无默认 → voice 留空")

    def test_default_voice_fills_unmapped(self):
        lines = [{"speaker": "路人", "text": "一"}]
        out, unmapped = dl.assign_voices(lines, {}, default_voice="tongtong")
        self.assertEqual(out[0]["voice"], "tongtong")
        self.assertEqual(unmapped, ["路人"], "用了默认音色也必须报未映射")

    def test_per_line_voice_passthrough(self):
        """角色表命中 → voice 进行；原 lines 不被就地改（纯函数）。"""
        lines = [{"speaker": "甲", "text": "一"}]
        out, _ = dl.assign_voices(lines, {"甲": "vv"})
        self.assertEqual(lines[0].get("voice"), None, "原输入不能被就地改")
        self.assertEqual(out[0]["voice"], "vv")


class TestToVoLines(unittest.TestCase):
    """to_vo_lines：结构化 → vo_build schema。"""

    def test_ids_and_passthrough(self):
        parsed = {"acts": ["场一"], "lines": []}
        lines = [{"speaker": "甲", "text": "一", "voice": "v1", "emotion": "欢快",
                  "shot": "S01"},
                 {"speaker": "乙", "text": "二"}]
        vo = dl.to_vo_lines(parsed, lines)
        self.assertEqual([l["id"] for l in vo["lines"]], ["L01", "L02"])
        self.assertEqual(vo["lines"][0]["voice"], "v1")
        self.assertEqual(vo["lines"][0]["emotion"], "欢快")
        self.assertEqual(vo["lines"][0]["shot"], "S01")
        self.assertNotIn("voice", vo["lines"][1])
        self.assertEqual(vo["acts"], ["场一"])

    def test_schema_feeds_vo_build(self):
        """接缝：产出的 JSON 必须能被 vo_build 的 at 校验/plan 流程接受——
        lines 有 id/text 即可（at 由 plan 生成）。"""
        vo = dl.to_vo_lines({"acts": [], "lines": []},
                            [{"speaker": "甲", "text": "一"}])
        for k in ("id", "text"):
            self.assertIn(k, vo["lines"][0])


class TestCmdShell(unittest.TestCase):
    """CLI 壳：--check / --out / 防线 die。"""

    def _write(self, td, name, content):
        p = Path(td) / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_check_mode_prints_summary(self):
        with tempfile.TemporaryDirectory() as td:
            script = self._write(td, "s.txt", "阿明：走吧\n小雨：好。")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = dl.cmd(_ns(script=str(script), check=True))
            self.assertEqual(rc, 0)
            self.assertIn("2 句台词", buf.getvalue())

    def test_out_writes_json(self):
        with tempfile.TemporaryDirectory() as td:
            script = self._write(td, "s.txt", "阿明：走吧")
            voices = self._write(td, "v.json", json.dumps({"阿明": "tongtong"}))
            out = Path(td) / "vo" / "vo_lines.json"
            with mock.patch.object(sys, "argv",
                                   ["dialogue.py", str(script),
                                    "--voices", str(voices), "--out", str(out)]):
                rc = dl.cmd(_ns(script=str(script), voices=str(voices), out=str(out)))
            self.assertEqual(rc, 0)
            back = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(back["lines"][0]["voice"], "tongtong")
            self.assertEqual(back["lines"][0]["id"], "L01")

    def test_empty_script_dies(self):
        """全注释剧本（无台词）→ die(2)，不是落盘空 lines。"""
        with tempfile.TemporaryDirectory() as td:
            script = self._write(td, "s.txt", "# 只有注释")
            with self.assertRaises(SystemExit) as cm:
                dl.cmd(_ns(script=str(script), check=True))
            self.assertEqual(cm.exception.code, 2)

    def test_bad_voices_json_dies(self):
        with tempfile.TemporaryDirectory() as td:
            script = self._write(td, "s.txt", "阿明：走吧")
            voices = self._write(td, "v.json", "{不是json")
            with self.assertRaises(SystemExit) as cm:
                dl.cmd(_ns(script=str(script), voices=str(voices),
                           out=str(Path(td) / "o.json")))
            self.assertEqual(cm.exception.code, 3)

    def test_voices_not_object_dies(self):
        """角色表是数组而不是对象 → die(3)，不是后面 KeyError 崩。"""
        with tempfile.TemporaryDirectory() as td:
            script = self._write(td, "s.txt", "阿明：走吧")
            voices = self._write(td, "v.json", json.dumps(["a", "b"]))
            with self.assertRaises(SystemExit) as cm:
                dl.cmd(_ns(script=str(script), voices=str(voices),
                           out=str(Path(td) / "o.json")))
            self.assertEqual(cm.exception.code, 3)

    def test_unmapped_reported_on_stderr(self):
        """未映射角色必须 stderr 报告——多角色剧全一个声音是事故（不静默）。"""
        with tempfile.TemporaryDirectory() as td:
            script = self._write(td, "s.txt", "甲：一\n乙：二")
            err = io.StringIO()
            with redirect_stderr(err):
                dl.cmd(_ns(script=str(script), check=True))
            self.assertIn("乙", err.getvalue(), "未映射角色乙必须点名")


def _ns(**kw):
    import argparse
    d = dict(script="", voices="", default_voice="", out="", check=False)
    d.update(kw)
    return argparse.Namespace(**d)


if __name__ == "__main__":
    unittest.main(verbosity=2)

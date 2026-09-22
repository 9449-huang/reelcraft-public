# -*- coding: utf-8 -*-
"""批十一红灯测试：fix_cue_timing（CPS 延长 / 重叠收缩 / unfixable 计数）+ main 接线。"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vo_build as vb  # noqa: E402

_slow = not os.environ.get("SLOW")


def _cue(at, dur, text="x", width_chars=1, words=None):
    """width_chars：按 CJK 计宽的字符数（每个 = 2 宽度单位）。"""
    t = "汉" * width_chars if width_chars > 1 else text
    return {"at": at, "dur": dur, "text": t, "words": words or []}


class TestFixCueTiming(unittest.TestCase):
    """SubtitleEdit 规则三件套：CPS 延长 / 重叠收缩 / 修不动的如实计数。"""

    def test_extends_fast_cue_into_gap(self):
        """40 宽显示 1s = CPS 40 > 20 → 应延长到 40/20=2.0s（后面有空隙）。"""
        cues = [_cue(0.0, 1.0, width_chars=20), _cue(5.0, 1.0, width_chars=2)]
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual(out[0]["dur"], 2.0, "应延长到 width/max_cps=2.0s")
        self.assertEqual(stats["extended"], 1)

    def test_extension_stops_before_next_at(self):
        """空隙不够 CPS 达标 → 只延到 next_at - min_gap，不侵入后条。"""
        cues = [_cue(0.0, 1.0, width_chars=20),   # 需要 2.0s 才达标
                _cue(1.5, 1.0, width_chars=2)]    # 可用到 1.5-0.08=1.42
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual(out[0]["dur"], 1.42)
        self.assertEqual(stats["extended"], 1)
        self.assertLessEqual(out[0]["at"] + out[0]["dur"],
                             out[1]["at"] - vb.MIN_CUE_GAP + 1e-9)

    def test_cps_ok_cue_not_touched(self):
        cues = [_cue(0.0, 2.5, width_chars=20),   # 40宽/2.5s = 16 ≤ 20 达标
                _cue(5.0, 1.0, width_chars=2)]
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual(out[0]["dur"], 2.5, "CPS 达标不应动")
        self.assertEqual(stats["extended"], 0)

    def test_zero_cps_disables_extension(self):
        cues = [_cue(0.0, 1.0, width_chars=20), _cue(5.0, 1.0, width_chars=2)]
        out, stats = vb.fix_cue_timing(cues, max_cps=0.0)
        self.assertEqual(out[0]["dur"], 1.0, "max_cps=0 关闭延长")
        self.assertEqual(stats["extended"], 0)

    def test_shrinks_overlap_caused_by_min_dur_clamp(self):
        """MIN_CUE_DUR 钳制（0.1s 自然长 → 0.3s）侵入下条 → 应缩短让位。"""
        cues = [_cue(0.0, 0.3, width_chars=1), _cue(0.2, 2.0, width_chars=1)]
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual(out[0]["dur"], 0.12, "缩到 next_at-min_gap-at")
        self.assertEqual(stats["shrunk"], 1)
        self.assertLessEqual(out[0]["at"] + out[0]["dur"], 0.2)

    def test_unfixable_overlap_kept_and_counted(self):
        """缩到负值（下条几乎贴脸）→ 不动、如实计数，不静默删也不装修好。"""
        cues = [_cue(0.0, 0.3, width_chars=1), _cue(0.05, 2.0, width_chars=1)]
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual(out[0]["dur"], 0.3, "修不动就保持原样")
        self.assertEqual(stats["unfixable"], 1)

    def test_words_kept_intact(self):
        """延长/收缩只动 dur —— words 时间戳是真实语音，不许被改写。"""
        words = [{"w": "汉", "at": 0.0, "dur": 0.9}]
        expected = [dict(w) for w in words]   # 快照：防入参别名让断言恒真
        # 宽 40 显示 1s = CPS 40 超标 → 会走延长分支（words 才真正面临被改写风险）
        cues = [_cue(0.0, 1.0, width_chars=20, words=words),
                _cue(5.0, 1.0, width_chars=2)]
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual(stats["extended"], 1, "必须真触发延长分支（否则测不到改写）")
        self.assertEqual(out[0]["words"], expected, "words 必须原样保留")

    def test_order_independent(self):
        """乱序输入先排序再修（内部排序，不要求调用方预排）。"""
        cues = [_cue(5.0, 1.0, width_chars=2), _cue(0.0, 1.0, width_chars=20)]
        out, stats = vb.fix_cue_timing(cues)
        self.assertEqual([c["at"] for c in out], [0.0, 5.0])
        self.assertEqual(out[0]["dur"], 2.0)


class TestCueCliDefaults(unittest.TestCase):
    """CLI 参数注册（--max-cps / --cue-gap），默认值锁死。"""

    def test_args_registered_with_defaults(self):
        src = Path(vb.__file__).read_text(encoding="utf-8")
        self.assertIn('"--max-cps"', src)
        self.assertIn('"--cue-gap"', src)
        # 默认值必须引用模块常量（改常量一处生效），不许硬编码数字漂移
        m = re.search(r'add_argument\("--max-cps"[^\n]*default=(\w+)', src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "MAX_CUE_CPS")
        m2 = re.search(r'add_argument\("--cue-gap"[^\n]*default=(\w+)', src)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group(1), "MIN_CUE_GAP")


@unittest.skipUnless(not _slow, "slow: SLOW=1 启用")
class TestCueTimingEndToEnd(unittest.TestCase):
    """端到端：main 的字幕输出真的应用了 fix（subs.json 里的时长被修过）。"""

    def test_subs_json_gap_enforced(self):
        import json
        import subprocess as sp
        import tempfile
        from ffmpeg_probe import find_ffmpeg
        td = Path(tempfile.mkdtemp())
        (td / "lines").mkdir()
        ff = find_ffmpeg()
        # 分句录音必须有（--skip-tts 直读）——3s 正弦，两句重叠排轴 0.25s 间距
        for lid in ("L01", "L02"):
            sp.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration=3", "-ar", "48000",
                    str(td / "lines" / f"{lid}.m4a")], check=True, timeout=120)
        # 无词轴 → 走旧路径；但手工构造重叠：两行 at 间距 0.25，行时长 3s 必然重叠
        data = {"acts": [],
                "lines": [{"id": "L01", "text": "甲" * 30, "at": 0.0},
                          {"id": "L02", "text": "乙" * 30, "at": 0.25}]}
        (td / "vo_lines_at.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = td / "vo.m4a"
        r = sp.run([sys.executable, str(Path(vb.__file__).resolve()),
                    str(td / "vo_lines_at.json"), "--out", str(out),
                    "--skip-tts", "--subs-out", str(td / "subs.json")],
                   capture_output=True, text=True, encoding="utf-8", timeout=240)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        subs = json.loads((td / "subs.json").read_text(encoding="utf-8"))
        rows = sorted([s for s in subs if s.get("pos") == "bottom"], key=lambda c: c["at"])
        self.assertEqual(len(rows), 2, "两行各一条 cue")
        for a, b in zip(rows, rows[1:]):
            self.assertLessEqual(a["at"] + a["dur"], b["at"] + 1e-6,
                                 f"相邻字幕不得重叠：{a['at']}+{a['dur']} vs {b['at']}")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""vo_build.py — 旁白（VO）构建：分句 TTS/录音 → 精确拼接 → 精确字幕时间轴。

解决的问题：整段 TTS 时长不可控，字幕只能靠估算，结果"字幕和朗读对不上"。
本脚本按**分句**处理——每句独立合成、读回真实时长、用 adelay 精确落位，
因此输出的 subtitles_final.json 与声音 100% 对齐。

用法：
  # ① 有 TTS key：分句合成 + 拼接 + 打轴
  python vo_build.py vo/vo_lines.json --out vo/vo.mp3 --total 55.94

  # ② 用户自录：把 11 条录音放 vo/lines/L01.wav ... 用 --skip-tts 只拼接打轴
  python vo_build.py vo/vo_lines.json --out vo/vo.mp3 --total 55.94 --skip-tts

  # ③ 某句太长挤到下一句：--auto-shift 自动顺延后续句子
  python vo_build.py vo/vo_lines.json --out vo/vo.mp3 --total 55.94 --auto-shift

输出：
  vo/vo.mp3              拼接好的完整旁白（与成片等长，不足补静音）
  vo/subtitles_final.json  精确字幕轴（可直接喂 postprocess --subtitles）
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mg_core import PRODUCT_EXTS, _ffmpeg   # 产物白名单 + ffmpeg 路径双单源（v4.7.9/v4.8.0）
MEDIA_GEN = str(Path(__file__).resolve().parent / "media_gen.py")

# ─── 字幕切行（v4.13 词级接线）──────────────────────────────
# 有词轴时按**词边界**断行；无词轴时整句一条（旧行为）。
# 宽度按东亚排版惯例计：CJK 全角算 2、其余算 1——只数字符会让中英字幕总有一边过长。
MAX_CUE_WIDTH = 40           # 单条字幕最大显示宽度（CJK 计 2 → 约 20 汉字）
MAX_CUE_DUR = 6.0            # 单条字幕最长秒数
MIN_CUE_DUR = 0.30           # 单条字幕最短秒数（更短会被 drawtext 一闪而过）
MAX_CUE_CPS = 20.0           # 单条字幕最大阅读速度（显示宽度/秒；CJK 计 2 → 约 10 汉字/秒）
MIN_CUE_GAP = 0.08           # 相邻 cue 最小间隔秒（≈2 帧 @25fps，SubtitleEdit 惯例）

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


def _is_cjk_char(ch: str) -> bool:
    return bool(ch) and bool(_CJK_RE.match(ch))


def _display_width(text: str) -> int:
    """字幕显示宽度：CJK 全角计 2、其余计 1。"""
    return sum(2 if _is_cjk_char(c) else 1 for c in text)


def _words_text(ws: list) -> str:
    """词列表 → 字幕文本。CJK 直接相连；拉丁/数字之间补空格（否则 Andsomy）。"""
    out = ""
    for w in ws:
        t = str(w.get("w") or "").strip()
        if not t:
            continue
        if out and not _is_cjk_char(out[-1]) and not _is_cjk_char(t[0]):
            out += " "
        out += t
    return out


def _abs_words(words, dur: float) -> tuple:
    """句内词轴 → 规范化词轴 + 越界计数（越界词夹回 [0, dur]，**且必须被计数**）。

    whisper 的锚点插值可能吐出略超句长的末词；静默夹掉会让"词跑到句外"无人知晓。
    """
    out: list = []
    clamped = 0
    for w in words or []:
        if not isinstance(w, dict) or not str(w.get("w") or "").strip():
            continue
        try:
            at = float(w.get("at") or 0.0)
            d = float(w.get("dur") or 0.0)
        except (TypeError, ValueError):
            continue
        if at < 0.0 or at > dur:
            at = max(0.0, min(at, dur))
            clamped += 1
        d = max(0.02, min(d, dur - at)) if dur > 0 else max(0.02, d)
        out.append({"w": str(w["w"]).strip(), "at": round(at, 3), "dur": round(d, 3)})
    return out, clamped


_PUNCT_RE = re.compile(r"[\s，。！？、,.!?;:；：\"'“”‘’()（）\-—…]+")


def _map_words_to_text(text: str, words: list) -> list | None:
    """把词顺序映射回原文的字符区间（保标点）。有一个词对不上就返回 None。

    词轴来自**录音转写**、line.text 来自**用户输入**：转写会丢标点，甚至用词不同。
    能顺序对上时用它从原文取字幕文本（标点回来）；对不上则回退词拼接（时间仍准）。
    """
    if not text:
        return None
    hay, hay_low = text, text.casefold()
    pos = 0
    spans: list = []
    for w in words:
        needle = _PUNCT_RE.sub("", str(w.get("w") or ""))
        if not needle:
            spans.append(None)
            continue
        idx = hay.find(needle, pos)
        if idx < 0:
            idx = hay_low.find(needle.casefold(), pos)
        if idx < 0:
            return None
        spans.append((idx, idx + len(needle)))
        pos = idx + len(needle)
    return spans


def _cue_text(text: str, spans, groups: list, gi: int, gw: list) -> str:
    """cue 文本：能定位到原文就用原文切段（含标点），否则用词拼接。"""
    if spans:
        valid = [j for j in groups[gi] if spans[j]]
        if valid:
            start = spans[valid[0]][0]
            end = len(text)
            for k in range(gi + 1, len(groups)):
                nxt = [j for j in groups[k] if spans[j]]
                if nxt:
                    end = spans[nxt[0]][0]        # 到下一片首词的起点——中间的标点归本片
                    break
            out = text[start:end].strip()
            if out:
                return out
    return _words_text(gw)


def split_line_cues(line: dict, *, max_width: int = MAX_CUE_WIDTH,
                    max_dur: float = MAX_CUE_DUR,
                    min_dur: float = MIN_CUE_DUR) -> list:
    """一行（句）→ 若干条字幕 cue（纯函数可单测）。

    **有词轴**：只在词边界断行，单条不超过 max_width / max_dur；尾片短于 min_dur
    则并回前一条（避免一闪而过）。文本优先取**原文切片**（标点不丢），对不上原文时
    回退词拼接。每条 cue 带自己的 `words`（绝对时间）供逐词高亮。
    **无词轴**：整句一条，字段与旧版逐字一致（向后兼容）。
    """
    base = float(line.get("at") or 0.0)
    span = max(0.0, float(line.get("dur") or 0.0))
    text = str(line.get("text") or "")
    words = [w for w in (line.get("words") or [])
             if isinstance(w, dict) and str(w.get("w") or "").strip()]
    if not words:
        return [{"at": round(base, 3), "dur": round(span, 3), "text": text}]

    groups: list = []                      # 存词索引，便于回查原文位置
    cur: list = []
    for i in range(len(words)):
        cand = cur + [i]
        st = float(words[cand[0]].get("at") or 0.0)
        en = (float(words[cand[-1]].get("at") or 0.0)
              + float(words[cand[-1]].get("dur") or 0.0))
        too_wide = _display_width(_words_text([words[j] for j in cand])) > max_width
        too_long = max_dur > 0 and (en - st) > max_dur
        if cur and (too_wide or too_long):
            groups.append(cur)
            cur = [i]
        else:
            cur = cand
    if cur:
        groups.append(cur)

    if min_dur > 0 and len(groups) >= 2:
        tail = [words[j] for j in groups[-1]]
        tail_dur = (float(tail[-1].get("at") or 0.0)
                    + float(tail[-1].get("dur") or 0.0)
                    - float(tail[0].get("at") or 0.0))
        if tail_dur < min_dur:
            groups[-2].extend(groups.pop())

    spans = _map_words_to_text(text, words)
    cues: list = []
    for gi, g in enumerate(groups):
        gw = [words[j] for j in g]
        at = float(gw[0].get("at") or 0.0)
        end = float(gw[-1].get("at") or 0.0) + float(gw[-1].get("dur") or 0.0)
        cues.append({"at": round(at, 3),
                     "dur": round(max(end - at, MIN_CUE_DUR), 3),
                     "text": _cue_text(text, spans, groups, gi, gw),
                     "words": [{"w": str(x.get("w") or "").strip(),
                                "at": round(float(x.get("at") or 0.0), 3),
                                "dur": round(float(x.get("dur") or 0.0), 3)} for x in gw]})
    return cues


def fix_cue_timing(cues: list, *, max_cps: float = MAX_CUE_CPS,
                   min_gap: float = MIN_CUE_GAP) -> tuple:
    """字幕时长整形（纯函数；SubtitleEdit 的 CPS/最小间隔规则，2026-09 实测定稿）。

    - **CPS 超标 → 延长**：说话速度改不了，拆行也不降 CPS（总时长不变）；能做的是
      把行尾 cue 的显示时间**延长到后面空隙里**（至 next_at-min_gap，至多 width/max_cps）。
    - **重叠 → 收缩**：MIN_CUE_DUR 钳制可能让短 cue 侵入下一条（下条锚定语音不能推）
      → 缩短前条；若缩无可缩（间隙为负）则**保持原样并计数**——不静默删、不假装修好。
    - words 一律不动（时间戳是真实语音）；输入顺序不限（内部先按 at 排）。
    返回 (排序后的 cues, {extended, shrunk, unfixable})。
    """
    rows = sorted(cues, key=lambda c: (float(c.get("at") or 0.0),
                                       float(c.get("at") or 0.0) + float(c.get("dur") or 0.0)))
    stats = {"extended": 0, "shrunk": 0, "unfixable": 0}
    for i, c in enumerate(rows):
        at = float(c.get("at") or 0.0)
        dur = float(c.get("dur") or 0.0)
        if dur <= 0:
            continue
        end = at + dur
        limit = None
        if i + 1 < len(rows):
            nxt = float(rows[i + 1].get("at") or 0.0)
            limit = nxt - min_gap
        width = _display_width(str(c.get("text") or ""))
        # ① CPS 超标且有后方空隙 → 延长
        if max_cps > 0 and width > 0 and (limit is None or limit > end):
            want = width / max_cps
            cap = limit if limit is not None else at + want
            new_dur = min(want, cap) - at
            if new_dur > dur + 1e-9:
                c["dur"] = round(new_dur, 3)
                stats["extended"] += 1
                end = at + c["dur"]
        # ② 与后条重叠 → 收缩（下条锚定语音不能推）
        if limit is not None and end > limit + 1e-9:
            room = limit - at
            if room >= 0.05:                       # 缩了至少还看得见
                c["dur"] = round(room, 3)
                stats["shrunk"] += 1
            else:
                stats["unfixable"] += 1            # 修不动：如实计数，不假装
    return rows, stats


def die(msg: str, code: int = 1) -> None:
    print(f"[vo_build] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def probe(path: str | Path) -> float:
    """返回音频/视频时长秒。"""
    out = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def tts_line(text: str, out: Path, voice: str, speed: float, emotion: str = "") -> None:
    cmd = [sys.executable, MEDIA_GEN, "tts", "--text", text, "--out", str(out)]
    if voice:
        cmd += ["--voice", voice]
    if emotion:
        cmd += ["--emotion", emotion]
    if speed and speed != 1.0:
        cmd += ["--speed", str(speed)]
    rc = subprocess.call(cmd)
    if rc != 0:
        die(f"TTS 失败：{out.name}（rc={rc}）。若已自录，加 --skip-tts 跳过合成", 3)


# ---------- fit：VO 轴 vs 镜头时长对账（A1'，纯函数可单测） ----------
def fit_report(vo_data: dict, clip_durs: dict, gap: float = 0.3,
               tol: float = 0.5) -> list:
    """每镜对账：VO 需求时长（末句 at+dur+gap - 首句 at）vs clip 实际时长。
    verdict：ok（差 ≤ tol）/ warn（VO 比镜长）/ info（镜无 VO，空镜合法）/ missing（镜缺成片）。
    clip_durs 值为 -1.0 表示静态图片镜（.webp 探不出时长，时长由 concat/kenburns 决定）——
    报 info 不报 missing，VO 需求照常算出来给参考。
    句长未知（无 _dur，未合成）时按 0 计——对账应在 TTS 后跑才精确，但提前跑也能查结构。"""
    lines = [ln for ln in vo_data.get("lines", []) if ln.get("shot")]
    # 每镜 VO 段：首句 at → 末句 at+dur+gap
    spans: dict = {}
    for ln in lines:
        s = ln["shot"]
        end = ln.get("at", 0) + ln.get("_dur", 0) + gap
        if s not in spans:
            spans[s] = [ln.get("at", 0), end, 1]
        else:
            spans[s][1] = max(spans[s][1], end)
            spans[s][2] += 1
    all_shots = sorted(set(spans) | set(clip_durs), key=str)
    rows = []
    for s in all_shots:
        has_vo, has_clip = s in spans, s in clip_durs
        if not has_clip:
            rows.append({"shot": s, "n_lines": spans.get(s, [0, 0, 0])[2],
                         "vo": round(spans[s][1] - spans[s][0], 2) if has_vo else 0.0,
                         "clip": None, "diff": None, "verdict": "missing",
                         "advice": "成片缺失——先 batch/harvest 补齐"})
            continue
        clip = float(clip_durs[s])
        if clip < 0:
            # 静态图片镜：probe 不到时长（哨兵 -1.0），不是缺成片
            vo_need = round(spans[s][1] - spans[s][0], 2) if has_vo else 0.0
            rows.append({"shot": s, "n_lines": spans[s][2] if has_vo else 0,
                         "vo": vo_need, "clip": None, "diff": None,
                         "verdict": "info",
                         "advice": "静态图片镜（webp），时长由 concat/kenburns 决定——"
                                   "kenburns 生成时按此 VO 需求配 --duration"})
            continue
        if not has_vo:
            rows.append({"shot": s, "n_lines": 0, "vo": 0.0, "clip": round(clip, 2),
                         "diff": None, "verdict": "info",
                         "advice": "此镜无旁白（空镜/纯音乐合法），无需对齐"})
            continue
        vo_need = spans[s][1] - spans[s][0]
        diff = round(clip - vo_need, 2)
        if diff >= -tol:
            verdict, advice = "ok", ("" if abs(diff) <= tol else
                                      f"镜比 VO 长 {diff}s——可留给转场呼吸，或 trim 镜")
        else:
            verdict = "warn"
            advice = (f"VO 比镜长 {-diff:.2f}s——建议：镜延长（hybrid 用 kenburns 补长）"
                      f"或该镜 VO speed≈{min(1.4, 1 + (-diff) / max(vo_need, 0.1)):.2f} 提速")
        rows.append({"shot": s, "n_lines": spans[s][2], "vo": round(vo_need, 2),
                     "clip": round(clip, 2), "diff": diff, "verdict": verdict,
                     "advice": advice})
    return rows


# ---------- plan：VO 先行反推每镜时长（#6，与 fit_report 互为镜像） ----------
def plan_axis(items: list, gap: float = 0.5, pad: float = 0.8) -> dict:
    """声音链反向：句子按序自动排轴（at = 上一句 at+dur+gap），按 shot 聚合出
    每镜需求时长（末句 at+dur − 首句 at + pad）。无 shot 的句子照常排轴但不进镜聚合。
    items: [{"id","text","dur"(TTS 真实秒),"shot"?,"words"?}]
      · words（可选）= 词级时间轴，**相对句首**（word_axis 的输出），
        排轴时转成成片绝对时间后挂在 line 上；越界词夹回句内并计入 word_stats
    返回 {"lines":[...每句带 at，有词轴则带 words...],
          "shots":{sid:{"need","n_lines","first_at","last_end"}},
          "total": 末句收尾+pad, "word_stats":{"lines","words","clamped"}}"""
    lines = []
    at = 0.0
    stats = {"lines": 0, "words": 0, "clamped": 0}
    for it in items:
        dur = float(it.get("dur", 0))
        rec = {"id": it.get("id", "?"), "text": it.get("text", ""),
               "at": round(at, 2), "dur": round(dur, 2)}
        if it.get("shot"):
            rec["shot"] = it["shot"]
        ws, n_clamped = _abs_words(it.get("words"), dur)
        if ws:
            shift = rec["at"]                   # 句内相对时间 → 成片绝对时间
            for w in ws:
                w["at"] = round(shift + w["at"], 3)
            rec["words"] = ws
            stats["lines"] += 1
            stats["words"] += len(ws)
            stats["clamped"] += n_clamped
        lines.append(rec)
        at += dur + gap
    spans: dict = {}
    for rec in lines:
        s = rec.get("shot")
        if not s:
            continue
        end = rec["at"] + rec["dur"]
        if s not in spans:
            spans[s] = [rec["at"], end, 1]
        else:
            spans[s][1] = max(spans[s][1], end)
            spans[s][2] += 1
    shots = {s: {"need": round(spans[s][1] - spans[s][0] + pad, 2),
                 "n_lines": spans[s][2],
                 "first_at": spans[s][0], "last_end": round(spans[s][1], 2)}
             for s in spans}
    total = round((lines[-1]["at"] + lines[-1]["dur"] + pad) if lines else pad, 2)
    return {"lines": lines, "shots": shots, "total": total, "word_stats": stats}


def bgm_filter_chain(total: float, duck_db: int = -14) -> str:
    """BGM 侧准备链：循环补齐到成片长 + 全程基础音量。
    duck_db 是 BGM 的**全程音量**（VO 出现时 sidechaincompress 在此基础上进一步压低），
    不是"仅非 VO 时段"的音量——BGM 任何时刻都不该盖过人声。"""
    return (f"aloop=loop=-1:size=2e+09,atrim=0:{total:.3f},"
            f"aresample=48000,volume={duck_db}dB[bgm]")


def bgm_assembly(labels: list, bgm_idx: int, total: float, duck: int = -14) -> list:
    """BGM 闪避混音的 fc 片段（纯函数可单测）：
    ① BGM 输入 → 循环裁齐压基准音量 → [bgmprep]
    ② VO 总线（静音底+各句 amix）asplit 两路：出片 [voout] + 闪避 key [vokey]
    ③ [bgmprep] 主输入 + [vokey] 触发 → sidechaincompress → [ducked]
       （**顺序不能反**：主输入是被压缩方——BGM 被压，VO 只当触发器）
    ④ [voout]+[ducked] amix → [mix]——VO 必须在成片里，只输出 BGM 是 bug
    """
    return [
        f"[{bgm_idx}:a]" + bgm_filter_chain(total, duck).replace("[bgm]", "[bgmprep]"),
        "".join(labels) +
        f"amix=inputs={len(labels)}:normalize=0:duration=first[voax0]",
        "[voax0]asplit=2[voout][vokey]",
        "[bgmprep][vokey]sidechaincompress=threshold=0.03:ratio=8:"
        "attack=200:release=1000[ducked]",
        f"[voout][ducked]amix=inputs=2:normalize=0:duration=first[mix]",
    ]


def _existing_recording(lines_dir: Path, lid: str) -> Path | None:
    """找已有录音（.mp3/.m4a/.wav 任一），没有返回 None。"""
    for ext in (".mp3", ".m4a", ".wav"):
        cand = lines_dir / f"{lid}{ext}"
        if cand.exists():
            return cand
    return None


def cmd_plan(args) -> None:
    """#6 声音链反向：VO 先行 → TTS/录音取真实时长 → 自动排轴 → 反推每镜该多长。
    与 fit 互为镜像：fit 是"镜定时长 → 对账 VO"，plan 是"VO 定时长 → 生成镜时长计划"。
    plan 输出的 vo_lines_at.json 可直接喂正向 vo_build（--out 合成成片）。

    --words（v4.13/v4.16）：对每句录音跑**词级时间轴**（word_axis；中文自动走
    sherpa paraformer 逐字、whisper 兜底英文），
    词 at 相对句首；排轴时转成成片绝对时间写进 vo_lines_at.json → 字幕按词边界切行。
    显式要词轴而运行时不具备（缺模型/依赖）→ die(2) 且给安装指引，**绝不静默降级**。

    --punct（v4.19）：标点恢复——隐含 --words（标点要挂回逐字词目，无词轴就没有挂点）。
    只有 sherpa 后端支持；whisper 后端会**静默忽略** punctuate 参数，因此
    rep["punct"] 为空时必须 die(2)——"要了标点却没拿到"是最典型的静默失败。
    """
    src = Path(args.lines)
    if not src.exists():
        die(f"找不到 {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    if not lines:
        die("vo_lines.json 的 lines 为空")

    lines_dir = Path(args.dir) if args.dir else src.parent / "lines"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # ① 逐句取真实时长：已有录音直接 probe，否则 TTS 合成
    items = []
    for ln in lines:
        lid = ln["id"]
        f = _existing_recording(lines_dir, lid)
        if f is None:
            if args.skip_tts:
                die(f"--skip-tts 但缺少录音 {lines_dir}/{lid}.mp3|m4a|wav", 2)
            voice = ln.get("voice") or args.voice
            tts_line(ln["text"], lines_dir / f"{lid}.mp3", voice,
                     float(ln.get("speed") or args.speed), ln.get("emotion", ""))
            f = lines_dir / f"{lid}.mp3"
        d = probe(f)
        if d <= 0:
            die(f"{f.name} 时长为 0，文件可能损坏", 4)
        items.append({"id": lid, "text": ln["text"], "dur": d, "shot": ln.get("shot", "")})
        print(f"  {lid} {d:.2f}s  {ln['text']}")

    # ② 词级时间轴（可选增强）：词 at 相对句首，交给 plan_axis 转绝对时间
    want_punct = bool(getattr(args, "punct", False))  # --punct 隐含 --words：标点挂回词目
    if getattr(args, "words", False) or want_punct:
        import word_axis                    # 可选依赖 → 函数内 import（有 AST 守卫）
        wmd = getattr(args, "word_models_dir", "") or None
        wlang = getattr(args, "word_lang", "")
        n_words = 0
        for it in items:
            rec = _existing_recording(lines_dir, it["id"])
            if rec is None:
                continue                    # 上面已 die，这里只做防御
            kw = {"models_dir": wmd,
                  "backend": getattr(args, "word_backend", "") or "auto"}
            if wlang:
                kw["language"] = wlang
            if want_punct:
                kw["punctuate"] = True
            try:
                rep = word_axis.transcribe(rec, **kw)
            except RuntimeError as e:
                die(str(e), 2)              # 显式要词轴却跑不了 → 明说，绝不降级
            if rep.get("error"):
                # auto 但两个后端都不可用 → transcribe 不抛、只带回 error，
                # 这里必须把安装指引递出去（否则只剩"一句词轴都没拿到"，排查抓瞎）
                die(f"词轴后端不可用：{rep['error']}", 2)
            if want_punct and not rep.get("punct"):
                # whisper 路径**静默忽略** punctuate（无标点能力）——必须在这里
                # 拦下，否则“要了标点却拿到无标点词轴”无人知晓（静默失败主形态）。
                die(f"--punct 未生效：后端 {rep.get('backend') or '?'} 不支持标点恢复"
                    "（只有 sherpa 后端支持，ct-transformer）。中文请 --word-backend sherpa"
                    "并确认 ~/.workbuddy/models/sherpa-punct/model.onnx 已就位", 2)
            ws = list(rep.get("words") or [])
            if ws:
                it["words"] = ws
                n_words += len(ws)
            pn = "，标点✓" if rep.get("punct") else ""
            print(f"  {it['id']} 词轴 {len(ws)} 词（{rep.get('backend') or '无'}{pn}）")
        if not n_words:
            die("--words 但一句词轴都没拿到（转写为空？录音是纯音乐/静音？）", 2)

    # ③ 排轴 + 反推每镜需求
    res = plan_axis(items, gap=args.gap, pad=args.pad)

    # ④ 落盘：带 at 的 vo_lines（喂正向 vo_build）+ 计划表（喂 kenburns/出片）
    at_path = out.parent / "vo_lines_at.json"
    at_path.write_text(json.dumps(
        {"acts": data.get("acts", []),
         "lines": [{"id": r["id"], "text": r["text"], "at": r["at"],
                    "shot": r.get("shot", ""),
                    **({"words": r["words"]} if r.get("words") else {})}
                   for r in res["lines"]]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'句':6s} {'起':6s} {'时长':6s} {'镜':6s}")
    for r in res["lines"]:
        print(f"{r['id']:6s} {r['at']:5.2f}s {r['dur']:5.2f}s {r.get('shot','—'):6s}")
    print(f"\n每镜需求时长（VO 长度 + pad {args.pad}s 呼吸）：")
    for sid, b in sorted(res["shots"].items()):
        print(f"  {sid}  {b['need']:.2f}s  （{b['n_lines']} 句）")
    dur_csv = ",".join(
        f"{b['need']:.2f}" for _, b in sorted(res["shots"].items()))
    print(f"\n[plan] 成片参考总长 {res['total']:.2f}s（末句收尾 + pad）")
    ws_stat = res.get("word_stats") or {}
    if ws_stat.get("lines"):
        extra = f"；越界夹回 {ws_stat['clamped']} 个" if ws_stat.get("clamped") else ""
        print(f"[plan] 词级时间轴：{ws_stat['lines']} 句 / {ws_stat['words']} 词{extra}"
              f"（字幕将按词边界切行）")
    print(f"[plan] 全缓推可这样跑（按镜序）：")
    print(f"  python postprocess.py kenburns-all shots/ --outdir clips/ --duration \"{dur_csv}\"")
    print(f"[plan] -> {at_path.name}（喂 vo_build 合成成片）+ {out.name}（计划表）")
    sys.exit(0)


def cmd_fit(args) -> None:
    """fit 子命令：读 vo_lines.json + clips/ 目录逐镜 probe，打印对账表。"""
    data = json.loads(Path(args.lines).read_text(encoding="utf-8"))
    clips_dir = Path(args.clips)
    if not clips_dir.is_dir():
        die(f"clips 目录不存在：{clips_dir}", 2)
    clip_durs = {}
    for ext in PRODUCT_EXTS:      # 单源（v4.7.9）：加产物后缀时不会漏掉这里
        for f in clips_dir.glob(f"clip_*{ext}"):
            sid = f.stem.replace("clip_", "")
            d = probe(f)
            # 静态 webp 探不出时长（probe=0）→ 哨兵 -1.0，fit_report 里报 info
            # （图片镜不缺成片，时长由 concat/kenburns 决定，不能误报 missing）
            clip_durs[sid] = d if d > 0 else -1.0
    rows = fit_report(data, clip_durs, gap=args.gap)
    badge = {"ok": "✓", "warn": "⚠", "info": "·", "missing": "✗"}
    n_warn = sum(1 for r in rows if r["verdict"] == "warn")
    n_miss = sum(1 for r in rows if r["verdict"] == "missing")
    print(f"{'镜':6s} {'VO句':4s} {'VO需求':7s} {'镜长':7s} {'差':7s} 判定  建议")
    for r in rows:
        clip_s = f"{r['clip']:.2f}" if r["clip"] is not None else "—"
        diff_s = f"{r['diff']:+.2f}" if r["diff"] is not None else "—"
        print(f"{r['shot']:6s} {r['n_lines']:<4d} {r['vo']:>6.2f}s {clip_s:>7s} {diff_s:>7s} "
              f"{badge[r['verdict']]} {r['advice']}")
    print(f"\n[fit] {len(rows)} 镜：warn {n_warn} / missing {n_miss}"
          + ("——warn 镜先处理再进 concat" if n_warn else "，声画时长对齐 ✓"))
    sys.exit(0)


def main() -> None:
    # 双模式：首参 fit/plan → 子命令；否则走原 VO 构建流程（向后兼容）
    if len(sys.argv) > 1 and sys.argv[1] == "fit":
        ap = argparse.ArgumentParser(prog="vo_build.py fit",
                                     description="VO 时间轴 vs 镜头时长对账（TTS 后跑最精确）")
        ap.add_argument("lines", help="vo_lines.json")
        ap.add_argument("clips", help="clips/ 目录（clip_*.mp4/webp）")
        ap.add_argument("--gap", type=float, default=0.3)
        cmd_fit(ap.parse_args(sys.argv[2:]))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "plan":
        ap = argparse.ArgumentParser(
            prog="vo_build.py plan",
            description="声音链反向：VO 先行，按 TTS 真实时长自动排轴并反推每镜该多长")
        ap.add_argument("lines", help="vo_lines.json（可不含 at——由本命令自动排）")
        ap.add_argument("--out", required=True, help="计划表输出路径（如 vo/plan.json）")
        ap.add_argument("--dir", default="", help="分句音频目录（默认 vo_lines 同级的 lines/）")
        ap.add_argument("--gap", type=float, default=0.5, help="句间间隔秒（默认 0.5）")
        ap.add_argument("--pad", type=float, default=0.8, help="每镜尾部呼吸秒（默认 0.8）")
        ap.add_argument("--skip-tts", action="store_true", help="不合成，用已有录音")
        ap.add_argument("--voice", default="", help="音色名")
        ap.add_argument("--speed", type=float, default=1.0, help="语速倍率")
        ap.add_argument("--words", action="store_true",
                        help="对每句录音跑词级时间轴（v4.16：中文走 sherpa paraformer "
                             "逐字 ~/.workbuddy/models/sherpa-paraformer-zh；whisper 兜底英文）"
                             "→ 字幕按词边界切行 + ASS 逐词高亮；缺模型/依赖会明确报错")
        ap.add_argument("--punct", action="store_true",
                        help="标点恢复（v4.19：sherpa ct-transformer 把标点挂回逐字词目，"
                             "隐含 --words；仅 sherpa 后端支持——whisper 后端会明确报错，"
                             "不静默给无标点轴）")
        ap.add_argument("--word-lang", default="", help="词轴语言（en/zh…；留空用默认）")
        ap.add_argument("--word-backend", default="", choices=["", "auto", "whisper", "sherpa"],
                        help="词轴后端（留空=auto：中文优先 sherpa 逐字、whisper 兜底英文）")
        ap.add_argument("--word-models-dir", default="",
                        help="词轴模型目录（默认 ~/.workbuddy/models）")
        cmd_plan(ap.parse_args(sys.argv[2:]))
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("lines", help="vo_lines.json（含 acts 与 lines）")
    ap.add_argument("--out", required=True, help="输出旁白音频 vo.mp3")
    ap.add_argument("--total", type=float, default=0.0,
                    help="目标总长（=成片时长）；0=按最后一句+2s 自动")
    ap.add_argument("--dir", default="", help="分句音频目录（默认 输出文件同级的 lines/）")
    ap.add_argument("--voice", default="", help="音色名（留空用 media_gen 默认）")
    ap.add_argument("--speed", type=float, default=1.0, help="语速倍率（服务商支持时生效）")
    ap.add_argument("--skip-tts", action="store_true", help="不合成，直接用已有录音")
    ap.add_argument("--auto-shift", action="store_true",
                    help="某句超长时自动顺延后续句子（gap 用 --gap）")
    ap.add_argument("--gap", type=float, default=0.3, help="句间最小间隔秒（auto-shift 用）")
    ap.add_argument("--max-cps", type=float, default=MAX_CUE_CPS,
                    help="单条字幕最大阅读速度（显示宽度/秒；CJK 计 2 → 20≈10 汉字/秒；"
                         "0=关闭。超标时把 cue 延长进后方空隙，SubtitleEdit 规则）")
    ap.add_argument("--cue-gap", type=float, default=MIN_CUE_GAP,
                    help="相邻字幕 cue 最小间隔秒（默认 0.08≈2 帧；重叠时缩前条让位）")
    ap.add_argument("--subs-out", default="", help="字幕输出路径（默认与 --out 同级 subtitles_final.json）")
    ap.add_argument("--bgm", default="", help="BGM 音频文件（循环补齐到成片长，VO 出现自动闪避压低）")
    ap.add_argument("--bgm-duck", type=int, default=-14,
                    help="BGM 基础音量 dB（默认 -14；负得越多 BGM 越安静）")
    args = ap.parse_args()

    src = Path(args.lines)
    if not src.exists():
        die(f"找不到 {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    acts = data.get("acts", [])
    lines = data.get("lines", [])
    if not lines:
        die("vo_lines.json 的 lines 为空")

    # at 字段校验（v4.7 审计 P1）：lines[i]["at"] 直下标会 KeyError 裸奔 exit 1。
    # 必须发生在 TTS/录音检查**之前**——错误的数据不该开始烧合成额度。
    for i, ln in enumerate(lines):
        if "at" not in ln or not isinstance(ln.get("at"), (int, float)):
            die(f"第 {i+1} 句（{ln.get('id', '?')}）缺 at 字段（起读秒数）——"
                f"vo_lines_at.json 由 plan 子命令生成；手工新写请补 at 值", 2)

    out = Path(args.out)
    if out.suffix.lower() == ".mp3":
        # imageio_ffmpeg 自带的 ffmpeg 无可用 MP3 编码器（"Exactly one MP3 audio stream
        # is required"）。成片本身是 mp4+aac，中间文件用 m4a/aac 无影响。
        print(f"[vo_build] 注意：MP3 编码器不可用，输出改为 {out.with_suffix('.m4a').name}",
              file=sys.stderr)
        out = out.with_suffix(".m4a")
    out.parent.mkdir(parents=True, exist_ok=True)
    lines_dir = Path(args.dir) if args.dir else out.parent / "lines"
    lines_dir.mkdir(parents=True, exist_ok=True)

    # ① 分句合成（或复用已有录音）
    print(f"[vo_build] 共 {len(lines)} 句，目录 {lines_dir}", file=sys.stderr)
    for ln in lines:
        lid = ln["id"]
        f = None
        for ext in (".mp3", ".m4a", ".wav"):
            cand = lines_dir / f"{lid}{ext}"
            if cand.exists():
                f = cand
                break
        if args.skip_tts:
            if f is None:
                die(f"--skip-tts 但缺少录音 {lines_dir}/{lid}.mp3|m4a|wav", 2)
        else:
            # f 为 None = 无既有录音 → 走 TTS 合成。
            # v4.7.7 修 P0：旧版直接 `f.exists()`，f=None 时 AttributeError，
            # 且崩在**调用 TTS 之前**（连合成请求都没发出去）。
            if f is None or not f.exists():
                # per-line 声音属性覆盖全局（感情/音色/语速逐句可换：高潮句用激昂档）
                voice = ln.get("voice") or args.voice
                emotion = ln.get("emotion", "")
                try:
                    speed = float(ln.get("speed") or args.speed)
                except (TypeError, ValueError):
                    die(f"{lid} 的 speed {ln.get('speed')!r} 不是数字", 2)
                tts_line(ln["text"], lines_dir / f"{lid}.mp3", voice, speed, emotion)
                f = lines_dir / f"{lid}.mp3"
        ln["_file"] = str(f)
        ln["_dur"] = probe(f)
        if ln["_dur"] <= 0:
            die(f"{f.name} 时长为 0，文件可能损坏", 4)
        print(f"  {lid} {ln['_dur']:.2f}s  {ln['text']}")

    # ② 越界检查 / 自动顺延
    total = args.total or (lines[-1]["at"] + lines[-1]["_dur"] + 2.0)
    for i, ln in enumerate(lines):
        nxt = lines[i + 1]["at"] if i + 1 < len(lines) else total
        avail = nxt - ln["at"]
        if ln["_dur"] > avail:
            msg = (f"{ln['id']} 实际 {ln['_dur']:.2f}s 超过可用 {avail:.2f}s "
                   f"（下一句 {lines[i+1]['id'] if i+1 < len(lines) else '片尾'} @ {nxt}s）")
            if args.auto_shift:
                print(f"[vo_build] 顺延：{msg}", file=sys.stderr)
            else:
                print(f"[vo_build] 警告：{msg}（可用 --auto-shift 自动顺延）", file=sys.stderr)
    if args.auto_shift:
        for i in range(len(lines) - 1):
            need = lines[i]["at"] + lines[i]["_dur"] + args.gap
            if lines[i + 1]["at"] < need:
                lines[i + 1]["at"] = round(need, 2)

    # ③ 精确拼接：静音底(总长) + 各句 adelay 落位 + amix；可选 BGM 床（VO 为 key 闪避）
    inputs: list[str] = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    for ln in lines:
        inputs += ["-i", ln["_file"]]
    bgm_path = getattr(args, "bgm", "")
    if bgm_path:
        inputs += ["-stream_loop", "-1", "-i", bgm_path]
    fc = [f"[0:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS[base]"]
    labels = ["[base]"]
    for i, ln in enumerate(lines, start=1):
        ms = int(round(ln["at"] * 1000))
        lab = f"l{i}"
        fc.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms},apad,"
                  f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS[{lab}]")
        labels.append(f"[{lab}]")
    if bgm_path:
        # BGM 输入是最后一个（idx = 句数+1）；闪避链见 bgm_assembly 注释
        bgm_idx = len(lines) + 1
        duck = int(getattr(args, "bgm_duck", -14))
        fc.extend(bgm_assembly(labels, bgm_idx, total, duck))
    else:
        fc.append("".join(labels) +
                  f"amix=inputs={len(labels)}:normalize=0:duration=first[mix]")
    run = [_ffmpeg(), "-y", "-loglevel", "error", *inputs,
           "-filter_complex", ";".join(fc), "-map", "[mix]",
           "-c:a", "aac", "-b:a", "192k", "-ac", "2", str(out)]
    print(" ", " ".join(run), file=sys.stderr)
    rc = subprocess.call(run)
    if rc != 0:
        die(f"拼接失败 rc={rc}", 5)
    print(f"[vo_build] -> {out}  ({probe(out):.2f}s)")

    # ④ 输出精确字幕轴（v4.13：句子带词轴时按**词边界**切成多条 cue）
    subs = []
    for a in acts:
        subs.append({"at": a["at"], "dur": a.get("dur", 2.6),
                     "text": a["text"], "pos": a.get("pos", "center"),
                     "size": a.get("size", 54), "fade": a.get("fade", 0.8)})
    n_cut = 0
    line_cues: list = []
    for ln in lines:
        cues = split_line_cues({"at": round(ln["at"], 2), "dur": round(ln["_dur"], 2),
                                "text": ln["text"], "words": ln.get("words") or []})
        n_cut += max(0, len(cues) - 1)
        line_cues.extend(cues)
    line_cues, timing_stats = fix_cue_timing(
        line_cues, max_cps=float(args.max_cps), min_gap=float(args.cue_gap))
    for c in line_cues:
        c.update({"pos": "bottom", "size": 44, "fade": 0.35})
        subs.append(c)
    subs_path = Path(args.subs_out) if args.subs_out else out.parent / "subtitles_final.json"
    subs_path.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")
    cut_note = f"，其中 {n_cut} 处按词切行" if n_cut else ""
    tm_note = (f"；CPS 延长 {timing_stats['extended']} / 收缩 {timing_stats['shrunk']}"
               f" / 修不动 {timing_stats['unfixable']}"
               if any(timing_stats.values()) else "")
    print(f"[vo_build] -> {subs_path}  ({len(subs)} 条，与朗读精确对齐{cut_note}{tm_note})")

    # ⑤ 摘要
    voiced = sum(ln["_dur"] for ln in lines)
    print(f"[vo_build] 有声 {voiced:.1f}s / 总长 {total:.1f}s "
          f"（留白 {total - voiced:.1f}s，占比 {(total-voiced)/total*100:.0f}%）")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""postprocess.py — 后期统一（默认规格：1280x720/H.264/yuv420p）+ 自检。

用法：
  python postprocess.py concat  clips/ --out out.mp4 [--target-res 1280x720] [--bgm audio.mp3]
                                        [--slogan "文房四宝 · 皖美传承"] [--slogan-position left]
                                        [--slogan-fade 0.8] [--slogan-at -1] [--xfade 0.5]
  # 声音与字幕（三件套）：
  #   --voice 旁白.mp3 --voice-delay 1.0    旁白混音（TTS 产出）
  #   --bgm 音乐.mp3 --bgm-db -18            BGM 自动循环铺满并压低
  #   --subtitles subs.json                 多段字幕 [{at,dur,text,pos,size,fade}]
  #   --ambient-db -10                      原片环境音压低（有人声时）
  python postprocess.py check    out.mp4
  python postprocess.py extract  clip.mp4 last.png   # 抽末帧（也可用 media_gen last-frame）
  python postprocess.py kenburns shot.png clip.mp4   # 关键帧→视频兜底
"""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mg_core import PRODUCT_EXTS, _ffmpeg, natkey, run_capture
# ffmpeg 路径单源：从 mg_core 取（v4.8.0 收编——本地副本曾各自维护 _ffmpeg_cache，
# audio_triage 那份漏了缓存，每次调用重探测文件系统）

def run(cmd: list[str]) -> int:
    print(" ".join(cmd), file=sys.stderr)
    rc = subprocess.call(cmd)
    if rc != 0:
        die(f"ffmpeg 失败 rc={rc}", 5)
    return rc

def find_font() -> str:
    """中文落版字体探测（Windows）。
    优先级：_FFMPEG_FONT 环境变量 > 楷体（书法感，中式落版首选）> 雅黑 > 黑体 > 宋体。
    """
    candidates = [
        os.environ.get("_FFMPEG_FONT", ""),   # 曾误写成 "_ffmpeg()_FONT"（函数名拼进变量名，#5）
        "C:/Windows/Fonts/simkai.ttf",   # 楷体
        "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
        "C:/Windows/Fonts/msyhbd.ttc",   # 雅黑粗体
        "C:/Windows/Fonts/simhei.ttf",   # 黑体
        "C:/Windows/Fonts/simsun.ttc",   # 宋体
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    die("未找到中文字体（simkai/msyh/simhei/simsun 均缺失）。请设置环境变量 _FFMPEG_FONT 指向任一 .ttf/.ttc", 2)
    return ""  # unreachable

def _escape_drawtext(text: str) -> str:
    """drawtext 文本转义：反斜杠翻倍；单引号前加反斜杠。

    ★ **不再转义 % **（v4.15 实测推翻旧做法）：旧版按「#11 的 100% 解析异常」把百分号
    写成「反斜杠+%」，但在 ffmpeg 7.1 上那与裸 % 、%% 一样触发 drawtext 的
    `Stray % near '...'`，后果是**整条字幕一个字都不画、rc 仍为 0**（典型静默失败）。
    正解是在滤镜里加 `expansion=none`（见 `_drawtext_filters`）让 % 当字面量。
    """
    return str(text).replace("\\", "\\\\").replace("'", "\\'")



# ─── filter 路径转义 + 画质门禁 + 编码器选择（v4.14 ③ ffmpeg 能力升级）───
ENC_CRF = 20                 # libx264 质量（与既有链一致）
ENC_QSV_QUALITY = 28         # QSV global_quality（数值语义与 crf 不同，28 ≈ crf 20~23）
_QSV_CACHE: bool | None = None


def ff_path(p) -> str:
    """本地路径 → 可安全放进 ffmpeg filter 参数的形式。

    ★ 实测（2026-09-22，ffmpeg 7.1）：`removelogo=filename=C:\\Users\\...` 直接崩
    （rc=4294967274，"No option name near 'Usersword4...'"）；**单引号包住但不转义冒号
    同样崩**。可靠写法只有两种：转义冒号（`C\\:/path`，可再套单引号）或"相对名 + cwd"。
    所以 filter 里的**任何文件名**（removelogo / vidstab / ssim stats_file / subtitles /
    fontfile）都必须走这里。**返回值已含单引号**，直接写 `opt={ff_path(p)}`——再套一层引号会崩。
    """
    esc = str(p).replace("\\", "/").replace(":", "\\:")
    return f"'{esc}'"


# ─── 字幕渲染：ASS 生成（v4.15，替代 drawtext 为默认通道）──────────────
# 为什么换：drawtext 每句一个滤镜、转义规则刁钻（\ ' %），且**结构上做不到逐词高亮**。
# ASS 走 libass，一次加载全部字幕，带正式样式 + karaoke 高亮。
# ★ 实测（2026-09-22，ffmpeg 7.1，PIL 数像素判定）：
#   · `ass='C\:/path'`（ff_path 的输出）**真出像素**；裸路径 rc=4294967274（与 removelogo 同因）
#   · 字体用**家族名**（Microsoft YaHei）即可，不必给 fontfile（libass 走 DirectWrite）；
#     `fontsdir=` 也被接受，自定义字体仍可覆盖
#   · `\k` 语义：**未唱 = SecondaryColour，已唱 = PrimaryColour**（帧序列实测 蓝→白单调）
#     → 所以「K 样式」把 Primary 设成高亮色、Secondary 设成常态色，才能得到"唱到就点亮"
ASS_PLAIN_STYLE = "S"            # 无词轴：常态色
ASS_K_STYLE = "K"                # 有词轴：Primary=已唱高亮，Secondary=未唱常态
ASS_DEFAULT_FONT = "Microsoft YaHei"
ASS_HIGHLIGHT = "gold"           # 逐词高亮的"已唱"色

_ASS_COLORS = {
    "white": "&H00FFFFFF", "black": "&H00000000", "yellow": "&H0000FFFF",
    "gold": "&H0000D7FF", "orange": "&H0000A5FF", "red": "&H000000FF",
    "green": "&H0000FF00", "blue": "&H00FF0000", "cyan": "&H00FFFF00",
    "gray": "&H00808080", "grey": "&H00808080",
}
_ASS_POS = {"bottom": (2, 80), "center": (5, 0), "left": (4, 0)}


def ass_color(name, *, alpha: str = "00") -> str:
    """颜色名 / `&H...` 串 → ASS `&HAABBGGRR`。认不出回退白（字幕不该因颜色名崩）。"""
    s = str(name or "").strip()
    if re.match(r"^&H[0-9A-Fa-f]{6,8}$", s):
        return s if len(s) == 10 else "&H" + alpha + s[2:].rjust(6, "0")
    return _ASS_COLORS.get(s.lower(), _ASS_COLORS["white"])


def ass_time(t) -> str:
    """秒 → ASS 时间戳 `H:MM:SS.cc`（厘秒、小时不补零）。非法/负值/NaN → 0:00:00.00。"""
    try:
        v = float(t)
    except (TypeError, ValueError):
        v = 0.0
    if v != v or v < 0:                     # NaN / 负
        v = 0.0
    cs = int(round(v * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    sec, cc = divmod(rem, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cc:02d}"


def ass_text(s) -> str:
    """ASS 文本转义：`\\`→`\\\\`，`{`→`\\{`，`}`→`\\}`，换行→`\\N`。

    ★ 与 drawtext 不同：`%` 在 ASS **不特殊**（照抄 _escape_drawtext 会多出反斜杠）。
    """
    t = str(s).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return t.replace("\r\n", "\\N").replace("\r", "\\N").replace("\n", "\\N")


def karaoke_text(text, words, *, sep_cjk: bool = True) -> str:
    """cue 文本 + 词轴 → 带 `{\\k厘秒}` 的 ASS 文本（逐词高亮）。

    文本尽量取**原文**（保标点）：借 vo_build._map_words_to_text 把词映回原文区间
    （函数内 import——postprocess 是底层模块，不该在顶层依赖 vo_build）。
    对不上原文则回退词拼接，与 vo_build._cue_text 同一策略（原文优先，时间始终来自词轴）。
    无可用词 → 原样返回（不带标签，与旧行为一致）。
    """
    ws = [w for w in (words or [])
          if isinstance(w, dict) and str(w.get("w") or "").strip()]
    if not ws:
        return ass_text(text)
    src = str(text or "")
    is_cjk = None
    spans = None
    try:
        from vo_build import _is_cjk_char, _map_words_to_text
        is_cjk = _is_cjk_char
        spans = _map_words_to_text(src, ws)
    except Exception:
        spans = None
    segs: list = []                          # [(片段文本, 词)]
    if spans:
        prev = 0
        for i, w in enumerate(ws):
            sp = spans[i]
            if not sp:                       # 纯标点词：不占段落（时间并入下一词）
                continue
            segs.append((src[prev:sp[1]], w))
            prev = sp[1]
        if segs and prev < len(src):         # 尾部残余（句点等）挂在最后一段
            segs[-1] = (segs[-1][0] + src[prev:], segs[-1][1])
    if not segs:                             # 回退：词拼接（CJK 之间不补空格）
        for w in ws:
            t = str(w.get("w") or "").strip()
            pre = " "
            if not segs or (sep_cjk and is_cjk is not None
                            and (is_cjk(segs[-1][0][-1:]) or is_cjk(t[:1]))):
                pre = ""
            segs.append((pre + t, w))
    out = []
    for seg, w in segs:
        try:
            cs = max(1, int(round(float(w.get("dur") or 0.0) * 100)))
        except (TypeError, ValueError):
            cs = 1
        out.append(f"{{\\k{cs}}}{ass_text(seg)}")
    return "".join(out)


def _ass_style_line(name: str, *, font: str, size: int, primary: str, secondary: str,
                    back_color: str, border_style: int, outline: int, shadow: int,
                    align: int = 2) -> str:
    """ASS `Style:` 行（字段顺序固定，少一个就整份失效）。"""
    return (f"Style: {name},{font},{size},{primary},{secondary},&H00000000,"
            f"{back_color},0,0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
            f"{align},20,20,24,1")


def cues_to_ass(cues, *, width, height, duration: float = 0.0, font: str = "",
                style=None, karaoke: bool = True, base_size: int = 48,
                base_color: str = "white") -> str:
    """cue 列表 → 完整 ASS 文档（纯函数）。

    cue = {at, dur, text, pos?, size?, fade?, words?}；`dur<=0` = 出现后持续到片尾
    （--slogan 的语义）。有 `words` 且 karaoke=True → 走 K 样式逐词高亮。
    样式是**全局**的、逐条差异用 override 标签（\\an/\\pos/\\fs/\\fad）——这是 ASS 的模型。
    """
    st = dict(style or {})
    fam = font or ASS_DEFAULT_FONT
    plain = ass_color(st.get("fontcolor") or base_color)
    hi = ass_color(st.get("karaoke_color") or ASS_HIGHLIGHT)
    box = bool(st.get("box"))
    border_style = 3 if box else 1           # 3 = OpaqueBox（底框风格）
    outline = int(st.get("outline", 8 if box else 2))
    shadow = int(st.get("shadow", 0 if box else 1))
    back = ass_color(st.get("back_colour") or "black", alpha="A0")
    doc = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {int(width)}", f"PlayResY: {int(height)}",
        "WrapStyle: 2",                       # 2 = 不自动折行（断句已在 split_line_cues 做过）
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _ass_style_line(ASS_PLAIN_STYLE, font=fam, size=int(base_size), primary=plain,
                        secondary=plain, back_color=back, border_style=border_style,
                        outline=outline, shadow=shadow),
        _ass_style_line(ASS_K_STYLE, font=fam, size=int(base_size), primary=hi,
                        secondary=plain, back_color=back, border_style=border_style,
                        outline=outline, shadow=shadow), "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    W, H = int(width), int(height)
    for s in cues or []:
        at = float(s.get("at") or 0.0)
        dur = float(s.get("dur") or 0.0)
        end = at + dur if dur > 0 else max(at + 0.5, float(duration or 0.0))
        if end <= at:
            end = at + 0.5
        words = s.get("words") or []
        use_k = bool(karaoke and words)
        align, mv = _ASS_POS.get(str(s.get("pos") or "bottom"), _ASS_POS["bottom"])
        # 位置用 \pos 精确复刻旧 drawtext 的几何（\an2 底中 /\an5 正中 /\an4 左中）
        if align == 2:
            pos = f"{W // 2},{H - mv}"
        elif align == 4:
            pos = f"70,{H // 2}"
        else:
            pos = f"{W // 2},{H // 2}"
        tags = f"\\an{align}\\pos({pos})"
        try:
            size = int(s.get("size") or base_size)
        except (TypeError, ValueError):
            size = int(base_size)
        if size != int(base_size):
            tags += f"\\fs{size}"
        try:
            fade = float(s.get("fade") or 0.0)
        except (TypeError, ValueError):
            fade = 0.0
        if fade > 0:
            ms = int(round(fade * 1000))
            tags += f"\\fad({ms},{ms})"
        body = karaoke_text(s.get("text", ""), words) if use_k else ass_text(s.get("text", ""))
        doc.append(f"Dialogue: 0,{ass_time(at)},{ass_time(end)},"
                   f"{ASS_K_STYLE if use_k else ASS_PLAIN_STYLE},,0,0,0,,{tags}{body}")
    return "\n".join(doc) + "\n"


def ass_filter(ass_path, fonts_dir: str = "") -> str:
    """→ `ass=<ff_path>`（可选 `fontsdir`，同样走 ff_path——裸路径必崩）。"""
    s = f"ass={ff_path(ass_path)}"
    if fonts_dir:
        s += f":fontsdir={ff_path(fonts_dir)}"
    return s


def ass_style_spec(preset: str) -> dict:
    """预设名 → ASS 样式规格（读 subtitle_presets.json 的 `ass` 子字典）。

    用**显式**的 ass 字典，而不是去解析 drawtext 的 box/borderw 参数串——那是"解析自己的
    历史格式"，脆且易漂。预设不存在/无 ass 键 → 空字典（用默认样式）。
    """
    if not preset:
        return {}
    try:
        item = (load_presets() or {}).get(preset) or {}
    except Exception:
        return {}
    spec = item.get("ass")
    return dict(spec) if isinstance(spec, dict) else {}


_SSIM_ALL = re.compile(r"\bAll:\s*([0-9.]+)", re.I)


def parse_ssim_stats(text: str):
    """ssim stats/日志文本 → 末帧 All 值（float）；取不到返回 None。

    为什么不用 stderr：ssim 的结果是 **info 级**日志，`-loglevel error` 会把它吞掉 ——
    rc=0 却零信息（实测踩到）。`stats_file=` 才是可靠通道。
    """
    if not text:
        return None
    last = None
    for line in text.splitlines():
        for m in _SSIM_ALL.finditer(line):
            try:
                last = float(m.group(1))
            except ValueError:
                continue
    if last is None and re.search(r"All:\s*1\.0+\s*\(inf\)", text, re.I):
        return 1.0
    return last


def ssim_verdict(value, min_ssim: float = 0.95):
    """(级别, 说明)。**取不到值 = WARN**（未知 ≠ 合格，同 duration_verdict 口径）。"""
    if value is None:
        return "WARN", f"SSIM 取不到（参考对比失败？）阈值 {min_ssim}"
    if value + 1e-9 < min_ssim:
        return "FAIL", f"SSIM {value:.4f} < 阈值 {min_ssim}（画质劣化）"
    return "PASS", f"SSIM {value:.4f} ≥ {min_ssim}"


def _ssim_between(a: str, b: str):
    """两路视频/图片的 SSIM（末帧 All 值）；取不到返回 None。

    ★ stats_file 的路径同样必须 ff_path 转义；数值只在 stats 文件里——
      `-loglevel error` 会吞掉 stderr 版（rc=0 却零信息，实测踩到）。
    """
    import tempfile as _tf
    with _tf.TemporaryDirectory(prefix="rc_ssim_") as td:
        sf = Path(td) / "ssim.log"
        r = subprocess.run(
            [_ffmpeg(), "-y", "-loglevel", "error", "-i", a, "-i", b,
             "-lavfi", f"ssim=stats_file={ff_path(sf)}", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"[qcgate] ssim 对比失败 rc={r.returncode}: "
                  f"{(r.stderr or '').strip()[-200:]}", file=sys.stderr)
            return None
        if not sf.exists():
            return None
        return parse_ssim_stats(sf.read_text(encoding="utf-8", errors="replace"))


def resolve_encoder(use_qsv: bool, *, available: bool):
    """(编码参数 list, 实际 encoder 名)。请求 QSV 但不可用 → 回退 libx264 **并如实标注**。

    实测（2026-09-22）本机 h264_qsv 在 纯编码 / +drawtext / +subtitles 三种链路都能出片，
    且字幕真烧上（抽帧底部亮像素 89，无字幕对照 0）。
    """
    if use_qsv and available:
        return ["-c:v", "h264_qsv", "-global_quality", str(ENC_QSV_QUALITY)], "h264_qsv"
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(ENC_CRF)], "libx264"


def voice_speed_filter(speed: float) -> str:
    """旁白变速过滤串（rubberband —— **变速不变调**）。1.0 → 空串（调用方不加）。"""
    try:
        v = float(speed)
    except (TypeError, ValueError):
        die(f"--voice-speed 需要数字，收到 {speed!r}", 2)
    if abs(v - 1.0) < 1e-6:
        return ""
    if not (0.5 <= v <= 2.0):
        die(f"--voice-speed {v} 超出安全范围 0.5~2.0（再夸张会明显失真）", 2)
    return f"rubberband=tempo={v:g}"


def qsv_available() -> bool:
    """本机 h264_qsv 真能用？（encoder 列表 + 0.2s 冒烟编码；结果缓存）。

    只在"列表里有"不够——驱动/权限问题会让它装得出来却跑不起来。
    """
    global _QSV_CACHE
    if _QSV_CACHE is None:
        ok = False
        try:
            r = subprocess.run([_ffmpeg(), "-hide_banner", "-encoders"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
            if "h264_qsv" in (r.stdout or ""):
                import tempfile as _tf
                with _tf.TemporaryDirectory() as td:
                    p = os.path.join(td, "q.mp4")
                    args, _ = resolve_encoder(True, available=True)
                    r2 = subprocess.run(
                        [_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi",
                         "-i", "testsrc=size=160x120:rate=10:duration=0.2", *args, p],
                        capture_output=True, timeout=60)
                    ok = (r2.returncode == 0 and os.path.exists(p)
                          and os.path.getsize(p) > 0)
        except Exception:
            ok = False
        _QSV_CACHE = ok
    return _QSV_CACHE


# ─── 字幕：srt 导入 + 样式预设（v4.2） ─────────────────────
_SRT_TS = re.compile(r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2,}):(\d{2}):(\d{2}),(\d{3})$")


def parse_srt(text: str) -> list:
    """标准 srt 文本 → 内部字幕轴 [{at, dur, text}]（纯函数可单测）。

    规则：时间戳行必须是 HH:MM:SS,mmm --> HH:MM:SS,mmm（毫秒逗号，标准 srt）；
    块序号行可有可无（容错）；多行文本用空格拼一句；非法时间戳块整块丢弃。
    """
    subs = []
    cur_lines: list = []
    cur_span = None
    for raw in text.splitlines() + [""]:
        line = raw.strip()
        m = _SRT_TS.match(line) if line else None
        if m:
            g = [int(x) for x in m.groups()]
            start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
            end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
            if cur_span is not None and cur_lines:      # 上一块收尾
                subs.append({"at": cur_span[0], "dur": round(cur_span[1] - cur_span[0], 3),
                             "text": " ".join(cur_lines)})
            cur_span, cur_lines = (start, end), []
            continue
        if cur_span is None:
            continue                    # 时间戳出现前的行（序号/头部垃圾）忽略
        if not line:                    # 空行 = 块结束
            if cur_lines:
                subs.append({"at": cur_span[0], "dur": round(cur_span[1] - cur_span[0], 3),
                             "text": " ".join(cur_lines)})
            cur_span, cur_lines = None, []
            continue
        if line.isdigit() and not cur_lines:
            continue                    # 纯数字首行 = 块序号，容错跳过
        cur_lines.append(line)
    return subs


_PRESETS_PATH = Path(__file__).resolve().parent / "subtitle_presets.json"


def load_presets() -> dict:
    return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))


def apply_preset(subs: list, preset: str) -> list:
    """字幕条目缺的样式字段按预设填充；条目自带字段优先（纯函数可单测）。

    预设键：pos/size/fade/fontcolor/box（box 为 drawtext 附加参数串，空=无）。
    预设不存在 → ValueError（CLI 层转 die）。
    """
    presets = load_presets()
    if preset not in presets:
        raise ValueError(f"未知字幕预设: {preset}（可用: "
                         f"{[k for k in presets if not k.startswith('_')]}）")
    p = presets[preset]
    out = []
    for s in subs:
        d = dict(s)
        for k in ("pos", "size", "fade", "fontcolor", "box"):
            if k not in d and k in p:
                d[k] = p[k]
        out.append(d)
    return out



_PROBE_CACHE: dict = {}   # path -> info（同进程内 probe 结果复用：audit/triage 不重复起 ffmpeg）


def probe(path: str) -> dict:
    """ffprobe 等价的简化：跑 ffmpeg -i，提取第一行 Duration 与 Stream 行。
    结果按 path 进程内缓存——一个 20 镜 audit 只起 20 个进程而非 40。"""
    if path in _PROBE_CACHE:
        return _PROBE_CACHE[path]
    out = run_capture([_ffmpeg(), "-i", path]).stderr
    info = {}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    # 编码格式：只比分辨率不够判断能否 -c copy——同分辨率但 h264 vs vp9
    # 直拼同样会失败或花屏（#4），一致性签名必须带上 codec。
    m = re.search(r"Video:\s*([A-Za-z0-9_]+)", out)
    if m:
        info["codec"] = m.group(1).lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", out)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", out)
    if m:
        info["bitrate_kbps"] = int(m.group(1))
    if re.search(r"Audio:", out):
        info["audio"] = True
    _PROBE_CACHE[path] = info
    return info

def duration_verdict(info: dict, max_duration: float) -> tuple[str, str]:
    """时长校验的显式三态判定（纯函数）：('ok'|'warn'|'fail', 说明)。

    'warn' = ffprobe 没解析出 Duration（动图 webp / 容器异常 / 流式封装）——
    **不能当通过**：旧逻辑用 info.get("duration", 0) 兜底，缺失时 `0 <= max` 恒真，
    规格门禁形同虚设（"假合格"）。max_duration <= 0 表示不校验。
    """
    if not max_duration or max_duration <= 0:
        return "ok", "未设时长上限"
    dur = info.get("duration")
    if dur is None:
        return "warn", "时长无法判定（ffprobe 未解析出 Duration）——请人工确认"
    if dur > max_duration:
        return "fail", f"{dur}s > {max_duration}s"
    return "ok", f"{dur}s <= {max_duration}s"


# ─── 拼接 + 后期统一 ──────────────────────────────────────
def _collect_clips(clips_dir: Path) -> list[Path]:
    """收集待拼分片：clip_*.mp4 / .webp，按镜号自然排序。
    两处都不能省——自然排序（#2，字典序会让 S10 排到 S2 前导致镜序错乱）、
    扩展名走 PRODUCT_EXTS（#3，只认 .mp4 会把 webp 分镜整镜丢掉）。"""
    clips = sorted([p for ext in PRODUCT_EXTS for p in clips_dir.glob(f"clip_*{ext}")],
                   key=natkey)
    if not clips:
        die(f"未在 {clips_dir} 找到 clip_*({' / '.join(PRODUCT_EXTS)})")
    return clips


def audio_plan(probes: list[dict]) -> str:
    """整卷音轨策略（纯函数，可单测）：
      none  = 全部分片都无音轨 → 不产出音轨
      all   = 全部分片都有音轨 → 直接 acrossfade / concat
      mixed = 部分有音轨 → **必须给无音轨的分片补等长静音**，否则整卷丢音

    历史 bug（2026-09 复测发现）：曾用 `all(probe(c).get("audio") ...)` 判定，
    mixed 场景（真视频带环境音 + kenburns 静帧片段混编——hybrid 模式的常态）
    直接落进 none 分支 → 整卷音轨被静默丢弃，rc=0 无声成片。
    """
    flags = [bool((p or {}).get("audio")) for p in probes]
    if not flags or not any(flags):
        return "none"
    return "all" if all(flags) else "mixed"


def audio_src(has_audio: bool, idx: int, dur: float) -> str:
    """单片音轨输入链：有音轨 → 重采样；无音轨 → 补等长静音（混编保音轨的关键）。"""
    if has_audio:
        return f"[{idx}:a]aresample=48000,asetpts=PTS-STARTPTS[a{idx}]"
    d = dur if dur > 0 else 5.0
    return (f"anullsrc=r=48000:cl=stereo,atrim=0:{d:.3f},"
            f"asetpts=PTS-STARTPTS[a{idx}]")


def cmd_concat(args) -> None:
    clips_dir = Path(args.clips)
    clips = _collect_clips(clips_dir)
    # 编码器（v4.14）：--qsv 走核显；不可用则回退 libx264 并**如实打印**
    use_qsv = bool(getattr(args, "qsv", False))
    enc, enc_name = resolve_encoder(use_qsv, available=qsv_available())
    if use_qsv and enc_name != "h264_qsv":
        print("[postprocess] --qsv 请求了但本机不可用 → 已回退 libx264", file=sys.stderr)
    # 旁白变速与字幕互斥：字幕轴是按**原速**旁白打的，变速后必然错位
    sp_f = voice_speed_filter(getattr(args, "voice_speed", 1.0))
    if sp_f and (getattr(args, "subtitles", "") or getattr(args, "slogan", "")):
        die("--voice-speed 会让字幕轴失效（字幕按原速旁白打轴）——去掉字幕，或先变速再重新打轴", 2)
    if sp_f:
        print(f"[postprocess] 旁白变速 {sp_f}（rubberband 变速不变调）", file=sys.stderr)
    # 一次性探测每片（规格/时长/音轨）——避免同一文件被反复 ffprobe，也保证
    # 规格告警、xfade offset、音轨补齐用的是同一份数据
    _probes = [probe(str(c)) for c in clips]
    _durs = [float(p.get("duration") or 0.0) for p in _probes]
    _audio = [bool(p.get("audio")) for p in _probes]
    # 混合 provider 提示：不同 provider 输出分辨率不同（如 Agnes 1088x832 vs 智谱 1920x1080），
    # 统一缩放至 --target-res 时可能引入轻微画质/比例差异，提示人工确认。
    # 签名含 codec：同分辨率不同编码（h264 vs vp9）直拼同样会花屏（#4）
    res_set = {(p.get("width"), p.get("height"), p.get("codec")) for p in _probes}
    # .webp 动图进 concat demuxer 不可靠（时长/时间基解析异常）→ 一律走重编码分支；
    # 音轨存在性不一致（真视频带环境音 + kenburns 静帧片段）→ -c copy 直拼会失败，
    # 同样强制重编码（重编码分支会给无音轨片补静音，保整卷音轨）
    mixed_audio = len(set(_audio)) > 1
    need_recode = any(c.suffix.lower() != ".mp4" for c in clips) or mixed_audio
    if need_recode:
        why = "非 mp4 分片（如 .webp 动图）" if any(c.suffix.lower() != ".mp4" for c in clips) \
            else "音轨存在性不一致（部分分片无音轨）"
        print(f"[postprocess] 检测到{why}，强制重编码拼接", file=sys.stderr)
    if len(res_set) > 1:
        # 打印时用 str 归一：元组里可能有 None（probe 不到），直接 sorted 会
        # 抛 TypeError（None 与 int/str 不可比），反倒让提示信息本身把流程打断
        print(f"[postprocess] ⚠ 输入的 clip 规格不一致 "
              f"{sorted(res_set, key=lambda t: tuple(str(x) for x in t))}，"
              f"将统一缩放至 {args.target_res}（可能引入轻微画质/比例差异）", file=sys.stderr)

    tw, th = args.target_res.split("x")
    merged = clips_dir / "_merged.mp4"
    listfile: Path | None = None

    if args.xfade != "0" and len(clips) > 1:
        # 链式交叉溶解：每段先统一分辨率/帧率/时间基，再 xfade
        # 逐转场时长：--xfade 可为单值（0.5）或逗号列表（0.5,0.5,1.0,...，len=镜数-1）
        # offset 递推：T_1 = L_0 - d_1；T_k = L_{k-1} - d_k；L_k = T_k + dur_k（L = 输出总长）
        xs = [float(x) for x in str(args.xfade).split(",")]
        if len(xs) == 1:
            durs = [xs[0]] * (len(clips) - 1)
        elif len(xs) == len(clips) - 1:
            durs = xs
        else:
            die(f"--xfade 列表长度 {len(xs)} ≠ 转场数 {len(clips)-1}")
        inputs: list[str] = []
        for c in clips:
            inputs += ["-i", str(c)]
        fc: list[str] = []
        for i in range(len(clips)):
            fc.append(
                f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th},settb=AVTB,setpts=PTS-STARTPTS,fps=24[f{i}]"
            )
        prev = "f0"
        total_len = _durs[0] or 5.0                           # L_0
        for i in range(1, len(clips)):
            dur = _durs[i] or 5.0
            d_k = durs[i - 1]
            offset = total_len - d_k
            label = f"x{i}"
            fc.append(f"[{prev}][f{i}]xfade=transition=fade:duration={d_k}:offset={offset:.3f}[{label}]")
            prev = label
            total_len = offset + dur                            # L_i
        # 音频链：acrossfade 与视频 xfade 逐段对齐（前提：各 clip 音频时长≈视频时长）。
        # 混编（部分分片无音轨）时给无音轨片补等长静音——用 all() 判定会整卷丢音。
        aplan = audio_plan(_probes)
        if aplan != "none":
            for i in range(len(clips)):
                fc.append(audio_src(_audio[i], i, _durs[i]))
            aprev = "a0"
            for i in range(1, len(clips)):
                alabel = f"ya{i}"
                fc.append(f"[{aprev}][a{i}]acrossfade=d={durs[i-1]}:c1=tri:c2=tri[{alabel}]")
                aprev = alabel
            map_args = ["-map", f"[{prev}]", "-map", f"[{aprev}]",
                        "-c:a", "aac", "-b:a", "192k"]
        else:
            map_args = ["-map", f"[{prev}]"]
        run([_ffmpeg(), "-y", "-loglevel", "error", *inputs,
             "-filter_complex", ";".join(fc), *map_args,
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", str(merged)])
    else:
        # 直切：分辨率/编码一致 → concat demuxer -c copy 快速拼接；
        # 不一致 → 逐段统一（scale/crop/settb/fps）再 concat filter 重编码——
        # 上面已 warn 分辨率不一致，仍 -c copy 直拼会花屏或失败（#4）
        if len(res_set) == 1 and not need_recode:
            listfile = clips_dir / "_concat.txt"
            listfile.write_text("\n".join(f"file '{c.name}'" for c in clips), encoding="utf-8")
            run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", str(listfile), "-c", "copy", str(merged)])
        else:
            inputs: list[str] = []
            for c in clips:
                inputs += ["-i", str(c)]
            fc = []
            for i in range(len(clips)):
                fc.append(
                    f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                    f"crop={tw}:{th},settb=AVTB,setpts=PTS-STARTPTS,fps=24[v{i}]"
                )
            # 音轨：写死 a=0 会静默丢掉环境音（xfade 分支是保音轨的，两分支必须对称）。
            # 混编时给无音轨片补等长静音（同 xfade 分支，见 audio_plan 的历史 bug 说明）
            aplan = audio_plan(_probes)
            if aplan != "none":
                for i in range(len(clips)):
                    fc.append(audio_src(_audio[i], i, _durs[i]))
                fc.append("".join(f"[v{i}][a{i}]" for i in range(len(clips))) +
                          f"concat=n={len(clips)}:v=1:a=1[outv][outa]")
                map_args = ["-map", "[outv]", "-map", "[outa]",
                            "-c:a", "aac", "-b:a", "192k"]
            else:
                fc.append("".join(f"[v{i}]" for i in range(len(clips))) +
                          f"concat=n={len(clips)}:v=1:a=0[outv]")
                map_args = ["-map", "[outv]"]
            run([_ffmpeg(), "-y", "-loglevel", "error", *inputs,
                 "-filter_complex", ";".join(fc), *map_args,
                 "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                 "-pix_fmt", "yuv420p", str(merged)])

    # 第二步：统一分辨率/帧率/编码（默认下限 H.264 yuv420p ≥1280x720）
    # Agnes video 实测输出 1088x832（4:3 偏方），用 increase+crop 填满裁切
    # （不要用 decrease+pad：会出黑边，观感差）
    norm = clips_dir / "_norm.mp4"
    vf = f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},fps=24"
    if args.freeze_last > 0:
        # 末帧定格（落版余韵）：clone 最后一帧 N 秒
        vf += f",tpad=stop_mode=clone:stop_duration={args.freeze_last}"
    norm_cmd = [
        _ffmpeg(), "-y", "-loglevel", "error", "-i", str(merged),
        "-vf", vf,
        *enc,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if probe(str(merged)).get("audio"):
        # 合并片段带环境音：显式映射音轨（BGM/旁白在下一步混音步骤叠加）
        norm_cmd += ["-map", "0:v", "-map", "0:a", "-c:a", "aac", "-b:a", "192k"]
    norm_cmd += [str(norm)]
    run(norm_cmd)

    # 第二步半：旁白 + BGM 混音（视频流 copy，只重编码音频，秒级完成）
    if args.voice or args.bgm:
        mixed = clips_dir / "_mixed.mp4"
        cmd: list[str] = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(norm)]
        nxt = 1
        voice_idx = None
        bgm_idx = None
        if args.voice:
            cmd += ["-i", args.voice]
            voice_idx = nxt
            nxt += 1
        if args.bgm:
            # BGM 通常短于成片：循环铺满
            cmd += ["-stream_loop", "-1", "-i", args.bgm]
            bgm_idx = nxt
            nxt += 1
        fc: list[str] = []
        srcs: list[str] = []
        has_amb = bool(probe(str(norm)).get("audio"))
        # 成片（画面）时长：旁白要补静音到这一刻。**必须给 whole_dur**——
        # 无参 apad 造无限音频流，本 ffmpeg 版本下 `-shortest` 不会终止它
        # → 整个 concat 挂起（2026-09-12 实证：40s 未结束）。probe 失败退回不补。
        _out_dur = float(probe(str(norm)).get("duration") or 0.0) \
            + (float(args.freeze_last) if args.freeze_last > 0 else 0.0)
        if has_amb:
            # 原始环境音：默认压低垫底（-10dB），不抢人声
            fc.append(f"[0:a]aresample=48000,volume={args.ambient_db}dB[amb]")
            srcs.append("[amb]")
        if voice_idx is not None:
            d = int(round(args.voice_delay * 1000))
            # apad=whole_dur：旁白结束后补静音到成片长度。两个作用——
            # ① 单路：音频短于画面时 `-shortest` 会把画面截到音频长度
            #    （v4.7.7 修 P0：实证 10s 画面 + 3s 旁白 → 4.02s 成片，rc=0）；
            # ② 多路：避免 amix 在某路结束时抬升其余音轨电平。
            pad = f",apad=whole_dur={_out_dur:.3f}" if _out_dur > 0 else ""
            sp_c = f",{sp_f}" if sp_f else ""
            fc.append(f"[{voice_idx}:a]aresample=48000,adelay={d}|{d},"
                      f"volume={args.voice_db}dB{sp_c}{pad}[vo]")
            srcs.append("[vo]")
        if bgm_idx is not None:
            fc.append(f"[{bgm_idx}:a]aresample=48000,volume={args.bgm_db}dB[bm]")
            srcs.append("[bm]")
        if len(srcs) == 1:
            # 单路音频（静帧成片只加旁白 / 只铺 BGM）——**amix 要求 ≥2 输入，
            # inputs=1 会让 ffmpeg 无限挂起**（不报错、不退出，2026-09 实测复现）。
            # 直接透传该路，不做混音。
            fc.append(f"{srcs[0]}acopy[mix]")
        elif srcs:
            # normalize=0：各路音量已用 volume= 显式指定，避免某路结束时 amix
            # 自动重新归一化导致音量突跳
            fc.append("".join(srcs) +
                      f"amix=inputs={len(srcs)}:duration=first:dropout_transition=0"
                      f":normalize=0[mix]")
        if srcs:
            # 执行混音必须在 if/elif **之外**——曾把 run 缩进在 amix 分支内，
            # 单路分支只拼了 filter 不执行 → rc=0 但旁白被静默丢弃（2026-09 实测）
            run([*cmd, "-filter_complex", ";".join(fc),
                 "-map", "0:v", "-map", "[mix]",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                 "-shortest", str(mixed)])
            norm = mixed

    # 第三步：烧字幕（--subtitles JSON 列表，--slogan 为快捷单条）
    subs: list[dict] = []
    if args.subtitles:
        sp = Path(args.subtitles)
        if not sp.exists():
            die(f"字幕文件不存在: {sp}")
        if sp.suffix.lower() == ".srt":
            # srt 导入（v4.2）：剪映/PR 导出的标准 srt 直接用
            subs = parse_srt(sp.read_text(encoding="utf-8-sig"))
            if not subs:
                die(f"srt 无有效字幕块（时间戳需为 HH:MM:SS,mmm --> 标准格式）: {sp}", 2)
        else:
            subs = json.loads(sp.read_text(encoding="utf-8"))
            if not isinstance(subs, list):
                die("字幕 JSON 需为数组 [{at,dur,text,...}]")
    preset = getattr(args, "subtitle_preset", "")
    if subs and preset:
        try:
            subs = apply_preset(subs, preset)
        except ValueError as e:
            die(str(e), 2)
    if args.slogan:
        total = probe(str(norm)).get("duration", 0) or 0
        at = args.slogan_at if args.slogan_at >= 0 else max(0.0, total - 4.0)
        subs.append({"at": at, "dur": 0, "text": args.slogan,
                     "pos": args.slogan_position, "size": 64,
                     "fade": args.slogan_fade})

    # 字幕：默认走 ASS/libass（v4.15，支持逐词高亮），drawtext 保留为回滚通道
    ass_subs = clips_dir / "_subs.ass"
    if subs:
        out_tmp = clips_dir / "_with_text.mp4"
        if str(getattr(args, "subtitle_render", "") or "ass") == "drawtext":
            vf = ",".join(_drawtext_filters(subs, find_font()))
        else:
            info = probe(str(norm))
            w = int(info.get("width") or 0) or 1280
            h = int(info.get("height") or 0) or 720
            ass_subs.write_text(cues_to_ass(
                subs, width=w, height=h,
                duration=float(info.get("duration") or 0.0),
                font=getattr(args, "subtitle_font", "") or ASS_DEFAULT_FONT,
                style=ass_style_spec(preset),
                base_size=int(subs[0].get("size", 48) or 48),
                base_color=subs[0].get("fontcolor") or "white",
                karaoke=not getattr(args, "no_karaoke", False)), encoding="utf-8")
            # 自定义字体文件（_FFMPEG_FONT）时把所在目录交给 libass；默认不传——
            # ASS 用**家族名**解析字体，不需要 find_font()（少一个 die 点）
            env_font = os.environ.get("_FFMPEG_FONT", "")
            vf = ass_filter(ass_subs,
                            fonts_dir=str(Path(env_font).parent) if env_font else "")
            n_k = sum(1 for s in subs if s.get("words"))
            k_note = f"（{n_k} 条逐词高亮）" if n_k else ""
            print(f"[postprocess] 字幕走 ASS/libass：{len(subs)} 条{k_note}", file=sys.stderr)
        txt_cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(norm),
                   "-vf", vf, *enc,
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                   # 注意：一旦显式 -map，就必须把视频流也写上，否则会输出纯音轨
                   "-map", "0:v"]
        if probe(str(norm)).get("audio"):
            txt_cmd += ["-map", "0:a", "-c:a", "aac", "-b:a", "192k"]
        txt_cmd += [str(out_tmp)]
        run(txt_cmd)
        norm = out_tmp

    # 最终改名
    final = Path(args.out)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(norm, final)
    # 清理中间文件（listfile 仅直切分支存在；out_tmp 仅字幕分支存在）
    out_tmp = clips_dir / "_with_text.mp4"
    for p in (merged, listfile, out_tmp, ass_subs):
        try:
            if p is not None and p.exists() and p.resolve() != final.resolve():
                p.unlink()
        except OSError:
            pass
    print(f"[postprocess] -> {final}")
    print(json.dumps(probe(str(final)), indent=2))

def _drawtext_filters(subs: list, font: str) -> list:
    """旧的 drawtext 通道：cue 列表 → 一组 drawtext 滤镜串（`--subtitle-render drawtext`）。

    v4.15 起默认不再走这里——drawtext **结构上做不到逐词高亮**，且转义规则刁钻（\\ ' %）。
    保留它是为了回滚与极端兼容场景；位置/淡入淡出的表达式逻辑原样不动。
    """
    font_arg = ff_path(font)          # 单源：已含引号，直接嵌进 filter
    draws: list[str] = []
    for _i, s in enumerate(subs):
        text = _escape_drawtext(str(s.get("text", "")))
        at = float(s.get("at", 0))
        dur = float(s.get("dur", 0))
        fade = float(s.get("fade", 0.6))
        size = int(s.get("size", 48))
        pos = s.get("pos", "bottom")
        fontcolor = s.get("fontcolor", "white")
        box_extra = str(s.get("box", "") or "")   # 预设附加参数（底框/描边），空=无
        if pos == "center":
            xy = "x=(w-text_w)/2:y=(h-text_h)/2"
        elif pos == "left":
            xy = "x=70:y=(h-text_h)/2"
        else:
            xy = f"x=(w-text_w)/2:y=h-{80 + size}"
        # 有底框/描边时阴影会让字发糊，二选一；无框才带默认阴影。
        # 拼接时逐段补冒号，绝不输出空段，避免产生 "::" 双冒号（ffmpeg 报 Invalid argument）
        extras = []
        if box_extra:
            extras.append(box_extra)
        else:
            extras.append("shadowcolor=black@0.7:shadowx=3:shadowy=3")
        extra_str = ":" + ":".join(extras)
        if dur > 0:
            end = at + dur
            if fade > 0:
                # 淡入 + 淡出（表达式内逗号必须转义为 \, ）
                alpha = (f"if(lt(t\\,{at:.2f})\\,0\\,"
                         f"if(lt(t\\,{at + fade:.2f})\\,((t-{at:.2f})/{fade:.2f})\\,"
                         f"if(lt(t\\,{end:.2f})\\,1\\,"
                         f"if(lt(t\\,{end + fade:.2f})\\,(({end:.2f}-t)/{fade:.2f})\\,0))))")
            else:
                alpha = f"between(t\\,{at:.2f}\\,{end:.2f})"
        else:
            # 无 dur = 出现后持续到片尾（落版）
            if fade > 0:
                alpha = (f"if(lt(t\\,{at:.2f})\\,0\\,"
                         f"if(lt(t\\,{at + fade:.2f})\\,((t-{at:.2f})/{fade:.2f})\\,1))")
            else:
                alpha = f"gte(t\\,{at:.2f})"
        draws.append(
            f"drawtext=text='{text}':fontfile={font_arg}:"
            f"fontcolor={fontcolor}:fontsize={size}:{xy}{extra_str}"
            f":expansion=none:alpha='{alpha}'"   # expansion=none：% 当字面量，否则含 % 的字幕整条消失
        )
    return draws


# ─── 自检 ─────────────────────────────────────────────────
def cmd_check(args) -> None:
    path = args.path
    info = probe(path)
    print(json.dumps(info, indent=2))
    # 自检阈值可配置（默认 = 通用平台质量下限；目标平台/赛事不同时用 --min-res/--max-duration/--min-fps 覆盖）
    min_w, min_h = (int(x) for x in args.min_res.split("x"))
    dur_level, dur_msg = duration_verdict(info, args.max_duration)
    rules = [
        (f"resolution >= {args.min_res}",
         "ok" if (info.get("width", 0) >= min_w and info.get("height", 0) >= min_h) else "fail",
         f"{info.get('width')}x{info.get('height')}"),
        (f"duration <= {args.max_duration}s", dur_level, dur_msg),
        (f"fps >= {args.min_fps}",
         "ok" if info.get("fps", 0) >= args.min_fps - 0.1 else "fail",
         f"{info.get('fps')}"),
    ]
    for name, level, detail in rules:
        print(f"  [{level.upper()}] {name}  ({detail})")
    # WARN 不拦（动图 webp 常常本就没有 Duration），但必须显式打出来，绝不当 OK
    if any(level == "fail" for _, level, _ in rules):
        sys.exit(2)

# ─── QC 硬门禁（机器可判的自动判，判不了的才交人眼，#4）────
def _frame_stats(frames: list) -> dict:
    """对已抽好的帧算机器指标：每帧平均亮度 / 相邻帧差异（运动量）。
    只做"能算的"——美学崩坏（多手/扭曲）不算，那部分仍交给抽帧人眼。"""
    from PIL import Image
    means, diffs = [], []
    prev = None
    for p in frames:
        im = Image.open(p).convert("L").resize((64, 64))   # 灰度缩略，够算亮度/运动
        px = list(im.tobytes())                            # tobytes：灰度每字节=一像素（getdata 已弃用）
        means.append(sum(px) / len(px))
        if prev is not None:
            diffs.append(sum(abs(a - b) for a, b in zip(px, prev)) / len(px))
        prev = px
    motion = sum(diffs) / len(diffs) if diffs else 0.0
    return {"means": means, "motion": motion}

def qc_times(dur, n: int) -> list[float]:
    """qcgate 抽帧时间点（纯函数）。

    v4.8.0 修：原来 `info.get("duration", 5.0)` 兜底——probe 解析不出 Duration 时
    （异常容器/部分 webp）30s 的片只测开头 5s，后半段黑帧/静帧全漏检却打 PASS。
    时长未知就**只抽首帧**（其余交给 duration_verdict 的 WARN 提示人工复核）。"""
    if dur and float(dur) > 0:
        d = float(dur)
        return [d * i / (n - 1) if n > 1 else d / 2 for i in range(n)]
    return [0.0]


def cmd_qcgate(args) -> None:
    """单段 QC 门禁：规格 + 黑帧/过曝/静帧 机器判定，输出 PASS/WARN/FAIL 并给 exit code。
    与 `check`（只查规格）互补：这里补上"画面是不是坏的"这类机器可判项。
    机器判不了的（手部/面部崩坏、主体漂移）不在这里——用 `qc` 抽帧人眼。
    exit: 0=PASS/WARN，2=FAIL（--strict 时 WARN 也算 FAIL）。"""
    path = args.path
    if not Path(path).exists():
        die(f"文件不存在: {path}", 2)
    info = probe(path)
    min_w, min_h = (int(x) for x in args.min_res.split("x"))
    verdict = []          # (级别, 说明)
    def add(level, msg):
        verdict.append((level, msg))

    # ① 规格（与 check 同阈值）
    if not (info.get("width", 0) >= min_w and info.get("height", 0) >= min_h):
        add("FAIL", f"分辨率 {info.get('width')}x{info.get('height')} < {args.min_res}")
    dur_level, dur_msg = duration_verdict(info, args.max_duration)
    if dur_level == "fail":
        add("FAIL", f"时长 {dur_msg}")
    elif dur_level == "warn":
        add("WARN", dur_msg)      # 未知 ≠ 合规；--strict 会把它升级为 FAIL
    if info.get("fps", 0) < args.min_fps - 0.1:
        add("FAIL", f"帧率 {info.get('fps')} < {args.min_fps}")

    # ② 抽帧机器判定
    dur = info.get("duration") or 0.0
    n = args.frames
    times = qc_times(dur, n)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="qcgate_") as td:
        frames = []
        for i, t in enumerate(times):
            fp = Path(td) / f"f{i:02d}.png"
            rc = subprocess.call([_ffmpeg(), "-y", "-loglevel", "error",
                                  "-ss", f"{max(0.0, t - 0.05):.2f}", "-i", str(path),
                                  "-frames:v", "1", str(fp)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if rc == 0 and fp.exists():
                frames.append(fp)
        if not frames:
            add("FAIL", "抽帧失败（文件可能损坏/无视频流）")
        else:
            st = _frame_stats(frames)
            means = st["means"]
            black = sum(1 for m in means if m < args.black_th) / len(means)
            white = sum(1 for m in means if m > args.white_th) / len(means)
            if black >= 0.8:
                add("FAIL", f"≈{black:.0%} 帧近黑（均值<{args.black_th}）——疑似生成失败/审核拒/纯色")
            elif black >= 0.3:
                add("WARN", f"≈{black:.0%} 帧偏黑（可能故意淡入淡出，请人眼确认）")
            if white >= 0.6:
                add("WARN", f"≈{white:.0%} 帧过曝（均值>{args.white_th}）")
            if st["motion"] < args.motion_th and len(frames) >= 3:
                add("WARN", f"近乎完全静帧（相邻帧均值差 {st['motion']:.3f}<{args.motion_th}）——"
                            f"i2v 可能未生效或纯静帧源；stills/kenburns 档可忽略")

    # ③ 参考对比（--ref，v4.14）：ssim 量化"劣化到什么程度"（图与视频皆可）
    if getattr(args, "ref", ""):
        ref = Path(args.ref)
        if not ref.exists():
            add("FAIL", f"--ref 参考不存在: {ref}")
        else:
            _sv = _ssim_between(str(path), str(ref))
            _lv, _msg = ssim_verdict(_sv, float(getattr(args, "min_ssim", 0.95)))
            add(_lv, f"对比参考 {ref.name}：{_msg}")

    levels = {v[0] for v in verdict}
    if "FAIL" in levels:
        badge, rc = "FAIL", 2
    elif "WARN" in levels:
        badge, rc = ("WARN", 2) if args.strict else ("WARN", 0)
    else:
        badge, rc = "PASS", 0
    print(f"[qcgate] {Path(path).name}  {badge}")
    for lv, msg in verdict:
        print(f"  [{lv}] {msg}")
    if not verdict:
        print("  机器可判项全过（规格/黑帧/过曝/静帧）——美学崩坏仍需 `qc` 抽帧人眼")
    sys.exit(rc)

# ─── 跨镜首帧一致性粗检（qcseq，优化⑤）────────────────────
# ---------- pick：count 选优半自动（A3，纯函数可单测） ----------
def score_images(paths: list) -> list:
    """对同镜 N 张候选图算启发式分（清晰度+对比度+亮度合理域），降序返回。
    **只排序不拍板**——启发式有偏差，最终选哪张必须人看；本命令的价值是把
    明显度差的沉底，让人只在 top 几张里挑。"""
    from PIL import Image, ImageFilter, ImageStat
    rows = []
    for p in paths:
        try:
            with Image.open(p) as im:
                gray = im.convert("L")
                # 清晰度：拉普拉斯近似（锐化后差分能量）——高=边缘多=清晰
                sharp = ImageStat.Stat(
                    gray.filter(ImageFilter.FIND_EDGES)).stddev[0]
                # 对比度：灰度标准差
                contrast = ImageStat.Stat(gray).stddev[0]
                # 亮度合理域：过暗/过曝扣分（目标 ~128 中值）
                bright = ImageStat.Stat(gray).mean[0]
                bright_penalty = abs(bright - 128) / 128
                score = sharp * 2.0 + contrast - bright_penalty * 30
            rows.append({"path": str(p), "sharpness": round(sharp, 2),
                         "contrast": round(contrast, 2),
                         "brightness": round(bright, 1),
                         "score": round(score, 2)})
        except Exception as e:
            rows.append({"path": str(p), "sharpness": 0, "contrast": 0,
                         "brightness": 0, "score": -999, "error": str(e)})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def cmd_pick(args) -> None:
    """pick 子命令：shot_XX_{1..N}.png 候选打分排序，人看 top 选优。"""
    paths = [Path(t) for t in args.images]
    rows = score_images(paths)
    print(f"[pick] {len(rows)} 张候选（启发式排序，只做参考——最终你拍板）：")
    for i, r in enumerate(rows, 1):
        err = f"  读取失败：{r.get('error', '')[:60]}" if r.get("error") else ""
        print(f"  #{i}  {Path(r['path']).name:24s} 分 {r['score']:>8.2f}  "
              f"清晰 {r['sharpness']:>6.2f} 对比 {r['contrast']:>6.2f} "
              f"亮度 {r['brightness']:>6.1f}{err}")
    if rows and not rows[0].get("error"):
        print(f"\n[pick] 建议重点看 #{1}~#3，确认后用它覆盖正式帧名（或 batch --retry-failed 重拍最差的）")


def _hsv_features(im) -> tuple:
    """PIL Image → (108 桶归一化 HSV 直方图, 环形均值色相°, 均值 S 0-1, 均值 V 0-1)。
    32x32 缩略足够判色调跳变；H 12 桶 × S 3 桶 × V 3 桶。"""
    from PIL import Image
    hsv = im.convert("HSV").resize((32, 32))
    px = list(hsv.tobytes())            # HSV 模式每像素 3 字节 H,S,V
    hist = [0.0] * 108
    sin_sum = cos_sum = s_sum = v_sum = 0.0
    n = max(1, len(px) // 3)
    for i in range(n):
        h, s, v = px[i * 3], px[i * 3 + 1], px[i * 3 + 2]
        hist[(h * 12 // 256) * 9 + (s * 3 // 256) * 3 + (v * 3 // 256)] += 1.0
        rad = h * math.pi / 127.5       # H 0-255 → 0-2π（环形统计，红=0 不被截断成两端）
        sin_sum += math.sin(rad)
        cos_sum += math.cos(rad)
        s_sum += s
        v_sum += v
    hist = [c / n for c in hist]
    avg_h = (math.degrees(math.atan2(sin_sum, cos_sum)) + 360.0) % 360.0
    return hist, avg_h, s_sum / n / 255.0, v_sum / n / 255.0

def _bhattacharyya(h1: list, h2: list) -> float:
    """Bhattacharyya 系数：同分布→1，无交集→0。衡量两张首帧色调分布重叠度。"""
    return sum(math.sqrt(p * q) for p, q in zip(h1, h2))

def compare_frames(im1, im2) -> dict:
    """相邻镜首帧对比 → {bc, dh, ds, dv}。bc=Bhattacharyya 系数（主判据），
    dh=环形色相差°（参考），ds/dv=饱和度/明度均值差（参考）。"""
    f1, h1, s1, v1 = _hsv_features(im1)
    f2, h2, s2, v2 = _hsv_features(im2)
    dh = abs(h1 - h2)
    return {"bc": _bhattacharyya(f1, f2), "dh": min(dh, 360.0 - dh),
            "ds": abs(s1 - s2), "dv": abs(v1 - v2)}

def qcseq_decide(features: list, threshold: float = 0.45,
                 hue_min: float = 30.0, sat_max: float = 0.5) -> list:
    """纯决策函数：features=[(name, PIL Image), ...] 按镜序 → 相邻对判定列表。
    WARN = BC 低于阈值 且 色相/饱和度确实漂移（ΔH>hue_min 或 ΔS>sat_max）。
    单看 BC 会误判"同色相不同明度"（如正红↔暗红：硬分桶下 V 维边界抖动 → BC=0，
    但人眼看只是明暗差，不是跑偏）；组合判据专抓"整镜色调/彩度跳变"。"""
    out = []
    for (na, ia), (nb, ib) in zip(features, features[1:]):
        m = compare_frames(ia, ib)
        m.update({"a": na, "b": nb,
                  "warn": m["bc"] < threshold and
                          (m["dh"] > hue_min or m["ds"] > sat_max)})
        out.append(m)
    return out

def cmd_qcseq(args) -> None:
    """跨镜首帧一致性粗检：抽每段首帧 → HSV 直方图相邻 Bhattacharyya 对比。
    抓"某镜风格跑偏"（混编画风跳变）：stylegrid 给人眼看全貌，这里给机器定量。
    exit: 0=全过，1=有 WARN（报告性质，不拦流程），2=输入错误。"""
    import tempfile
    targets: list[Path] = []
    for t in args.clips:
        p = Path(t)
        if p.is_dir():
            targets += sorted((f for f in p.iterdir()
                               if f.suffix in PRODUCT_EXTS and f.name.startswith("clip_")),
                              key=lambda f: natkey(f.name))
        else:
            targets.append(p)
    if len(targets) < 2:
        die("qcseq 需要 ≥2 个成片（传文件列表，或含 clip_*.{mp4,webp} 的目录）", 2)
    features = []
    with tempfile.TemporaryDirectory(prefix="qcseq_") as td:
        from PIL import Image
        for i, c in enumerate(targets):
            if not c.exists():
                die(f"文件不存在: {c}", 2)
            fp = Path(td) / f"f{i:02d}.png"
            rc = subprocess.call([_ffmpeg(), "-y", "-loglevel", "error",
                                  "-i", str(c), "-frames:v", "1", str(fp)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if rc != 0 or not fp.exists():
                die(f"{c.name} 抽首帧失败（文件损坏/无视频流）", 2)
            with Image.open(fp) as im:      # 句柄必须及时关：Windows 下 TemporaryDirectory
                features.append((c.name, im.copy()))  # 清理会因文件占用 PermissionError
    pairs = qcseq_decide(features, threshold=args.threshold)
    warn_n = 0
    for m in pairs:
        warn_n += m["warn"]
        print(f"[qcseq] {m['a']} ↔ {m['b']}  BC={m['bc']:.2f}  ΔH={m['dh']:.0f}°  "
              f"ΔS={m['ds']:.2f}  {'WARN' if m['warn'] else 'OK'}")
    if warn_n:
        print(f"[qcseq] {warn_n} 对相邻镜色调/彩度跳变（BC<{args.threshold} 且色相或饱和度漂移）——"
              f"人眼确认是否某镜风格跑偏：跑偏镜单独重跑，或整体统一调色也可接受")
        sys.exit(1)
    print(f"[qcseq] {len(pairs)} 对相邻镜首帧色调一致性全过")

# ─── 抽末帧 ───────────────────────────────────────────────
def cmd_extract(args) -> None:
    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    run([_ffmpeg(), "-y", "-loglevel", "error", "-sseof", "-0.05",
         "-i", args.video, "-frames:v", "1", out])
    print(f"[postprocess] last frame -> {out}")

# ─── Ken Burns 兜底：静帧 → 缓慢推近视频 ──────────────────
def cmd_stabilize(args) -> None:
    """vidstab 两遍防抖（v4.14）：第一遍分析运动 → 第二遍按分析结果变换。

    两遍是 vidstab 的设计（不是可选优化）：detect 写 transforms 文件，transform 读它。
    实测（2026-09-22，ffmpeg 7.1）：`input=` 路径必须走 ff_path 转义，否则 filter
    解析器直接崩（rc=4294967274）；另外 `unsharp` **不是**本滤镜的选项（别凭记忆写参数）。
    """
    src = Path(args.input)
    if not src.exists():
        die(f"输入不存在: {src}", 2)
    info = probe(str(src))
    if not info.get("duration"):
        die("探测不到时长（文件损坏或无视频流）", 2)
    out = Path(args.out) if args.out else src.with_name(src.stem + "_stab.mp4")
    os.makedirs(os.path.dirname(str(out)) or ".", exist_ok=True)
    import tempfile as _tf
    enc, enc_name = resolve_encoder(getattr(args, "qsv", False),
                                   available=qsv_available())
    print(f"[stabilize] {src.name} → {out.name}（smoothing={args.smoothing} "
          f"shakiness={args.shakiness} 编码器={enc_name}）")
    r2 = -1
    with _tf.TemporaryDirectory(prefix="rc_stab_") as td:
        trf = Path(td) / "transforms.trf"
        r1 = run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                  "-vf", f"vidstabdetect=result={ff_path(trf)}:shakiness={args.shakiness}",
                  "-f", "null", "-"])
        if r1 != 0 or not trf.exists():
            die(f"vidstabdetect 失败 rc={r1}（滤镜不可用或输入异常）", 3)
        r2 = run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                  "-vf", f"vidstabtransform=input={ff_path(trf)}:zoom=0:"
                         f"smoothing={args.smoothing}",
                  *enc, "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    if r2 != 0 or not out.exists() or out.stat().st_size == 0:
        die(f"vidstabtransform 失败 rc={r2}", 3)
    print(f"[stabilize] -> {out}  ({probe(str(out)).get('duration', 0):.2f}s)")


def cmd_kenburns(args) -> None:
    """视频生成失败时的兜底：关键帧 → N 秒缓慢推近片段（与主链同规格）。
    注意 zoompan 表达式内的逗号必须写成 \\, 否则会与 filter 参数分隔符冲突。"""
    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fps = 24
    frames = max(24, int(args.duration * fps))
    zoom_inc = args.zoom
    vf = (
        "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
        f"zoompan=z='min(zoom+{zoom_inc}\\,1.15)':d={frames}:"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1280x720:fps=24,"
        "format=yuv420p"
    )
    run([_ffmpeg(), "-y", "-loglevel", "error", "-loop", "1", "-i", args.image,
         "-vf", vf, "-t", str(args.duration),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-movflags", "+faststart", out])
    print(f"[postprocess] kenburns -> {out}")

def cmd_kenburns_all(args) -> None:
    """stills/hybrid 档批量：目录内图片逐张转缓推片段（纯本地，0 API 调用）。
    输出 clip_NN.mp4 命名对齐 concat 的 glob("clip_*.mp4") 契约；
    注意：NN 是**图片序号**（第几张图），不是 vo_lines.json 里的 shot_id——
    S01/S02… 与 clip_01/clip_02… 只有在镜序与图序一致时才对应，混剪前用
    vo_build.py fit 对账确认。已存在的片段跳过（断点续跑，可反复重跑）。"""
    src = Path(args.dir)
    if not src.is_dir():
        die(f"目录不存在: {src}")
    imgs: list[Path] = []
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        imgs.extend(src.glob(ext))
    imgs = sorted(set(imgs), key=natkey)      # 帧名不补零时字典序会错乱（#2）
    if not imgs:
        die(f"目录无 png/jpg 图片: {src}")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    durs = [d.strip() for d in args.duration.split(",") if d.strip()]
    made = skipped = 0
    for i, p in enumerate(imgs, 1):
        out = outdir / f"clip_{i:02d}.mp4"
        dur = float(durs[(i - 1) % len(durs)]) if durs else 5.0
        if out.exists() and out.stat().st_size > 0:
            print(f"[postprocess] 已存在 {out.name}，跳过")
            skipped += 1
            continue
        kb_args = argparse.Namespace(image=str(p), out=str(out),
                                     duration=dur, zoom=args.zoom)
        cmd_kenburns(kb_args)
        made += 1
    print(f"[postprocess] kenburns-all 完成：新建 {made} / 跳过 {skipped} / 共 {len(imgs)} 镜 -> {outdir}")
    print(f"[postprocess] 下一步: python scripts/postprocess.py concat {outdir} --out final.mp4")

def die(msg: str, code: int = 1) -> None:
    print(f"[postprocess] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)

# ─── webp2mp4（LTX .webp 产物转码，Exif 损坏容错）────────────
def _webp_frames(path: Path):
    """Pillow 解帧绕过 LTX webp 的损坏 Exif（ffmpeg 直转报 invalid TIFF header）。"""
    from PIL import Image, ImageSequence
    im = Image.open(path)
    frames = [f.copy().convert("RGB") for f in ImageSequence.Iterator(im)]
    durs = []
    im.seek(0)
    try:
        while True:
            durs.append(im.info.get("duration", 40))
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    fps = 1000.0 / (sum(durs) / len(durs)) if durs else 24.0
    return frames, fps


def cmd_webp2mp4(args) -> None:
    import tempfile
    src = Path(args.src)
    is_dir = src.is_dir()
    targets = sorted(src.glob("*.webp"), key=natkey) if is_dir else [src]
    if not targets:
        die(f"没有可转的 .webp：{src}")
    for w in targets:
        if is_dir:
            dst = (Path(args.outdir) if args.outdir else w.parent) / (w.stem + ".mp4")
        else:
            dst = Path(args.dst) if args.dst else w.with_suffix(".mp4")
        frames, native_fps = _webp_frames(w)
        fps = args.fps or round(native_fps, 2)
        tmp = Path(tempfile.mkdtemp(prefix="webp2mp4_"))
        for i, f in enumerate(frames):
            f.save(tmp / f"f{i:04d}.png")
        print(f"[webp2mp4] {w.name} -> {dst.name} ({len(frames)}帧 @{fps}fps)")
        run([_ffmpeg(), "-y", "-loglevel", "error", "-framerate", str(fps),
             "-i", str(tmp / "f%04d.png"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
             "-movflags", "+faststart", str(dst)])
        shutil.rmtree(tmp, ignore_errors=True)   # 清理失败不影响产物


# ─── stylegrid（混编画风质检：N 镜首帧并排拼图）──────────────
def cmd_stylegrid(args) -> None:
    """N 镜首帧并排拼一张大图——混编多模型时画风跳变一眼可见（纯 Pillow，0 API）。"""
    from PIL import Image, ImageDraw
    frames = sorted(Path(args.frames).glob("*.png"), key=natkey)[:args.max]
    if not frames:
        die(f"没有 *.png：{args.frames}")
    cell_w, label_h, pad = args.cell, 22, 4
    cells = []
    for p in frames:
        im = Image.open(p).convert("RGB")
        h = max(1, round(im.height * cell_w / im.width))
        cells.append((p.stem, im.resize((cell_w, h))))
    cell_h = max(c[1].height for c in cells) + label_h
    cols = min(args.cols, len(cells))
    rows = (len(cells) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (cell_w + pad) + pad,
                               rows * (cell_h + pad) + pad), (245, 245, 242))
    draw = ImageDraw.Draw(canvas)
    for idx, (name, im) in enumerate(cells):
        x = pad + (idx % cols) * (cell_w + pad)
        y = pad + (idx // cols) * (cell_h + pad)
        draw.text((x + 2, y + 2), name, fill=(40, 40, 40))
        canvas.paste(im, (x, y + label_h))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"[stylegrid] {len(cells)} 镜 -> {out}（{rows}行×{cols}列，画风跳变一眼可见）")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("concat")
    c.add_argument("clips", help="clips 目录")
    c.add_argument("--out", required=True)
    c.add_argument("--target-res", default="1280x720",
                   help="默认 1280x720（通用质量下限；Agnes 1088x832 源仅 1.18x 放大画质最好；1920x1080 可选但更软）")
    c.add_argument("--bgm", default="", help="可选 BGM 音频文件（自动循环铺满）")
    c.add_argument("--bgm-db", type=float, default=-18.0, help="BGM 音量 dB（默认 -18）")
    c.add_argument("--voice", default="", help="旁白音频（TTS 产出），与环境音/BGM 混音")
    c.add_argument("--voice-delay", type=float, default=1.0, help="旁白起始延迟秒")
    c.add_argument("--voice-db", type=float, default=0.0, help="旁白音量 dB（默认 0）")
    c.add_argument("--ambient-db", type=float, default=-10.0,
                   help="原片环境音音量 dB（有人声时压低，默认 -10）")
    c.add_argument("--subtitles", default="",
                   help="字幕 JSON [{at,dur,text,...}] 或 .srt（v4.2 起剪映/PR 导出的标准 srt 直接用）")
    c.add_argument("--subtitle-preset", default="",
                   help="字幕样式预设（v4.2）：news 新闻底框 / movie 电影底幕 / variety 综艺描边，"
                        "见 scripts/subtitle_presets.json；条目自带字段优先于预设")
    c.add_argument("--subtitle-render", default="ass", choices=["ass", "drawtext"],
                   help="字幕渲染通道：ass=libass（v4.15 默认，支持逐词高亮）/ "
                        "drawtext=旧通道（回滚用；结构上无法逐词高亮）")
    c.add_argument("--subtitle-font", default="",
                   help="ASS 字体**家族名**（默认 Microsoft YaHei）；自定义字体文件走 "
                        "_FFMPEG_FONT 环境变量（其所在目录会作为 fontsdir 传给 libass）")
    c.add_argument("--no-karaoke", action="store_true",
                   help="关掉逐词高亮（有词轴的 cue 也按普通字幕渲染）")
    c.add_argument("--slogan", default="", help="可选烧字幕 slogan（等价于追加一条末段字幕）")
    c.add_argument("--slogan-position", default="left", choices=["left", "bottom"],
                   help="left=左侧负空间垂直居中（中式落版默认）；bottom=底部居中")
    c.add_argument("--slogan-fade", type=float, default=0.8, help="落版淡入秒数")
    c.add_argument("--slogan-at", type=float, default=-1,
                   help="落版出现时刻（秒）；默认 -1 = 距结尾 4 秒自动淡入")
    c.add_argument("--xfade", default="0",
                   help="转场秒数：单值（0.5=全部）或逗号列表逐转场指定（如 0.5,0.5,1.0，len=镜数-1）；0=直切")
    c.add_argument("--qsv", action="store_true",
                    help="用核显 h264_qsv 编最终成片（本机实测可用；不可用自动回退 libx264）")
    c.add_argument("--voice-speed", type=float, default=1.0,
                    help="旁白整体变速（rubberband，变速不变调）；**有字幕时禁用**（字幕轴会失效）")
    c.add_argument("--freeze-last", type=float, default=0.0,
                   help="末帧定格秒数（tpad clone，落版余韵用，建议 4-6）")

    ck = sub.add_parser("check")
    ck.add_argument("path")
    ck.add_argument("--min-res", default="1280x720", help="最低分辨率（默认 1280x720，通用质量下限）")
    ck.add_argument("--max-duration", type=float, default=120.0, help="最大时长秒（默认 120，通用上限）")
    ck.add_argument("--min-fps", type=float, default=24.0, help="最低帧率（默认 24）")

    qg = sub.add_parser("qcgate", help="单段 QC 硬门禁：规格+黑帧/过曝/静帧机器判定（#4）")
    qg.add_argument("--ref", default="",
                    help="参考视频/图：额外做 ssim 对比（判画质劣化，如 kenburns 输出 vs 源图）")
    qg.add_argument("--min-ssim", type=float, default=0.95,
                    help="ssim 下限（默认 0.95；低于此判 FAIL）")
    qg.add_argument("path")
    qg.add_argument("--min-res", default="1280x720")
    qg.add_argument("--max-duration", type=float, default=120.0)
    qg.add_argument("--min-fps", type=float, default=24.0)
    qg.add_argument("--frames", type=int, default=6, help="抽帧数（默认 6，沿时长均布）")
    qg.add_argument("--black-th", type=float, default=12.0, help="近黑帧亮度阈值（灰度均值 0-255）")
    qg.add_argument("--white-th", type=float, default=243.0, help="过曝帧亮度阈值")
    qg.add_argument("--motion-th", type=float, default=0.4, help="静帧运动量阈值（相邻帧均值差）")
    qg.add_argument("--strict", action="store_true", help="WARN 也判 FAIL（exit 2）")

    qs = sub.add_parser("qcseq", help="跨镜首帧一致性粗检：HSV 直方图相邻对比（WARN 报告，不拦流程）")
    qs.add_argument("clips", nargs="+",
                    help="成片列表（≥2），或含 clip_*.{mp4,webp} 的目录（自然排序即镜序）")
    qs.add_argument("--threshold", type=float, default=0.45,
                    help="Bhattacharyya 系数 WARN 阈值（低于则报色调跳变，默认 0.45）")

    e = sub.add_parser("extract")
    e.add_argument("video")
    e.add_argument("out")

    st = sub.add_parser("stabilize", help="两遍防抖 vidstab：detect→transform（手持抖动/生成漂移）")
    st.add_argument("input")
    st.add_argument("--out", default="")
    st.add_argument("--smoothing", type=int, default=10,
                    help="平滑帧数（越大越稳，但可能裁切更多）")
    st.add_argument("--shakiness", type=int, default=5, help="抖动强度 1-10")
    st.add_argument("--qsv", action="store_true", help="核显编码（不可用自动回退）")
    kb = sub.add_parser("kenburns")
    kb.add_argument("image")
    kb.add_argument("out")
    kb.add_argument("--duration", type=float, default=5.0)
    kb.add_argument("--zoom", default="0.0008", help="每帧 zoom 增量（0.0008≈5s 推近 10%）")

    kba = sub.add_parser("kenburns-all", help="stills 档批量：目录图片逐张转缓推片段（0 API，断点续跑）")
    kba.add_argument("dir", help="图片目录（*.png/*.jpg，文件名排序即镜序）")
    kba.add_argument("--outdir", default="clips", help="片段输出目录（默认 clips/）")
    kba.add_argument("--duration", default="5",
                     help="每镜秒数；逗号列表循环取（如 5,4,6 → 1/4/7 镜 5s，2/5/8 镜 4s…）")
    kba.add_argument("--zoom", default="0.0008")

    p2m = sub.add_parser("webp2mp4", help="动画 webp → mp4（Pillow 解帧，绕过 LTX 损坏 Exif）")
    p2m.add_argument("src", help=".webp 文件或含 *.webp 的目录")
    p2m.add_argument("dst", nargs="?", help="输出 mp4（单文件模式；目录模式用 --outdir）")
    p2m.add_argument("--outdir", help="目录模式输出目录（默认原地同名 .mp4）")
    p2m.add_argument("--fps", type=float, default=None, help="强制帧率（默认读 webp 原生帧时长）")

    sg = sub.add_parser("stylegrid", help="N 镜首帧并排拼图（混编画风跳变一眼可见，0 API）")
    sg.add_argument("frames", help="frames 目录（*.png，文件名排序即镜序）")
    sg.add_argument("--out", default="style_grid.png")
    sg.add_argument("--cols", type=int, default=5)
    sg.add_argument("--cell", type=int, default=320, help="单格宽 px")
    sg.add_argument("--max", type=int, default=20, help="最多取前 N 张")

    pk = sub.add_parser("pick", help="选优半自动：候选图启发式打分排序（只参考，人拍板）")
    pk.add_argument("images", nargs="+", help="候选图路径（shot_XX_1.png ... 或通配符展开）")

    args = ap.parse_args()
    if args.cmd == "concat":
        cmd_concat(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "qcgate":
        cmd_qcgate(args)
    elif args.cmd == "qcseq":
        cmd_qcseq(args)
    elif args.cmd == "pick":
        cmd_pick(args)
    elif args.cmd == "extract":
        cmd_extract(args)
    elif args.cmd == "stabilize":
        cmd_stabilize(args)
    elif args.cmd == "kenburns":
        cmd_kenburns(args)
    elif args.cmd == "kenburns-all":
        cmd_kenburns_all(args)
    elif args.cmd == "webp2mp4":
        cmd_webp2mp4(args)
    elif args.cmd == "stylegrid":
        cmd_stylegrid(args)

if __name__ == "__main__":
    main()

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

from ffmpeg_probe import find_ffmpeg
from mg_core import PRODUCT_EXTS, natkey, run_capture

_ffmpeg_cache: dict[str, str] = {}
def _ffmpeg() -> str:
    if "exe" not in _ffmpeg_cache:
        _ffmpeg_cache["exe"] = find_ffmpeg()
    return _ffmpeg_cache["exe"]

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
    """drawtext 文本转义：\\ → \\\\，' → \\'，% → \\%（%{...} 是 drawtext 表达式语法，
    字幕含 \"100%\" 不转义会解析异常，#11）。顺序必须先 \\ 再 ' 最后 %。"""
    return str(text).replace("\\", "\\\\").replace("'", "\\'").replace("%", "\\%")

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


def cmd_concat(args) -> None:
    clips_dir = Path(args.clips)
    clips = _collect_clips(clips_dir)
    # 混合 provider 提示：不同 provider 输出分辨率不同（如 Agnes 1088x832 vs 智谱 1920x1080），
    # 统一缩放至 --target-res 时可能引入轻微画质/比例差异，提示人工确认。
    # 签名含 codec：同分辨率不同编码（h264 vs vp9）直拼同样会花屏（#4）
    res_set = {(probe(str(c)).get("width"), probe(str(c)).get("height"),
                probe(str(c)).get("codec")) for c in clips}
    # .webp 动图进 concat demuxer 不可靠（时长/时间基解析异常）→ 一律走重编码分支
    need_recode = any(c.suffix.lower() != ".mp4" for c in clips)
    if need_recode:
        print(f"[postprocess] 检测到非 mp4 分片（如 .webp 动图），强制重编码拼接",
              file=sys.stderr)
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
        total_len = probe(str(clips[0])).get("duration", 5.0)   # L_0
        for i in range(1, len(clips)):
            dur = probe(str(clips[i])).get("duration", 5.0)
            d_k = durs[i - 1]
            offset = total_len - d_k
            label = f"x{i}"
            fc.append(f"[{prev}][f{i}]xfade=transition=fade:duration={d_k}:offset={offset:.3f}[{label}]")
            prev = label
            total_len = offset + dur                            # L_i
        # 音频链：acrossfade 与视频 xfade 逐段对齐（前提：各 clip 音频时长≈视频时长）
        has_audio = all(probe(str(c)).get("audio") for c in clips)
        if has_audio:
            for i in range(len(clips)):
                fc.append(f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
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
            # 音轨：写死 a=0 会静默丢掉环境音（xfade 分支是保音轨的，两分支必须对称）
            has_audio = all(probe(str(c)).get("audio") for c in clips)
            if has_audio:
                for i in range(len(clips)):
                    fc.append(f"[{i}:a]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
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
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
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
        if probe(str(norm)).get("audio"):
            # 原始环境音：默认压低垫底（-10dB），不抢人声
            fc.append(f"[0:a]aresample=48000,volume={args.ambient_db}dB[amb]")
            srcs.append("[amb]")
        if voice_idx is not None:
            d = int(round(args.voice_delay * 1000))
            # apad：旁白结束后补静音，避免 amix 在某路输入结束时抬升其余音轨电平
            fc.append(f"[{voice_idx}:a]aresample=48000,adelay={d}|{d},"
                      f"volume={args.voice_db}dB,apad[vo]")
            srcs.append("[vo]")
        if bgm_idx is not None:
            fc.append(f"[{bgm_idx}:a]aresample=48000,volume={args.bgm_db}dB[bm]")
            srcs.append("[bm]")
        if srcs:
            # normalize=0：各路音量已用 volume= 显式指定，避免某路结束时 amix
            # 自动重新归一化导致音量突跳
            fc.append("".join(srcs) +
                      f"amix=inputs={len(srcs)}:duration=first:dropout_transition=0"
                      f":normalize=0[mix]")
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
        subs = json.loads(sp.read_text(encoding="utf-8"))
        if not isinstance(subs, list):
            die("字幕 JSON 需为数组 [{at,dur,text,...}]")
    if args.slogan:
        total = probe(str(norm)).get("duration", 0) or 0
        at = args.slogan_at if args.slogan_at >= 0 else max(0.0, total - 4.0)
        subs.append({"at": at, "dur": 0, "text": args.slogan,
                     "pos": args.slogan_position, "size": 64,
                     "fade": args.slogan_fade})

    if subs:
        out_tmp = clips_dir / "_with_text.mp4"
        font = find_font()
        font_escaped = font.replace("\\", "/").replace(":", "\\:")
        draws: list[str] = []
        for i, s in enumerate(subs):
            text = _escape_drawtext(str(s.get("text", "")))
            at = float(s.get("at", 0))
            dur = float(s.get("dur", 0))
            fade = float(s.get("fade", 0.6))
            size = int(s.get("size", 48))
            pos = s.get("pos", "bottom")
            if pos == "center":
                xy = "x=(w-text_w)/2:y=(h-text_h)/2"
            elif pos == "left":
                xy = "x=70:y=(h-text_h)/2"
            else:
                xy = f"x=(w-text_w)/2:y=h-{80 + size}"
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
                f"drawtext=text='{text}':fontfile='{font_escaped}':"
                f"fontcolor=white:fontsize={size}:{xy}:"
                f"shadowcolor=black@0.7:shadowx=3:shadowy=3:alpha='{alpha}'"
            )
        txt_cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(norm),
                   "-vf", ",".join(draws),
                   "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        # 注意：一旦显式 -map，就必须把视频流也写上，否则会输出纯音轨
        txt_cmd += ["-map", "0:v"]
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
    for p in (merged, listfile, out_tmp):
        try:
            if p is not None and p.exists() and p.resolve() != final.resolve():
                p.unlink()
        except OSError:
            pass
    print(f"[postprocess] -> {final}")
    print(json.dumps(probe(str(final)), indent=2))

# ─── 自检 ─────────────────────────────────────────────────
def cmd_check(args) -> None:
    path = args.path
    info = probe(path)
    print(json.dumps(info, indent=2))
    # 自检阈值可配置（默认 = 通用平台质量下限；目标平台/赛事不同时用 --min-res/--max-duration/--min-fps 覆盖）
    min_w, min_h = (int(x) for x in args.min_res.split("x"))
    rules = [
        (f"resolution >= {args.min_res}",
         info.get("width", 0) >= min_w and info.get("height", 0) >= min_h),
        (f"duration <= {args.max_duration}s",
         info.get("duration", 0) <= args.max_duration),
        (f"fps >= {args.min_fps}",
         info.get("fps", 0) >= args.min_fps - 0.1),
    ]
    for name, ok in rules:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    if not all(ok for _, ok in rules):
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
    if args.max_duration and info.get("duration", 0) > args.max_duration:
        add("FAIL", f"时长 {info.get('duration')}s > {args.max_duration}s")
    if info.get("fps", 0) < args.min_fps - 0.1:
        add("FAIL", f"帧率 {info.get('fps')} < {args.min_fps}")

    # ② 抽帧机器判定
    dur = info.get("duration", 5.0)
    n = args.frames
    times = [dur * i / (n - 1) if n > 1 else dur / 2 for i in range(n)]
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
    已存在的片段跳过（断点续跑，可反复重跑）。"""
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
                   help="字幕 JSON：[{at,dur,text,pos:bottom|center|left,size,fade}]")
    c.add_argument("--slogan", default="", help="可选烧字幕 slogan（等价于追加一条末段字幕）")
    c.add_argument("--slogan-position", default="left", choices=["left", "bottom"],
                   help="left=左侧负空间垂直居中（中式落版默认）；bottom=底部居中")
    c.add_argument("--slogan-fade", type=float, default=0.8, help="落版淡入秒数")
    c.add_argument("--slogan-at", type=float, default=-1,
                   help="落版出现时刻（秒）；默认 -1 = 距结尾 4 秒自动淡入")
    c.add_argument("--xfade", default="0",
                   help="转场秒数：单值（0.5=全部）或逗号列表逐转场指定（如 0.5,0.5,1.0，len=镜数-1）；0=直切")
    c.add_argument("--freeze-last", type=float, default=0.0,
                   help="末帧定格秒数（tpad clone，落版余韵用，建议 4-6）")

    ck = sub.add_parser("check")
    ck.add_argument("path")
    ck.add_argument("--min-res", default="1280x720", help="最低分辨率（默认 1280x720，通用质量下限）")
    ck.add_argument("--max-duration", type=float, default=120.0, help="最大时长秒（默认 120，通用上限）")
    ck.add_argument("--min-fps", type=float, default=24.0, help="最低帧率（默认 24）")

    qg = sub.add_parser("qcgate", help="单段 QC 硬门禁：规格+黑帧/过曝/静帧机器判定（#4）")
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

    args = ap.parse_args()
    if args.cmd == "concat":
        cmd_concat(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "qcgate":
        cmd_qcgate(args)
    elif args.cmd == "qcseq":
        cmd_qcseq(args)
    elif args.cmd == "extract":
        cmd_extract(args)
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
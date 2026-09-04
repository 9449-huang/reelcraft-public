#!/usr/bin/env python3
"""postprocess.py — 后期统一（比赛片规格）+ 自检。

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
import os
import re
import subprocess
import sys
from pathlib import Path

from ffmpeg_probe import find_ffmpeg

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
    优先级：_ffmpeg()_FONT 环境变量 > 楷体（书法感，中式落版首选）> 雅黑 > 黑体 > 宋体。
    """
    candidates = [
        os.environ.get("_ffmpeg()_FONT", ""),
        "C:/Windows/Fonts/simkai.ttf",   # 楷体
        "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
        "C:/Windows/Fonts/msyhbd.ttc",   # 雅黑粗体
        "C:/Windows/Fonts/simhei.ttf",   # 黑体
        "C:/Windows/Fonts/simsun.ttc",   # 宋体
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    die("未找到中文字体（simkai/msyh/simhei/simsun 均缺失）。请设置环境变量 _ffmpeg()_FONT 指向任一 .ttf/.ttc", 2)
    return ""  # unreachable

def probe(path: str) -> dict:
    """ffprobe 等价的简化：跑 ffmpeg -i，提取第一行 Duration 与 Stream 行。"""
    out = subprocess.run([_ffmpeg(), "-i", path], capture_output=True, text=True).stderr
    info = {}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", out)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", out)
    if m:
        info["bitrate_kbps"] = int(m.group(1))
    if re.search(r"Audio:", out):
        info["audio"] = True
    return info

# ─── 拼接 + 后期统一 ──────────────────────────────────────
def cmd_concat(args) -> None:
    clips_dir = Path(args.clips)
    clips = sorted(clips_dir.glob("clip_*.mp4"))
    if not clips:
        die(f"未在 {clips_dir} 找到 clip_*.mp4")
    # 混合 provider 提示：不同 provider 输出分辨率不同（如 Agnes 1088x832 vs 智谱 1920x1080），
    # 统一缩放至 --target-res 时可能引入轻微画质/比例差异，提示人工确认。
    res_set = {(probe(str(c)).get("width"), probe(str(c)).get("height")) for c in clips}
    if len(res_set) > 1:
        print(f"[postprocess] ⚠ 输入的 clip 分辨率不一致 {sorted(res_set)}，"
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
        # 直切：先 concat（-c copy 保持速度）
        listfile = clips_dir / "_concat.txt"
        listfile.write_text("\n".join(f"file '{c.name}'" for c in clips), encoding="utf-8")
        run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c", "copy", str(merged)])

    # 第二步：统一分辨率/帧率/编码（比赛要求 H.264 yuv420p ≥1280x720）
    # Agnes video 实测输出 1088x832（4:3 偏方），用 increase+crop 填满裁切
    # （不要用 decrease+pad：会出黑边，比赛片观感差）
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
            text = str(s.get("text", "")).replace("\\", "\\\\").replace("'", "\\'")
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
    # 自检阈值可配置（默认对齐黄山杯公益/商业赛道通用下限；其它赛事按需调整）
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
    imgs = sorted(set(imgs))
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

def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("concat")
    c.add_argument("clips", help="clips 目录")
    c.add_argument("--out", required=True)
    c.add_argument("--target-res", default="1280x720",
                   help="默认 1280x720（比赛硬指标下限，Agnes 1088x832 源仅 1.18x 放大画质最好；1920x1080 可选但更软）")
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
    ck.add_argument("--min-res", default="1280x720", help="最低分辨率（默认 1280x720，黄山杯下限）")
    ck.add_argument("--max-duration", type=float, default=120.0, help="最大时长秒（默认 120，黄山杯下限）")
    ck.add_argument("--min-fps", type=float, default=24.0, help="最低帧率（默认 24）")

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

    args = ap.parse_args()
    if args.cmd == "concat":
        cmd_concat(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "extract":
        cmd_extract(args)
    elif args.cmd == "kenburns":
        cmd_kenburns(args)
    elif args.cmd == "kenburns-all":
        cmd_kenburns_all(args)

if __name__ == "__main__":
    main()
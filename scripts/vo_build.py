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

from ffmpeg_probe import find_ffmpeg

_ffmpeg_cache: dict[str, str] = {}
def _ffmpeg() -> str:
    if "exe" not in _ffmpeg_cache:
        _ffmpeg_cache["exe"] = find_ffmpeg()
    return _ffmpeg_cache["exe"]
MEDIA_GEN = str(Path(__file__).resolve().parent / "media_gen.py")


def die(msg: str, code: int = 1) -> None:
    print(f"[vo_build] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def probe(path: str | Path) -> float:
    """返回音频/视频时长秒。"""
    out = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def tts_line(text: str, out: Path, voice: str, speed: float) -> None:
    cmd = [sys.executable, MEDIA_GEN, "tts", "--text", text, "--out", str(out)]
    if voice:
        cmd += ["--voice", voice]
    if speed and speed != 1.0:
        cmd += ["--speed", str(speed)]
    rc = subprocess.call(cmd)
    if rc != 0:
        die(f"TTS 失败：{out.name}（rc={rc}）。若已自录，加 --skip-tts 跳过合成", 3)


def main() -> None:
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
    ap.add_argument("--subs-out", default="", help="字幕输出路径（默认与 --out 同级 subtitles_final.json）")
    args = ap.parse_args()

    src = Path(args.lines)
    if not src.exists():
        die(f"找不到 {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    acts = data.get("acts", [])
    lines = data.get("lines", [])
    if not lines:
        die("vo_lines.json 的 lines 为空")

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
        f = lines_dir / f"{lid}.mp3"
        if not f.exists():
            f = lines_dir / f"{lid}.wav"
        if args.skip_tts:
            if not f.exists():
                die(f"--skip-tts 但缺少录音 {lines_dir}/{lid}.mp3|wav", 2)
        else:
            if not f.exists():
                tts_line(ln["text"], lines_dir / f"{lid}.mp3", args.voice, args.speed)
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

    # ③ 精确拼接：静音底(总长) + 各句 adelay 落位 + amix
    inputs: list[str] = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    for ln in lines:
        inputs += ["-i", ln["_file"]]
    fc = [f"[0:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS[base]"]
    labels = ["[base]"]
    for i, ln in enumerate(lines, start=1):
        ms = int(round(ln["at"] * 1000))
        lab = f"l{i}"
        fc.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms},apad,"
                  f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS[{lab}]")
        labels.append(f"[{lab}]")
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

    # ④ 输出精确字幕轴
    subs = []
    for a in acts:
        subs.append({"at": a["at"], "dur": a.get("dur", 2.6),
                     "text": a["text"], "pos": a.get("pos", "center"),
                     "size": a.get("size", 54), "fade": a.get("fade", 0.8)})
    for ln in lines:
        subs.append({"at": round(ln["at"], 2), "dur": round(ln["_dur"], 2),
                     "text": ln["text"], "pos": "bottom", "size": 44, "fade": 0.35})
    subs_path = Path(args.subs_out) if args.subs_out else out.parent / "subtitles_final.json"
    subs_path.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[vo_build] -> {subs_path}  ({len(subs)} 条，与朗读精确对齐)")

    # ⑤ 摘要
    voiced = sum(ln["_dur"] for ln in lines)
    print(f"[vo_build] 有声 {voiced:.1f}s / 总长 {total:.1f}s "
          f"（留白 {total - voiced:.1f}s，占比 {(total-voiced)/total*100:.0f}%）")


if __name__ == "__main__":
    main()

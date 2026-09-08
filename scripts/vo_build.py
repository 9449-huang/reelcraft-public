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


def cmd_fit(args) -> None:
    """fit 子命令：读 vo_lines.json + clips/ 目录逐镜 probe，打印对账表。"""
    data = json.loads(Path(args.lines).read_text(encoding="utf-8"))
    clips_dir = Path(args.clips)
    if not clips_dir.is_dir():
        die(f"clips 目录不存在：{clips_dir}", 2)
    clip_durs = {}
    for ext in (".mp4", ".webp"):
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
    # 双模式：首参 fit → 对账子命令；否则走原 VO 构建流程（向后兼容）
    if len(sys.argv) > 1 and sys.argv[1] == "fit":
        ap = argparse.ArgumentParser(prog="vo_build.py fit",
                                     description="VO 时间轴 vs 镜头时长对账（TTS 后跑最精确）")
        ap.add_argument("lines", help="vo_lines.json")
        ap.add_argument("clips", help="clips/ 目录（clip_*.mp4/webp）")
        ap.add_argument("--gap", type=float, default=0.3)
        cmd_fit(ap.parse_args(sys.argv[2:]))
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
            if not f.exists():
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

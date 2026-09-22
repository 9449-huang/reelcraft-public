#!/usr/bin/env python3
"""delogo_watermark.py — 去除视频固定位置角标水印。

用法:
  python delogo_watermark.py final.mp4 --provider <channel>  # 按 watermark_profiles.json 实测框自动缩放
  python delogo_watermark.py in.mp4 --dry-run             # 只出标注框放大自检图，确认框位（不抹除）
  python delogo_watermark.py in.mp4 --x 1100 --y 600 --w 60 --h 60   # 手动指定框

原理: ffmpeg delogo 滤镜对矩形区域做边缘插值修复，适合**小而静态**的角标水印；
动态/大面积水印档案记 fatal，换渠道或上视频修复模型（如 ProPainter），本脚本不适用。

输出: <stem>_nowm.mp4 + <stem>_nowm_check.png（处理后 3x 自检图，务必目检）
      --dry-run 时输出 <stem>_wmdry.png（红框标注 3x 图，用于确认框位）
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from ffmpeg_probe import find_ffmpeg

# 内置兜底框：可选。设为 None 则必须用 --provider 档案或 --x/y/w/h 手动指定
BASE_W, BASE_H = 1280, 720
BASE_BOX = None  # (x, y, w, h) @ BASE_W x BASE_H

PROFILES = Path(__file__).resolve().parent / "watermark_profiles.json"


def die(msg: str, code: int = 1) -> None:
    """与其他脚本同形（v4.11.0）：显式整数退出码 + 前缀打 stderr。

    原实现是 `sys.exit(msg)`——传字符串走 Python 隐式行为（打印 + rc=1），
    看着能用，但 `die(2)` 会静默变成"退出码 2 且什么都不打印"，且无前缀难 grep。
    """
    print(f"[delogo] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def mask_filter(x: int, y: int, w: int, h: int) -> str:
    """生成 removelogo 用的 mask 图（黑底 + 白框）的 ffmpeg 滤镜串（纯函数可单测）。

    removelogo 的语义是"mask 上非黑处为 logo"——白色实心块即声明该矩形永远是角标。
    """
    return f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white@1:t=fill"


def removelogo_filter(mask_path) -> str:
    """`removelogo` 滤镜串（v4.14 去水印 v2）。

    ★ 路径必须**转义冒号 + 套单引号**：实测（2026-09-22，ffmpeg 7.1）裸 Windows 路径
    与"只套引号但不转义冒号"两种写法都会让 filter 解析器直接崩（rc=4294967274）。
    """
    esc = str(mask_path).replace("\\", "/").replace(":", r"\:")
    return f"removelogo=filename='{esc}'"


def load_profile(provider: str) -> dict:
    if not PROFILES.exists():
        die(f"档案不存在: {PROFILES.name}")
    data = json.loads(PROFILES.read_text(encoding="utf-8"))
    p = data.get(provider)
    if not isinstance(p, dict):
        die(f"档案无 '{provider}' 条目。先按 SKILL.md Step 1 问③ probe 目检，确认后回写 {PROFILES.name}")
    kind = p.get("kind", "unknown")
    if kind == "clean":
        die(f"档案标记 {provider} 无水印，无需抹除（{p.get('note', '')}）")
    if kind == "fatal":
        die(f"档案标记 {provider} 水印不可 delogo（{p.get('note', '')}）→ 换渠道或上视频修复模型")
    if kind != "corner-delogo" or not p.get("box"):
        die(f"档案 '{provider}' kind={kind}，缺有效 box，请先 probe 确认框位并回写档案")
    return p


def parse_size(stderr: str) -> tuple[int, int] | None:
    """从 `ffmpeg -i` 的 stderr 取**真实视频流**分辨率（纯函数可单测）。

    v4.8.0 修：带封面图的 mp4 会先列出 mjpeg 缩略图（如 320x240），原来
    `re.search` 取**首个**匹配 → 框位按缩略图分辨率算错（已实证）。这里逐行扫、
    跳过 mjpeg 流。"""
    for line in stderr.splitlines():
        if "Video:" not in line or "mjpeg" in line.lower():
            continue
        mm = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if mm:
            return int(mm.group(1)), int(mm.group(2))
    return None


def clamp_box(x, y, w, h, W, H) -> tuple[int, int, int, int]:
    """把水印框钳制到画面内（**每维独立**，保 1px 边）。

    v4.8.0 修：原实现 `if x+w>=W or y+h>=H` 一刀切 + 钳后不校验不变式——
    框比画面还大时 `min(x, W-w-1)` 得负值再被 `max(1, …)` 抬回 1，框仍越界，
    却打印"已钳制"（假象）。这里先拒绝放不下的框，再逐维钳制。"""
    if w >= W - 2 or h >= H - 2:
        die(f"水印框 {w}x{h} 相对画面 {W}x{H} 太大（每维至少留 2px）——"
            f"检查 --w/--h 或档案里的 base_res")
    return max(1, min(int(x), W - w - 1)), max(1, min(int(y), H - h - 1)), w, h


def probe_size(ff: str, src: Path) -> tuple[int, int]:
    """从 `ffmpeg -i` 的 stderr 解析视频分辨率。"""
    r = subprocess.run([ff, "-i", str(src)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    size = parse_size(r.stderr)
    if not size:
        die("无法从 ffmpeg 输出解析分辨率，请用 --x/--y/--w/--h 手动指定")
    return size


def scale_box(box, bw, bh, W, H):
    """框坐标按实际分辨率等比缩放（相对 base_res）。"""
    if (W, H) == (bw, bh):
        return box
    return (round(box[0] * W / bw), round(box[1] * H / bh),
            max(8, round(box[2] * W / bw)), max(8, round(box[3] * H / bh)))


def main() -> None:
    ap = argparse.ArgumentParser(description="去固定角标水印（delogo，档案驱动）")
    ap.add_argument("input", help="输入视频")
    ap.add_argument("--out", default="", help="输出路径（默认 <stem>_nowm.mp4）")
    ap.add_argument("--provider", default="", help="渠道名：读 watermark_profiles.json 的实测框（渠道名，如 agnes）")
    ap.add_argument("--dry-run", action="store_true", help="不抹除，只出红框标注 3x 自检图确认框位")
    ap.add_argument("--x", type=int, help="水印框左上角 x（覆盖档案/默认）")
    ap.add_argument("--y", type=int, help="水印框左上角 y")
    ap.add_argument("--w", type=int, help="水印框宽")
    ap.add_argument("--h", type=int, help="水印框高")
    ap.add_argument("--crf", type=int, default=18, help="重编码质量（默认 18，近无损）")
    ap.add_argument("--mode", choices=["delogo", "mask"], default="delogo",
                    help="delogo=边缘插值（默认，兼容旧行为）；mask=removelogo，按框生成 "
                         "mask 图声明\"这里永远是 logo\"（固定角标时序一致，不逐帧抖动）")
    ap.add_argument("--keep-mask", default="", help="mask 模式：把生成的 mask 图另存到该路径")
    a = ap.parse_args()

    ff = find_ffmpeg()
    src = Path(a.input)
    if not src.exists():
        die(f"输入不存在: {src}")
    W, H = probe_size(ff, src)

    # 框来源优先级：手动 --x/y/w/h > --provider 档案 > 内置默认（若设置）
    if a.provider:
        p = load_profile(a.provider)
        pbw, pbh = p.get("base_res", [BASE_W, BASE_H])
        bx, by, bw, bh = scale_box(tuple(p["box"]), pbw, pbh, W, H)
        src_label = f"档案[{a.provider}]"
    elif BASE_BOX is not None:
        bx, by, bw, bh = scale_box(BASE_BOX, BASE_W, BASE_H, W, H)
        src_label = "内置默认"
    else:
        bx, by, bw, bh = 0, 0, 0, 0
        src_label = "手动指定"
    x = a.x if a.x is not None else bx
    y = a.y if a.y is not None else by
    w = a.w if a.w is not None else bw
    h = a.h if a.h is not None else bh

    if src_label == "手动指定" and not all(v is not None for v in (a.x, a.y, a.w, a.h)):
        die("无档案且无内置默认框：请提供 --provider 或完整的 --x/--y/--w/--h")

    # delogo 硬约束：框须完全在画面内且至少留 1px 边
    if w < 8 or h < 8:
        die("水印框太小（至少 8x8）")
    _nx, _ny, w, h = clamp_box(x, y, w, h, W, H)
    if (_nx, _ny) != (x, y):
        print(f"[warn] 框超出画面，已钳制到 ({_nx},{_ny},{w},{h})")
    x, y = _nx, _ny

    # 自检图公共参数：水印区外扩 3 倍、3x 放大
    cx, cy = max(0, x - w * 3), max(0, y - h * 3)
    cw, ch = min(W - cx, w * 7), min(H - cy, h * 7)

    if a.dry_run:
        out_png = src.with_name(src.stem + "_wmdry.png")
        vf = (f"drawbox=x={x}:y={y}:w={w}:h={h}:color=red:t=3,"
              f"crop={cw}:{ch}:{cx}:{cy},scale={cw * 3}:{ch * 3}:flags=neighbor")
        subprocess.run([ff, "-y", "-v", "error", "-ss", "1", "-i", str(src),
                        "-vf", vf, "-frames:v", "1", str(out_png)], check=True)
        print(f"[dry-run] {src.name}: {W}x{H} 框=({x},{y},{w},{h}) 框源={src_label}")
        print(f"[dry-run] 红框标注图 {out_png.name} —— 目检框是否罩准水印（不罩准就调 --x/--y/--w/--h）")
        return

    out = Path(a.out) if a.out else src.with_name(src.stem + "_nowm.mp4")
    print(f"[delogo] {src.name}: {W}x{H} 框=({x},{y},{w},{h}) 框源={src_label} "
          f"模式={a.mode} -> {out.name}")

    def _run_ff(args: list) -> None:
        """跑 ffmpeg 并显式报错（原实现用 check=True 抛裸异常）。"""
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            last = (r.stderr or "").strip().splitlines()
            die(f"ffmpeg 失败 rc={r.returncode}: {(last[-1][:200] if last else '')}")

    enc_tail = ["-c:v", "libx264", "-crf", str(a.crf), "-preset", "slow",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"]

    if a.mode == "mask":
        import tempfile
        with tempfile.TemporaryDirectory(prefix="rc_mask_") as _td:
            mask_png = Path(a.keep_mask) if a.keep_mask else Path(_td) / "logo_mask.png"
            _run_ff([ff, "-y", "-v", "error", "-f", "lavfi",
                     "-i", f"color=c=black:s={W}x{H}", "-vf", mask_filter(x, y, w, h),
                     "-frames:v", "1", str(mask_png)])
            _run_ff([ff, "-y", "-v", "error", "-i", str(src),
                     "-vf", removelogo_filter(mask_png), *enc_tail, str(out)])
            if a.keep_mask:
                print(f"[mask] 已留存 mask 图 {mask_png}")
    else:
        _run_ff([ff, "-y", "-v", "error", "-i", str(src),
                 "-vf", f"delogo=x={x}:y={y}:w={w}:h={h}", *enc_tail, str(out)])

    check = out.with_name(out.stem + "_check.png")
    subprocess.run(
        [ff, "-y", "-v", "error", "-ss", "1", "-i", str(out),
         "-vf", f"crop={cw}:{ch}:{cx}:{cy},scale={cw * 3}:{ch * 3}:flags=neighbor",
         "-frames:v", "1", str(check)],
        check=True)
    print(f"[done] 已输出 {out}，自检图 {check.name}（请目检水印区有无残影/模糊斑）")
    if not a.provider and (a.x is None):
        print("[hint] 若这是新渠道实测框，请回写 scripts/watermark_profiles.json，同渠道永久免测")


if __name__ == "__main__":
    main()

"""reelcraft 查询/质检命令：status、plan-check、qc 抽帧、last-frame。"""
from __future__ import annotations
from mg_core import (
    KEY_ENV_FILE,
    PROVIDERS,
    _ffmpeg,
    _load_state,
    die,
    key_mask,
    list_keys,
)
import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ffmpeg_probe import find_ffmpeg

def cmd_last_frame(args) -> None:
    """ffmpeg 抽取视频最后一帧 PNG，作为下一镜的首帧。"""
    ffmpeg = _ffmpeg()
    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    # -sseof -0.05：从末尾 0.05s 处取一帧（最末帧）
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-sseof", "-0.05", "-i", args.video, "-frames:v", "1", out]
    rc = os.system(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    if rc != 0:
        die(f"ffmpeg 抽末帧失败 rc={rc}", 4)
    print(f"[media_gen] last-frame -> {out}")
def cmd_qc(args) -> None:
    """从视频抽首/中/尾 3 帧供 Read 工具视觉验收。
    用法：media_gen.py qc clip_01.mp4 qc_frames/
    """
    ffmpeg = _ffmpeg()
    src = Path(args.video)
    if not src.exists():
        die(f"视频不存在: {src}", 2)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    dur = 5.0
    # 用 ffmpeg 探测时长
    probe_out = subprocess.run([ffmpeg, "-i", str(src)], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", probe_out)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    name = src.stem
    # 首帧 / 中帧：-ss 定位
    for tag, t in [("first", 0.0), ("mid", dur / 2)]:
        out = outdir / f"{name}_{tag}.png"
        cmd = [ffmpeg, "-y", "-loglevel", "error",
               "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", str(out)]
        rc = subprocess.call(cmd)
        if rc != 0:
            die(f"抽帧失败 {tag} rc={rc}", 4)
    # 末帧：-sseof -0.05 从末尾取
    out = outdir / f"{name}_last.png"
    cmd = [ffmpeg, "-y", "-loglevel", "error",
           "-sseof", "-0.05", "-i", str(src), "-frames:v", "1", str(out)]
    rc = subprocess.call(cmd)
    if rc != 0:
        die(f"抽帧失败 last rc={rc}", 4)
    print(f"[media_gen] qc {src.name} -> {outdir}/ ({name}_first/mid/last.png)")
    print("[media_gen] 用 Read 查看 3 帧，对照 i2v_prompt 验收：运动方向/幅度/主体稳定性/有无崩坏")

# ─── 状态查看 ──────────────────────────────────────────────
# ─── status 能力探测（启发式，仅供参考）───────────────────
_PROBE_KW = {
    "image": ("image", "flux", "dall", "kolors", "seedream", "cogview",
              "qwen-image", "stable", "sd3", "sdxl", "irag", "hunyuan-image"),
    "video": ("video", "sora", "kling", "wan", "hunyuan-video",
              "cogvideo", "ltx", "mochi", "vidu"),
    "tts": ("tts", "speech", "audio", "cosyvoice", "voice", "spark"),
}

def _probe_models(base: str, key: str) -> dict | None:
    """GET /models 按模型名猜能力。注意：部分网关 /models 不鉴权/不过滤，
    结果**仅供参考**（猜的≠能用），最终以实跑为准。不可达返回 None。"""
    try:
        req = urllib.request.Request(f"{base}/models")
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=8) as r:
            js = json.loads(r.read().decode("utf-8", "ignore"))
        ids = [m.get("id", "") for m in js.get("data", []) if isinstance(m, dict)]
    except Exception:
        return None
    guess: dict[str, list[str]] = {}
    for role, kws in _PROBE_KW.items():
        hits = [i for i in ids if any(kw in i.lower() for kw in kws)]
        if hits:
            guess[role] = hits[:6]          # 每角色最多展示 6 个，防刷屏
    return {"total": len(ids), "guess": guess}

def cmd_status(args) -> None:
    state = _load_state() or {}
    print(json.dumps({"providers": {p: PROVIDERS[p]["label"] for p in PROVIDERS},
                      "cooldown": state.get("cooldown", {})}, indent=2, ensure_ascii=False))
    no_keys = []
    for p in PROVIDERS:
        # status 不应因某 provider 未配 key 而退出（list_keys 默认 required=True 会 die）
        keys = list_keys(p, required=False)
        if not keys:
            no_keys.append(p)
            continue
        for k in keys:
            extra = []
            # 冷却/黑名单可视：让"为什么这把 key 不参与"一眼可见
            cd_until = ((state.get("cooldown") or {}).get(p) or {}).get(str(k["n"]), 0)
            if cd_until > time.time():
                remain = int(cd_until - time.time())
                label = "blacklisted" if remain > 3600 else "cooldown"
                extra.append(f"{label}={remain}s" if label == "cooldown"
                             else f"blacklisted(≈{remain // 3600}h)")
            if k["roles"] != {"image", "video"}:
                extra.append("roles=" + ",".join(sorted(k["roles"])))
            if k.get("tier"):
                extra.append("tier=" + k["tier"])
            if k["image_model"]:
                extra.append(f"img={k['image_model']}")
            if k["video_model"]:
                extra.append(f"vid={k['video_model']}")
            if k["image_sizes"]:
                extra.append(f"sizes={len(k['image_sizes'])}项自填")
            suffix = f"  [{' '.join(extra)}]" if extra else ""
            print(f"  {p} key#{k['n']}: {key_mask(k['key'])}  base={k['base']}{suffix}")
    if no_keys:
        print(f"  {'/'.join(no_keys)}: 未配置 key（{KEY_ENV_FILE}）")
    if getattr(args, "no_probe", False):
        return
    print("  —— /models 能力探测（启发式猜的，仅供参考；能否真用以实跑为准）——")
    for p in PROVIDERS:
        for k in list_keys(p, required=False):
            r = _probe_models(k["base"], k["key"])
            if r is None:
                print(f"    {p} key#{k['n']}: 探测失败（/models 不可达，不一定影响使用）")
                continue
            if not r["guess"]:
                print(f"    {p} key#{k['n']}: 列出 {r['total']} 个模型，无生图/视频/TTS 关键字命中（可能纯文字模型）")
                continue
            parts = [f"{role}✅({','.join(ids)})" for role, ids in r["guess"].items()]
            missing = [role for role in ("image", "video", "tts") if role not in r["guess"]]
            tail = f"  未命中: {'/'.join(missing)}" if missing else ""
            print(f"    {p} key#{k['n']}: {'  '.join(parts)}{tail}")

# ─── plan 校验（字段拼错静默失效是坑）────────────────────
PLAN_KNOWN_KEYS = {"role_assign", "workers", "workers_image", "workers_video",
                   "watermark", "mode", "hero_shots", "video_pool_order", "tier_map"}

def cmd_plan_check(args) -> None:
    """校验 plan.json：未知键 warn（拼错会被静默忽略）、枚举值/类型检查。"""
    p = Path(args.plan)
    if not p.exists():
        die(f"plan 文件不存在: {p}", 2)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"plan.json 解析失败: {e}", 2)
    issues = 0
    for k in sorted(set(d) - PLAN_KNOWN_KEYS):
        print(f"[plan-check] [warn] 未知字段 {k!r}（拼错会静默失效；已知字段：{sorted(PLAN_KNOWN_KEYS)}）")
        issues += 1
    if d.get("mode") and d["mode"] not in ("full", "hybrid", "stills"):
        print(f"[plan-check] [warn] mode={d['mode']!r} 不在 full/hybrid/stills")
        issues += 1
    if d.get("role_assign") and d["role_assign"] not in ("one-stop", "split"):
        print(f"[plan-check] [warn] role_assign={d['role_assign']!r} 不在 one-stop/split")
        issues += 1
    for numf in ("workers", "workers_image", "workers_video"):
        if numf in d and (not isinstance(d[numf], int) or d[numf] < 1):
            print(f"[plan-check] [warn] {numf}={d[numf]!r} 应为正整数")
            issues += 1
    if isinstance(d.get("mode"), str) and "workers" in d and "workers_image" not in d:
        print("[plan-check] [hint] 旧字段 workers 建议迁移为 workers_image / workers_video 分列")
    print(f"[plan-check] {'发现问题 ' + str(issues) + ' 个' if issues else 'OK'}")
    sys.exit(1 if issues else 0)

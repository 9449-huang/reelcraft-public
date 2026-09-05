"""reelcraft 生成入口（薄壳）：单发 image/video/edit/tts 命令 + argparse dispatch。
共享逻辑在 mg_core，批量在 mg_batch，查询在 mg_status——CLI 用法不变。"""
from __future__ import annotations
from mg_core import (
    AllKeysFailed,
    PROVIDERS,
    _abs_url,
    _download_image,
    _final_out,
    _gen_image_once,
    _image_rpm_for,
    _insert_suffix,
    _poll_video_task,
    _resolve_async_task,
    _wait_existing_task,
    call_with_failover,
    die,
    http_call,
    image_to_url_or_path,
    key_mask,
    list_keys,
    pools_for_role,
    video_throttle,
)
from mg_batch import cmd_batch, cmd_harvest
from mg_status import cmd_status, cmd_qc, cmd_last_frame, cmd_plan_check
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

def cmd_image(args) -> None:
    size = args.size
    pools = [args.provider] if args.provider else pools_for_role("image")
    for p in pools:
        if p not in PROVIDERS:
            die(f"不支持 provider: {p}", 2)
    if not pools:
        die("没有支持 image 角色的可用池。检查 media_keys.env（MEDIA_*_1_KEY/_BASE/_ROLES）"
            "或 MEDIA_PRIORITY", 2)
    errs: list[str] = []

    # 批量选优：--count N 一次出 N 张（命名 shot_01_1.png ~ shot_01_N.png）
    count = max(1, args.count)
    rpm = _image_rpm_for(pools[0], size)

    outs: list[str] = []
    for i in range(count):
        out = _insert_suffix(args.out, "" if count == 1 else f"_{i+1}")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            print(f"[media_gen] 已存在 {out}，跳过", file=sys.stderr)
            outs.append(out)
            continue
        if i > 0:                     # 出图节流（按池 RPM；Agnes 1K=20RPM / 2K=10RPM）
            wait = (60.0 / rpm + 1.0) if rpm else 2.0
            print(f"[media_gen] 出图节流，等待 {wait:.0f}s（第 {i+1}/{count} 张）", file=sys.stderr)
            time.sleep(wait)
        resp, used_key = _gen_image_once(pools, args, size, errs)
        _pinfo = PROVIDERS[used_key["pool"]]["models"].get("image") or {}
        resp = _resolve_async_task(used_key, resp, _pinfo.get("task_poll_path", "/tasks"),
                                   out=out, pool=used_key["pool"])
        _download_image(resp, out, used_key.get("base", ""))
        print(f"[media_gen] image OK via {used_key['pool']} key#{used_key['n']} ({key_mask(used_key['key'])}) -> {out}")
        outs.append(out)
    if count > 1:
        print(f"[media_gen] 共 {len(outs)} 张候选，人工选优后进 i2v：\n  " + "\n  ".join(outs))
def cmd_video(args) -> None:
    if args.wait_task:
        return _wait_existing_task(args)
    pools = [args.provider] if args.provider else pools_for_role("video")
    for p in pools:
        if p not in PROVIDERS:
            die(f"不支持 provider: {p}", 2)
    if not pools:
        die("没有支持 video 角色的可用池。检查 media_keys.env（MEDIA_*_1_KEY/_BASE/_ROLES）"
            "或 MEDIA_PRIORITY", 2)

    # 各池预校验（显式指定时硬失败；自动路由时跳过不满足参数的池）
    valid: list[tuple[str, dict]] = []
    for pool in pools:
        info = PROVIDERS[pool]["models"].get("video")
        if not info:
            if args.provider:
                die(f"{pool} 不支持视频", 2)
            continue
        nf = args.num_frames
        if nf and info.get("supports_num_frames") and nf not in info.get("frame_choices", []):
            if args.provider:
                die(f"num_frames={nf} 不在白名单 {info['frame_choices']}", 2)
            continue
        if info.get("payload_style") == "zhipu":
            if args.video_size not in info["sizes"]:
                if args.provider:
                    die(f"{pool} 不支持 video size={args.video_size}。可选: {info['sizes']}", 2)
                continue
            if args.duration not in info["durations"]:
                if args.provider:
                    die(f"{pool} duration={args.duration} 不在 {info['durations']}", 2)
                continue
        valid.append((pool, info))
    if not valid:
        die("没有满足参数的视频池（检查 num_frames / --video-size / --duration）", 2)

    # 节流：pin 锁 key 时按 key 计时（多 worker 各走各的 1RPM），否则按池共享通道
    for pool, info in valid:
        rpm = info.get("rpm") or 1
        tag = f"{pool}_key{args.pin_key}" if args.pin_key else f"{pool}_shared"
        video_throttle(rpm, tag)

    # 断点续跑（已存在则跳过，不白等节流）
    out = args.out
    if os.path.exists(out) and os.path.getsize(out) > 0:
        print(f"[media_gen] 已存在 {out}，跳过生成", file=sys.stderr)
        return

    errs: list[str] = []
    resp = used_key = None
    for pool, info in valid:
        def call_fn(k: dict, _info=info, _prefix=PROVIDERS[pool]["key_env_prefix"]) -> dict:
            headers = {"Authorization": f"Bearer {k['key']}", "Content-Type": "application/json"}
            if _info.get("payload_style") == "zhipu":
                payload: dict[str, Any] = {
                    "model": k.get("video_model") or _info["default"],
                    "prompt": args.prompt,
                    "with_audio": False,
                    "fps": 30,
                    "size": args.video_size,
                    "duration": args.duration,
                }
                if args.image:
                    payload["image_url"] = image_to_url_or_path(args.image)
            else:
                model = k.get("video_model") or _info["default"]
                if not model:
                    die(f"该池未配置模型名（模板无默认值）。请在 env 加 {_prefix}{k['n']}_VIDEO_MODEL=模型名", 2)
                pfield = k.get("video_prompt_field") or "prompt"
                payload = {"model": model, pfield: args.prompt}
                if _info.get("default_duration"):
                    payload["duration"] = _info["default_duration"]
                if args.image:
                    _img = image_to_url_or_path(args.image)
                    payload[_info.get("image_param", "image")] = (
                        [_img] if _info.get("image_list") else _img)
                nf2 = args.num_frames
                if nf2 and _info.get("supports_num_frames"):
                    payload["num_frames"] = nf2
                if args.negative and _info.get("supports_negative"):
                    payload["negative_prompt"] = args.negative
            vpath = k.get("video_task_path") or k.get("task_path") or _info["task_path"]
            return http_call("POST", f"{k['base']}{vpath}", headers, payload, timeout=300)

        try:
            resp, used_key = call_with_failover(pool, call_fn, kind="video", pin_key=args.pin_key)
            used_key["pool"] = pool
            break
        except AllKeysFailed as e:
            errs.append(str(e))
            if args.provider:
                die(str(e), 3)
            print(f"[media_gen] {pool} 全部 key 失败，尝试下一池…", file=sys.stderr)
            continue
    if resp is None or used_key is None:
        die("所有可用池均失败:\n  " + "\n  ".join(errs), 3)
    provider = used_key["pool"]
    info = PROVIDERS[provider]["models"]["video"]
    video_id = resp.get("video_id") or resp.get("id") or resp.get("task_id")
    direct_url = resp.get("video_url") or resp.get("url")
    du = _abs_url(direct_url, used_key.get("base", ""))
    if isinstance(du, str) and du.startswith("http"):
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        out = _final_out(out, du)
        urllib.request.urlretrieve(du, out)
        print(f"[media_gen] video OK via {provider} key#{used_key['n']} -> {out}")
        return
    if not video_id:
        die(f"无 video_id: {json.dumps(resp)[:300]}")
    print(f"[media_gen] task_id={video_id}（受理成功，轮询中…）", file=sys.stderr)
    _poll_video_task(provider, info, used_key, str(video_id), out, args)

# ─── 图编辑（魔塔 Qwen-Image-Edit）──────────────────────
def cmd_edit(args) -> None:
    """首帧小改（移物/调光/局部重绘），不用整图重 roll。
    流程：POST /images/generations (X-ModelScope-Async-Mode) → task_id → 轮询 /tasks/{id}
    """
    provider = "modelscope"
    info = PROVIDERS[provider]["models"]["image_edit"]
    if not args.image or not Path(args.image).exists():
        die(f"输入图不存在: {args.image}", 2)

    keys = list_keys(provider)
    k = keys[0]

    def submit() -> dict:
        headers = {
            "Authorization": f"Bearer {k['key']}",
            "Content-Type": "application/json",
            "X-ModelScope-Async-Mode": info["async_header"],
        }
        body = {
            "model": args.model or info["default"],
            "prompt": args.prompt,
            "image_url": [image_to_url_or_path(args.image)],   # 列表：支持多图编辑扩展
        }
        return http_call("POST", f"{k['base']}{info['task_path']}", headers, body, timeout=120)

    resp = submit()
    task_id = resp.get("task_id")
    if not task_id:
        die(f"无 task_id: {json.dumps(resp, ensure_ascii=False)[:300]}")

    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(args.wait)
        try:
            req = urllib.request.Request(f"{k['base']}{info['poll_path']}/{task_id}")
            req.add_header("Authorization", f"Bearer {k['key']}")
            req.add_header("X-ModelScope-Task-Type", "image_generation")
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            print(f"[media_gen] 轮询异常: {e}", file=sys.stderr)
            continue
        status = str(st.get("task_status") or "").upper()
        if status == "SUCCEED":
            imgs = st.get("output_images") or []
            if not imgs:
                die(f"成功但无图: {json.dumps(st, ensure_ascii=False)[:300]}")
            out = args.out
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            urllib.request.urlretrieve(imgs[0], out)
            print(f"[media_gen] edit OK via {provider} -> {out}")
            return
        if status in ("FAILED", "FAIL", "ERROR"):
            die(f"编辑任务失败: {json.dumps(st, ensure_ascii=False)[:300]}")
    die("编辑轮询超时 5 分钟")
def cmd_tts(args) -> None:
    """OpenAI 兼容 /audio/speech 接口。
    配置：media_keys.env 加
      export MEDIA_TTS_1_KEY="xxx"
      export MEDIA_TTS_1_BASE="https://host/v1"      # 含 /v1
      export MEDIA_TTS_1_MODEL="cosyvoice-v1"        # 或 Spark-TTS 等
    """
    key = os.environ.get("MEDIA_TTS_1_KEY", "")
    base = os.environ.get("MEDIA_TTS_1_BASE", "").rstrip("/")
    model = os.environ.get("MEDIA_TTS_1_MODEL", "")
    if not (key and base):
        die("TTS 未配置。请在 ~/.workbuddy/media_keys.env 加 "
            "MEDIA_TTS_1_KEY / MEDIA_TTS_1_BASE / MEDIA_TTS_1_MODEL（见 SKILL.md 声音设计节）", 2)
    text = args.text
    if not text and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    if not text:
        die("需 --text 或 --text-file", 2)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model or "cosyvoice-v1", "input": text,
            "voice": args.voice, "response_format": "mp3"}
    if args.speed and args.speed != 1.0:
        body["speed"] = args.speed        # 非标准字段，服务商不支持可忽略
    req = urllib.request.Request(f"{base}/audio/speech",
                                 data=json.dumps(body).encode(), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=300) as r:
        audio = r.read()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(audio)
    print(f"[media_gen] tts OK -> {out} ({len(audio)//1024}KB)")
def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("image")
    p.add_argument("--prompt", required=True)
    p.add_argument("--size", default="1024x1024")
    p.add_argument("--provider", default="",
                   help="key 池名（留空=按 MEDIA_PRIORITY 自动选池，跨池兜底）")
    p.add_argument("--out", required=True)
    p.add_argument("--count", type=int, default=1, help="批量出图张数（选优用），命名 shot_01_1.png ~ _N.png")
    p.add_argument("--pin-key", type=int, default=0, help="锁定第 N 把 key（0=轮转；双 worker 并行时各锁一把）")

    v = sub.add_parser("video")
    v.add_argument("--prompt", required=True)
    v.add_argument("--image", default="")
    v.add_argument("--provider", default="",
                   help="key 池名（留空=按 MEDIA_PRIORITY 自动选池，跨池兜底）")
    v.add_argument("--out", required=True)
    v.add_argument("--num-frames", type=int, default=121)
    v.add_argument("--negative", default="blurry, distorted faces, warped hands, extra limbs, text artifacts, watermark, camera shake, flickering, plastic skin, oversaturated")
    v.add_argument("--wait", type=int, default=15, help="轮询间隔秒")
    v.add_argument("--video-size", default="1920x1080", help="智谱专用：输出分辨率（默认 1920x1080）")
    v.add_argument("--duration", type=int, default=5, help="智谱专用：时长秒（5 或 10）")
    v.add_argument("--pin-key", type=int, default=0, help="锁定第 N 把 key（0=轮转；双 worker 并行时各锁一把）")
    v.add_argument("--poll-timeout", type=int, default=0,
                   help="轮询上限秒（0=自动：显式池 30 分钟 / 自动路由 20 分钟；超时落盘 task_id 并 exit 4）")
    v.add_argument("--wait-task", default="",
                   help="不重新提交，续等已落盘任务（零扣分）。值=task_id")

    h = sub.add_parser("harvest", help="收割已完成的落盘任务（video/image 两类 pending）")

    pc = sub.add_parser("plan-check", help="校验 plan.json：未知键 warn、枚举值/类型检查")
    pc.add_argument("plan", help="plan.json 路径")

    ed = sub.add_parser("edit")
    ed.add_argument("--image", required=True, help="待编辑的图（如 shots/shot_01.png）")
    ed.add_argument("--prompt", required=True, help="编辑指令，如 'move the ink stick to the left'")
    ed.add_argument("--out", required=True)
    ed.add_argument("--model", default="", help="覆盖默认 Qwen/Qwen-Image-Edit-2509")
    ed.add_argument("--wait", type=int, default=5, help="轮询间隔秒")

    bt = sub.add_parser("batch", help="批量并行（跨池混编：N worker 各绑一个 (池,key)，吞吐×N）")
    bt.add_argument("shots", help="分镜 JSON 目录（含 S*.json / shot_*.json）")
    bt.add_argument("--phase", required=True, choices=["images", "videos"])
    bt.add_argument("--provider", default="",
                    help="留空=全部承担该角色的池混编；单池名=只跑该池；逗号分隔=指定几池混编")
    bt.add_argument("--workers", type=int, default=1, help="并行 worker 数（=使用的 key 数，≤ key 总数）")
    bt.add_argument("--retries", type=int, default=2, help="每镜失败重试次数（默认 2）")
    bt.add_argument("--dry-run", action="store_true", help="仅打印执行计划不实际生成")
    bt.add_argument("--negative", default="blurry, distorted faces, warped hands, extra limbs, text artifacts, watermark, camera shake, flickering, plastic skin, oversaturated")
    bt.add_argument("--qc", action="store_true", help="videos 阶段每段生成后自动抽 3 帧到 clips/qc/")
    bt.add_argument("--retry-failed", action="store_true",
                    help="读上轮 batch_run.json，只重跑 FAIL/PENDING 镜（PENDING 先 harvest，仍在生成的不重提交）")

    tt = sub.add_parser("tts", help="语音合成（OpenAI 兼容，需配置 MEDIA_TTS_*）")
    tt.add_argument("--text", default="")
    tt.add_argument("--text-file", default="")
    tt.add_argument("--out", required=True)
    tt.add_argument("--voice", default="Cherry")
    tt.add_argument("--speed", type=float, default=1.0,
                    help="语速倍率（服务商支持时生效，1.0=正常）")

    qc = sub.add_parser("qc", help="抽首/中/尾 3 帧供视觉验收")
    qc.add_argument("video")
    qc.add_argument("out", help="输出目录")

    lf = sub.add_parser("last-frame")
    lf.add_argument("video")
    lf.add_argument("out")

    st = sub.add_parser("status", help="key 健康 + /models 能力探测（--no-probe 跳过探测）")
    st.add_argument("--no-probe", action="store_true", help="只看 key 配置，不发 /models 探测")

    args = ap.parse_args()
    if args.cmd == "image":
        cmd_image(args)
    elif args.cmd == "video":
        cmd_video(args)
    elif args.cmd == "harvest":
        cmd_harvest(args)
    elif args.cmd == "edit":
        cmd_edit(args)
    elif args.cmd == "batch":
        cmd_batch(args)
    elif args.cmd == "tts":
        cmd_tts(args)
    elif args.cmd == "qc":
        cmd_qc(args)
    elif args.cmd == "last-frame":
        cmd_last_frame(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "plan-check":
        cmd_plan_check(args)

if __name__ == "__main__":
    main()
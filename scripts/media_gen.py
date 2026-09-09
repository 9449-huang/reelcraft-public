"""reelcraft 生成入口（薄壳）：单发 image/video/edit/tts 命令 + argparse dispatch。
共享逻辑在 mg_core，批量在 mg_batch，查询在 mg_status——CLI 用法不变。"""
from __future__ import annotations
from mg_core import (
    AllKeysFailed,
    PROVIDERS,
    _abs_url,
    _download,
    _download_image,
    _extract_video_url,
    _final_out,
    _gen_image_once,
    _image_rpm_for,
    _insert_suffix,
    _poll_video_task,
    _resolve_async_task,
    _wait_existing_task,
    build_last_frame_fields,
    call_with_failover,
    die,
    find_existing_product,
    http_call,
    image_to_url_or_path,
    key_mask,
    list_keys,
    pools_for_role,
    video_throttle,
)
from mg_batch import cmd_batch, cmd_harvest
from mg_status import cmd_status, cmd_qc, cmd_last_frame, cmd_plan_check
import mg_core                     # 账本挂点用（ledger_append / LEDGER_FILE）
import mg_caps                      # 能力单源（#2）：声明=候选，实测=权威
import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
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

    # 各池预校验（显式指定时硬失败；自动路由时跳过不满足参数的池）。
    # size/duration 默认取各池配置（default_size / default_duration），用户显式传参才覆盖。
    valid: list[tuple[str, dict, str, object]] = []   # (pool, info, eff_size, eff_dur)
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
        # 每池生效尺寸：CLI 显式 > 池 default_size
        eff_size = args.video_size or info.get("default_size", "")
        # 时长：智谱走 int（duration 5/10）；带字符串 durations 的网关走 video_duration / default_duration
        if info.get("payload_style") == "zhipu":
            eff_dur: object = args.duration
            if eff_size not in info["sizes"]:
                if args.provider:
                    die(f"{pool} 不支持 video size={eff_size}。可选: {info['sizes']}", 2)
                continue
            if eff_dur not in info["durations"]:
                if args.provider:
                    die(f"{pool} duration={eff_dur} 不在 {info['durations']}", 2)
                continue
        else:
            if "sizes" in info and eff_size not in info["sizes"]:
                if args.provider:
                    die(f"{pool} 不支持 video size={eff_size}。可选: {info['sizes']}", 2)
                continue
            if "durations" in info:
                eff_dur = args.video_duration or info.get("default_duration", "") or "short"
                if eff_dur not in info["durations"]:
                    if args.provider:
                        die(f"{pool} 不支持 video duration={eff_dur}。可选: {info['durations']}", 2)
                    continue
            else:
                eff_dur = None     # 无时长白名单的池（如 agnes）不传 duration
        valid.append((pool, info, eff_size, eff_dur))
    if not valid:
        die("没有满足参数的视频池（检查 num_frames / --video-size / --duration）", 2)

    # 节流：pin 锁 key 时按 key 计时（多 worker 各走各的 1RPM），否则按池共享通道
    for pool, info, _sz, _du in valid:
        rpm = info.get("rpm") or 1
        tag = f"{pool}_key{args.pin_key}" if args.pin_key else f"{pool}_shared"
        video_throttle(rpm, tag)

    # 断点续跑（已存在则跳过，不白等节流；产物可能因上游落成 .webp——
    # 只查 args.out 会把已完成镜误判为未完成而重复提交扣费，#3 单命令漏网点）
    existing = find_existing_product(args.out)
    if existing:
        print(f"[media_gen] 已存在 {existing}，跳过生成", file=sys.stderr)
        return

    errs: list[str] = []
    out = args.out
    resp = used_key = None
    for pool, info, eff_size, eff_dur in valid:
        def call_fn(k: dict, _info=info, _prefix=PROVIDERS[pool]["key_env_prefix"],
                    _size=eff_size, _dur=eff_dur) -> dict:
            headers = {"Authorization": f"Bearer {k['key']}", "Content-Type": "application/json"}
            if _info.get("payload_style") == "zhipu":
                payload: dict[str, Any] = {
                    "model": k.get("video_model") or _info["default"],
                    "prompt": args.prompt,
                    "with_audio": False,
                    "fps": 30,
                    "size": _size,
                    "duration": _dur,
                }
                if args.image:
                    payload["image_url"] = image_to_url_or_path(args.image)
            else:
                model = k.get("video_model") or _info["default"]
                if not model:
                    die(f"该池未配置模型名（模板无默认值）。请在 env 加 {_prefix}{k['n']}_VIDEO_MODEL=模型名", 2)
                pfield = k.get("video_prompt_field") or "prompt"
                payload = {"model": model, pfield: args.prompt}
                if _size:
                    payload["size"] = _size
                if _dur:
                    payload["duration"] = _dur
                if args.image:
                    _img = image_to_url_or_path(args.image)
                    payload[_info.get("image_param", "image")] = (
                        [_img] if _info.get("image_list") else _img)
                # 过渡镜（首尾帧双条件）：池/key 声明支持才传尾帧（字段名可配）
                if getattr(args, "last_frame", ""):
                    lf = build_last_frame_fields(_info, k)
                    if lf:
                        _lfurl = image_to_url_or_path(args.last_frame)
                        _lfname = next(iter(lf))
                        payload[_lfname] = (
                            [_lfurl] if (k.get("last_frame_list")
                                         or _info.get("last_frame_list")) else _lfurl)
                    else:
                        die(f"{pool} 未声明支持首尾帧双条件（last_frame_param）。"
                            "custom 池在 env 加 MEDIA_<P>_n_LAST_FRAME_PARAM=<字段名> 启用", 2)
                nf2 = args.num_frames
                if nf2 and _info.get("supports_num_frames"):
                    payload["num_frames"] = nf2
                if args.negative and _info.get("supports_negative"):
                    payload["negative_prompt"] = args.negative
            vpath = k.get("video_task_path") or k.get("task_path") or _info["task_path"]
            # 同步阻塞型网关（LTXBridge 风）POST 会阻塞到出片，超时给足（对齐 bridge task_timeout 1800s）
            return http_call("POST", f"{k['base']}{vpath}", headers, payload, timeout=1800)

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
    # 同步出片 URL 复用统一解析（兼容 video_url/url/data dict/data list，#8）
    du = _abs_url(_extract_video_url(resp), used_key.get("base", ""))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if isinstance(du, str) and os.path.exists(du) and not du.startswith("http"):
        # 本地路径（LTXBridge 风同步响应带 local_path）：直接拷贝，不走网络/轮询（#10）
        out = _final_out(out, du)
        shutil.copyfile(du, out)
        print(f"[media_gen] video OK via {provider} key#{used_key['n']}（本地直拷）-> {out}")
        return
    if isinstance(du, str) and du.startswith("http"):
        out = _final_out(out, du)
        _download(du, out)
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
            _download(imgs[0], out)
            mg_core.ledger_append(mg_core.LEDGER_FILE,
                                  {"provider": provider, "key": k["n"], "op": "edit",
                                   "ok": True, "ms": 0})
            print(f"[media_gen] edit OK via {provider} -> {out}")
            return
        if status in ("FAILED", "FAIL", "ERROR"):
            die(f"编辑任务失败: {json.dumps(st, ensure_ascii=False)[:300]}")
    die("编辑轮询超时 5 分钟")
def cmd_run(args) -> None:
    """优化⑩：一键编排入口——转发 pipeline.py（参数单一事实源在 pipeline 侧）。
    subprocess 转发而非 import cmd：直接复用 pipeline 的 argparse/退出码，零重复实现。"""
    pipe = str(Path(__file__).resolve().parent / "pipeline.py")
    cmd = [sys.executable, pipe, args.shots]
    for flag in ("--final", "--stop-after", "--target-res", "--bgm",
                 "--slogan", "--slogan-position", "--provider-image", "--provider-video",
                 "--workers-image", "--workers-video", "--watermark"):
        val = getattr(args, flag[2:].replace("-", "_"), "")
        if val:
            cmd += [flag, str(val)]
    for flag in ("--dry-run", "--qcgate", "--qcgate-strict", "--retry-failed",
                 "--no-lint", "--no-qcseq", "--watermark-dry-run"):
        if getattr(args, flag[2:].replace("-", "_"), False):
            cmd.append(flag)
    sys.exit(subprocess.call(cmd))


def cmd_audit(args) -> None:
    """优化⑪：项目进度审计——转发 pipeline.py audit。"""
    pipe = str(Path(__file__).resolve().parent / "pipeline.py")
    sys.exit(subprocess.call([sys.executable, pipe, "audit", args.shots]))


def cmd_envcheck(args) -> None:
    """#65：外部环境自检——转发 envcheck.py（subprocess 零重复实现）。"""
    ec = str(Path(__file__).resolve().parent / "envcheck.py")
    sys.exit(subprocess.call([sys.executable, ec]))


def cmd_clean(args) -> None:
    """#66：工作区产物治理——转发 pipeline.py clean（scan-only 默认）。"""
    pipe = str(Path(__file__).resolve().parent / "pipeline.py")
    cmd = [sys.executable, pipe, "clean", args.shots]
    if getattr(args, "yes", False):
        cmd.append("--yes")
    if getattr(args, "purge", False):
        cmd.append("--purge")
    sys.exit(subprocess.call(cmd))


def cmd_ledger(args) -> None:
    """#5 成本账本：读 JSONL 账本出汇总表（调用次数/成败/白烧/耗时）。
    免费档语境下"成本"= RPM 限速下的调用次数 + 失败白烧的次数。"""
    path = Path(args.file) if getattr(args, "file", "") else mg_core.LEDGER_FILE
    rows = mg_core.ledger_read(path)
    s = mg_core.ledger_summarize(rows, days=args.days)
    if not s["total"]:
        print(f"[ledger] 账本为空：{path}\n"
              "  （调用记录在 image/video/tts/edit 每次真实请求后自动追加）")
        sys.exit(0)
    rate = s["ok"] / s["total"] * 100
    print(f"[ledger] {path}")
    print(f"  调用 {s['total']} 次：成功 {s['ok']} / 失败 {s['fail']}"
          f"（成功率 {rate:.0f}%，白烧 {s['wasted']} 次）")
    print("\n  按渠道：")
    for k, b in sorted(s["by_provider"].items(), key=lambda x: -x[1]["total"]):
        print(f"    {k:12s} {b['total']:4d} 次  ok {b['ok']:4d} / fail {b['fail']:3d}"
              f"   耗时 {b['ms']/1000:6.1f}s")
    print("\n  按操作：")
    for k, b in sorted(s["by_op"].items(), key=lambda x: -x[1]["total"]):
        print(f"    {k:12s} {b['total']:4d} 次  ok {b['ok']:4d} / fail {b['fail']:3d}"
              f"   均耗时 {(b['ms']/max(b['total'],1)):.0f}ms")
    print("\n  按天：")
    for k, b in sorted(s["by_day"].items()):
        print(f"    {k}  {b['total']:4d} 次  ok {b['ok']:4d} / fail {b['fail']:3d}")
    if args.json:
        print("\n" + json.dumps(s, ensure_ascii=False, indent=2))
    sys.exit(0)


def _tts_emotion_prefix(emotion: str) -> str:
    """CosyVoice 系 TTS 的情感走文本引导（官方示例句式），拼在正文前。
    空情感返回空串（原样合成）。纯函数可单测。"""
    e = (emotion or "").strip()
    return f"你能用{e}的情感说吗，" if e else ""


def _scan_tts_slots(env: dict, max_n: int = 200) -> list:
    """扫已配 TTS key 序号（纯函数可单测）：起始空号跳过，遇配置后连续 3 空号停。
    判"已配"用真值（空字符串=未配），与 mg_core.list_keys 口径一致。"""
    ns, streak, tried = [], 0, False
    for n in range(1, max_n):
        has = bool(env.get(f"MEDIA_TTS_{n}_KEY") or env.get(f"MEDIA_TTS_{n}_BASE"))
        if has:
            ns.append(n)
            streak, tried = 0, True
        else:
            streak += 1
            if tried and streak >= 3:
                break
    return ns


def cmd_tts(args) -> None:
    """OpenAI 兼容 /audio/speech 接口。多把 key 自动 failover（n 从 1 依次试，成功即停）；
    每把 key 可独立 MEDIA_TTS_<n>_VOICE（降级时音色自动切换）；--emotion 走文本引导。
    配置：media_keys.env 加
      export MEDIA_TTS_1_KEY="xxx"
      export MEDIA_TTS_1_BASE="https://host/v1"      # 含 /v1
      export MEDIA_TTS_1_MODEL="cosyvoice-v1"        # 或 Spark-TTS 等
      export MEDIA_TTS_1_VOICE="xxx"                 # 可选：该 key 默认音色
    """
    text = args.text
    if not text and args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    if not text:
        die("需 --text 或 --text-file", 2)
    prefix = _tts_emotion_prefix(getattr(args, "emotion", ""))
    if prefix:
        text = prefix + text
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 扫描已配 TTS key 序号（遇配置后连续 3 空号停，容忍断档与起始空号；上限动态不封死 19）
    slots = _scan_tts_slots(os.environ)
    last_err = ""
    for n in slots:
        key = os.environ.get(f"MEDIA_TTS_{n}_KEY", "")
        base = os.environ.get(f"MEDIA_TTS_{n}_BASE", "").rstrip("/")
        if not (key or base):
            continue       # 不会发生（slots 已过滤），防御保留
        if not (key and base):
            last_err = f"MEDIA_TTS_{n} 的 KEY/BASE 不完整"
            continue
        model = os.environ.get(f"MEDIA_TTS_{n}_MODEL", "") or "cosyvoice-v1"
        # CLI --voice 显式传入时优先（环境变量只是各 key 的默认值）
        voice = args.voice or os.environ.get(f"MEDIA_TTS_{n}_VOICE", "")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
        if args.speed and args.speed != 1.0:
            body["speed"] = args.speed        # 非标准字段，服务商不支持可忽略
        req = urllib.request.Request(f"{base}/audio/speech",
                                     data=json.dumps(body).encode(), method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                audio = r.read()
        except Exception as e:
            last_err = f"key#{n} {base}：{e}"
            print(f"[tts] key#{n} {base} 失败（{e}）——降级下一把", file=sys.stderr)
            mg_core.ledger_append(mg_core.LEDGER_FILE,
                                  {"provider": "tts", "key": n, "op": "tts",
                                   "ok": False, "err": str(e),
                                   "ms": int((time.time() - t0) * 1000)})
            continue
        if len(audio) < 64:
            # 200 但空体/极小响应是网关半死状态——落盘会产出 0KB 废 mp3 静默流入下游
            last_err = f"key#{n} {base}：响应仅 {len(audio)} 字节（疑似空体）"
            print(f"[tts] {last_err}——降级下一把", file=sys.stderr)
            mg_core.ledger_append(mg_core.LEDGER_FILE,
                                  {"provider": "tts", "key": n, "op": "tts",
                                   "ok": False, "err": last_err,
                                   "ms": int((time.time() - t0) * 1000)})
            continue
        # 原子写：先 tmp 后 replace，失败/被杀不留半截文件
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_bytes(audio)
        os.replace(tmp, out)
        mg_core.ledger_append(mg_core.LEDGER_FILE,
                              {"provider": "tts", "key": n, "op": "tts", "ok": True,
                               "ms": int((time.time() - t0) * 1000)})
        print(f"[media_gen] tts OK key#{n} -> {out} ({len(audio)//1024}KB)"
              + (f"  emotion={getattr(args, 'emotion', '')}" if prefix else ""))
        return
    if out.exists():
        print(f"[tts] ⚠️ {out} 是上次跑的旧文件，本次全 key 失败未覆盖——下游勿复用",
              file=sys.stderr)
    die(f"TTS 全部失败：{last_err or 'TTS 未配置（见 SKILL.md 声音设计节）'}", 3)
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
    v.add_argument("--last-frame", default="",
                   help="尾帧图（首尾帧双条件/过渡镜）：池声明支持才生效——"
                        "custom 池配 MEDIA_<P>_n_LAST_FRAME_PARAM=<字段名> 启用")
    v.add_argument("--provider", default="",
                   help="key 池名（留空=按 MEDIA_PRIORITY 自动选池，跨池兜底）")
    v.add_argument("--out", required=True)
    v.add_argument("--num-frames", type=int, default=121)
    v.add_argument("--negative", default="blurry, distorted faces, warped hands, extra limbs, text artifacts, watermark, camera shake, flickering, plastic skin, oversaturated")
    v.add_argument("--wait", type=int, default=15, help="轮询间隔秒")
    v.add_argument("--video-size", default="", help="视频分辨率（留空=用所选池的默认，如智谱 1920x1080、本地网关 1280x720）")
    v.add_argument("--duration", type=int, default=5, help="智谱专用：时长秒（5 或 10）")
    v.add_argument("--video-duration", default="", help="本地网关风：时长 short/medium/long（留空=池默认 short）")
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
    bt.add_argument("--qc", action="store_true", help="videos 阶段每段生成后自动抽 3 帧到 clips/qc/（人眼）")
    bt.add_argument("--qcgate", action="store_true",
                    help="videos 阶段每段过 postprocess qcgate 机器门禁（黑帧/过曝/静帧/规格）；"
                         "FAIL 的镜记为 FAIL，--retry-failed 会重跑（#4）")
    bt.add_argument("--qcgate-strict", action="store_true",
                    help="qcgate 的 WARN（偏黑/过曝/静帧）也判 FAIL 重跑（默认只拦硬 FAIL）")
    bt.add_argument("--retry-failed", action="store_true",
                    help="读上轮 batch_run.json，只重跑 FAIL/PENDING 镜（PENDING 先 harvest，仍在生成的不重提交）")
    bt.add_argument("--only", default="",
                    help="只跑指定镜号（逗号分隔，按 shot_id；hybrid 模式喂 hero_shots 用）")
    bt.add_argument("--no-lint", action="store_true",
                    help="跳过出镜前 prompt lint（slop/词数/主体漂移检查；默认强制）")
    bt.add_argument("--video-size", default="", help="视频分辨率覆盖（默认留空=每池各自默认：智谱 1920x1080 / 本地网关 1280x720）")
    bt.add_argument("--video-duration", default="", help="视频时长覆盖（默认留空=每池默认 short；本地网关风可 short/medium/long）")

    tt = sub.add_parser("tts", help="语音合成（OpenAI 兼容，多 key failover，需配置 MEDIA_TTS_*）")
    tt.add_argument("--text", default="")
    tt.add_argument("--text-file", default="")
    tt.add_argument("--out", required=True)
    tt.add_argument("--voice", default="",
                    help="音色名（显式传参优先于 env；缺省读 MEDIA_TTS_<n>_VOICE，再留空用服务商默认）")
    tt.add_argument("--emotion", default="",
                    help="情感词（高兴/悲伤/激昂/温柔…）——CosyVoice 系走文本引导，原样拼进正文")
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

    # 优化⑩：统一 CLI 入口——一键编排/审计都从这里进，不用记 pipeline.py 脚本名
    runp = sub.add_parser(
        "run", help="一键编排（转发 pipeline.py：images→videos→harvest→kenburns→sound→concat→watermark）")
    runp.add_argument("shots", help="shots 目录（含 plan.json；缺 mode 会 die 退回 Step 1）")
    runp.add_argument("--final", default="", help="成片输出路径（默认 <shots>/../final.mp4）")
    runp.add_argument("--stop-after", default="concat",
                      choices=["images", "videos", "harvest", "kenburns", "sound", "concat", "watermark"],
                      help="跑到该阶段后停（默认 concat，停在 QC 门前）")
    runp.add_argument("--dry-run", action="store_true", help="只打印各阶段命令不执行")
    runp.add_argument("--workers-image", type=int, default=3)
    runp.add_argument("--workers-video", type=int, default=3)
    runp.add_argument("--provider-image", default="")
    runp.add_argument("--provider-video", default="")
    runp.add_argument("--qcgate", action="store_true",
                      help="视频阶段过机器门禁（FAIL 记失败待重跑）")
    runp.add_argument("--qcgate-strict", action="store_true")
    runp.add_argument("--retry-failed", action="store_true", help="只补跑上轮 FAIL/PENDING 镜")
    runp.add_argument("--no-lint", action="store_true", help="跳过出镜前 prompt lint")
    runp.add_argument("--no-qcseq", action="store_true",
                      help="跳过 concat 前的跨镜首帧一致性粗检（qcseq）")
    runp.add_argument("--watermark", default="", help="去水印 provider（渠道名，读 watermark_profiles.json）")
    runp.add_argument("--watermark-dry-run", action="store_true",
                      help="水印只列待处理档/出红框自检图，不真抹")
    runp.add_argument("--target-res", default="1280x720")
    runp.add_argument("--bgm", default="")
    runp.add_argument("--slogan", default="")
    runp.add_argument("--slogan-position", default="left", choices=["left", "bottom"])

    au = sub.add_parser("audit", help="项目进度审计：每镜 出图/出片/失败 + 下一步清单")
    au.add_argument("shots", help="shots 目录")

    ec = sub.add_parser("envcheck", help="#65 外部环境自检：ffmpeg/PIL/字体/key env/本地服务——开跑前体检")
    cn = sub.add_parser("clean", help="#66 工作区产物治理：默认 scan-only 清单；--yes 移入 .trash；--purge 真删")
    cn.add_argument("shots", help="shots 目录")
    cn.add_argument("--yes", action="store_true", help="执行清理（移入 .trash/<时间戳>/，可反悔）")
    cn.add_argument("--purge", action="store_true", help="清空 .trash（不可逆）")

    tr = sub.add_parser("triage", help="方案B 出片听诊：每镜要不要补朗读（机器粗筛，拍板留人）")
    tr.add_argument("clips", nargs="+", help="成片文件或含 clip_*.mp4/webp 的目录")
    tr.add_argument("--pool", default="", help="模型池名（配合 --update-profile 写档案）")
    tr.add_argument("--update-profile", action="store_true",
                    help="把结果写进 ~/.workbuddy/.audio_profiles.json（同模型下次免测）")
    tr.add_argument("--refresh", action="store_true",
                    help="忽略档案实测结论强制重测（配合 --pool）")

    lg = sub.add_parser("ledger", help="#5 成本账本：调用次数/成败/白烧统计（按渠道/操作/天）")
    lg.add_argument("--days", type=int, default=0, help="只看最近 N 天（0=全部）")
    lg.add_argument("--file", default="", help="账本路径（默认 ~/.workbuddy/.media_ledger.jsonl）")
    lg.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")

    mg_caps.build_parser(sub)        # caps show / probe / clear

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
    elif args.cmd == "caps":
        mg_caps.cmd_caps(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "audit":
        cmd_audit(args)
    elif args.cmd == "envcheck":
        cmd_envcheck(args)
    elif args.cmd == "clean":
        cmd_clean(args)
    elif args.cmd == "triage":
        import audio_triage
        sys.exit(audio_triage.cmd(args))
    elif args.cmd == "ledger":
        cmd_ledger(args)

if __name__ == "__main__":
    main()
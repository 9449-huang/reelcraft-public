"""reelcraft 批量命令：跨池混编 batch、harvest 收割（video/image 双类 pending）。"""
from __future__ import annotations
from mg_core import (
    PROVIDERS,
    _abs_url,
    _extract_video_url,
    _final_out,
    _interleave_by_pool,
    _load_state,
    _pop_pending_task,
    die,
    list_keys,
    pools_for_role,
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

def cmd_harvest(args) -> None:
    """收割已完成的落盘任务（提交即扣模式下，慢任务出片自动变现）。
    video / image 两类 pending 都支持（kind 字段分流）。"""
    pt = _load_state().get("pending_tasks", {})
    if not pt:
        print("[media_gen] harvest: 无落盘的未收任务")
        return
    for tid, rec in list(pt.items()):
        pool = rec.get("pool", "")
        kind = rec.get("kind", "video")
        if kind == "image":
            _harvest_image(tid, rec, pool)
        else:
            _harvest_video(tid, rec, pool)

def _harvest_image(tid: str, rec: dict, pool: str) -> None:
    info = PROVIDERS.get(pool, {}).get("models", {}).get("image") or {}
    if not info:
        print(f"[harvest] {tid}: 池 {pool} 不在当前配置，跳过")
        return
    keys = list_keys(pool, required=False)
    if not keys:
        print(f"[harvest] {tid}: 池 {pool} 无可用 key，跳过")
        return
    k = keys[0]
    poll_path = rec.get("poll_path") or info.get("task_poll_path", "/tasks")
    url = f"{k['poll']}{poll_path}/{tid}"
    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {k['key']}")
        with urllib.request.urlopen(req, timeout=30) as r:
            st = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        print(f"[harvest] {tid}: 查询失败 {e}")
        return
    status = str(st.get("task_status") or "").upper()
    imgs = st.get("output_images") or []
    if imgs:
        out = rec.get("out") or f"harvest_{tid}.png"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        got = _abs_url(imgs[0], k.get("base", ""))
        out = _final_out(out, got)
        urllib.request.urlretrieve(got, out)
        _pop_pending_task(tid)
        print(f"[harvest] {tid}: 已收割 -> {out}")
    elif status in ("FAIL", "FAILED", "ERROR"):
        _pop_pending_task(tid)
        print(f"[harvest] {tid}: 任务已失败，从落盘移除")
    else:
        print(f"[harvest] {tid}: 仍在生成（status={status or '?'}），保留落盘")

def _harvest_video(tid: str, rec: dict, pool: str) -> None:
    info = PROVIDERS.get(pool, {}).get("models", {}).get("video")
    if not info:
        print(f"[harvest] {tid}: 池 {pool} 不在当前配置，跳过")
        return
    keys = list_keys(pool, required=False)
    if not keys:
        print(f"[harvest] {tid}: 池 {pool} 无可用 key，跳过")
        return
    k = keys[0]
    if info.get("poll_style") == "path":
        url = f"{k['poll']}{info['poll_path']}/{tid}"
    else:
        url = f"{k['poll']}{info['poll_path']}?{info['poll_param']}={tid}"
    try:
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {k['key']}")
        with urllib.request.urlopen(req, timeout=30) as r:
            st = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        print(f"[harvest] {tid}: 查询失败 {e}")
        return
    got = _extract_video_url(st)
    status = str(st.get("task_status") or st.get("status") or "").upper()
    if got:
        out = rec.get("out") or f"harvest_{tid}.mp4"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        out = _final_out(out, got)
        urllib.request.urlretrieve(got, out)
        _pop_pending_task(tid)
        print(f"[harvest] {tid}: 已收割 -> {out}")
    elif status in ("FAIL", "FAILED", "ERROR"):
        _pop_pending_task(tid)
        print(f"[harvest] {tid}: 任务已失败，从落盘移除")
    else:
        print(f"[harvest] {tid}: 仍在生成（status={status or '?'}），保留落盘")
def cmd_batch(args) -> None:
    """读 shots 目录下 S*.json / shot_*.json，images/videos 两阶段可并行多 worker。
    workers=N 时：N 个 worker 跨池混编（每个绑定一个 (池, key) 对，按 _ROLES 过滤），
    从共享队列动态取镜；不同池不同模型可同场开工。
    --provider 留空 = 全部承担该角色的池混编；传单池 = 只跑该池；逗号分隔 = 指定混编。
    断点续跑：已有产物跳过。每镜失败自动重试 --retries 次；某 worker 连续失败 ≥3
    则退场（不拖垮全队，该镜记 FAIL）。--retry-failed 读上轮 batch_run.json
    一键补跑 FAIL/PENDING 镜（PENDING 先 harvest，仍在生成的不重提交防重复扣费）。
    成功镜写入 batch_run.json（含所用 provider/key，便于追溯与混合 provider 拼接告警）。
    """
    import queue
    import threading as _t

    shots_dir = Path(args.shots)
    if not shots_dir.is_dir():
        die(f"shots 目录不存在: {shots_dir}", 2)
    jsons = sorted(shots_dir.glob("S*.json")) + sorted(shots_dir.glob("shot_*.json"))
    if not jsons:
        die(f"未找到分镜 JSON: {shots_dir}/S*.json", 2)

    role = "image" if args.phase == "images" else "video"
    raw = (args.provider or "").strip()
    pool_names = ([p.strip().lower() for p in raw.split(",") if p.strip()]
                  if raw else pools_for_role(role))
    for p in pool_names:
        if p not in PROVIDERS:
            die(f"不支持 provider: {p}", 2)
    # 收集 (池, key) 候选并按池轮流交错 → 混编时模型分布均匀
    spec: list[tuple[str, dict]] = []
    for p in pool_names:
        for k in list_keys(p, required=False, role=role):
            spec.append((p, k))
    if not spec:
        die(f"没有承担 {role} 角色的可用 key（检查 MEDIA_*_1_KEY/_BASE/_ROLES，缺省 image,video）", 2)
    interleaved = _interleave_by_pool(spec, pool_names)
    if args.workers > len(interleaved):
        print(f"[batch] [warn] workers={args.workers} 超过可用 (池,key) 对数 "
              f"{len(interleaved)}，已截断", file=sys.stderr)
        args.workers = len(interleaved)
    assigned = interleaved[:args.workers]

    frames_dir = shots_dir / "frames"
    clips_dir = shots_dir / "clips"
    frames_dir.mkdir(exist_ok=True)
    clips_dir.mkdir(exist_ok=True)
    qc_dir = clips_dir / "qc"

    def make_cmd(j: Path, sid: str, pool: str, pin: int) -> tuple[list[str], Path, str]:
        """构造单镜命令；返回 (cmd, out_path, skip_reason)。skip_reason 非空=跳过。"""
        d = json.loads(j.read_text(encoding="utf-8"))
        if args.phase == "images":
            out = frames_dir / f"{sid}.png"
            if out.exists() and out.stat().st_size > 0:
                return [], out, "skip (exists)"
            cmd = [sys.executable, str(Path(__file__).resolve()), "image",
                   "--provider", pool, "--prompt", d["t2i_prompt"],
                   "--size", d.get("size", "1344x768"), "--out", str(out),
                   "--pin-key", str(pin)]
            return cmd, out, ""
        out = clips_dir / f"clip_{sid}.mp4"   # clip_ 前缀与 postprocess concat 的 glob("clip_*.mp4") 对齐
        if out.exists() and out.stat().st_size > 0:
            return [], out, "skip (exists)"
        frame = frames_dir / f"{sid}.png"
        if not frame.exists():
            return [], out, "MISS (no frame)"
        cmd = [sys.executable, str(Path(__file__).resolve()), "video",
               "--provider", pool, "--prompt", d.get("i2v_prompt", ""),
               "--image", str(frame), "--out", str(out),
               "--num-frames", str(d.get("num_frames", 121)),
               "--negative", args.negative, "--pin-key", str(pin)]
        if getattr(args, "video_size", ""):
            cmd += ["--video-size", args.video_size]
        if getattr(args, "video_duration", ""):
            cmd += ["--video-duration", args.video_duration]
        return cmd, out, ""

    plan: list[tuple[Path, str]] = []
    for j in jsons:
        d = json.loads(j.read_text(encoding="utf-8"))
        sid = d.get("shot_id") or j.stem
        plan.append((j, sid))

    # --retry-failed：读上轮 batch_run.json，只重跑 FAIL/PENDING 镜；
    # PENDING 先 harvest（在生成的不重提交，防重复扣费），成功镜靠断点续跑跳过
    if getattr(args, "retry_failed", False):
        run_file = shots_dir / "batch_run.json"
        if not run_file.exists():
            die("--retry-failed 需要先跑过一次 batch（batch_run.json 不存在）", 2)
        prev_shots = json.loads(run_file.read_text(encoding="utf-8")).get("shots", {})
        bad_sids = {sid for sid, tag in prev_shots.items()
                    if tag.endswith("(failed)") or tag.endswith("(timeout-pending)")}
        if not bad_sids:
            print("[batch] 上轮无失败镜，无需补跑")
            sys.exit(0)
        cmd_harvest(args)                    # 先收割已完成出片的 PENDING 任务
        still = {Path(r.get("out", "x.zip")).stem.replace("clip_", "")
                 for r in (_load_state().get("pending_tasks") or {}).values() if r.get("out")}
        waiting = bad_sids & still
        bad_sids -= waiting
        if waiting:
            print(f"[batch] 仍在生成、暂不重提交：{sorted(waiting)}（出片后 harvest 收割）",
                  file=sys.stderr)
        plan = [(j, sid) for j, sid in plan if sid in bad_sids]
        if not plan:
            print(f"[batch] 失败镜全部在生成中或无法定位，本轮无任务"
                  f"（仍在生成：{sorted(waiting)}）")
            sys.exit(0)
        print(f"[batch] 补跑 {len(plan)} 镜：{[sid for _, sid in plan]}", file=sys.stderr)

    # dry-run：仅打印计划
    if args.dry_run:
        print(f"[batch] DRY-RUN phase={args.phase} workers={args.workers} "
              f"shots={len(plan)} retries={args.retries} "
              f"pools={','.join(p for p, _ in assigned)}")
        for w, (p, k) in enumerate(assigned):
            print(f"  W{w+1} -> {p} key#{k['n']}"
                  + (f" tier={k['tier']}" if k.get("tier") else ""))
        print("  (各镜由哪个 worker 执行由动态队列决定，下方命令以 W1 的池为例)")
        for j, sid in plan:
            cmd, out, skip = make_cmd(j, sid, assigned[0][0], assigned[0][1]["n"])
            if skip:
                print(f"  {sid} -> {out}  [{skip}]")
            else:
                print(f"  {sid} -> {out}\n    {' '.join(cmd)}")
        sys.exit(0)

    task_q: "queue.Queue[tuple[Path, str]]" = queue.Queue()
    for item in plan:
        task_q.put(item)

    results: list[str] = []
    provider_map: dict[str, str] = {}
    rl = _t.Lock()

    def run_worker(w: int) -> None:
        pool, k = assigned[w]
        pin = k["n"]
        consecutive_fail = 0
        while True:
            if consecutive_fail >= 3:
                print(f"[batch W{w+1}] {pool} key#{pin} 连续失败 {consecutive_fail} 次，"
                      f"worker 退场（其余 worker 继续；FAIL 镜可用单命令跨池兑底补跑）",
                      file=sys.stderr)
                return
            try:
                j, sid = task_q.get_nowait()
            except queue.Empty:
                return
            cmd, out, skip = make_cmd(j, sid, pool, pin)
            if skip:
                with rl:
                    results.append(f"skip {sid}")
                consecutive_fail = 0
                continue
            last_rc = 1
            for attempt in range(1, args.retries + 1):
                rc = subprocess.call(cmd)
                last_rc = rc
                if rc == 0:
                    break
                if rc == 4:
                    # 超时协议：任务已受理落盘（提交即扣），重试=重复扣费，严禁重试
                    print(f"[batch W{w+1}] {sid} 轮询超时(rc=4)：任务已落盘，跑 harvest 收割",
                          file=sys.stderr)
                    break
                print(f"[batch W{w+1}] {sid} 失败(rc={rc})，重试 {attempt}/{args.retries}",
                      file=sys.stderr)
            # 断点续跑兜底：重试后仍非 0，但产物已存在且非空，视为成功
            if last_rc != 0 and out.exists() and out.stat().st_size > 0:
                last_rc = 0
            with rl:
                if last_rc == 0:
                    consecutive_fail = 0
                    results.append(f"OK {sid} ({pool} key#{pin})")
                    provider_map[sid] = f"{pool} key#{pin}"
                    if args.qc and args.phase == "videos":
                        qc_dir.mkdir(exist_ok=True)
                        subprocess.call([sys.executable, str(Path(__file__).resolve()),
                                         "qc", str(out), str(qc_dir)])
                elif last_rc == 4:
                    # 超时在途不算失败（不进 consecutive_fail，慢池 worker 不误退场）
                    results.append(f"PENDING {sid} (任务已落盘，跑 harvest 收割)")
                    provider_map[sid] = f"{pool} key#{pin} (timeout-pending)"
                else:
                    consecutive_fail += 1
                    results.append(f"FAIL(rc={last_rc}) {sid} [{pool} key#{pin}]")
                    provider_map[sid] = f"{pool} key#{pin} (failed)"

    threads: list[_t.Thread] = []
    for w in range(args.workers):
        th = _t.Thread(target=run_worker, args=(w,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()

    # 每镜所用 provider/key 落盘，便于追溯与混合 provider 拼接告警
    run_info = {"providers": sorted({p for p, _ in assigned}), "phase": args.phase,
                "workers": args.workers, "retries": args.retries,
                "assignment": [f"{p} key#{k['n']}" for p, k in assigned],
                "shots": provider_map}
    (shots_dir / "batch_run.json").write_text(
        json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8")

    for r in results:
        print(r, flush=True)
    failed = [r for r in results if r.startswith("FAIL")]
    print(f"[batch] phase={args.phase} workers={args.workers} done. "
          f"failed={failed or 'none'}  (明细见 {shots_dir / 'batch_run.json'})", flush=True)
    sys.exit(1 if failed else 0)

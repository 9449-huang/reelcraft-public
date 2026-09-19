"""reelcraft 批量命令：跨池混编 batch、harvest 收割（video/image 双类 pending）。"""
from __future__ import annotations
from mg_core import (
    find_product,
    is_transition,
    PROVIDERS,
    _abs_url,
    _download,
    _extract_video_url,
    _final_out,
    _interleave_by_pool,
    _load_state,
    _pop_pending_task,
    PRODUCT_EXTS,
    die,
    list_keys,
    list_shot_files,
    load_character_bible,
    merge_character_refs,
    natkey,
    negative_for_shot,
    pools_for_role,
    run_capture,
)
import mg_core                      # 模块名引用必需（v4.7.7 修 P0：harvest 调 mg_core.xxx
                                    # 但只有 from-import → NameError；328 测试全绿没抓到）
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

def prompt_lint_snapshot(j: Path, phase: str) -> str:
    """出镜前 prompt lint（优化②）：返回 PASS/FAIL。延迟 import 避免循环依赖。
    lint 词表默认读 references/anti-slop-lexicon.md，缺失回退内置。"""
    import prompt_lint
    hard, mood = prompt_lint.parse_lexicon_md(
        Path(__file__).resolve().parents[1] / "references" / "anti-slop-lexicon.md")
    try:
        shot = json.loads(j.read_text(encoding="utf-8"))
    except Exception:
        return "FAIL"
    issues = prompt_lint.lint_shot(shot, phase, hard, mood)
    return "FAIL" if any(i["level"] == "FAIL" for i in issues) else "PASS"

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
    url = mg_core.build_poll_url(k["poll"], {"poll_style": "path"}, tid,
                                  poll_path=poll_path)
    st, ferr = mg_core.fetch_task_state(
        url, {"Authorization": f"Bearer {k['key']}"}, timeout=30)
    if st is None:
        print(f"[harvest] {tid}: 查询失败 {ferr}")
        return
    status = str(st.get("task_status") or "").upper()
    imgs = st.get("output_images") or []
    if imgs:
        out = rec.get("out") or f"harvest_{tid}.png"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        got = _abs_url(imgs[0], k.get("base", ""))
        out = _final_out(out, got)
        _download(got, out)
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
    # 落盘记录只有 poll_path（无风格字段）→ 按视频池 info 的 poll_style 构造；
    # 记录里的 poll_path 优先（旧任务可能来自已改配置的池）
    rec_path = rec.get("poll_path") or ""
    url = mg_core.build_poll_url(k["poll"], info, tid,
                                  poll_path=rec_path)
    st, ferr = mg_core.fetch_task_state(
        url, {"Authorization": f"Bearer {k['key']}"}, timeout=30)
    if st is None:
        print(f"[harvest] {tid}: 查询失败 {ferr}")
        return
    got = _extract_video_url(st)
    status = str(st.get("task_status") or st.get("status") or "").upper()
    if got:
        out = rec.get("out") or f"harvest_{tid}.mp4"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        out = _final_out(out, got)
        _download(got, out)
        _pop_pending_task(tid)
        print(f"[harvest] {tid}: 已收割 -> {out}")
    elif status in ("FAIL", "FAILED", "ERROR"):
        _pop_pending_task(tid)
        print(f"[harvest] {tid}: 任务已失败，从落盘移除")
    else:
        print(f"[harvest] {tid}: 仍在生成（status={status or '?'}），保留落盘")
def make_cmd(j: Path, sid: str, pool: str, pin: int, frames_dir: Path,
             clips_dir: Path, phase: str, args, bible: dict | None = None
             ) -> tuple[list[str], Path, str]:
    """构造单镜命令；返回 (cmd, out_path, skip_reason)。skip_reason 非空=跳过。
    v4.6 起为模块级函数（cmd_batch 与 pass2 过渡镜共用）；
    worker 子命令一律走 media_gen.py 入口（mg_batch 无 __main__）。
    bible：角色圣经（<shots>/characters.json，v4.9.0）——shot 用 characters:[id] 引用。"""
    d = json.loads(j.read_text(encoding="utf-8"))
    mg_py = str(Path(__file__).resolve().parent / "media_gen.py")
    if phase == "images":
        # 过渡镜无 t2i_prompt（首帧来自邻居 clip 末帧，编排自动抽）——不进 images 阶段
        if is_transition(d):
            return [], frames_dir / f"{sid}.png", "skip (transition)"
        out = frames_dir / f"{sid}.png"
        if out.exists() and out.stat().st_size > 0:
            return [], out, "skip (exists)"
        # 角色一致性（v4.7.1）：shot JSON 的 ref_image（字符串或列表）→ 图生图参考。
        # 相对路径按 shots 根解析（shot JSON 就在 shots/ 下，写 "char_hero.png" 即可）
        # v4.9.0：再叠角色圣经——shot 写 characters:["hero"] 即引用 <shots>/characters.json
        # 里定义好的角色设定图（漫剧跨几十镜引用同一角色，不该每镜手抄路径）
        refs, miss_chars = merge_character_refs(d, bible or {})
        if miss_chars:
            return [], out, (f"MISS (character {','.join(miss_chars)} 不在 characters.json——"
                             f"先补角色定义，别静默丢一致性)")
        cmd = [sys.executable, mg_py, "image",
               "--provider", pool, "--prompt", d["t2i_prompt"],
               "--size", d.get("size", "1344x768"), "--out", str(out),
               "--pin-key", str(pin)]
        for r in refs:
            rp = Path(r)
            if not rp.is_absolute() and not rp.exists():
                cand = frames_dir.parent / r
                if cand.exists():
                    rp = cand
            if not rp.exists():
                return [], out, f"MISS (ref_image {r} not found)"
            cmd += ["--ref-image", str(rp)]
        return cmd, out, ""
    out = clips_dir / f"clip_{sid}.mp4"   # clip_ 前缀与 postprocess concat 的 glob("clip_*.mp4") 对齐
    # 断点续跑：产物可能是 .webp（LTX/本地网关出动画 webp，_final_out 按真实后缀落盘）
    # 只认 .mp4 会误判"未完成"→重新提交→重复扣费（#3）
    existing = find_product(clips_dir, sid)
    if existing:
        return [], existing, "skip (exists)"
    # ── 过渡镜（v4.6 方案二）：首帧=from 镜 clip 末帧（seed），尾帧=to 镜首帧 ──
    if is_transition(d):
        tr = d["transition"]
        seed = frames_dir / f"{sid}_seed.png"          # 编排抽的 from 末帧
        src_clip = find_product(clips_dir, tr["from"])
        if not seed.exists():
            return [], out, f"MISS (transition {sid}: seed {seed.name} 未抽——先跑普通镜再跑过渡镜)"
        if src_clip is not None and seed.stat().st_mtime < src_clip.stat().st_mtime:
            return [], out, (f"STALE (transition {sid}: from clip {src_clip.name} 比新"
                             f"——删 seed 重抽)")
        last = frames_dir / f"{tr['to']}.png"
        if not last.exists():
            return [], out, f"MISS (transition {sid}: to 首帧 {last.name} 不存在)"
        cmd = [sys.executable, mg_py, "video",
               "--provider", pool, "--prompt", d.get("i2v_prompt", ""),
               "--image", str(seed), "--out", str(out),
               "--num-frames", str(d.get("num_frames", 121)),
               "--negative", negative_for_shot(d, args.negative),
               "--pin-key", str(pin),
               "--last-frame", str(last)]
        if getattr(args, "video_size", ""):
            cmd += ["--video-size", args.video_size]
        if getattr(args, "video_duration", ""):
            cmd += ["--video-duration", args.video_duration]
        return cmd, out, ""
    frame = frames_dir / f"{sid}.png"
    if not frame.exists():
        return [], out, "MISS (no frame)"
    cmd = [sys.executable, mg_py, "video",
           "--provider", pool, "--prompt", d.get("i2v_prompt", ""),
           "--image", str(frame), "--out", str(out),
           "--num-frames", str(d.get("num_frames", 121)),
           "--negative", negative_for_shot(d, args.negative),
           "--pin-key", str(pin)]
    # 过渡镜（首尾帧双条件）：shot JSON 写 last_frame=<图片路径> 即透传。
    # 图片须已存在（通常是下一镜的关键帧或上镜末帧抽帧）——缺失时提前报，
    # 别等提交后才发现（提交即扣额度）
    if d.get("last_frame"):
        _lf = Path(d["last_frame"])
        if not _lf.exists():
            return [], out, f"MISS (last_frame {d['last_frame']} not found)"
        cmd += ["--last-frame", str(_lf)]
    if getattr(args, "video_size", ""):
        cmd += ["--video-size", args.video_size]
    if getattr(args, "video_duration", ""):
        cmd += ["--video-duration", args.video_duration]
    return cmd, out, ""


def estimate_calls(n_shots: int, retries: int = 0) -> dict:
    """本次批量的调用次数预估（纯函数）。

    上限 = 镜数 × (1 + 重试次数)：每镜至少 1 次生成，最多再重试 retries 次。
    免费池的"成本"就是额度消耗，故以调用次数为准（ledger 只有事后记账，缺事前量）。"""
    n, r = max(0, int(n_shots)), max(0, int(retries))
    return {"shots": n, "retries": r, "max_calls": n * (1 + r)}


def budget_error(est: dict, limit: int) -> str | None:
    """额度门禁判定（纯函数）：超限返回错误消息，否则 None。limit<=0 = 未设门禁。"""
    lim = int(limit or 0)
    if lim > 0 and int(est.get("max_calls", 0)) > lim:
        return (f"预估调用上限 {est['max_calls']} 次 > 门禁 {lim} 次"
                f"（{est['shots']} 镜 × 最多 {1 + int(est.get('retries', 0))} 次）——"
                f"收窄 --only / 降 --retries，或调高 --max-calls")
    return None


def skip_kind(reason: str) -> str:
    """make_cmd 的 skip 原因分类（纯函数，可单测）：

    - **done**：真的不用跑——`skip (exists)`（断点续跑）、`skip (transition)`
      （过渡镜不在 images 阶段跑）→ 视为正常，不报错、不进重试清单。
    - **blocked**：**没跑成**——`MISS (...)`（缺首帧/缺依赖/seed 未抽）、
      `STALE (...)`（邻居重拍后 seed 过期）→ 必须进 batch_run.json 并计入失败退出码，
      否则成片静默缺一段、`--retry-failed` 永远抓不到（2026-09 复测发现的真 P0）。
    """
    return "blocked" if str(reason).startswith(("MISS", "STALE")) else "done"


def record_skip(results: list[str], provider_map: dict, sid: str, reason: str) -> bool:
    """记录一次跳过；返回是否 blocked。调用方据此决定是否继续保持 worker 状态。"""
    if skip_kind(reason) == "blocked":
        results.append(f"MISS {sid} [{reason}]")
        provider_map[sid] = "blocked"
        return True
    results.append(f"skip {sid}")
    return False


def run_shot_once(j: Path, sid: str, pool: str, pin: int, cmd: list[str], out: Path,
                  args, clips_dir: Path, qc_dir: Path, results: list[str],
                  provider_map: dict, rl, consecutive_fail: int) -> int:
    """执行单镜：lint → 重试 → 断点兜底 → qcgate → qc → 记结果；返回新的连续失败计数。

    **pass1（多 worker 队列）与 pass2（过渡镜串行）共用此实现**——此前两份复制逻辑
    导致 pass2 漏掉 qcgate/qc（locality 崩塌：改一处必漏另一处）。新增镜头级能力
    只需在这里加一次。
    """
    pp_py = str(Path(__file__).resolve().parent / "postprocess.py")
    mg_py = str(Path(__file__).resolve().parent / "media_gen.py")
    # 出镜前 prompt lint（优化②）：slop 词/词数越界/i2v 重述主体，FAIL 直接记失败不重试
    # ——prompt 问题重试 N 次也是白烧额度。--no-lint 可关。
    if not getattr(args, "no_lint", False):
        if prompt_lint_snapshot(j, args.phase) == "FAIL":
            with rl:
                results.append(f"FAIL(lint) {sid} [{pool} key#{pin}]")
                provider_map[sid] = f"{pool} key#{pin} (lint)"
            print(f"[batch] {sid} prompt lint 未过（slop/词数/主体漂移）——"
                  f"修 {j.name} 后重跑（lint 详情："
                  f"`python prompt_lint.py {j} --kind {args.phase}`）", file=sys.stderr)
            return consecutive_fail
    last_rc = 1
    for attempt in range(1, args.retries + 1):
        rc = subprocess.call(cmd)
        last_rc = rc
        if rc == 0:
            break
        if rc == 4:
            # 超时协议：任务已受理落盘（提交即扣），重试=重复扣费，严禁重试
            print(f"[batch] {sid} 轮询超时(rc=4)：任务已落盘，跑 harvest 收割",
                  file=sys.stderr)
            break
        print(f"[batch] {sid} 失败(rc={rc})，重试 {attempt}/{args.retries}", file=sys.stderr)
    # 断点续跑兜底：重试后仍非 0，但产物已存在且非空，视为成功
    # （.webp 同认：_final_out 可能把产物落成动画 webp，见 make_cmd 的 #3 说明）
    _prod = out if out.exists() and out.stat().st_size > 0 else None
    if _prod is None and args.phase == "videos":
        for _ext in PRODUCT_EXTS:
            _w = clips_dir / f"clip_{sid}{_ext}"
            if _w.exists() and _w.stat().st_size > 0:
                _prod = _w
                break
    if last_rc != 0 and _prod is not None:
        last_rc = 0
    # QC 硬门禁（#4）：机器能判的（黑帧/过曝/静帧/规格）就地判，FAIL 覆盖为失败 →
    # --retry-failed 重跑。只拦"明显生成失败"，美学崩坏仍交人眼。
    if last_rc == 0 and args.qcgate and args.phase == "videos" and _prod is not None:
        gate_cmd = [sys.executable, pp_py, "qcgate", str(_prod)]
        if args.qcgate_strict:
            gate_cmd.append("--strict")
        try:
            g = run_capture(gate_cmd, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"[batch] {sid} qcgate 超时 120s（ffmpeg 抽帧挂起？），记 FAIL 待重跑",
                  file=sys.stderr)
            last_rc = 2
            g = None
        if g is not None and g.returncode != 0:
            for ln in (g.stdout or "").splitlines():
                if ln.strip():
                    print(f"[batch] {sid} qcgate: {ln.strip()}", file=sys.stderr)
            last_rc = 2      # 记为 FAIL，--retry-failed 会重跑
            print(f"[batch] {sid} qcgate 未过（黑帧/过曝/静帧/规格），记 FAIL 待重跑",
                  file=sys.stderr)
    with rl:
        if last_rc == 0:
            consecutive_fail = 0
            results.append(f"OK {sid} ({pool} key#{pin})")
            provider_map[sid] = f"{pool} key#{pin}"
            if args.qc and args.phase == "videos":
                qc_dir.mkdir(exist_ok=True)
                subprocess.call([sys.executable, mg_py, "qc", str(out), str(qc_dir)])
        elif last_rc == 4:
            # 超时在途不算失败（不进 consecutive_fail，慢池 worker 不误退场）
            results.append(f"PENDING {sid} (任务已落盘，跑 harvest 收割)")
            provider_map[sid] = f"{pool} key#{pin} (timeout-pending)"
        else:
            consecutive_fail += 1
            results.append(f"FAIL(rc={last_rc}) {sid} [{pool} key#{pin}]")
            provider_map[sid] = f"{pool} key#{pin} (failed)"
    return consecutive_fail


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
    # 跨平台去重枚举：Windows glob 大小写不敏感，两个 pattern 会重复匹配 shot_*.json
    # （见 mg_core.list_shot_files），不去重则每镜双跑
    jsons = list_shot_files(shots_dir)
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
    bible = load_character_bible(shots_dir)   # 角色圣经（v4.9.0，缺省 {} 不影响旧流程）

    plan: list[tuple[Path, str]] = []
    for j in jsons:
        d = json.loads(j.read_text(encoding="utf-8"))
        sid = d.get("shot_id") or j.stem
        plan.append((j, sid))

    # ── 过渡镜依赖校验（v4.6 方案二）──────────────────────
    # transition.from/to 指向不存在的镜 → 提前 die（否则永远 MISS，静默死循环）。
    # 本校验对两个 phase 都生效（images 阶段也需要知道全部镜号做依赖图）。
    sids = {sid for _, sid in plan}
    for j, sid in plan:
        d = json.loads(j.read_text(encoding="utf-8"))
        if not is_transition(d):
            continue
        tr = d["transition"]
        missing = [dep for dep in (tr["from"], tr["to"]) if dep not in sids]
        if missing:
            die(f"过渡镜 {sid} 的 transition 依赖 {missing} 不在分镜列表"
                f" {sorted(sids, key=natkey)}——检查 shot_id 拼写", 2)

    # --only：只跑指定镜号（逗号分隔，按 shot_id）。hybrid 模式需要它：
    # 重点镜出真视频、过场镜交给 kenburns，所以视频阶段只喂 hero_shots。
    only = {s.strip() for s in (getattr(args, "only", "") or "").split(",") if s.strip()}
    if only:
        # 过渡镜**不按 --only 硬滤**（v4.7.4）：它是两镜之间的"桥"——邻居在本次运行
        # 就该接上（hybrid 核心场景：重点镜出真视频、过渡镜衔接重点镜）；from 镜不在
        # 本次运行但有历史 clip 也能抽 seed。只有 from 完全带不动（不在 --only 且无
        # 任何历史产物）时才排除——留着的唯一作用是 pass2 刷 MISS。
        kept: list[tuple[Path, str]] = []
        for j, sid in plan:
            d = json.loads(j.read_text(encoding="utf-8"))
            if sid in only or not is_transition(d):
                kept.append((j, sid))
                continue
            frm = d["transition"]["from"]
            if frm in only or find_product(clips_dir, frm):
                kept.append((j, sid))
            else:
                print(f"[batch] 过渡镜 {sid} 跳过：from 镜 {frm} 不在 --only 且无历史产物"
                      f"（要跑它就把 {frm} 加进 --only 或先出它的片）", file=sys.stderr)
        plan = kept
        if not any(sid in only for _, sid in plan):
            die(f"--only 的镜号 {sorted(only, key=natkey)} 在分镜 JSON 里一个都没匹配上", 2)
        n_trans = sum(1 for _, sid in plan if sid not in only)
        print(f"[batch] --only 只跑 {sorted(only, key=natkey)}"
              + (f" + {n_trans} 个过渡桥" if n_trans else "")
              + f"（共 {len(plan)} 镜）", file=sys.stderr)

    # 事前预算门禁（v4.9.0）：ledger 只记得事后账，跑之前先把调用上限算出来——
    # 免费池的"成本"就是额度，误跑一次（忘 --only / retries 没收）要等跑完才知道
    est = estimate_calls(len(plan), getattr(args, "retries", 0))
    if est["shots"]:
        print(f"[batch] 预估：{est['shots']} 镜 → 调用上限 {est['max_calls']} 次"
              f"（每镜 1 + 重试 {est['retries']}）", file=sys.stderr)
    _berr = budget_error(est, getattr(args, "max_calls", 0))
    if _berr:
        die(_berr, 2)

    # --retry-failed：读上轮 batch_run.json，只重跑 FAIL/PENDING 镜；
    # PENDING 先 harvest（在生成的不重提交，防重复扣费），成功镜靠断点续跑跳过
    if getattr(args, "retry_failed", False):
        run_file = shots_dir / "batch_run.json"
        if not run_file.exists():
            die("--retry-failed 需要先跑过一次 batch（batch_run.json 不存在）", 2)
        prev_shots = json.loads(run_file.read_text(encoding="utf-8")).get("shots", {})
        bad_sids = {sid for sid, tag in prev_shots.items()
                    if tag.endswith("(failed)") or tag.endswith("(timeout-pending)")
                    or tag == "blocked"}
        if not bad_sids:
            print("[batch] 上轮无失败镜，无需补跑")
            sys.exit(0)
        cmd_harvest(args)                    # 先收割已完成出片的 PENDING 任务
        still = {Path(r.get("out", "x.zip")).stem.replace("clip_", "")
                 for r in (_load_state().get("pending_tasks") or {}).values() if r.get("out")}
        waiting = bad_sids & still
        bad_sids -= waiting
        if waiting:
            print(f"[batch] 仍在生成、暂不重提交：{sorted(waiting, key=natkey)}"
                  f"（出片后 harvest 收割）",
                  file=sys.stderr)
        plan = [(j, sid) for j, sid in plan if sid in bad_sids]
        if not plan:
            print(f"[batch] 失败镜全部在生成中或无法定位，本轮无任务"
                  f"（仍在生成：{sorted(waiting, key=natkey)}）")
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
            cmd, out, skip = make_cmd(j, sid, assigned[0][0], assigned[0][1]["n"],
                                      frames_dir, clips_dir, args.phase, args, bible)
            if skip:
                print(f"  {sid} -> {out}  [{skip}]")
            else:
                print(f"  {sid} -> {out}\n    {' '.join(cmd)}")
        sys.exit(0)

    # ── 两趟调度（v4.6 方案二）────────────────────────────
    # 过渡镜依赖邻居 clip（from 末帧），必须等 pass1 普通镜出片。真跑（非 dry-run）
    # 时 pass1 完成后先抽 from 镜末帧（seed），再跑 pass2 过渡镜；缺依赖 MISS 跳过。
    trans = [(j, sid) for j, sid in plan
             if is_transition(json.loads(j.read_text(encoding="utf-8")))]
    normal = [(j, sid) for j, sid in plan if (j, sid) not in trans]
    if args.phase == "videos" and trans and not args.dry_run:
        print(f"[batch] 过渡镜 {sorted((sid for _, sid in trans), key=natkey)}"
              f" 依赖邻居出片，pass1 普通镜先行", file=sys.stderr)

    task_q: "queue.Queue[tuple[Path, str]]" = queue.Queue()
    for item in normal:
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
            cmd, out, skip = make_cmd(j, sid, pool, pin, frames_dir, clips_dir,
                                      args.phase, args, bible)
            if skip:
                with rl:
                    record_skip(results, provider_map, sid, skip)
                consecutive_fail = 0
                continue
            consecutive_fail = run_shot_once(
                j, sid, pool, pin, cmd, out, args, clips_dir, qc_dir,
                results, provider_map, rl, consecutive_fail)

    threads: list[_t.Thread] = []
    for w in range(args.workers):
        th = _t.Thread(target=run_worker, args=(w,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()

    # ── pass2：抽 from 镜末帧 + 跑过渡镜（v4.6 方案二）────────
    # 过渡镜首帧 = from 镜 clip 末帧。真跑时抽帧（ffmpeg -sseof）；dry-run 已在
    # 上方 dry-run 分支提前退出。STALE 检测在 make_cmd（mtime 比较）。
    if args.phase == "videos" and trans and not args.dry_run:
        mg_path = str(Path(__file__).resolve().parent / "media_gen.py")
        for j, sid in trans:
            d = json.loads(j.read_text(encoding="utf-8"))
            tr = d["transition"]
            src_clip = find_product(clips_dir, tr["from"])
            seed = frames_dir / f"{sid}_seed.png"
            if src_clip is None:
                print(f"[batch] 过渡镜 {sid}: from 镜 {tr['from']} 无 clip"
                      f"（pass1 未出片？）——MISS 跳过", file=sys.stderr)
                continue
            # clip 比 seed 新 → 重抽（邻居重拍失效链）
            if seed.exists() and seed.stat().st_mtime >= src_clip.stat().st_mtime:
                pass    # seed 仍新鲜，不重抽
            else:
                rc = subprocess.call([sys.executable, mg_path, "last-frame",
                                      str(src_clip), str(seed)])
                if rc != 0:
                    print(f"[batch] 过渡镜 {sid}: 抽 {src_clip.name} 末帧失败"
                          f"——MISS 跳过", file=sys.stderr)
                    continue
        # 过渡镜单线程逐镜跑（通常 1-2 个，不值得再开多 worker；
        # 且要按 natkey 顺序出片保证 concat 顺序可预期）
        for j, sid in trans:
            cmd, out, skip = make_cmd(j, sid, assigned[0][0], assigned[0][1]["n"],
                                      frames_dir, clips_dir, args.phase, args, bible)
            if skip:
                with rl:
                    record_skip(results, provider_map, sid, skip)
                print(f"[batch] {sid} -> {out}  [{skip}]", file=sys.stderr)
                continue
            # 与 pass1 共用执行器 → 过渡镜同样过 lint / qcgate / qc（v4.7.1 补）
            run_shot_once(j, sid, assigned[0][0], assigned[0][1]["n"], cmd, out, args,
                          clips_dir, qc_dir, results, provider_map, rl, 0)

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
    blocked = [r for r in results if r.startswith("MISS")]
    pending = [r for r in results if r.startswith("PENDING")]
    print(f"[batch] phase={args.phase} workers={args.workers} done. "
          f"failed={failed or 'none'}"
          + (f"  blocked={len(blocked)}（缺首帧/缺依赖，补后 --retry-failed）" if blocked else "")
          + (f"  pending={len(pending)}（超时在途，跑 harvest 收割）" if pending else "")
          + f"  (明细见 {shots_dir / 'batch_run.json'})", flush=True)
    sys.exit(batch_exit_code(results))


def batch_exit_code(results: list[str]) -> int:
    """批量结果 → 进程退出码（供 pipeline 编排决策，须可单测）。
    0=全成功；1=有 FAIL（重试后仍失败）；4=有 PENDING（超时在途，任务已受理落盘，
    重试=重复扣费——pipeline 必须 exit 4 交还 agent 问用户三选，不能静默放行）。"""
    if any(r.startswith("PENDING") for r in results):
        return 4
    # MISS=没跑成（缺首帧/缺依赖）：必须非零，否则 pipeline 带着缺口继续 concat
    if any(r.startswith(("FAIL", "MISS")) for r in results):
        return 1
    return 0

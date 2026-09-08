"""pipeline.py — 一键编排：把 Step 2-6 的既有命令按 plan.json 串起来（#1）。

边界（重要，勿越界）：
  本脚本只做**编排**，不做任何审美决策——mode / hero_shots / workers / 池顺序 / 水印
  全部读自 Step 1 已落盘的 plan.json（那是"问过用户并记住"的结果）。
  plan.json 缺 mode 等决策 → 直接 die 退回 Step 1，**绝不默认 full 替用户拍板**
  （SKILL 的"绝不自动降级"在这里同样成立）。

阶段（顺序固定，可 --stop-after 提前停）：
  images   关键帧批量（batch --phase images，断点续跑）
  videos   重点镜 i2v（full=全部镜 / hybrid=hero_shots；stills 档跳过此阶段）
           有 PENDING（超时在途）→ exit 4 交还 agent：切池 / 续等 / 放弃（问用户三选）
  harvest  收割在途任务
  kenburns hybrid/stills 的缓推补齐（只补缺失镜，断点续跑）
  sound    声音设计（vo_build：vo_lines.json → 旁白 + 字幕；有 vo_lines.json 才跑）
  concat   后期统一 + 声音三件套 + xfade + 自检 → final.mp4（之后停在 QC 门前）
  watermark 去水印旁线（可选：--watermark <provider>；--watermark-dry-run 只列待处理档）

**停在 QC 门前**：concat 一完就停——视觉验收（qcgate 机器门禁可自动，抽帧美学
验收必须人看）是人工环节，不自动进交付。final.mp4 的路径打印出来交还 agent。
watermark 是可选的交付前清洗，不默认开启（需用户明示 provider 才跑）。

用法：
  python scripts/pipeline.py shots60/ --dry-run            # 只打印各阶段命令
  python scripts/pipeline.py shots60/ --stop-after images  # 每阶段人工把关
  python scripts/pipeline.py shots60/ --final out/我的片.mp4
  python scripts/pipeline.py shots60/ --watermark <渠道> --watermark-dry-run  # 只列要抹水的档
"""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mg_core import list_shot_files, natkey  # noqa: E402

STAGES = ("images", "videos", "harvest", "kenburns", "sound", "concat", "watermark")


def die(msg: str, rc: int = 2) -> None:
    print(f"[pipeline] ERROR: {msg}", file=sys.stderr)
    sys.exit(rc)


def _run(cmd: list[str], dry: bool, log: list[str]) -> int:
    log.append(" ".join(cmd))
    print("+ " + " ".join(cmd), flush=True)
    if dry:
        return 0
    r = subprocess.run(cmd)
    return r.returncode


def _shots(shots_dir: Path) -> list[dict]:
    """分镜清单（shot_id 用 shot_id 字段，缺省用文件 stem；自然排序，跨平台去重）。"""
    js = list_shot_files(shots_dir)
    if not js:
        die(f"未找到分镜 JSON: {shots_dir}/S*.json 或 shot_*.json", 2)
    out = []
    for j in js:
        d = json.loads(j.read_text(encoding="utf-8"))
        out.append({"sid": d.get("shot_id") or j.stem, "path": j})
    return out


def _sync_frames(shots_dir: Path, shots: list[dict]) -> int:
    """把散在 shots/ 下的首帧图（shot_01.png 这类手动 Step 4 产物）同步进 frames/。
    手动流程的首帧放在 shots/ 根目录，batch 的 images 阶段则落在 shots/frames/——
    两者统一，保证后续 i2v/kenburns 都从 frames/ 取图。只补不覆盖。"""
    frames = shots_dir / "frames"
    frames.mkdir(exist_ok=True)
    n = 0
    for s in shots:
        sid, f = s["sid"], frames / f"{s['sid']}.png"
        if f.exists() and f.stat().st_size > 0:
            continue
        cands = [shots_dir / f"{sid}.png",
                 shots_dir / f"shot_{sid}.png"]
        # 命名兜底：schema 用 shot_id="S03"（引 frames/S03.png），而 SKILL 手动流程
        # 产物是 shots/shot_01.png（无 S 前缀）——只查上面两个候选会漏同步。
        # 补 shot_<去S前缀>.png：S03 → shot_03.png
        bare = sid[1:] if sid[:1].upper() == "S" and sid[1:].isdigit() else ""
        if bare:
            cands.append(shots_dir / f"shot_{bare}.png")
        for c in cands:
            if c.exists() and c.stat().st_size > 0:
                shutil.copyfile(c, f)
                n += 1
                break
    return n


def cmd(args) -> None:
    shots_dir = Path(args.shots)
    if not shots_dir.is_dir():
        die(f"shots 目录不存在: {shots_dir}", 2)
    plan_p = Path(args.plan) if args.plan else shots_dir / "plan.json"
    if not plan_p.exists():
        die(f"找不到 plan.json（{plan_p}）——先走 SKILL Step 1 向用户确认并落盘"
            f"（mode/重点镜/池顺序是审美决策，编排器不替你拍板）", 3)
    plan = json.loads(plan_p.read_text(encoding="utf-8"))

    mode = plan.get("mode", "")
    if mode not in ("full", "hybrid", "stills"):
        die(f"plan.json 缺 mode（或值非法: {mode!r}）——回 Step 1 问④ 落盘后再跑", 3)
    hero = plan.get("hero_shots")
    if mode == "hybrid" and not hero:
        die("mode=hybrid 但 plan.json 没有 hero_shots——回 Step 1 问④ 让用户指定重点镜", 3)
    hero = [str(x) for x in hero] if hero else None

    shots = _shots(shots_dir)
    frames = shots_dir / "frames"
    clips = shots_dir / "clips"
    stop = args.stop_after
    # P2-1 修复：用户显式传 --watermark 却 stop_after 不含 watermark 阶段 → 静默跳过
    # （"以为做了 dry-run，实际一行没跑"）。这里自动提升 stop 点并提示。
    if args.watermark and STAGES.index("watermark") > STAGES.index(stop):
        print(f"[pipeline] --watermark={args.watermark} 已给 → 自动把 stop 提升到 watermark 阶段"
              f"（原 --stop-after={stop} 会跳过水印，白给参数）", file=sys.stderr)
        stop = "watermark"
    qcseq_info: dict | None = None
    log: list[str] = []
    mg = str(Path(__file__).resolve().parent / "media_gen.py")
    pp = str(Path(__file__).resolve().parent / "postprocess.py")
    vb = str(Path(__file__).resolve().parent / "vo_build.py")
    dlg = str(Path(__file__).resolve().parent / "delogo_watermark.py")

    def upto(stage: str) -> bool:
        return STAGES.index(stage) <= STAGES.index(stop)

    # ── 阶段 1: 关键帧（三档都需要首帧，永远全镜）──────────
    if upto("images"):
        copied = 0 if args.dry_run else _sync_frames(shots_dir, shots)
        if copied:
            print(f"[pipeline] 同步 {copied} 张散图进 {frames}/")
        cmd = [sys.executable, mg, "batch", str(shots_dir), "--phase", "images",
               "--workers", str(args.workers_image)]
        if args.provider_image:
            cmd += ["--provider", args.provider_image]
        rc = _run(cmd, args.dry_run, log)
        if rc != 0 and not args.dry_run:
            if rc == 4:
                die(f"关键帧阶段有超时在途任务（已落盘，harvest 收割后重跑即断点续跑）。"
                    f"明细见 {shots_dir}/batch_run.json。", 4)
            die(f"关键帧阶段 rc={rc}（明细见 {shots_dir}/batch_run.json）。"
                f"修好后重跑 pipeline 会断点续跑。", rc)

    # ── 阶段 2: i2v（full 全镜 / hybrid 重点镜 / stills 跳过）──
    pending = False
    if upto("videos") and mode in ("full", "hybrid"):
        # 出镜前全镜 prompt lint（优化②）：slop/词数越界/主体漂移在烧额度前拦住。
        # 缺 plan 已在上方 die；这里 lint 全部 i2v_prompt，FAIL 直接退回 Step 3 改写。
        if not args.no_lint and not args.dry_run:
            import prompt_lint
            lex_p = Path(__file__).resolve().parents[1] / "references" / "anti-slop-lexicon.md"
            hard, mood = prompt_lint.parse_lexicon_md(lex_p)
            bad = []
            for s in shots:
                try:
                    d = json.loads(s["path"].read_text(encoding="utf-8"))
                except Exception:
                    bad.append(s["sid"])
                    continue
                if any(i["level"] == "FAIL"
                       for i in prompt_lint.lint_shot(d, "videos", hard, mood)):
                    bad.append(s["sid"])
            if bad:
                die(f"{len(bad)} 镜 i2v prompt lint 未过：{sorted(bad, key=natkey)}。"
                    f"回 Step 3 按 anti-slop 词表改写（详情："
                    f"`python prompt_lint.py {shots_dir} --kind videos`）——"
                    f"别让 slop 词烧额度。", 3)
        cmd = [sys.executable, mg, "batch", str(shots_dir), "--phase", "videos",
               "--workers", str(args.workers_video)]
        if args.provider_video:
            cmd += ["--provider", args.provider_video]
        if mode == "hybrid" and hero:
            cmd += ["--only", ",".join(hero)]
            print(f"[pipeline] hybrid：真视频只跑重点镜 {hero}")
        if args.qcgate:
            cmd += ["--qcgate"]
            if args.qcgate_strict:
                cmd += ["--qcgate-strict"]
        if args.retry_failed:
            cmd += ["--retry-failed"]
        rc = _run(cmd, args.dry_run, log)
        if rc != 0 and not args.dry_run:
            br = shots_dir / "batch_run.json"
            tags = json.loads(br.read_text(encoding="utf-8")).get("shots", {}) if br.exists() else {}
            # PENDING 优先于 FAIL：任务已受理在途，重试=重复扣费，必须 exit 4 交还 agent
            pend = [sid for sid, t in tags.items() if t.endswith("(timeout-pending)")]
            if rc == 4 or pend:
                # 超时在途：交还 agent 问用户三选（切池/续等/放弃），不自动重试防重复扣费
                n = len(pend) or "≥1（明细缺失）"
                die(f"有 {n} 镜超时在途（提交已扣、任务在跑）"
                    + (f"：{sorted(pend, key=natkey)}" if pend else "")
                    + f"。按 SKILL「视频超时询问协议」问用户：切下一池 / 续等 --wait-task / 放弃。"
                    f"出片后 `harvest` 收割，再 `--retry-failed` 补 FAIL 镜。", 4)
            failed = [sid for sid, t in tags.items() if t.endswith("(failed)")]
            die(f"视频阶段有 {len(failed)} 镜失败：{sorted(failed, key=natkey)}。"
                f"修好后 `pipeline.py --retry-failed` 或 batch --retry-failed 补跑。", 1)

    # ── 阶段 3: 收割 ──────────────────────────────────────
    if upto("harvest"):
        rc = _run([sys.executable, mg, "harvest"], args.dry_run, log)
        # harvest 不因"还有任务在生成"失败（那些保留落盘）；仅真出错（如状态文件
        # 损坏导致崩溃）才中断。rc!=0 时停止，避免带着半收状态进 concat。
        if rc != 0 and not args.dry_run:
            die(f"harvest 收割失败 rc={rc}——先修复再继续，别把在途任务带进 concat。", rc)

    # ── 阶段 4: 缓推补齐（hybrid 的非重点镜 / stills 全部）──
    # 只转"还没有 clip 的镜"：hybrid 的重点镜在阶段2已出真视频（done=True 跳过），
    # 剩下的（hybrid 过场镜 / stills 全镜）才补缓推。天然按 mode 区分，无需判断 hero。
    if upto("kenburns") and mode in ("hybrid", "stills"):
        made = 0
        for s in shots:
            sid = s["sid"]
            done = any((clips / f"clip_{sid}{e}").exists() and
                       (clips / f"clip_{sid}{e}").stat().st_size > 0
                       for e in (".mp4", ".webp"))
            if done:
                continue
            img = frames / f"{sid}.png"
            if not img.exists() and not args.dry_run:
                die(f"缓推需要首帧 {img}，但 images 阶段没产出它", 2)
            rc = _run([sys.executable, pp, "kenburns", str(img),
                       str(clips / f"clip_{sid}.mp4"),
                       "--duration", str(args.kenburns_dur)], args.dry_run, log)
            if rc != 0 and not args.dry_run:
                die(f"kenburns {sid} 失败 rc={rc}", rc)
            made += 1
        if made:
            print(f"[pipeline] 缓推补齐 {made} 镜")

    # ── 阶段 5: 声音设计（vo_build：旁白 + 字幕；有 vo_lines.json 才跑）──
    # 声音是"要不要加"的审美决策：shots/vo_lines.json 存在（用户写了脚本）才自动做，
    # 缺省不加旁白（纯音乐/环境音成片也是选项）。产出自动带进 concat。
    voice_final = args.voice
    subs_final = args.subtitles
    if upto("sound"):
        vo_lines = Path(args.sound_lines) if args.sound_lines else shots_dir / "vo_lines.json"
        if vo_lines.exists():
            vo_out = shots_dir / "vo.m4a"
            subs_out = shots_dir / "subtitles_final.json"
            cmd = [sys.executable, vb, str(vo_lines), "--out", str(vo_out),
                   "--subs-out", str(subs_out)]
            if args.sound_voice:
                cmd += ["--voice", args.sound_voice]
            if args.sound_speed != 1.0:
                cmd += ["--speed", str(args.sound_speed)]
            if args.sound_skip_tts:
                cmd += ["--skip-tts"]
            if args.sound_auto_shift:
                cmd += ["--auto-shift", "--gap", str(args.sound_gap)]
            rc = _run(cmd, args.dry_run, log)
            if rc != 0 and not args.dry_run:
                die(f"声音设计（vo_build）失败 rc={rc}", rc)
            if not args.voice:
                voice_final = str(vo_out)
            if not args.subtitles:
                subs_final = str(subs_out)
        else:
            print(f"[pipeline] 无 {vo_lines.name}，跳过声音设计（纯画面/自己混音）")

    # ── 阶段 6: 后期统一 + 声音三件套 + xfade + 自检 → final ──
    final = Path(args.final) if args.final else (shots_dir.parent / "final.mp4")
    if upto("concat"):
        # 优化⑤：concat 前跨镜首帧一致性粗检（WARN 报告不拦——跑偏镜重跑或整体调色，是人的决策）
        # P2-2 修复：区分 exit 1=WARN（提示放行）与 exit 2=真错误（输入错误/抽帧失败，必须中断）
        if not args.no_qcseq:
            qrc = _run([sys.executable, pp, "qcseq", str(clips)], args.dry_run, log)
            if qrc == 1 and not args.dry_run:
                qcseq_info = {"rc": 1, "note": "有 WARN：相邻镜色调跳变（不拦，人眼确认跑偏镜）"}
                print("[pipeline] qcseq 提示相邻镜色调跳变（不拦流程）——人眼确认跑偏镜，"
                      "单独重跑或整体调色后重新 concat")
            elif qrc >= 2:
                die(f"qcseq 检查失败（exit {qrc}：输入错误/抽帧失败）——先修复再 concat，"
                    f"别带着坏片段拼接", qrc)
        cmd = [sys.executable, pp, "concat", str(clips), "--out", str(final),
               "--target-res", args.target_res]
        if plan.get("xfade"):
            cmd += ["--xfade", ",".join(str(x) for x in plan["xfade"])]
        if plan.get("freeze_last"):
            cmd += ["--freeze-last", str(plan["freeze_last"])]
        if voice_final:
            cmd += ["--voice", voice_final]
        if args.bgm:
            cmd += ["--bgm", args.bgm, "--bgm-db", str(args.bgm_db)]
        if subs_final:
            cmd += ["--subtitles", subs_final]
        if args.slogan:
            cmd += ["--slogan", args.slogan,
                    "--slogan-position", args.slogan_position]
        rc = _run(cmd, args.dry_run, log)
        if rc != 0 and not args.dry_run:
            die(f"concat 失败 rc={rc}", rc)

    # ── 阶段 7: 去水印旁线（可选：--watermark <provider>；默认不跑）──
    if upto("watermark") and args.watermark:
        wm_cmd = [sys.executable, dlg, str(final), "--provider", args.watermark]
        if args.watermark_dry_run:
            wm_cmd += ["--dry-run"]
        rc = _run(wm_cmd, args.dry_run, log)
        if rc != 0 and not args.dry_run:
            die(f"去水印失败 rc={rc}（--watermark-dry-run 先目检框位，确认后再真跑）", rc)

    # 执行日志落盘（可追溯每阶段实际跑了什么；P3-7：qcseq 结果也记上，audit 可读）
    if not args.dry_run:
        (shots_dir / "pipeline_run.json").write_text(
            json.dumps({"plan_mode": mode, "hero": hero, "stop_after": stop,
                        "stages": STAGES[:STAGES.index(stop) + 1], "final": str(final),
                        "qcseq": qcseq_info},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    if args.dry_run:
        print(f"\n[pipeline] DRY-RUN 完成：以上 {len(log)} 条命令未执行")
        return
    print(f"\n[pipeline] ✅ 编排完成 → {final}")
    print(f"[pipeline] ⏸ 停在 QC 门前：视觉验收（抽帧美学）必须人看，"
          f"机器门禁可先跑 `postprocess.py qcgate {final}`；"
          f"验收过再交付，不合格按诊断表单变量重拍。")


def audit(shots_dir: Path) -> int:
    """优化⑪：项目进度审计视图——每镜 出图/出片/失败 状态 + 对应操作清单。
    读 batch_run.json 状态标签（OK/FAIL/lint/timeout-pending），对照 frames/ 与 clips/ 产物。
    输出可执行的下一步清单（harvest / --retry-failed / 缺帧补图），exit 0 恒。"""
    from postprocess import probe   # 方案B：audit 探 clip 音轨（有声/哑片 列）
    frames = shots_dir / "frames"
    clips = shots_dir / "clips"
    js = list_shot_files(shots_dir)
    if not js:
        print(f"[audit] {shots_dir}: 未找到分镜 JSON", file=sys.stderr)
        return 2
    run_file = shots_dir / "batch_run.json"
    prev = {}
    if run_file.exists():
        prev = (json.loads(run_file.read_text(encoding="utf-8")) or {}).get("shots", {})
    plan = {}
    plan_p = shots_dir / "plan.json"
    if plan_p.exists():
        plan = json.loads(plan_p.read_text(encoding="utf-8"))
    run_log = {}
    run_p = shots_dir / "pipeline_run.json"
    if run_p.exists():
        run_log = json.loads(run_p.read_text(encoding="utf-8"))

    rows = []
    n_fail = n_pend = n_missing_frame = 0
    for j in js:
        d = json.loads(j.read_text(encoding="utf-8"))
        sid = d.get("shot_id") or j.stem
        frame = (frames / f"{sid}.png").exists() and (frames / f"{sid}.png").stat().st_size > 0
        clip_path = next(((clips / f"clip_{sid}{e}") for e in (".mp4", ".webp")
                          if (clips / f"clip_{sid}{e}").exists() and
                          (clips / f"clip_{sid}{e}").stat().st_size > 0), None)
        clip = clip_path is not None
        # 方案B：出片带不带音轨（probe 首帧视频即可——audio 探测驱动 triage 决策）
        au = "—"
        if clip_path is not None:
            try:
                au = "有音" if probe(str(clip_path)).get("audio") else "哑片"
            except Exception as e:
                au = "?"
        tag = prev.get(sid, "")
        st = "OK" if clip else ("出图✓" if frame else "缺帧")
        if tag.endswith("(timeout-pending)"):
            st = f"PENDING 在途 → harvest"
            n_pend += 1
        elif tag.endswith("(failed)") or tag.endswith("(lint)"):
            st = f"FAIL({tag[-12:-1].strip()}) → 修后 --retry-failed"
            n_fail += 1
        elif not frame and not clip:
            st = "缺首帧 → images 阶段"
            n_missing_frame += 1
        rows.append((sid, "出图✓" if frame else "—", "出片✓" if clip else "—", au, st))

    print(f"[audit] {shots_dir}  mode={plan.get('mode', '?')}"
          + (f" hero={plan.get('hero_shots')}" if plan.get("hero_shots") else ""))
    qc = run_log.get("qcseq") or {}
    if qc:
        print(f"  上次 qcseq：rc={qc.get('rc')} {qc.get('note', '')}")
    print(f"  镜数 {len(rows)}   出图 {sum(1 for r in rows if r[1] == '出图✓')}   "
          f"出片 {sum(1 for r in rows if r[2] == '出片✓')}"
          f"   有声 {sum(1 for r in rows if r[3] == '有音')} / 哑片 {sum(1 for r in rows if r[3] == '哑片')}"
          f"（triage 可判要不要补朗读）")
    for sid, f, c, au, st in rows:
        print(f"  {sid:6s} frame={f:4s} clip={c:4s} {au:4s}  {st}")
    todo = []
    if n_pend:
        todo.append(f"harvest 收割 {n_pend} 个在途任务")
    if n_fail:
        todo.append(f"--retry-failed 补跑 {n_fail} 个 FAIL/lint 镜")
    if n_missing_frame:
        todo.append(f"images 阶段补 {n_missing_frame} 镜首帧")
    if not todo and all(r[2] == "出片✓" for r in rows):
        todo.append("全部出片 → concat 拼接出 final")
    print("[audit] 下一步：" + ("；".join(todo) if todo else "无需操作"))
    return 0


# ─── #66 clean：工作区中间产物治理（scan-only 默认，移入 .trash 绝不直接删）──
_TRASH_NAME = ".trash"

# 可再生中间产物：删了可由 shots JSON + plan 重新生成（--yes 后移入 .trash）
_CLEAN_DIRS = ("frames", "clips", "qc_frames", "caps_smoke")
_CLEAN_FILES = ("style_grid.png", "pipeline_run.json")
_CLEAN_GLOBS = ("*.tmp",)
# 绝不动：shots/*.json（源）、plan.json、vo_lines.json（旁白脚本）、batch_run.json
# （账单+断点续跑依据）、final*.mp4（成片）、.trash 本身
# （实现上走默认 keep 分支，无需枚举——见 _walk 的 else）


def plan_clean(root: Path) -> dict:
    """纯扫描分类：{"remove": [...], "keep": [...]}。不改任何文件（scan-only）。"""
    remove: list[Path] = []
    keep: list[Path] = []

    def _walk(p: Path) -> None:
        for child in sorted(p.iterdir()):
            if child.name == _TRASH_NAME:
                continue
            if child.is_dir():
                if child.name in _CLEAN_DIRS:
                    remove.extend(f for f in child.rglob("*") if f.is_file())
                else:
                    _walk(child)
                continue
            if child.name in _CLEAN_FILES or child.suffix == ".tmp" \
               or any(child.match(g) for g in _CLEAN_GLOBS):
                remove.append(child)
            else:
                keep.append(child)   # 其余一律 keep（含 final_* 成片/plan/vo_lines/batch_run）

    if root.is_dir():
        _walk(root)
    return {"remove": remove, "keep": keep}


def execute_clean(plan: dict, root: Path) -> int:
    """把 remove 清单逐文件移入 <root>/.trash/<时间戳>/（可反悔），返回移动数。
    Trash, not delete：绝不 unlink——反悔就把 .trash 里的文件搬回去。"""
    if not plan["remove"]:
        return 0
    ts = time.strftime("%Y%m%d-%H%M%S")
    trash = root / _TRASH_NAME / ts
    moved = 0
    for f in plan["remove"]:
        dest = trash / f.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            f.replace(dest)
            moved += 1
        except OSError as e:
            print(f"[clean] 跳过 {f}（{e}）", file=sys.stderr)
    return moved


def purge_trash(root: Path) -> int:
    """清空 .trash（不可逆——仅对已确认不要的再生产物使用）。返回删除文件数。
    Windows 文件被占用（播放器/杀软）不炸：单文件失败跳过并告警，其余继续。"""
    trash = root / _TRASH_NAME
    if not trash.exists():
        return 0
    n = 0
    for f in sorted(trash.rglob("*"), reverse=True):
        if f.is_file():
            try:
                f.unlink()
                n += 1
            except OSError as e:
                print(f"[clean] purge 跳过 {f}（{e}）", file=sys.stderr)
    for sub in sorted(trash.rglob("*"), reverse=True):
        if sub.is_dir() and not any(sub.iterdir()):
            try:
                sub.rmdir()
            except OSError:
                pass
    try:
        trash.rmdir() if trash.exists() and not any(trash.iterdir()) else None
    except OSError as e:
        print(f"[clean] purge 未能移除 {trash}（{e}）——残留文件下次再 purge", file=sys.stderr)
    return n


def cmd_clean(shots_dir: Path, yes: bool, purge: bool) -> int:
    if purge:
        n = purge_trash(shots_dir)
        print(f"[clean] 已清空 .trash（{n} 个文件，不可逆）")
        return 0
    if not shots_dir.is_dir():
        print(f"[clean] 目录不存在: {shots_dir}", file=sys.stderr)
        return 2
    plan = plan_clean(shots_dir)
    print(f"[clean] {shots_dir}")
    print(f"  可清除（中间产物，可由 shots JSON 重新生成）：{len(plan['remove'])} 个文件")
    for p in plan["remove"][:40]:
        print(f"    - {p.relative_to(shots_dir)}")
    if len(plan["remove"]) > 40:
        print(f"    … 其余 {len(plan['remove']) - 40} 个略")
    print(f"  保留（源/账单/成片）：{len(plan['keep'])} 个文件")
    for p in plan["keep"][:20]:
        print(f"    + {p.relative_to(shots_dir)}")
    if not yes:
        print("[clean] scan-only：确认无误后加 --yes 执行（移入 .trash 可反悔；--purge 真删）")
        return 0
    n = execute_clean(plan, shots_dir)
    print(f"[clean] 已移入 {shots_dir / _TRASH_NAME}（{n} 个文件）。反悔=搬回去；彻底删=--purge")
    return 0


def main() -> None:
    # audit / clean 子命令：位置参数拦截（不破坏 `pipeline.py <shots> --dry-run` 主入口）
    if len(sys.argv) > 1 and sys.argv[1] == "audit":
        if len(sys.argv) < 3:
            print("[pipeline] audit 需要一个 shots 目录", file=sys.stderr)
            sys.exit(2)
        sys.exit(audit(Path(sys.argv[2])))
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        rest = sys.argv[2:]
        flags = [a for a in rest if a.startswith("--")]
        dirs = [a for a in rest if not a.startswith("--")]
        if not dirs:
            print("[pipeline] clean 需要一个 shots 目录", file=sys.stderr)
            sys.exit(2)
        sys.exit(cmd_clean(Path(dirs[0]), "--yes" in flags, "--purge" in flags))
    ap = argparse.ArgumentParser(description="reelcraft 一键编排（读 plan.json，审美决策不替你拍板）")
    ap.add_argument("shots", help="shots 目录（含 S*.json + plan.json）")
    ap.add_argument("--plan", default="", help="plan.json 路径（默认 <shots>/plan.json）")
    ap.add_argument("--stop-after", default="concat", choices=STAGES,
                    help="跑到该阶段后停（默认 concat；想每阶段人工把关可 --stop-after images）")
    ap.add_argument("--dry-run", action="store_true", help="只打印各阶段命令不执行")
    ap.add_argument("--workers-image", type=int, default=3)
    ap.add_argument("--workers-video", type=int, default=3)
    ap.add_argument("--provider-image", default="", help="覆盖出图池（默认按 env/MEDIA_PRIORITY）")
    ap.add_argument("--provider-video", default="", help="覆盖出视频池")
    ap.add_argument("--qcgate", action="store_true", help="视频阶段过机器门禁（FAIL 记失败待重跑）")
    ap.add_argument("--qcgate-strict", action="store_true", help="qcgate 的 WARN 也判失败")
    ap.add_argument("--kenburns-dur", type=float, default=5.0, help="缓推单镜时长秒")
    ap.add_argument("--retry-failed", action="store_true",
                    help="视频阶段只补跑上轮 FAIL/PENDING 镜")
    ap.add_argument("--no-lint", action="store_true",
                    help="跳过出镜前 prompt lint（slop/词数/主体漂移；默认强制）")
    ap.add_argument("--no-qcseq", action="store_true",
                    help="跳过 concat 前的跨镜首帧一致性粗检（qcseq）")
    # 后期透传
    ap.add_argument("--final", default="", help="成片输出路径（默认 <shots>/../final.mp4）")
    ap.add_argument("--target-res", default="1280x720")
    ap.add_argument("--voice", default="", help="旁白音频（默认用 sound 阶段 vo_build 产物）")
    ap.add_argument("--bgm", default="")
    ap.add_argument("--bgm-db", type=float, default=-18.0)
    ap.add_argument("--subtitles", default="", help="字幕 JSON（默认用 sound 阶段产物）")
    ap.add_argument("--slogan", default="")
    ap.add_argument("--slogan-position", default="left", choices=["left", "bottom"])
    # 声音设计（vo_build）
    ap.add_argument("--sound-lines", default="", help="vo_lines.json 路径（默认 <shots>/vo_lines.json）")
    ap.add_argument("--sound-voice", default="", help="旁白音色（留空用 TTS 默认）")
    ap.add_argument("--sound-speed", type=float, default=1.0, help="旁白语速倍率")
    ap.add_argument("--sound-skip-tts", action="store_true", help="vo_build --skip-tts（用已有录音）")
    ap.add_argument("--sound-auto-shift", action="store_true", help="超长句自动顺延后续句")
    ap.add_argument("--sound-gap", type=float, default=0.3, help="句间最小间隔秒（auto-shift 用）")
    # 去水印旁线（可选）
    ap.add_argument("--watermark", default="", help="去水印 provider（渠道名，读 watermark_profiles.json）")
    ap.add_argument("--watermark-dry-run", action="store_true",
                    help="只列待处理档/出红框自检图，不真抹（目检框位后再真跑）")
    args = ap.parse_args()
    cmd(args)


if __name__ == "__main__":
    main()

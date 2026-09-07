"""mg_caps.py — 能力单源：声明是候选，实测是权威（架构整改 #2）。

整改前，同一个问题（"这个池到底支持什么"）有三个互相矛盾的答案：
  1. PROVIDERS 硬编码（sizes / durations / rpm / supports_*）——写代码的人说它支持；
  2. status 的 /models 探测——按模型名关键字猜（猜中 ≠ 能用，猜不中也不等于不支持）；
  3. 真跑一次才知道——但结果从不落盘，下一轮换个人照样从头猜一遍。

本模块把三者收敛成一条链、一个出口：

    声明（PROVIDERS，候选与默认值）
        ↓  caps probe --real（真发一次最小请求）
    实测（~/.workbuddy/.media_caps.json，权威事实）
        ↓
    effective() —— 生成/校验/展示一律只读它

新后端接入从此零改码：
    配好 env → media_gen.py caps probe <pool> --kind video --real
             → 看 ok / 失败原因 → 结果落盘，后续所有决策自动采用

用法：
  media_gen.py caps show [pool]                      # 声明 / 实测 / 生效 三栏对照
  media_gen.py caps probe <pool> --kind video --real # 真发一次（默认只 /models 探测）
  media_gen.py caps clear [pool]                     # 丢弃实测，回落到声明
  media_gen.py caps sync [--check]                   # 实测快照 ↔ model-capabilities.md 单源同步
"""
from __future__ import annotations
from mg_core import PROVIDERS, die, list_keys, run_capture
from mg_status import _probe_models
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

CAPS_FILE = Path.home() / ".workbuddy" / ".media_caps.json"
CAPS_TTL = 7 * 86400          # 实测有效期：超过 7 天视为 stale（上游可能已改版）
SMOKE_PROMPT = "a red circle on white background, smoke test"


# ─── 读写 ────────────────────────────────────────────────
def load() -> dict:
    if not CAPS_FILE.exists():
        return {}
    try:
        d = json.loads(CAPS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}        # 坏文件不致命：当作没测过，回落声明


def _write(caps: dict) -> None:
    CAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CAPS_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(caps, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # 写失败不留半截 .tmp 残骸（对比 _download 的清理逻辑）
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    os.replace(tmp, CAPS_FILE)


def record(pool: str, kind: str, res: dict) -> None:
    """写一条实测结果（覆盖同 pool/kind 的旧值）。"""
    caps = load()
    caps.setdefault(pool, {})[kind] = res
    _write(caps)


def clear(pool: str = "", kind: str = "") -> int:
    caps = load()
    if not pool:
        n = len(caps)
        _write({})
        return n
    if pool not in caps:
        return 0
    if kind:
        n = 0
        for k in (kind, f"{kind}:models"):
            if caps[pool].pop(k, None) is not None:
                n += 1
    else:
        n = len(caps.pop(pool, {}))
    _write(caps)
    return n


# ─── 生效视图（唯一出口）────────────────────────────────
def effective(pool: str, kind: str) -> dict:
    """返回该池该角色的能力生效视图。
    declared = PROVIDERS 声明（候选/默认）；measured = 实测（有且未过期即权威）。"""
    spec: dict[str, Any] = ((PROVIDERS.get(pool, {}).get("models") or {}).get(kind) or {})
    caps_pool = load().get(pool) or {}
    m = caps_pool.get(kind)
    # 兼容非 --real 探测键（f"{kind}:models"）：实测(--real)与线索(probe 不带 --real)
    # 分开落盘互不覆盖，读侧回退补齐——此前只读 kind 键，probe 结果落盘即读不回
    # （写读 key 错位，show 永远显示"未测过"）
    if m is None:
        m = caps_pool.get(f"{kind}:models")
    stale = False
    if m:
        stale = (time.time() - float(m.get("probed_at_ts", 0))) > CAPS_TTL
    measured = None if stale else m
    return {
        "pool": pool,
        "kind": kind,
        "declared": spec,
        "measured": measured,
        "stale": stale,
        # 只有"真发过"的实测才算权威；纯 /models 探测仍是猜测，与声明同级
        "source": "measured" if (measured and measured.get("method") == "real-smoke") else "declared",
    }


# ─── 探测 ────────────────────────────────────────────────
def _measure(path: Path, kind: str) -> dict:
    """量出产物真实规格（不信任上游宣称）。"""
    info: dict[str, Any] = {"size_bytes": path.stat().st_size if path.exists() else 0}
    try:
        import postprocess
        p = postprocess.probe(str(path))
        if p.get("width") and p.get("height"):
            info["actual_res"] = f'{p["width"]}x{p["height"]}'
        if kind == "video":
            info["duration_s"] = round(float(p.get("duration", 0) or 0), 2)
            info["codec"] = p.get("codec", "")
            info["has_audio"] = bool(p.get("audio"))
    except Exception as e:
        info["measure_error"] = f"{e.__class__.__name__}: {e}"
    return info


def _real_smoke(pool: str, kind: str, *, model: str = "", size: str = "",
                image: str = "", timeout: int = 300, pin: int = 1) -> dict:
    """真发一次最小请求。成功出片 = 这条能力是真的；失败也要把原因存下来。

    走 subprocess 调 media_gen 自身（而不是在这里重写一遍调用逻辑）：
    端到端、零重复实现，且天然覆盖 key 池/节流/超时/落盘后缀纠正等全部真实路径。
    """
    mg = str(Path(__file__).resolve().parent / "media_gen.py")
    with tempfile.TemporaryDirectory(prefix="caps_") as td:
        out = Path(td) / (f"caps_smoke.{'mp4' if kind == 'video' else 'png'}")
        cmd = [sys.executable, mg, "video" if kind == "video" else "image",
               "--prompt", SMOKE_PROMPT, "--out", str(out),
               "--provider", pool, "--pin-key", str(pin)]
        if size:
            cmd += ["--video-size" if kind == "video" else "--size", size]
        if kind == "video" and image:
            cmd += ["--image", image]
        env = dict(os.environ)
        if model:
            # 模型只能经 env 覆盖（CLI 无 --model：模型是 key 的属性不是请求参数）
            env[f"MEDIA_{pool.upper()}_1_{kind.upper()}_MODEL"] = model
        t0 = time.time()
        try:
            r = run_capture(cmd, timeout=timeout, env=env)
            tail = (r.stderr or "").strip().splitlines()
            err = "\n".join(tail[-6:])[:600]
            rc, timed_out = r.returncode, False
        except subprocess.TimeoutExpired:
            rc, timed_out, err = -1, True, f"超时 {timeout}s（任务可能仍在生成，用 harvest 收割）"

        res: dict[str, Any] = {
            "method": "real-smoke",
            "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "probed_at_ts": time.time(),
            "elapsed_s": round(time.time() - t0, 1),
            "model": model or ((PROVIDERS.get(pool, {}).get("models") or {})
                               .get(kind, {}).get("default", "")),
            "requested_size": size or "",
            "i2v": bool(image),
            "timeout": timed_out,
            "rc": rc,
        }
        produced = out if out.exists() else None
        if produced is None:
            # 视频可能被 _final_out 改成 .webp —— 按产物白名单找真实落点
            from mg_core import PRODUCT_EXTS
            for ext in PRODUCT_EXTS:
                cand = out.with_suffix(ext)
                if cand.exists() and cand.stat().st_size > 0:
                    produced = cand
                    break
        if produced is not None and produced.stat().st_size > 0:
            res.update(ok=True)
            res.update(_measure(produced, kind))
            res["out_name"] = produced.name
        else:
            res.update(ok=False, error=err or f"rc={rc}，无产物")
        return res


def _models_smoke(pool: str) -> dict:
    r = None
    for k in list_keys(pool, required=False):
        r = _probe_models(k["base"], k["key"])
        if r:
            break
    res = {
        "method": "models-guess",
        "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probed_at_ts": time.time(),
    }
    if r is None:
        res.update(ok=False, error="/models 不可达（不一定影响使用）")
    else:
        res.update(ok=True, total_models=r.get("total", 0),
                   guess=r.get("guess") or {},
                   note="按模型名关键字猜的，仅供参考——能否真用以 --real 实跑为准")
    return res


# ─── CLI ─────────────────────────────────────────────────
def _fmt_view(v: dict) -> str:
    d, m = v["declared"], v["measured"]
    bits = [f"  {v['pool']}/{v['kind']}  生效源={v['source']}"
            + ("  ⚠ 实测已过期（>7 天），当前回落声明" if v["stale"] else "")]
    if d:
        keys = ("default", "sizes", "durations", "rpm", "default_size",
                "supports_negative", "supports_num_frames")
        decl = "  ".join(f"{k}={d[k]}" for k in keys if k in d)
        bits.append(f"    声明: {decl or '(无)'}")
    else:
        bits.append("    声明: (PROVIDERS 无该角色)")
    if m:
        if m.get("method") == "real-smoke":
            head = "实测✅" if m.get("ok") else "实测❌"
            bits.append(f"    {head} {m.get('probed_at')}  "
                        f"model={m.get('model') or '-'}  "
                        f"res={m.get('actual_res') or '-'}  "
                        f"{str(m.get('duration_s') or '') + 's' if m.get('duration_s') else ''}"
                        f"  {m.get('elapsed_s')}s")
            if m.get("ok") is False:
                bits.append(f"        失败: {str(m.get('error'))[:200]}")
            req, act = m.get("requested_size") or "", m.get("actual_res") or ""
            if req and act and req != act:
                # 实测抓到过：请求 1024x576、上游实际给 1312x736。上游不保证按请求出，
                # 别拿请求值当落版依据——这正是要测而不是要猜的原因
                bits.append(f"        ⚠ 请求 {req} ≠ 实测 {act}：上游不严格按请求出片，"
                            f"落版参数以实测为准")
        else:
            bits.append(f"    探测(/models，仅参考) {m.get('probed_at')}  "
                        f"{m.get('total_models')} 个模型 命中={m.get('guess') or '无'}")
    else:
        bits.append("    实测: 未测过（caps probe --real 之后这里就是权威值）")
    return "\n".join(bits)


def cmd_caps(args) -> None:
    if args.caps_cmd == "sync":
        sys.exit(caps_sync(Path(getattr(args, "file", str(CAPS_DOC))),
                           bool(getattr(args, "check", False))))
    if args.caps_cmd == "clear":
        n = clear(args.pool, getattr(args, "kind", "") or "")
        print(f"[caps] 已清除 {n} 条实测记录（回落声明）")
        return

    if args.caps_cmd == "show":
        pools = [args.pool] if args.pool else [p for p in PROVIDERS]
        for p in pools:
            if p not in PROVIDERS and p not in load():
                print(f"[caps] 未知池 {p!r}（已配池：{', '.join(PROVIDERS)}）")
                continue
            for kind in ("image", "video"):
                spec = (PROVIDERS.get(p, {}).get("models") or {})
                if not spec.get(kind) and not (load().get(p) or {}).get(kind):
                    continue        # 该池本来就没这个角色，不刷屏
                print(_fmt_view(effective(p, kind)))
        print("[caps] 提示：caps probe <pool> --kind video --real 可写入实测（权威）")
        return

    # ─── probe ───
    if args.pool not in PROVIDERS:
        die(f"未知池 {args.pool!r}。已声明：{', '.join(PROVIDERS)}"
            f"（custom 通用池按 MEDIA_CUSTOM_<n>_* 序号命名，如 custom_2）", 2)
    if not args.real:
        res = _models_smoke(args.pool)
        record(args.pool, f"{args.kind}:models", res)
        print(f"[{args.pool}] /models 探测："
              + (f"{res.get('total_models')} 个模型，命中 {res.get('guess')}"
                 if res.get("ok") else f"失败 {res.get('error')}"))
        print("  这是猜的。要确认真能出片：caps probe "
              f"{args.pool} --kind {args.kind} --real")
        return

    print(f"[caps] 真实冒烟 {args.pool}/{args.kind}"
          + (f" model={args.model}" if args.model else "")
          + (f" size={args.size}" if args.size else "")
          + (f" i2v={args.image}" if args.image else "")
          + f" timeout={args.timeout}s")
    res = _real_smoke(args.pool, args.kind, model=args.model, size=args.size,
                      image=args.image, timeout=args.timeout, pin=args.pin_key)
    record(args.pool, args.kind, res)
    if res.get("ok"):
        print(f"[caps] ✅ 出片 {res.get('out_name')}  res={res.get('actual_res') or '?'}  "
              f"dur={res.get('duration_s', '-')}s  codec={res.get('codec', '-')}  "
              f"耗时 {res['elapsed_s']}s")
    else:
        print(f"[caps] ❌ 未出片（rc={res.get('rc')}）：{str(res.get('error'))[:400]}",
              file=sys.stderr)
    print(f"[caps] 已写入 {CAPS_FILE}（后续 show/生成一律以此为准）")
    sys.exit(0 if res.get("ok") else 1)


def build_parser(sub) -> None:
    """在 media_gen 主 parser 上注册 caps 子命令。"""
    cp = sub.add_parser("caps", help="能力单源：声明/实测/生效（#2）")
    cs = cp.add_subparsers(dest="caps_cmd", required=True)

    s = cs.add_parser("show", help="打印 声明/实测/生效 三栏")
    s.add_argument("pool", nargs="?", default="", help="留空=全部池")

    pr = cs.add_parser("probe", help="探测能力；--real 真发一次最小请求")
    pr.add_argument("pool")
    pr.add_argument("--kind", required=True, choices=["image", "video"])
    pr.add_argument("--real", action="store_true", help="真发请求（否则只 /models 猜）")
    pr.add_argument("--model", default="", help="模型覆盖（经 env 注入，因模型是 key 的属性）")
    pr.add_argument("--size", default="", help="指定 size 实测（留空=用池默认）")
    pr.add_argument("--image", default="", help="i2v 实测：传首帧图路径")
    pr.add_argument("--timeout", type=int, default=300, help="冒烟超时秒（默认 300）")
    pr.add_argument("--pin-key", type=int, default=1, help="用第几把 key 冒烟（默认 1）")

    cl = cs.add_parser("clear", help="丢弃实测，回落声明")
    cl.add_argument("pool", nargs="?", default="", help="留空=全清")
    cl.add_argument("--kind", default="", choices=["", "image", "video"])

    sy = cs.add_parser("sync", help="实测快照 ↔ model-capabilities.md 单源同步（优化⑥）")
    sy.add_argument("--check", action="store_true",
                    help="只校验文档区块与实测一致（drift 检测），不一致 exit 1")
    sy.add_argument("--file", default=str(CAPS_DOC), help="目标文档路径（默认 model-capabilities.md）")


# ─── 文档单源化（优化⑥）：实测快照反向生成/校验 model-capabilities.md ──
# 此前实测数据（1088×832 分辨率、121 帧耗时等）手抄进 md，实测更新文档没跟上
# 就出现第三个矛盾源。现在 md 里的快照区块是 .media_caps.json 的生成物：
#   caps sync          重写区块（区块外叙述性文字机器不碰）
#   caps sync --check  drift 校验，不一致 exit 1（可挂进导出自检）
CAPS_DOC = Path(__file__).resolve().parent.parent / "references" / "model-capabilities.md"
AUTO_BEGIN = "<!-- caps-auto-begin -->"
AUTO_END = "<!-- caps-auto-end -->"


def _snapshot_row(pool: str, kind: str, caps: dict, now: float | None = None) -> str:
    """单行快照。与 effective() 的读侧约定一致：kind 键优先，f"{kind}:models" 回退。"""
    spec = ((PROVIDERS.get(pool, {}).get("models") or {}).get(kind) or {})
    pool_caps = caps.get(pool) or {}
    m = pool_caps.get(kind) or pool_caps.get(f"{kind}:models")
    if m is None:
        return (f"| {pool} | {kind} | 声明 | — | — | {spec.get('default') or '-'} | — | — | "
                f"未测（caps probe --real 后为权威） |")
    ts = time.time() if now is None else now
    stale = (ts - float(m.get("probed_at_ts", 0))) > CAPS_TTL
    src = "实测" if m.get("method") == "real-smoke" else "探测·仅参考"
    if stale:
        src += "（已过期>7天）"
    if m.get("method") == "real-smoke":
        ok = "✅" if m.get("ok") else "❌"
        req, act = m.get("requested_size") or "", m.get("actual_res") or ""
        note = ("" if m.get("ok") else str(m.get("error"))[:60]) or \
               (f"请求 {req} ≠ 实测 {act}" if req and act and req != act else "-")
        return (f"| {pool} | {kind} | {src} | {ok} | {m.get('probed_at', '-')} | "
                f"{m.get('model') or '-'} | {act or '-'}"
                f"{' / ' + str(m['duration_s']) + 's' if m.get('duration_s') else ''} | "
                f"{m.get('elapsed_s', '-')}s | {note} |")
    guess = m.get("guess") or {}
    return (f"| {pool} | {kind} | {src} | — | {m.get('probed_at', '-')} | - | - | - | "
            f"/models 命中 {json.dumps(guess, ensure_ascii=False)} |")


def render_snapshot(caps: dict, now: float | None = None) -> str:
    """实测快照 → 整块 markdown（含 begin/end 标记行）。纯函数（测试不碰文件系统）。
    now 参数：测试注入固定时间控制 stale 判定。"""
    rows = _all_rows(caps, now)
    lines = [
        AUTO_BEGIN,
        "",
        "## 实测快照（caps sync 自动生成，勿手改）",
        "",
        "单源：`~/.workbuddy/.media_caps.json`。本区块由 `media_gen.py caps sync` 重写；",
        "手改会被覆盖。结论性叙述（如裁切方案）写在上方各池小节，机器不碰。",
        "",
        "| 池 | 角色 | 生效源 | 结果 | 实测时间 | model | 实测规格 | 耗时 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        AUTO_END,
    ]
    return "\n".join(lines)          # 区块不含尾随换行：与 _extract_block 提取结果字节一致


def _all_rows(caps: dict, now: float | None = None) -> list:
    pools = list(PROVIDERS) + [p for p in sorted(caps) if p not in PROVIDERS]
    rows = []
    for p in pools:
        pool_caps = caps.get(p) or {}
        spec = PROVIDERS.get(p, {}).get("models") or {}
        for kind in ("image", "video"):
            if not spec.get(kind) and not pool_caps:
                continue        # 该池无该角色声明且从没测过，不刷屏
            if not spec.get(kind) and not (pool_caps.get(kind) or
                                           pool_caps.get(f"{kind}:models")):
                continue
            rows.append(_snapshot_row(p, kind, caps, now))
    return rows


def _extract_block(text: str) -> str | None:
    """从 md 提取现有快照区块（含标记）。无完整区块返回 None。"""
    if AUTO_BEGIN not in text or AUTO_END not in text:
        return None
    pre = text.split(AUTO_BEGIN, 1)[1]
    return AUTO_BEGIN + pre.split(AUTO_END, 1)[0] + AUTO_END


def caps_sync(file: Path, check_only: bool) -> int:
    caps = load()
    block = render_snapshot(caps)
    text = file.read_text(encoding="utf-8") if file.exists() else ""
    existing = _extract_block(text)
    if check_only:
        if existing == block:
            print(f"[caps] 快照与实测一致：{file}")
            return 0
        print(f"[caps] DRIFT：{file} 的快照区块与当前实测不一致——跑 `caps sync` 重写",
              file=sys.stderr)
        return 1
    if existing is not None:
        new_text = text.replace(existing, block)
    elif text:
        new_text = text.rstrip() + "\n\n" + block + "\n"
    else:
        new_text = "# 模型能力表\n\n" + block + "\n"
    file.write_text(new_text, encoding="utf-8")
    print(f"[caps] 快照已同步 → {file}（{len(_all_rows(caps))} 行）")
    return 0

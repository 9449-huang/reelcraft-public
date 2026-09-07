"""envcheck.py — 外部环境自检（#65）：开跑前 10 秒体检，别跑到一半才炸。

reelcraft 依赖一堆"不在 skill 代码里、在机器上"的东西：ffmpeg、PIL、中文字体、
各池 key env、本地服务（Edge TTS / LTX Bridge）。任何一个缺席，症状都是
"深处某个 subprocess 失败"，要人工翻日志。本模块把体检收敛成一条命令：

    python scripts/media_gen.py envcheck        # 或 python scripts/envcheck.py

设计（深模块）：
  - run_checks 依赖注入（env/prober 可替换），返回结果不打印——测试不用 mock 全世界；
  - 只报"通/不通/缺失"，绝不打印 key 本身（envcheck 常被截图贴日志）；
  - 远程 API base 不探测（能力看 caps probe，网络抖动不当环境故障）；
  - exit: 0 = 全过（warn 允许），1 = 有 fail。
"""
from __future__ import annotations
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from mg_core import PROVIDERS
from ffmpeg_probe import find_ffmpeg

# caps 文件路径复用 mg_caps 的定义（同一事实源）；延迟 import 避免 mg_caps 重 IO
CAPS_FILE = Path.home() / ".workbuddy" / ".media_caps.json"
_LOCAL_RE = re.compile(r"^(https?://)?(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/.*)?$", re.I)


def _result(check: str, level: str, detail: str) -> dict:
    return {"check": check, "level": level, "detail": detail}


# ─── key env 扫描（纯逻辑，env 注入）─────────────────────
def scan_key_env(pool: str, env: dict | None = None) -> dict:
    """扫某池的 MEDIA_<POOL>_<n>_* env：返回序号/断档/缺 BASE。
    list_keys 遇到"KEY 在 BASE 缺"会静默 break（后面序号全被吞），这里必须显式报。"""
    env = os.environ if env is None else env
    prefix = PROVIDERS[pool]["key_env_prefix"]
    ns, missing_base = [], []
    n, empty_streak = 1, 0
    # 不能在第一个空号就停——断档（#1 #3 有 #2 缺）正是要检测的场景，停了就漏报 #3。
    # 连续 3 个序号完全无 KEY/BASE 才认为扫到头。
    while empty_streak < 3:
        has_key = f"{prefix}{n}_KEY" in env
        has_base = f"{prefix}{n}_BASE" in env
        if has_key:
            ns.append(n)
            if not env.get(f"{prefix}{n}_BASE"):
                missing_base.append(n)
        empty_streak = 0 if (has_key or has_base) else empty_streak + 1
        n += 1
    usable = bool(ns) and not missing_base
    # 断档：已配序号之间缺号（如 [1,3] 缺 2）
    gaps = [i for i in range(min(ns), max(ns) + 1) if i not in ns] if ns else []
    return {"prefix": prefix, "ns": ns, "gaps": gaps, "missing_base": missing_base,
            "usable": usable}


def check_key_env(env: dict | None = None) -> list:
    env = os.environ if env is None else env
    out = []
    for pool in PROVIDERS:
        r = scan_key_env(pool, env=env)
        name = f"key-env:{pool}"
        if not r["ns"]:
            out.append(_result(name, "fail", f"{r['prefix']}1_KEY 未配置——该池整池不可用"))
            continue
        if r["missing_base"]:
            out.append(_result(name, "fail",
                               f"序号 {r['missing_base']} 有 KEY 无 BASE（list_keys 会静默吞掉后续 key）"))
            continue
        detail = f"key×{len(r['ns'])} 已配（序号 {r['ns']}）"
        if r["gaps"]:
            out.append(_result(name, "warn",
                               f"{detail}；序号断档缺 {r['gaps']}——断档后的 key 全部被忽略，"
                               f"想加 key 请补齐连续序号"))
        else:
            out.append(_result(name, "ok", detail))
    return out


# ─── 运行时二进制/库 ─────────────────────────────────────
def check_runtime(env: dict | None = None, runner=None) -> list:
    env = os.environ if env is None else env
    out = []
    # python
    v = sys.version_info
    if v >= (3, 10):
        out.append(_result("python", "ok", f"{v.major}.{v.minor}.{v.micro}"))
    else:
        out.append(_result("python", "fail", f"{v.major}.{v.minor} < 3.10（本 skill 用 3.10+ 语法）"))
    # ffmpeg
    try:
        ffmpeg = find_ffmpeg()
        out.append(_result("ffmpeg", "ok", ffmpeg))
    except (SystemExit, Exception) as e:   # find_ffmpeg 找不到会 die（SystemExit）或抛错
        out.append(_result("ffmpeg", "fail", f"未找到 ffmpeg（PATH / FFMPEG_PATH 均无）：{e}"))
        ffmpeg = None
    # libx264（concat/kenburns/qcgate 都依赖）
    if ffmpeg and runner:
        r = runner([ffmpeg, "-hide_banner", "-encoders"])
        if "libx264" in (r.stdout or ""):
            out.append(_result("libx264", "ok", "编码器可用"))
        else:
            out.append(_result("libx264", "fail", "ffmpeg 无 libx264 编码器——后期全挂"))
    # PIL
    try:
        import PIL  # noqa
        from PIL import Image  # noqa
        out.append(_result("pillow", "ok", PIL.__version__))
    except Exception as e:
        out.append(_result("pillow", "fail", f"PIL 不可用（qcseq/qc/kenburns-all 依赖）: {e}"))
    # 中文字体（drawtext 落版；_FFMPEG_FONT 可覆盖）
    font = _find_font_quiet(env)
    if font:
        out.append(_result("font", "ok", font))
    else:
        out.append(_result("font", "warn",
                           "未找到中文字体（simkai/msyh/simhei/simsun）——字幕/落版会挂；"
                           "可设 _FFMPEG_FONT 指向任一 .ttf/.ttc"))
    return out


def _find_font_quiet(env: dict) -> str:
    """find_font() 的不 die 版：返回字体路径或空串。探测顺序与其一致。"""
    p = (env.get("_FFMPEG_FONT") or "").strip()
    if p and Path(p).exists():
        return p
    if sys.platform != "win32":
        return ""
    for name in ("simkai.ttf", "msyh.ttc", "simhei.ttf", "simsun.ttc"):
        cand = Path("C:/Windows/Fonts") / name
        if cand.exists():
            return str(cand)
    return ""


# ─── 本地服务探测 ─────────────────────────────────────────
def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_local_services(env: dict | None = None, prober=_tcp_ok) -> list:
    """扫所有已配 key 的 base：指向 127.0.0.1/localhost 的做 TCP 探测（本地服务挂了必炸）；
    远程 base 跳过（能力看 caps probe，网络抖动不当环境故障）。"""
    env = os.environ if env is None else env
    out = []
    seen: set = set()
    for pool in PROVIDERS:
        r = scan_key_env(pool, env=env)
        for n in r["ns"]:
            base = (env.get(f"{r['prefix']}{n}_BASE") or "").rstrip("/")
            if not base or base in seen:
                continue
            seen.add(base)
            m = _LOCAL_RE.match(base)
            if not m:
                continue
            host = m.group(2).lower()
            port = int(m.group(3)[1:]) if m.group(3) else (443 if base.startswith("https") else 80)
            ok = prober(host, port)
            label = f"{pool} key#{n} → {base}"
            if ok:
                out.append(_result("local-service", "ok", f"{label} 可达"))
            else:
                out.append(_result("local-service", "fail",
                                   f"{label} 不可达——本地服务没起（TTS 启动脚本 / 启动桥接服务.bat）"))
    return out


def check_caps() -> list:
    if not CAPS_FILE.exists():
        return [_result("caps", "ok", "无实测记录（caps probe --real 后生成，不影响开跑）")]
    try:
        d = json.loads(CAPS_FILE.read_text(encoding="utf-8"))
        n = sum(len(v) for v in d.values() if isinstance(v, dict)) if isinstance(d, dict) else 0
        return [_result("caps", "ok", f"实测记录 {n} 条可读")]
    except Exception as e:
        return [_result("caps", "warn",
                        f"{CAPS_FILE.name} 损坏（{e}）——会当没测过回落声明，可 caps clear 重来")]


# ─── 汇总（唯一入口）─────────────────────────────────────
def run_checks(env: dict | None = None, prober=_tcp_ok, runner=None) -> list:
    return (check_key_env(env) + check_runtime(env, runner=runner)
            + check_local_services(env, prober=prober) + check_caps())


def summarize(results: list) -> tuple[int, str]:
    """(rc, 报告文本)。fail→exit 1；warn 允许（提示性质）。"""
    lines = []
    rc = 0
    for r in results:
        badge = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[r["level"]]
        lines.append(f"{badge} [{r['level']:>4}] {r['check']}: {r['detail']}")
        if r["level"] == "fail":
            rc = 1
    n_fail = sum(1 for r in results if r["level"] == "fail")
    n_warn = sum(1 for r in results if r["level"] == "warn")
    lines.append(f"\n[envcheck] {len(results)} 项：fail={n_fail} warn={n_warn}"
                 + ("——先修 fail 再开跑" if n_fail else
                    ("" if n_warn else "，环境全绿，可开跑")))
    return rc, "\n".join(lines)


def main(argv=None) -> None:
    def _runner(cmd):
        try:
            import subprocess as sp
            r = sp.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=15)
            return r
        except Exception:
            return None

    rc, report = summarize(run_checks(runner=_runner))
    print(report)
    sys.exit(rc)


if __name__ == "__main__":
    main()

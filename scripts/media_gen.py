#!/usr/bin/env python3
"""media_gen.py — 多 provider 生图/视频一体化（标准库零依赖）。

支持的 provider：
  agnes      免费档，速度限制型，无总量上限（推荐主力）
  zhipu      智谱 CogView/CogVideoX 免费模型，并发限制
  modelscope 魔塔 Qwen-Image-Edit（图编辑，非文生图）

设计要点：
  * 多 key 池轮转（media_keys.env 配置 MEDIA_<pool>_<n>_KEY）
  * key 角色 _ROLES（一条龙/分工）+ 模型覆盖 _IMAGE_MODEL/_VIDEO_MODEL + 全局优先级 MEDIA_PRIORITY
  * 失败分类熔断：401→换key；429→冷却换key；5xx→退避重试；审核拒绝→抛 ProviderFatal
  * 视频：1 RPM 串行节流；断点续跑（已完成 clip 跳过）
  * 任务间隔记录到 media_state.json，支持跨会话续跑
  * Key 永不打印、永不写产物

用法：
  python media_gen.py image   --prompt "..." --size 1024x1024 --out shot_01.png [--provider 池名]
  python media_gen.py video   --prompt "..." --image shot_01.png --out clip_01.mp4 [--provider 池名] [--num-frames 121] [--negative "blurry..."]
  python media_gen.py last-frame <video> <out_png>
  python media_gen.py status  # 打印当前 key 健康状态与额度统计

  # --provider 留空 = 按 MEDIA_PRIORITY 自动选池，单命令失败自动跨池兜底（batch 除外，必须显式指定）
"""
from __future__ import annotations
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

_ffmpeg_cache: dict[str, str] = {}
def _ffmpeg() -> str:
    """懒加载 ffmpeg 路径（跨平台探测，见 ffmpeg_probe.py）。"""
    if "exe" not in _ffmpeg_cache:
        _ffmpeg_cache["exe"] = find_ffmpeg()
    return _ffmpeg_cache["exe"]

# ─── 配置常量 ──────────────────────────────────────────────
KEY_ENV_FILE = Path.home() / ".workbuddy" / "media_keys.env"
LEGACY_KEY_ENV_FILE = Path.home() / ".workbuddy" / "agnes_key.env"
STATE_FILE = Path.home() / ".workbuddy" / ".media_state.json"

# Provider 能力表（核心：size 白名单 / num_frames / 端点模板）
PROVIDERS: dict[str, dict[str, Any]] = {
    "agnes": {
        "label": "Agnes",
        "free_kind": "speed-limited",          # 速度限制型，无总量上限
        "key_env_prefix": "MEDIA_AGNES_",
        "models": {
            "image": {
                "default": "agnes-image-2.1-flash",
                "sizes": ["1024x1024", "1024x576", "1344x768", "2048x1152"],
                # 1K=20RPM, 2K=10RPM, 3K/4K=~1RPM
                "rpm_by_size": lambda s: 20 if int(s.split("x")[0]) <= 1024 else (10 if int(s.split("x")[0]) <= 2048 else 1),
            },
            "video": {
                "default": "agnes-video-v2.0",
                "task_path": "/videos",
                "poll_path": "/agnesapi",
                "poll_param": "video_id",
                # Agnes 视频参数：num_frames 必须 8n+1；支持 negative_prompt
                "supports_num_frames": True,
                "supports_negative": True,
                "frame_choices": [9, 17, 25, 33, 41, 49, 57, 65, 73, 81, 89, 97, 105, 113, 121],
                "rpm": 1,
            },
        },
    },
    "zhipu": {
        "label": "智谱 GLM",
        "free_kind": "speed-limited-concurrent-30",
        "key_env_prefix": "MEDIA_ZHIPU_",
        "models": {
            "image": {
                "default": "CogView-3-Flash",
                "sizes": ["1024x1024", "768x1344", "1024x768", "864x1152", "1152x864", "1440x720", "720x1440"],
                "note": "免费档出图右下角带 'AI 生成' 水印（已验证）",
                "rpm": None,                    # 并发 30，无明确 RPM
            },
            "video": {
                # 智谱视频兜底：CogVideoX-Flash 免费，图生视频（i2v）
                # 官方文档: https://docs.bigmodel.cn （视频生成 API）
                "default": "cogvideox-flash",
                "task_path": "/videos/generations",
                "poll_path": "/async-result",   # path 式轮询: GET {base}/async-result/{id}
                "poll_style": "path",
                "payload_style": "zhipu",       # image_url / size / duration / fps 字段集
                "supports_num_frames": False,   # 用 duration(5|10) + fps(30|60)，不用 num_frames
                "supports_negative": False,
                "sizes": ["720x480", "1024x1024", "1280x960", "960x1280", "1920x1080", "1080x1920", "2048x1080", "3840x2160"],
                "durations": [5, 10],
                "rpm": 5,                       # 免费并发 30，保守节流
                "note": "图生视频需传 image_url；文生视频可不传。size 仅 i2v 支持。输出无 Agnes 的 4:3 问题，1920x1080 原生达标",
            },
        },
    },
    "modelscope": {
        "label": "魔塔 ModelScope",
        "free_kind": "free-inference",
        "key_env_prefix": "MEDIA_MODELSCOPE_",
        "models": {
            # 仅图编辑，无文生图。Qwen-Image-Edit-2509 支持多图编辑
            "image_edit": {
                "default": "Qwen/Qwen-Image-Edit-2509",
                "task_path": "/images/generations",          # base 已含 /v1
                "poll_path": "/tasks",                       # GET {base}/tasks/{task_id}
                "async_header": "true",                      # X-ModelScope-Async-Mode
                "rpm": None,
                "note": "首帧小改（移物/调光/局部重绘）不用整图重 roll；输出无水印",
            },
        },
    },
    "custom": {
        # 通用池（v2.7）：任意 OpenAI 兼容渠道，纯 env 配置零改码。
        # 视频“OpenAI 兼容”无行业标准，默认按"异步任务轮询"风格（提交任务+轮询取片）预设；
        # 另一常见风格是 Sora 风（POST /videos 提交，GET /videos/{id} 轮询）——
        # 用 _TASK_PATH=/_videos 改即可；风格族说明见 SKILL.md「通用池」节。
        "label": "Custom (任意 OpenAI 兼容渠道)",
        "free_kind": "user-configured",
        "key_env_prefix": "MEDIA_CUSTOM_",
        "models": {
            "image": {
                "default": "",                 # 必填 MEDIA_CUSTOM_n_IMAGE_MODEL
                "task_path": "/images/generations",          # OpenAI 标准风格
                "task_poll_path": "/tasks",    # 异步任务式渠道（如魔搭）的轮询路径
                "rpm": 20,
                # sizes 留空 = 不校验（custom 渠道能力未知，warn 不 die）；
                # 可用 MEDIA_CUSTOM_n_IMAGE_SIZES="1024x1024,1344x768" 自填白名单
                "note": "模型名必填；产出规格/水印未知，首用先 probe 一张目检",
            },
            "video": {
                "default": "",                 # 必填 MEDIA_CUSTOM_n_VIDEO_MODEL
                "task_path": "/videos/generations",          # 默认异步任务轮询风；Sora 风改 /videos
                "poll_path": "/videos",
                "poll_style": "path",
                "supports_num_frames": False,
                "supports_negative": False,
                "rpm": 5,
                "note": "模型名必填；产不出/轮询 404 多半是风格族不对，改 _TASK_PATH/_POLL_PATH",
            },
        },
    },
}

# ─── 状态机 ────────────────────────────────────────────────
class ProviderFatal(Exception):
    """审核拒绝 / 业务规则拒绝：换 key 也没用，必须改 prompt。"""


class RateLimitedError(Exception):
    """429 限流：应交给 call_with_failover 冷却并换 key，不由 http_call 内部重试。"""


class AllKeysFailed(Exception):
    """同池全部 key 失败：单命令模式（--provider 留空）可按 MEDIA_PRIORITY 尝试下一池。"""

# ─── 密钥与 base 加载 ───────────────────────────────────────
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'export\s+([A-Za-z_][A-Za-z0-9_]*)="(.*)"$', line)
        if m and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(2)

_load_env_file(KEY_ENV_FILE)
_load_env_file(LEGACY_KEY_ENV_FILE)  # 向后兼容

_state_lock = threading.Lock()

def _load_state() -> dict:
    with _state_lock:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
def _save_state(s: dict) -> None:
    try:
        with _state_lock:
            STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def list_keys(provider: str, pin: int = 0, required: bool = True, role: str = "") -> list[dict]:
    """返回 [{key, base, poll, n, roles, image_model, video_model}]（n 为序号）。
    pin>0 时只返回第 pin 把 key（多 worker 并行：各锁一把，互不踩 429）。
    role 非空时仅返回 _ROLES 声明含该角色的 key（image/video；缺省 image,video 即一条龙）。
    """
    prefix = PROVIDERS[provider]["key_env_prefix"]
    keys = []
    n = 1
    while True:
        k = os.environ.get(f"{prefix}{n}_KEY")
        if not k:
            break
        base = os.environ.get(f"{prefix}{n}_BASE", "").rstrip("/")
        if not base:
            break
        roles = {r.strip().lower() for r in
                 os.environ.get(f"{prefix}{n}_ROLES", "image,video").split(",") if r.strip()}
        entry = {"key": k, "base": base, "poll": os.environ.get(f"{prefix}{n}_POLL") or base,
                 "n": n, "roles": roles,
                 "image_model": os.environ.get(f"{prefix}{n}_IMAGE_MODEL", ""),
                 "video_model": os.environ.get(f"{prefix}{n}_VIDEO_MODEL", ""),
                 "image_sizes": [s.strip() for s in
                                 os.environ.get(f"{prefix}{n}_IMAGE_SIZES", "").split(",") if s.strip()]}
        if not role or role in roles:
            keys.append(entry)
        n += 1
    if pin:
        keys = [k for k in keys if k["n"] == pin]
    if not keys:
        if required:
            die(f"未找到 {provider} 的 key" +
                (f"（pin=#{pin}）" if pin else "") +
                (f"（role={role}，检查 _ROLES）" if role else "") +
                f"。请在 {KEY_ENV_FILE} 中配置 MEDIA_{provider.upper()}_1_KEY / _BASE", 2)
        return []
    return keys


# ─── 池路由：MEDIA_PRIORITY 决定默认池与跨池兜底顺序 ──────────
def _priority_pools() -> list[str]:
    """读 MEDIA_PRIORITY（逗号分隔池名）；未配置时用内置模板序（agnes,zhipu,modelscope）。"""
    raw = os.environ.get("MEDIA_PRIORITY", "").strip()
    if not raw:
        return list(PROVIDERS)
    return [p.strip().lower() for p in raw.split(",") if p.strip() and p.strip().lower() in PROVIDERS]


def pools_for_role(role: str) -> list[str]:
    """按优先级返回「模板支持该角色 且 实际配置了承担该角色的 key」的池。"""
    out = []
    for p in _priority_pools():
        if role not in PROVIDERS[p]["models"]:
            continue                      # 模板无此能力（如 modelscope 只有图编辑）
        if list_keys(p, required=False, role=role):
            out.append(p)
    return out

def key_mask(k: str) -> str:
    if len(k) <= 10:
        return "***"
    return k[:4] + "***" + k[-4:]

# ─── HTTP ─────────────────────────────────────────────────
def die(msg: str, code: int = 1) -> None:
    print(f"[media_gen] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)

def http_call(method: str, url: str, headers: dict, body: dict | None, timeout: int, max_retry: int = 3) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(max_retry):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw.decode("utf-8", "ignore")) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            code = e.code
            txt = e.read().decode("utf-8", "ignore")[:400]
            # 审核/策略拒绝（不重试）
            if code in (400, 403) and any(s in txt.lower() for s in ("content", "safety", "policy", "审核", "内容", "blocked", "violation")):
                raise ProviderFatal(f"内容审核拒绝 HTTP {code}: {txt[:200]}")
            # 业务参数错误（不重试）
            if code == 400:
                die(f"HTTP 400 (参数错误，请检查 prompt/size/model): {txt[:300]}")
            if code in (401,):
                raise PermissionError(f"HTTP 401 (key 无效): {txt[:200]}")
            # 429 限流：抛出让 failover 冷却并换 key（单 key 内不重试，避免同 key 死循环）
            if code == 429:
                raise RateLimitedError(f"HTTP 429 限流: {txt[:200]}")
            # 408/5xx：单 key 内指数退避重试
            if code in (408, 500, 502, 503, 504, 520, 522, 524):
                wait = min(60, 2 ** (attempt + 1))
                print(f"[media_gen] HTTP {code}, {wait}s 后重试 #{attempt+1}: {txt[:120]}", file=sys.stderr)
                time.sleep(wait)
                last_err = f"http_{code}: {txt}"
                continue
            die(f"HTTP {code}: {txt[:300]}")
        except Exception as e:
            wait = min(30, 2 ** (attempt + 1))
            print(f"[media_gen] {type(e).__name__}: {e}, {wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
            last_err = str(e)
    die(f"重试耗尽: {last_err}")

# ─── 调度：多 key 轮转 + 熔断 ──────────────────────────────
def call_with_failover(
    provider: str,
    call_fn,                       # fn(key_info) → dict（响应）
    *,
    kind: str,                     # "image" | "video"
    cooldown_default: int = 60,
    pin_key: int = 0,              # >0 时只锁定第 pin_key 把 key（双 key 并行）
) -> tuple[dict, dict]:
    """返回 (响应, 成功的 key_info)。失败分类：
       ProviderFatal → 抛出（不换 key）
       PermissionError/429 → 换 key；同 provider 全冷却则抛 AllKeysExhausted
    """
    keys = list_keys(provider, pin_key)
    state = _load_state() or {}
    cooldown = (state.get("cooldown") or {}).get(provider) or {}
    last_err = None
    for k in keys:
        kn = str(k["n"])
        if cooldown.get(kn, 0) > time.time():
            print(f"[media_gen] {provider} key #{kn} 冷却中，跳过", file=sys.stderr)
            continue
        try:
            resp = call_fn(k)
            cooldown.pop(kn, None)                 # 成功 → 清除冷却
            state.setdefault("cooldown", {})[provider] = cooldown
            _save_state(state)
            return resp, k
        except ProviderFatal:
            raise
        except PermissionError as e:
            print(f"[media_gen] {provider} key #{kn} 鉴权失败，记入黑名单: {e}", file=sys.stderr)
            cooldown[kn] = time.time() + 86400
            last_err = e
        except RateLimitedError as e:
            cooldown[kn] = time.time() + cooldown_default
            print(f"[media_gen] {provider} key #{kn} 限流，冷却 {cooldown_default}s: {e}", file=sys.stderr)
            last_err = e
        except Exception as e:
            last_err = e
            continue
    state.setdefault("cooldown", {})[provider] = cooldown
    _save_state(state)
    raise AllKeysFailed(f"{provider} 所有 key 失败: {last_err}")

# ─── 视频节流（按 key 各自计时 → 双 key 双线并行不互卡）────
_last_video_at: dict[str, float] = {}
_throttle_lock = threading.Lock()

def video_throttle(rpm: int, tag: str = "default") -> None:
    """tag 形如 agnes_key1 / agnes_key2 / default。每个 tag 独立计时。"""
    interval = max(1.0, 60.0 / max(1, rpm))
    with _throttle_lock:
        wait = interval - (time.time() - _last_video_at.get(tag, 0.0))
        _last_video_at[tag] = time.time() + max(0.0, wait)
    if wait > 0:
        print(f"[media_gen] 视频节流 {rpm} RPM [{tag}]，等待 {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)

# ─── 生图 ─────────────────────────────────────────────────
def _insert_suffix(path: str, suffix: str) -> str:
    """shot_01.png + '_2' → shot_01_2.png"""
    if not suffix:
        return path
    p = Path(path)
    return str(p.with_name(f"{p.stem}{suffix}{p.suffix}"))

def _download_image(resp: dict, out: str) -> None:
    d = (resp.get("data") or [{}])[0]
    url = d.get("url")
    b64 = d.get("b64_json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if url:
        urllib.request.urlretrieve(url, out)
    elif b64:
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
    else:
        die(f"响应无 url/b64: {json.dumps(resp)[:300]}")

def _resolve_async_task(used_key: dict, resp: dict, poll_path: str = "/tasks") -> dict:
    """异步任务式渠道兜底：响应无 url/b64 但有 task_id 时，轮询取最终结果。
    兼容魔搭风格（GET {base}/tasks/{id} → task_status/output_images）；
    非 task 式响应原样返回，同步渠道不受影响。"""
    d = (resp.get("data") or [{}])[0]
    if resp.get("url") or d.get("url") or d.get("b64_json"):
        return resp
    tid = resp.get("task_id")
    if not tid:
        return resp
    poll_url = f"{used_key['base']}{poll_path}/{tid}"
    headers = {"Authorization": f"Bearer {used_key['key']}",
               "X-ModelScope-Task-Type": "image_generation"}
    print(f"[media_gen] 异步任务式响应，轮询 {poll_url} …", file=sys.stderr)
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(3)
        try:
            req = urllib.request.Request(poll_url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            print(f"[media_gen] 轮询异常: {e}", file=sys.stderr)
            continue
        status = str(st.get("task_status") or "").upper()
        imgs = st.get("output_images") or []
        if imgs:
            return {"data": [{"url": imgs[0]}]}
        if status in ("FAILED", "FAIL", "ERROR"):
            die(f"生图任务失败: {json.dumps(st, ensure_ascii=False)[:300]}")
        if status == "SUCCEED" and not imgs:
            die(f"任务成功但无图: {json.dumps(st, ensure_ascii=False)[:300]}")
    die("轮询超时 5 分钟")

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
        resp = _resolve_async_task(used_key, resp, _pinfo.get("task_poll_path", "/tasks"))
        _download_image(resp, out)
        print(f"[media_gen] image OK via {used_key['pool']} key#{used_key['n']} ({key_mask(used_key['key'])}) -> {out}")
        outs.append(out)
    if count > 1:
        print(f"[media_gen] 共 {len(outs)} 张候选，人工选优后进 i2v：\n  " + "\n  ".join(outs))


def _image_rpm_for(pool: str, size: str):
    info = PROVIDERS[pool]["models"].get("image") or {}
    return info["rpm_by_size"](size) if "rpm_by_size" in info else info.get("rpm")


def _gen_image_once(pools: list[str], args, size: str, errs: list[str]) -> tuple[dict, dict]:
    """按池序尝试一次生图，返回 (resp, used_key)。
    显式 --provider：只试该池，失败即退（与旧版行为一致）；留空：按 MEDIA_PRIORITY 跨池兜底。
    """
    for pool in pools:
        info = PROVIDERS[pool]["models"].get("image")
        if not info:
            if args.provider:
                die(f"{pool} 不支持文生图（可能仅支持图编辑，见 edit 子命令）", 2)
            continue
        if "sizes" in info and size not in info["sizes"]:
            if args.provider:
                die(f"{pool} 不支持 size={size}。可选: {info['sizes']}", 2)
            continue

        def call_fn(k: dict, _info=info, _prefix=PROVIDERS[pool]["key_env_prefix"]) -> dict:
            model = k.get("image_model") or _info["default"]
            if not model:
                die(f"该池未配置模型名（模板无默认值）。请在 env 加 {_prefix}{k['n']}_IMAGE_MODEL=模型名", 2)
            if k.get("image_sizes") and size not in k["image_sizes"]:
                print(f"[media_gen] [warn] _IMAGE_SIZES 自填白名单不含 size={size}，仅警告不拦截（渠道真实能力以实跑为准）", file=sys.stderr)
            headers = {"Authorization": f"Bearer {k['key']}", "Content-Type": "application/json"}
            body = {"model": model, "prompt": args.prompt, "size": size, "n": 1}
            return http_call("POST", f"{k['base']}{_info.get('task_path', '/images/generations')}", headers, body, timeout=600)

        try:
            resp, used = call_with_failover(pool, call_fn, kind="image", pin_key=args.pin_key)
            used["pool"] = pool
            return resp, used
        except AllKeysFailed as e:
            errs.append(str(e))
            if args.provider:
                die(str(e), 3)
            print(f"[media_gen] {pool} 全部 key 失败，尝试下一池…", file=sys.stderr)
            continue
    die("所有可用池均失败:\n  " + "\n  ".join(errs), 3)

# ─── 视频 ─────────────────────────────────────────────────
def image_to_url_or_path(path: str) -> str:
    """本地图片 → data URI 或原样返回。"""
    p = Path(path)
    if not p.exists():
        return path
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    # 注：标准库无法无损压缩图片；大图直接转 base64 可能超网关限制。
    # 优先传 URL（Agnes 回传 url 时走 url 分支，不经过这里）；如需压缩请预先缩图。
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"

def cmd_video(args) -> None:
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
                payload = {"model": model, "prompt": args.prompt}
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
            return http_call("POST", f"{k['base']}{_info['task_path']}", headers, payload, timeout=300)

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
    if isinstance(direct_url, str) and direct_url.startswith("http"):
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        urllib.request.urlretrieve(direct_url, out)
        print(f"[media_gen] video OK via {provider} key#{used_key['n']} -> {out}")
        return
    if not video_id:
        die(f"无 video_id: {json.dumps(resp)[:300]}")

    # 轮询
    deadline = time.time() + 600
    poll_url_base = used_key["poll"]
    while time.time() < deadline:
        time.sleep(args.wait)
        # path 式（智谱 /async-result/{id}）vs 查询参数式（Agnes ?video_id=）
        if info.get("poll_style") == "path":
            poll_url = f"{poll_url_base}{info['poll_path']}/{video_id}"
        else:
            poll_url = f"{poll_url_base}{info['poll_path']}?{info['poll_param']}={video_id}"
        try:
            req = urllib.request.Request(poll_url)
            req.add_header("Authorization", f"Bearer {used_key['key']}")
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            print(f"[media_gen] 轮询异常: {e}", file=sys.stderr)
            continue
        url = None
        if isinstance(st, dict):
            status = str(st.get("task_status") or st.get("status") or "").upper()
            # 智谱: video_result[0].url；Agnes: video_url/url/data.video_url
            vr = st.get("video_result")
            if isinstance(vr, list) and vr and isinstance(vr[0], dict):
                u = vr[0].get("url")
                if isinstance(u, str) and u.startswith("http"):
                    url = u
            for k_ in ("video_url", "url"):
                if isinstance(st.get(k_), str) and st[k_].startswith("http"):
                    url = st[k_]
            data = st.get("data")
            if isinstance(data, dict):
                for k_ in ("video_url", "url"):
                    if isinstance(data.get(k_), str) and data[k_].startswith("http"):
                        url = data[k_]
            if status in ("FAIL", "FAILED", "ERROR") and not url:
                die(f"视频任务失败: {json.dumps(st, ensure_ascii=False)[:300]}")
        if url:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            urllib.request.urlretrieve(url, out)
            print(f"[media_gen] video OK via {provider} key#{used_key['n']} -> {out}")
            return
    die("轮询超时 10 分钟")

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

# ─── 抽末帧 ───────────────────────────────────────────────
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

# ─── 批量并行（N key 各取动态队列，吞吐×N）────────────────
def cmd_batch(args) -> None:
    """读 shots 目录下 S*.json / shot_*.json，images/videos 两阶段可并行多 worker。
    workers=N 时：N 个 worker 各锁一把 key（按 _ROLES 过滤后：worker w → 承担该阶段
    角色的第 w 把 key），从共享队列动态取镜，两路独立 1RPM，吞吐随 key 数线性提升。
    断点续跑：已有产物跳过。仅单池（--provider 必填）；跨池兜底见 image/video 单命令。
    每镜失败自动重试 --retries 次，仍失败记 FAIL。--dry-run 仅打印计划不执行。
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

    provider = args.provider
    if not provider:
        die("batch 只跑单池，请显式 --provider（跨池自动兜底仅单命令 image/video 支持）", 2)
    role = "image" if args.phase == "images" else "video"
    keys = list_keys(provider, required=False, role=role)
    if not keys:
        die(f"{provider} 没有承担 {role} 角色的 key"
            f"（检查 MEDIA_{provider.upper()}_*_ROLES，缺省 image,video）", 2)
    if args.workers > len(keys):
        die(f"workers={args.workers} 超过 {provider} 承担 {role} 角色的 key 数 {len(keys)}", 2)

    frames_dir = shots_dir / "frames"
    clips_dir = shots_dir / "clips"
    frames_dir.mkdir(exist_ok=True)
    clips_dir.mkdir(exist_ok=True)
    qc_dir = clips_dir / "qc"

    def make_cmd(j: Path, sid: str, pin: int) -> tuple[list[str], Path, str]:
        """构造单镜命令；返回 (cmd, out_path, skip_reason)。skip_reason 非空=跳过。"""
        d = json.loads(j.read_text(encoding="utf-8"))
        if args.phase == "images":
            out = frames_dir / f"{sid}.png"
            if out.exists() and out.stat().st_size > 0:
                return [], out, "skip (exists)"
            cmd = [sys.executable, str(Path(__file__).resolve()), "image",
                   "--provider", provider, "--prompt", d["t2i_prompt"],
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
               "--provider", provider, "--prompt", d.get("i2v_prompt", ""),
               "--image", str(frame), "--out", str(out),
               "--num-frames", str(d.get("num_frames", 121)),
               "--negative", args.negative, "--pin-key", str(pin)]
        return cmd, out, ""

    plan: list[tuple[Path, str]] = []
    for j in jsons:
        d = json.loads(j.read_text(encoding="utf-8"))
        sid = d.get("shot_id") or j.stem
        plan.append((j, sid))

    # dry-run：仅打印计划
    if args.dry_run:
        print(f"[batch] DRY-RUN phase={args.phase} workers={args.workers} "
              f"shots={len(plan)} retries={args.retries} provider={provider}")
        for j, sid in plan:
            cmd, out, skip = make_cmd(j, sid, 1)
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

    pinned_ns = [k["n"] for k in keys]    # 角色过滤后的实际 key 序号（可能不连续）

    def run_worker(w: int) -> None:
        pin = pinned_ns[w]                # worker w → 承担该角色的第 w 把 key
        while True:
            try:
                j, sid = task_q.get_nowait()
            except queue.Empty:
                break
            cmd, out, skip = make_cmd(j, sid, pin)
            if skip:
                with rl:
                    results.append(f"skip {sid}")
                continue
            last_rc = 1
            for attempt in range(1, args.retries + 1):
                rc = subprocess.call(cmd)
                last_rc = rc
                if rc == 0:
                    break
                print(f"[batch W{w+1}] {sid} 失败(rc={rc})，重试 {attempt}/{args.retries}",
                      file=sys.stderr)
            # 断点续跑兜底：重试后仍非 0，但产物已存在且非空，视为成功
            if last_rc != 0 and out.exists() and out.stat().st_size > 0:
                last_rc = 0
            with rl:
                if last_rc == 0:
                    results.append(f"OK {sid}")
                    provider_map[sid] = f"{provider} key#{pin}"
                    if args.qc and args.phase == "videos":
                        qc_dir.mkdir(exist_ok=True)
                        subprocess.call([sys.executable, str(Path(__file__).resolve()),
                                         "qc", str(out), str(qc_dir)])
                else:
                    results.append(f"FAIL(rc={last_rc}) {sid}")
                    provider_map[sid] = f"{provider} key#{pin} (failed)"

    threads: list[_t.Thread] = []
    for w in range(args.workers):
        th = _t.Thread(target=run_worker, daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()

    # 每镜所用 provider/key 落盘，便于追溯与混合 provider 拼接告警
    run_info = {"provider": provider, "phase": args.phase, "workers": args.workers,
                "retries": args.retries, "shots": provider_map}
    (shots_dir / "batch_run.json").write_text(
        json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8")

    for r in results:
        print(r, flush=True)
    failed = [r for r in results if r.startswith("FAIL")]
    print(f"[batch] phase={args.phase} workers={args.workers} done. "
          f"failed={failed or 'none'}  (明细见 {shots_dir / 'batch_run.json'})", flush=True)
    sys.exit(1 if failed else 0)

# ─── TTS（预留接口，需配置 MEDIA_TTS_*）──────────────────
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

# ─── 抽帧 QC（生成后视觉验收闭环）────────────────────────
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
            if k["roles"] != {"image", "video"}:
                extra.append("roles=" + ",".join(sorted(k["roles"])))
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

# ─── 入口 ─────────────────────────────────────────────────
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

    ed = sub.add_parser("edit")
    ed.add_argument("--image", required=True, help="待编辑的图（如 shots/shot_01.png）")
    ed.add_argument("--prompt", required=True, help="编辑指令，如 'move the ink stick to the left'")
    ed.add_argument("--out", required=True)
    ed.add_argument("--model", default="", help="覆盖默认 Qwen/Qwen-Image-Edit-2509")
    ed.add_argument("--wait", type=int, default=5, help="轮询间隔秒")

    bt = sub.add_parser("batch", help="批量并行（N key 各跑动态队列，吞吐×N；仅单池）")
    bt.add_argument("shots", help="分镜 JSON 目录（含 S*.json / shot_*.json）")
    bt.add_argument("--phase", required=True, choices=["images", "videos"])
    bt.add_argument("--provider", default="",
                    help="必填单池名（如 agnes）；跨池自动兜底不适用于 batch")
    bt.add_argument("--workers", type=int, default=1, help="并行 worker 数（=使用的 key 数，≤ key 总数）")
    bt.add_argument("--retries", type=int, default=2, help="每镜失败重试次数（默认 2）")
    bt.add_argument("--dry-run", action="store_true", help="仅打印执行计划不实际生成")
    bt.add_argument("--negative", default="blurry, distorted faces, warped hands, extra limbs, text artifacts, watermark, camera shake, flickering, plastic skin, oversaturated")
    bt.add_argument("--qc", action="store_true", help="videos 阶段每段生成后自动抽 3 帧到 clips/qc/")

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

if __name__ == "__main__":
    main()
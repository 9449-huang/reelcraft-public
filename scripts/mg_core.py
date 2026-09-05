"""reelcraft 共享核心：PROVIDERS 注册表、key 枚举/路由、状态与冷却、异常、HTTP、节流、生成工具、异步轮询。由 media_gen / mg_batch / mg_status 引用，勿反向依赖。"""
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
        m = re.match(r'export\s+([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"', line)  # 取首个引号内值，容忍行内注释
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
        return {}          # 状态文件不存在：返回空 dict（此前隐式返回 None，首次落盘会炸）
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
                                 os.environ.get(f"{prefix}{n}_IMAGE_SIZES", "").split(",") if s.strip()],
                 # 路径/字段名可配（覆盖 PROVIDERS 模板）：风格族不对时零改码适配
                 "task_path": os.environ.get(f"{prefix}{n}_TASK_PATH", ""),
                 "image_task_path": os.environ.get(f"{prefix}{n}_IMAGE_TASK_PATH", ""),
                 "video_task_path": os.environ.get(f"{prefix}{n}_VIDEO_TASK_PATH", ""),
                 "video_prompt_field": os.environ.get(f"{prefix}{n}_VIDEO_PROMPT_FIELD", "")}
        # 口味档位（卡三四档：ultra/high/mid/low，由用户自选后落 env）；非法值 warn+忽略
        tier_raw = os.environ.get(f"{prefix}{n}_TIER", "").strip().lower()
        if tier_raw and tier_raw not in ("ultra", "high", "mid", "low"):
            print(f"[media_gen] [warn] {prefix}{n}_TIER={tier_raw!r} 不是合法档位"
                  f"（ultra/high/mid/low），已忽略", file=sys.stderr)
            tier_raw = ""
        entry["tier"] = tier_raw
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
    keys = list_keys(provider, pin_key, role=kind)   # 按 _ROLES 过滤（如 custom 池图/视频分 key）
    state = _load_state() or {}
    cooldown = (state.get("cooldown") or {}).get(provider) or {}
    # 惰性清理：过期冷却/黑名单条目直接移除，防止状态文件只进不出
    now = time.time()
    for kn in [kn for kn, t in cooldown.items() if t <= now]:
        cooldown.pop(kn, None)
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

def _abs_url(url: str, base: str) -> str:
    """相对路径（如 /files/x.webp，本地桥接常见）按 base 的 origin 补全；绝对 URL 原样返回。"""
    if not url or not isinstance(url, str) or url.startswith(("http://", "https://")):
        return url
    try:
        p = urllib.parse.urlparse(base)
    except Exception:
        return url
    if not p.scheme:
        return url
    return f"{p.scheme}://{p.netloc}" + (url if url.startswith("/") else "/" + url)

def _final_out(out: str, url: str) -> str:
    """产物后缀与 --out 不符时按实际后缀存（避免 mp4 名装 webp 数据导致后续 ffmpeg 误判）。"""
    try:
        ue = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    except Exception:
        ue = ""
    oe = os.path.splitext(out)[1].lower()
    if ue and oe and ue != oe:
        new = os.path.splitext(out)[0] + ue
        print(f"[media_gen] 产物为 {ue}，已存为 {os.path.basename(new)}（用 ffmpeg 转码后再拼接）", file=sys.stderr)
        return new
    return out

def _download_image(resp: dict, out: str, base: str = "") -> None:
    d = (resp.get("data") or [{}])[0]
    url = _abs_url(d.get("url"), base)
    b64 = d.get("b64_json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if url:
        out = _final_out(out, url)
        urllib.request.urlretrieve(url, out)
    elif b64:
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
    else:
        die(f"响应无 url/b64: {json.dumps(resp)[:300]}")

def _resolve_async_task(used_key: dict, resp: dict, poll_path: str = "/tasks",
                        out: str = "", pool: str = "") -> dict:
    """异步任务式渠道兜底：响应无 url/b64 但有 task_id 时，轮询取最终结果。
    兼容魔搭风格（GET {base}/tasks/{id} → task_status/output_images）；
    非 task 式响应原样返回，同步渠道不受影响。
    轮询超时：有 out/pool 时任务落盘 pending（提交即扣不浪费额度）并 exit 4
    （与 video 超时协议对齐；batch worker 遇 rc=4 不重试，harvest 收割）。"""
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
    if out and pool:
        _save_pending_task(pool, str(tid), out, used_key.get("base", ""),
                           kind="image", poll_path=poll_path)
        print(f"[media_gen] 图任务轮询超时：task_id={tid} 已落盘，稍后跑 harvest 收割",
              file=sys.stderr)
        sys.exit(4)
    die("轮询超时 5 分钟")
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
            ipath = k.get("image_task_path") or k.get("task_path") or _info.get("task_path", "/images/generations")
            return http_call("POST", f"{k['base']}{ipath}", headers, body, timeout=600)

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

# ─── 视频超时协议：落盘 / 续等 / 收割 ─────────────────────
def _save_pending_task(pool: str, task_id: str, out: str, base: str,
                       kind: str = "video", poll_path: str = "") -> None:
    s = _load_state()
    s.setdefault("pending_tasks", {})[str(task_id)] = {
        "pool": pool, "out": out, "base": base, "submitted_at": time.time(),
        "kind": kind, "poll_path": poll_path,
    }
    _save_state(s)

def _pop_pending_task(task_id: str) -> dict | None:
    s = _load_state()
    rec = s.get("pending_tasks", {}).pop(str(task_id), None)
    if rec:
        _save_state(s)
    return rec

def _extract_video_url(st: dict) -> str | None:
    """从轮询响应提取视频 URL（兼容智谱/Agnes/网关等字段风格）。"""
    if not isinstance(st, dict):
        return None
    vr = st.get("video_result")
    if isinstance(vr, list) and vr and isinstance(vr[0], dict):
        u = vr[0].get("url")
        if isinstance(u, str) and u.startswith("http"):
            return u
    for k_ in ("video_url", "url"):
        if isinstance(st.get(k_), str) and st[k_].startswith("http"):
            return st[k_]
    data = st.get("data")
    if isinstance(data, dict):
        for k_ in ("video_url", "url"):
            if isinstance(data.get(k_), str) and data[k_].startswith("http"):
                return data[k_]
    return None

def _print_timeout_menu(exclude: str) -> None:
    """超时备选菜单：从用户实际接入动态生成，零硬编码池名。"""
    print("[media_gen] 备选视频池（按当前实际接入动态生成；agent 按 plan 的 video_pool_order 呈现给用户）：", file=sys.stderr)
    found = False
    for pool, pinfo in PROVIDERS.items():
        if pool == exclude or "video" not in pinfo.get("models", {}):
            continue
        try:
            ks = list_keys(pool, required=False)
        except Exception:
            ks = []
        if not ks:
            continue
        m = pinfo["models"]["video"]
        print(f"  - {pool}  [{pinfo.get('label', pool)}] free_kind={pinfo.get('free_kind', '?')}  {(m.get('note') or '')[:70]}", file=sys.stderr)
        found = True
    if not found:
        print("  （无其它可用视频池 → 选项：--wait-task 续等 / 放弃该镜）", file=sys.stderr)

def _poll_video_task(pool: str, info: dict, k: dict, video_id: str, out: str, args) -> None:
    """对已受理任务轮询到出片。超时：落盘 pending + 动态备池菜单 + exit 4（询问协议，agent 层向用户提问）。"""
    poll_url_base = k["poll"]
    pt = args.poll_timeout or (1800 if getattr(args, "provider", "") else 1200)
    deadline = time.time() + pt
    while time.time() < deadline:
        time.sleep(args.wait)
        if info.get("poll_style") == "path":
            poll_url = f"{poll_url_base}{info['poll_path']}/{video_id}"
        else:
            poll_url = f"{poll_url_base}{info['poll_path']}?{info['poll_param']}={video_id}"
        try:
            req = urllib.request.Request(poll_url)
            req.add_header("Authorization", f"Bearer {k['key']}")
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            print(f"[media_gen] 轮询异常: {e}", file=sys.stderr)
            continue
        url = _abs_url(_extract_video_url(st), k.get("base", ""))
        if url:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            out = _final_out(out, url)
            urllib.request.urlretrieve(url, out)
            print(f"[media_gen] video OK via {pool} -> {out}")
            return
        status = str(st.get("task_status") or st.get("status") or "").upper()
        if status in ("FAIL", "FAILED", "ERROR"):
            die(f"视频任务失败: {json.dumps(st, ensure_ascii=False)[:300]}")
    print(f"[media_gen] 轮询超时 {pt // 60} 分钟", file=sys.stderr)
    _save_pending_task(pool, video_id, out, k.get("base", ""))
    print(f"[media_gen] task {video_id} 已落盘（提交即扣，出片不浪费）：harvest 收割 / --wait-task {video_id} 零扣分续等", file=sys.stderr)
    _print_timeout_menu(pool)
    sys.exit(4)

def _wait_existing_task(args) -> None:
    rec = _load_state().get("pending_tasks", {}).get(str(args.wait_task))
    if not rec:
        die(f"落盘无记录: {args.wait_task}（可跑 harvest 查看待收列表）", 2)
    pool = rec["pool"]
    info = PROVIDERS[pool]["models"]["video"]
    keys = list_keys(pool, required=False)
    if not keys:
        die(f"池 {pool} 现无可用 key，无法续等", 2)
    out = rec.get("out") or args.out
    print(f"[media_gen] 续等 {pool} 任务 {args.wait_task}（零扣分，不重新提交）", file=sys.stderr)
    _poll_video_task(pool, info, keys[0], str(args.wait_task), out, args)
def _interleave_by_pool(spec: list[tuple[str, dict]],
                        pool_names: list[str]) -> list[tuple[str, dict]]:
    """(池,key) 候选按池轮流交错 → 混编时模型分布均匀（纯函数，可测）。"""
    by_pool: dict[str, list[tuple[str, dict]]] = {}
    for item in spec:
        by_pool.setdefault(item[0], []).append(item)
    out: list[tuple[str, dict]] = []
    while any(by_pool.values()):
        for p in pool_names:
            if by_pool.get(p):
                out.append(by_pool[p].pop(0))
    return out

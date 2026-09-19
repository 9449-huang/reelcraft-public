"""reelcraft 共享核心：PROVIDERS 注册表、key 枚举/路由、状态与冷却、异常、HTTP、节流、生成工具、异步轮询。由 media_gen / mg_batch / mg_status 引用，勿反向依赖。"""
from __future__ import annotations
import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import shutil
import socket
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
THROTTLE_FILE = Path.home() / ".workbuddy" / ".media_throttle.json"
LEDGER_FILE = Path.home() / ".workbuddy" / ".media_ledger.jsonl"
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
                # 多图参考（2026-09-11 实测）：extra_body.image=[...] → 走 /images/i2i/，
                # 角色特征保留。⚠️ 顶层 image 会被**静默忽略**（退化成 /images/t2i/）
                "ref_image_style": "extra_body",
                "ref_image_max": 4,
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
                # 首尾帧插值（2026-09-11 实测）：extra_body={"image":[首,尾],"mode":"keyframes"}
                # → 真插值（末帧≈第二关键帧）。免费池即支持，不必接付费池。
                "keyframes_style": "extra_body",
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
                "default_size": "1920x1080",    # 智谱视频原生达标分辨率
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


class RequestUncertain(Exception):
    """请求**已发出之后**才失败（读超时 / 连接被对端掐断）——服务端可能已受理。

    此时重试或换 key 都等于**再提交一次 = 再扣一次费**，所以既不重试也不 failover：
    直接抛到上层走"超时在途"协议（人工确认后台是否已有任务，再决定要不要重跑）。
    """

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

LOCK_TIMEOUT = 30.0          # 拿锁自旋上限（秒）：超时即告警降级，不再无限等
_WARNED_LOCKS: set[str] = set()   # 同一锁文件只告警一次，避免刷屏

class _FileLock:
    """跨进程文件锁（Windows msvcrt / POSIX fcntl）。
    batch 每镜是独立 subprocess，threading.Lock 挡不住跨进程竞态——
    state 读改写与节流落盘必须走这里。锁不住时降级为无锁执行（功能优先），
    但会向 stderr 告警，不做静默降级。"""

    def __init__(self, path):
        self.path = Path(str(path) + ".lock")
        self.fd = None

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT
        try:
            self.fd = open(self.path, "a+")
            self.fd.seek(0)
            while True:
                try:
                    import msvcrt
                    # 非阻塞 + 自旋到超时：LK_LOCK 会在内核里盲重试约 10s 再抛错，
                    # 期间无法感知也无法区分"慢"和"死等"，超时预算必须自己掌握
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except ImportError:
                    import fcntl
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.time() >= deadline:
                        raise
                    time.sleep(0.05)
        except Exception as e:
            # 拿不到锁：必须关句柄（否则泄漏）并**明确告警**——
            # 此前这里静默降级为无锁，等于 #1/#6 的跨进程保护在高并发下悄悄失效
            if self.fd is not None:
                try:
                    self.fd.close()
                except Exception:
                    pass
                self.fd = None
            if self.path.name not in _WARNED_LOCKS:
                _WARNED_LOCKS.add(self.path.name)
                print(f"[media_gen] ⚠ 文件锁获取失败（{self.path.name}），降级无锁：{e}",
                      file=sys.stderr)
        return self

    def __exit__(self, *exc):
        if self.fd is None:
            return False
        try:
            try:
                import msvcrt
                msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            self.fd.close()
        return False

def _atomic_write_json(path: Path, s: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)       # 原子替换：防并发写坏半截 JSON

def _load_state() -> dict:
    with _state_lock:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}          # 状态文件不存在：返回空 dict（此前隐式返回 None，首次落盘会炸）
# 注：旧的 _save_state() 已删除——它是"读改写"竞态的入口（#1），
# 所有写路径必须走下面的 _update_state()，别再把它加回来。

def _update_state(fn) -> dict:
    """跨进程安全的 state 读改写：文件锁内 读→fn(state)→原子写。
    fn 直接就地修改传入的 dict。返回处理后的 state。
    锁内顺手惰性清理过期冷却条目（防止状态文件只进不出）。"""
    with _state_lock:
        with _FileLock(STATE_FILE):
            s = {}
            if STATE_FILE.exists():
                try:
                    s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    s = {}
            now = time.time()
            for _p, _cd in (s.get("cooldown") or {}).items():
                if isinstance(_cd, dict):
                    for _kn in [k for k, t in _cd.items()
                                if not isinstance(t, (int, float)) or t <= now]:
                        _cd.pop(_kn, None)
            fn(s)
            _atomic_write_json(STATE_FILE, s)
            return s

def _cooldown_update(provider: str, kn: str, until: float | None) -> None:
    """跨进程安全更新单把 key 的冷却：until=None 清除，否则设为 until。"""
    def _fn(s):
        cd = s.setdefault("cooldown", {}).setdefault(provider, {})
        if until is None:
            cd.pop(kn, None)
        else:
            cd[kn] = until
    _update_state(_fn)

# ─── 成本账本（#5）：每次真实 API 调用记一条 JSONL ──────────
# 设计：append-only JSONL 而非 state 读改写——追加天然适合 batch 多进程并发，
# 且坏行只影响自己（summarize 容错跳过），不会炸整个 state。
# "成本"在免费档语境 = 调用次数（RPM 限速）与失败白烧的次数。

def ledger_append(path: Path, entry: dict) -> None:
    """锁内追加一行 JSON（ts/ms 缺省自动补）。path 父目录不存在则建。"""
    e = dict(entry)
    e.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    e.setdefault("ms", 0)
    e["err"] = str(e.get("err", ""))[:80]      # 防长堆栈撑爆文件
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FileLock(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

def ledger_read(path: Path) -> list:
    """读 JSONL → 行列表（坏行/非 dict/缺 ts 跳过，不炸）。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows = []
    for ln in raw:
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(r, dict) and r.get("ts"):
            rows.append(r)
    return rows

def ledger_summarize(rows: list, days: int = 0) -> dict:
    """聚合报表（纯函数）：总/成功/失败/白烧 + 按池/操作/天分组。
    days>0 只统计最近 N 天（按 ts 前缀比较，与 ledger_append 的 ts 格式一致）；
    坏行（None/非 dict/缺 ts）直接跳过——调用方喂原始行也不炸。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S") \
        if days > 0 else ""

    def _bucket(d, key, ok, ms):
        b = d.setdefault(key, {"total": 0, "ok": 0, "fail": 0, "ms": 0})
        b["total"] += 1
        b["ok" if ok else "fail"] += 1
        b["ms"] += int(ms or 0)
    out = {"total": 0, "ok": 0, "fail": 0, "wasted": 0,
           "by_provider": {}, "by_op": {}, "by_day": {}}
    for r in rows:
        if not isinstance(r, dict) or not r.get("ts"):
            continue
        try:    # ts 必须是可解析的日期形状（YYYY-MM-DD...），非法历史脏行跳过
            _dt.datetime.strptime(str(r["ts"])[:10], "%Y-%m-%d")
        except ValueError:
            continue
        if cutoff and str(r["ts"]) < cutoff:
            continue
        ok = bool(r.get("ok"))
        ms = r.get("ms") or 0
        out["total"] += 1
        out["ok" if ok else "fail"] += 1
        if not ok:
            out["wasted"] += 1
        _bucket(out["by_provider"], str(r.get("provider", "?")), ok, ms)
        _bucket(out["by_op"], str(r.get("op", "?")), ok, ms)
        _bucket(out["by_day"], str(r.get("ts", ""))[:10], ok, ms)
    return out

# 产物扩展名白名单：有的池（LTX Bridge 等）出的是 .webp 动图而非 .mp4。
# 凡是"这镜算不算已经出片了"的判断（断点续跑、拼接收集）都必须遍历这个常量，
# 别再各处手写 .mp4——漏一个扩展名就是整镜重做或整镜丢失（#3）。
PRODUCT_EXTS: tuple[str, ...] = (".mp4", ".webp")

def find_existing_product(out: str) -> str:
    """断点续跑：找"这镜算不算已经出片了"的落点——out 本身或按 PRODUCT_EXTS
    换后缀的兄弟产物。有的池（LTX Bridge 等）会经 _final_out 把 .mp4 落成 .webp，
    只查原路径会把已完成镜误判为未完成 → 重新提交 → 重复扣费（#3）。
    返回已存在且非空的落点路径；都没有返回空串。"""
    base = Path(out)
    cands = [base] + [base.with_suffix(ext) for ext in PRODUCT_EXTS
                      if ext.lower() != base.suffix.lower()]
    for c in cands:
        if c.exists() and c.stat().st_size > 0:
            return str(c)
    return ""

def run_capture(cmd: list[str], timeout: int | None = None, env: dict | None = None
                ) -> "subprocess.CompletedProcess[str]":
    """统一 subprocess 文本捕获 runner：强制 utf-8 解码。
    中文 Windows 的默认 locale 是 GBK，ffmpeg 等工具输出 UTF-8 中文（文件路径/提示）
    时 text=True 缺省按 GBK 解会炸 UnicodeDecodeError——所有捕获输出的调用必须走这里
    （或自带 encoding="utf-8", errors="replace"）。"""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def is_transition(d: dict) -> bool:
    """过渡镜判定（**唯一实现**——此前 mg_batch 定义一份、pipeline 内联两处，
    概念无单一真相源，加能力必漏一处）。带 transition 字段且 from/to 齐全即算。"""
    tr = d.get("transition")
    return isinstance(tr, dict) and bool(tr.get("from")) and bool(tr.get("to"))


def find_product(clips_dir, sid: str):
    """找 sid 的已有 clip 产物（.mp4/.webp，任一存在且非空即算；无则 None）。

    断点续跑只认"存在且非空"：产物可能是 .webp（部分渠道出动画 webp），
    只认 .mp4 会误判未完成 → 重新提交 → 重复扣费。"""
    for _ext in PRODUCT_EXTS:
        cand = Path(clips_dir) / f"clip_{sid}{_ext}"
        if cand.exists() and cand.stat().st_size > 0:
            return cand
    return None


def natkey(s) -> list:
    """自然排序 key：数字段按数值比较（S2 < S10、clip_S2 < clip_S10）。
    接受 str/Path。所有 glob 排序（分镜 JSON、clip 产物、帧序列）必须用它——
    字典序在镜号不补零（S1..S10）时会把 S10 排到 S2 前，导致拼接镜序错乱（#2）。"""
    return [int(x) if x.isdigit() else x
            for x in re.split(r"(\d+)", str(s).lower())]


# ─── 负面模板按题材分档（优化⑧）────────────────────────
# 一套 negative 打天下是偷懒：人物镜最怕换脸/肢体乱，风景镜怕画面脏乱，产品镜怕光斑/畸变。
# shot JSON 的 type/subject 关键词自动选档；兜底通用（原默认）。
NEGATIVE_TEMPLATES: dict[str, str] = {
    "character": ("distorted faces, warped hands, extra limbs, mutated fingers, "
                  "asymmetrical eyes, teeth artifacts, clothing artifacts, face swap, "
                  "body horror, plastic skin, oversaturated"),
    "landscape": ("cluttered composition, noisy details, oversharpened, chromatic "
                  "aberration, lens flare artifacts, double exposure, garbled textures, "
                  "oversaturated, watermark"),
    "product": ("distorted geometry, warped edges, specular blowout, reflection artifacts, "
                "floating parts, broken symmetry, missing details, oversaturated, "
                "text artifacts, watermark"),
}
NEGATIVE_GENERIC = ("blurry, distorted faces, warped hands, extra limbs, text artifacts, "
                    "watermark, camera shake, flickering, plastic skin, oversaturated")

_TYPE_KWS = {
    "character": ("character", "person", "portrait", "face", "human", "figure",
                  "role", "actor", "人物", "角色", "肖像", "人脸"),
    "landscape": ("landscape", "scenery", "nature", "mountain", "cityscape", "ocean",
                  "forest", "sky", "风景", "自然", "山川", "城市"),
    "product": ("product", "object", "bottle", "packaging", "gadget", "商品", "产品",
                "器物", "瓶"),
}

def _kw_hit(keywords, hay: str) -> bool:
    """题材关键词命中：英文按词边界（防 "nature" 误中 "naturally"、"product" 误中
    "produce" 的子串误档）；中文关键词无空格边界，保持子串（独立成义，误伤率低）。"""
    for k in keywords:
        if any("\u4e00" <= c <= "\u9fff" for c in k):
            if k in hay:
                return True
        elif re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", hay):
            return True
    return False


def negative_for_shot(shot: dict, fallback: str = "") -> str:
    """按 shot 题材自动选 negative 模板；命中多档取第一个（character→landscape→
    product），未命中兜底。题材来源：slot type / subject / role / dramatic_function。"""
    hay = " ".join([
        str(shot.get("type") or ""), str(shot.get("subject") or ""),
        str(shot.get("role") or ""), str(shot.get("dramatic_function") or ""),
    ]).lower()
    for kind in ("character", "landscape", "product"):
        if _kw_hit(_TYPE_KWS[kind], hay):
            return NEGATIVE_TEMPLATES[kind]
    return fallback or NEGATIVE_GENERIC


def list_shot_files(shots_dir) -> list:
    """枚举分镜 JSON（S*.json 与 shot_*.json 两种命名），自然排序，**按文件去重**。

    去重不可省：Windows 的文件 glob 大小写不敏感，`glob("S*.json")` 与
    `glob("shot_*.json")` 都会匹配到 `shot_01.json` → 每镜被算两次（双跑/双缓推）。
    Linux/macOS 大小写敏感不复现，故只有 Windows 上静默出错。
    返回 Path 列表（唯一、已排序）。"""
    d = Path(shots_dir)
    seen: dict[str, Path] = {}
    for pat in ("S*.json", "shot_*.json"):
        for p in d.glob(pat):
            seen[str(p.resolve())] = p          # 按解析后路径去重
    return [seen[k] for k in sorted(seen, key=lambda k: natkey(Path(k).name))]

def scan_tts_slots(env: dict, max_n: int = 200) -> list:
    """扫已配 TTS key 的序号（纯函数）：起始空号跳过，遇配置后连续 3 空号停。

    放在 mg_core 是刻意的——**唯一实现**。此前 media_gen 与 mg_status 各写一份，
    口径不一致导致 status 在 key#1 空缺时误报"tts 未配置"（假告警）。
    判"已配"用真值（空字符串=未配），与 list_keys 口径一致。
    """
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
                 "video_prompt_field": os.environ.get(f"{prefix}{n}_VIDEO_PROMPT_FIELD", ""),
                 # 过渡镜（首尾帧双条件）：key 级覆盖尾帧字段名/列表形（未配置=空串）
                 "last_frame_param": os.environ.get(f"{prefix}{n}_LAST_FRAME_PARAM", ""),
                 "last_frame_list": os.environ.get(f"{prefix}{n}_LAST_FRAME_LIST", "").strip().lower()
                     in ("1", "true", "yes", "list"),
                 # 能力级 key 覆盖（v4.7.8）：ref_image_supported/keyframes_supported 早就有
                 # `key.get(...)` 分支，但这里从不写入 → 形同虚设；而报错提示恰恰让用户
                 # 配 `_REF_IMAGE_STYLE=extra_body` → 照配仍 die（自指死循环）。补齐。
                 "ref_image_style": os.environ.get(f"{prefix}{n}_REF_IMAGE_STYLE", ""),
                 "ref_image_max": os.environ.get(f"{prefix}{n}_REF_IMAGE_MAX", ""),
                 "keyframes_style": os.environ.get(f"{prefix}{n}_KEYFRAMES_STYLE", "")}
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

_UNCERTAIN_CAUSES = None      # 延迟构造（http.client 要 import）


def _uncertain_causes() -> tuple:
    return (socket.timeout, TimeoutError, ConnectionResetError,
            http.client.RemoteDisconnected)


def classify_network_error(exc: BaseException, method: str) -> str:
    """网络异常分类（纯函数）：'uncertain' = 请求可能已送达；'retry' = 确定没送达。

    只有**非幂等**方法（POST/PUT/PATCH）才把"发出后失败"判为 uncertain——
    GET 重试无副作用，保留韧性。urllib 的 URLError 会把真实原因包在 .reason 里，
    必须拆开看（否则读超时会被误判为普通连接错误而重试）。
    """
    cause: BaseException = exc
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        cause = exc.reason
    if method.upper() in ("POST", "PUT", "PATCH") and isinstance(cause, _uncertain_causes()):
        return "uncertain"
    return "retry"


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
            # 非幂等请求"发出后失败"：服务端可能已受理 → 重试 = 再提交一次 = 重复扣费。
            # 原来是通用 except 一律重试 3 次（POST 就打 3 个任务），这里改成直接抛出。
            if classify_network_error(e, method) == "uncertain":
                raise RequestUncertain(
                    f"{method} {url} 发出后失败（{type(e).__name__}: {e}）——"
                    f"服务端可能已受理，已停止重试以免重复扣费。"
                    f"先确认后台是否已产生任务，再决定是否手动重跑")
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
    # 过期条目由 _update_state 惰性清理；这里仅按时间判断是否跳过
    last_err = None
    for k in keys:
        kn = str(k["n"])
        if cooldown.get(kn, 0) > time.time():
            print(f"[media_gen] {provider} key #{kn} 冷却中，跳过", file=sys.stderr)
            continue
        t0 = time.time()
        try:
            resp = call_fn(k)
            _cooldown_update(provider, kn, None)    # 成功 → 清除冷却（跨进程安全）
            ledger_append(LEDGER_FILE, {"provider": provider, "key": k["n"],
                                        "op": kind, "ok": True,
                                        "ms": int((time.time() - t0) * 1000)})
            return resp, k
        except ProviderFatal as e:
            ledger_append(LEDGER_FILE, {"provider": provider, "key": k["n"],
                                        "op": kind, "ok": False, "err": str(e),
                                        "ms": int((time.time() - t0) * 1000)})
            raise
        except PermissionError as e:
            print(f"[media_gen] {provider} key #{kn} 鉴权失败，记入黑名单: {e}", file=sys.stderr)
            _cooldown_update(provider, kn, time.time() + 86400)
            cooldown[kn] = time.time() + 86400      # 内存镜像同步，避免同轮重复打
            ledger_append(LEDGER_FILE, {"provider": provider, "key": k["n"],
                                        "op": kind, "ok": False, "err": str(e),
                                        "ms": int((time.time() - t0) * 1000)})
            last_err = e
        except RateLimitedError as e:
            _cooldown_update(provider, kn, time.time() + cooldown_default)
            cooldown[kn] = time.time() + cooldown_default   # 内存镜像同步
            print(f"[media_gen] {provider} key #{kn} 限流，冷却 {cooldown_default}s: {e}", file=sys.stderr)
            ledger_append(LEDGER_FILE, {"provider": provider, "key": k["n"],
                                        "op": kind, "ok": False, "err": str(e),
                                        "ms": int((time.time() - t0) * 1000)})
            last_err = e
        except RequestUncertain as e:
            # 不换 key：换 key 也是一次新提交（重复扣费）。但必须落账——
            # 这很可能是一笔**已经产生**的费用，账本漏了就等于白烧额度没人知道。
            ledger_append(LEDGER_FILE, {"provider": provider, "key": k["n"],
                                        "op": kind, "ok": False,
                                        "err": f"uncertain: {e}",
                                        "ms": int((time.time() - t0) * 1000)})
            raise
        except Exception as e:
            ledger_append(LEDGER_FILE, {"provider": provider, "key": k["n"],
                                        "op": kind, "ok": False, "err": str(e),
                                        "ms": int((time.time() - t0) * 1000)})
            last_err = e
            continue
    raise AllKeysFailed(f"{provider} 所有 key 失败: {last_err}")

# ─── 视频节流（按 key 各自计时 → 双 key 双线并行不互卡）────
# 时间戳落盘（THROTTLE_FILE）：batch 每镜是独立 subprocess，内存 dict 跨进程
# 各自为空会令 RPM 节流失效——必须跨进程共享（文件锁内读改写）。

def video_throttle(rpm: int, tag: str = "default") -> None:
    """tag 形如 agnes_key1 / agnes_key2 / default。每个 tag 独立计时（跨进程共享）。"""
    interval = max(1.0, 60.0 / max(1, rpm))
    wait = 0.0
    with _FileLock(THROTTLE_FILE):
        ts: dict[str, float] = {}
        try:
            ts = json.loads(THROTTLE_FILE.read_text(encoding="utf-8"))
        except Exception:
            ts = {}
        now = time.time()
        # 惰性清理：超过 1 天没再打过的 tag 直接丢，防止节流文件只进不出
        ts = {k: v for k, v in ts.items()
              if isinstance(v, (int, float)) and now - float(v) < 86400}
        wait = interval - (now - float(ts.get(tag, 0.0)))
        ts[tag] = now + max(0.0, wait)
        _atomic_write_json(THROTTLE_FILE, ts)
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
    """相对路径（如 /files/x.webp，本地桥接常见）按 base 的 origin 补全；绝对 URL 原样返回。
    Windows 本地路径（E:\\... / E:/...，盘符含冒号）不是 URL，原样返回（#10）。"""
    if not url or not isinstance(url, str):
        return url
    if url.startswith(("http://", "https://")):
        return url
    if len(url) >= 3 and url[0].isalpha() and url[1] == ":" and url[2] in ("\\", "/"):
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

def _download(url: str, out: str | Path, timeout: float = 120) -> None:
    """取产物（**统一入口**，轮询/收割/同步三路共用）。

    两类来源（v4.7 审计 P1 修复：轮询路径原对 local_path urlopen -> ValueError）：
    - 本地路径（Windows 盘符 / POSIX 绝对路径，_extract_video_url 原样透传的
      local_path）：直接 copyfile——不走网络、不受 http timeout 约束（#10）
    - URL：带超时的下载。先写临时文件再 os.replace 原子落盘，防半截文件被
      断点续跑误判吞掉（urlretrieve 无超时，网络半死会无限挂起，#7）。
      临时名带 pid：两个 subprocess 下同一 out 不会互相覆盖半截内容；
      失败必须清掉残骸，否则 *.dl 垃圾会一直堆在工作区。"""
    if not str(url).startswith(("http://", "https://")):
        # 本地路径（含 local_path）：urlopen 会抛 ValueError，必须直拷
        src = Path(url)
        if not src.is_file():
            raise FileNotFoundError(f"取产物失败：本地路径不存在 {url}")
        tmp = f"{out}.dl.{os.getpid()}"
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, out)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return
    tmp = f"{out}.dl.{os.getpid()}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _download_image(resp: dict, out: str, base: str = "") -> None:
    d = (resp.get("data") or [{}])[0]
    url = _abs_url(d.get("url"), base)
    b64 = d.get("b64_json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if url:
        out = _final_out(out, url)
        _download(url, out)
    elif b64:
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
    else:
        die(f"响应无 url/b64: {json.dumps(resp)[:300]}")

def build_poll_url(base: str, info: dict, task_id: str,
                   poll_path: str = "", style: str = "") -> str:
    """轮询 URL 构造（单源，批四 B）：path 式 /{path}/{id}、query 式 /{path}?{param}={id}。

    三处调用方曾各拼一份（_poll_video_task / _harvest_video / cmd_edit）——
    poll_style 分叉散落 = 加新池必漏一处。
    style 缺省沿 info['poll_style']；两者都未声明时按 query（与旧
    _poll_video_task 的 else 分支一致——agnes 等池依赖此缺省，勿改）。
    poll_path 非空时覆盖 info（harvest 用落盘记录里的路径，记录优先）。"""
    path = poll_path or info.get("poll_path") or "/tasks"
    if (style or info.get("poll_style") or "query") == "path":
        return f"{base}{path}/{task_id}"
    return f"{base}{path}?{info.get('poll_param', 'task_id')}={task_id}"


def fetch_task_state(url: str, headers: dict, timeout: float = 30) -> tuple[dict | None, str | None]:
    """单次任务状态查询（harvest 收割用）。返回 (state, err)：
    异常不 raise，返回 (None, err) 由调用方决定跳过/重试。"""
    try:
        req = urllib.request.Request(url)
        for hk, hv in headers.items():
            req.add_header(hk, hv)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore")), None
    except Exception as e:
        return None, str(e)


def poll_tasks(url: str, headers: dict, interval: float, deadline: float,
               timeout: float = 60, clock=time.time):
    """轮询生成器（骨架单源，批四 B）：每轮 sleep(interval) → GET → yield (state, err)。

    语义差异（终态集合/结果提取/超时后动作）留在调用方——生成器只管
    节奏与网络；网络异常 yield (None, err) 不吞，由调用方决定继续/终止。
    deadline 到点后生成器自然停止（调用方执行自己的超时动作）。"""
    while clock() < deadline:
        time.sleep(interval)
        try:
            req = urllib.request.Request(url)
            for hk, hv in headers.items():
                req.add_header(hk, hv)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                yield json.loads(r.read().decode("utf-8", "ignore")), None
        except Exception as e:
            yield None, str(e)


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
    poll_url = build_poll_url(used_key["base"], {}, str(tid),
                              poll_path=poll_path, style="path")
    headers = {"Authorization": f"Bearer {used_key['key']}",
               "X-ModelScope-Task-Type": "image_generation"}
    print(f"[media_gen] 异步任务式响应，轮询 {poll_url} …", file=sys.stderr)
    deadline = time.time() + 300
    for st, perr in poll_tasks(poll_url, headers, interval=3, deadline=deadline,
                               timeout=60):
        if st is None:
            print(f"[media_gen] 轮询异常: {perr}", file=sys.stderr)
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
                           kind="image", poll_path=poll_path,
                           key_n=used_key.get("n", 0))
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
        # 多图参考（角色一致性，v4.7）：池或**任一 key**（env 覆盖）声明支持才算候选
        refs_raw = [str(p) for p in (getattr(args, "ref_image", None) or [])]
        sup = ref_image_candidates(pool, info)
        if refs_raw and not sup:
            if args.provider:
                die(f"{pool} 未声明支持多图参考（ref_image_style）。custom 池在 env 加 "
                    f"{PROVIDERS[pool]['key_env_prefix']}1_REF_IMAGE_STYLE=extra_body 启用", 2)
            continue
        if refs_raw and sup and len(refs_raw) > sup["max"]:
            die(f"参考图最多 {sup['max']} 张（收到 {len(refs_raw)}）——"
                f"再多请合并成一张角色设定图", 2)

        def call_fn(k: dict, _info=info, _prefix=PROVIDERS[pool]["key_env_prefix"]) -> dict:
            model = k.get("image_model") or _info["default"]
            if not model:
                die(f"该池未配置模型名（模板无默认值）。请在 env 加 {_prefix}{k['n']}_IMAGE_MODEL=模型名", 2)
            if k.get("image_sizes") and size not in k["image_sizes"]:
                print(f"[media_gen] [warn] _IMAGE_SIZES 自填白名单不含 size={size}，仅警告不拦截（渠道真实能力以实跑为准）", file=sys.stderr)
            headers = {"Authorization": f"Bearer {k['key']}", "Content-Type": "application/json"}
            refs = [image_to_uri_shrunk(p) for p in refs_raw] if refs_raw else None
            body = build_image_body(model, args.prompt, size, n=1, refs=refs)
            ipath = k.get("image_task_path") or k.get("task_path") or _info.get("task_path", "/images/generations")
            return http_call("POST", f"{k['base']}{ipath}", headers, body, timeout=600)

        try:
            resp, used = call_with_failover(pool, call_fn, kind="image", pin_key=args.pin_key)
            used["pool"] = pool
            return resp, used
        except RequestUncertain as e:
            # 不确定 = 上游可能已受理：跨池兜底也是**再提交一次**，必须停。
            # 走超时在途协议（exit 4）：人工确认后台再决定是否重跑，不自动重扣。
            die(str(e), 4)
        except AllKeysFailed as e:
            errs.append(str(e))
            if args.provider:
                die(str(e), 3)
            print(f"[media_gen] {pool} 全部 key 失败，尝试下一池…", file=sys.stderr)
            continue
    die("所有可用池均失败:\n  " + "\n  ".join(errs), 3)

# ─── 视频 ─────────────────────────────────────────────────
def keyframes_supported(info: dict, key: dict | None = None) -> bool:
    """首尾帧插值（keyframes）支持的声明式判定。

    与 last_frame_param 是**两种不同的承载方式**：
      - keyframes_style == "extra_body"（Agnes 风，2026-09-11 实测）：
        extra_body = {"image": [首帧, 尾帧], "mode": "keyframes"}，不传顶层首帧
      - last_frame_param（custom 池风，各家字段名不同）：见 build_last_frame_fields
    未声明 → False（调用方走老路径）。"""
    key = key or {}
    return (key.get("keyframes_style") or info.get("keyframes_style")) == "extra_body"


def load_character_bible(shots_dir) -> dict:
    """读 `<shots>/characters.json`（角色圣经）。缺失/损坏 → {}（容错，绝不炸 batch）。

    v4.9.0（漫剧方向第一步）：角色级连续性（跨几十镜引用同一角色）不该靠每镜手抄
    参考图路径——在角色圣经里定义一次，shot 只写 `characters: ["hero"]`。"""
    p = Path(shots_dir) / "characters.json"
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[media_gen] [warn] characters.json 解析失败（{e}），本次跳过角色圣经",
              file=sys.stderr)
        return {}
    return d if isinstance(d, dict) else {}


def merge_character_refs(shot: dict, bible: dict) -> tuple[list[str], list[str]]:
    """shot 的 `characters`（角色 id）→ 参考图路径，与自带 `ref_image` 合并去重。

    返回 `(refs, missing_ids)`；missing_ids 非空 = 角色 id 在圣经里查不到——调用方
    必须报 MISS（**不能静默丢角色一致性**，那正是漫剧最贵的返工）。
    bible 为空时等价于只回退 `ref_image`（旧行为不变）。"""
    refs: list[str] = []
    chars = shot.get("characters") or []
    if isinstance(chars, str):
        chars = [c.strip() for c in chars.split(",") if c.strip()]
    missing: list[str] = []
    for cid in chars:
        ent = (bible.get("characters") or {}).get(str(cid))
        if not isinstance(ent, dict):
            missing.append(str(cid))
            continue
        for r in (ent.get("ref_images") or []):
            if str(r) not in refs:
                refs.append(str(r))
    own = shot.get("ref_image") or []
    if isinstance(own, str):
        own = [own]
    for r in own:
        if str(r) not in refs:
            refs.append(str(r))
    return refs, missing


def ref_image_candidates(pool: str, info: dict) -> dict | None:
    """多图参考能力判定（池级优先，其次 key 级 env 覆盖）。

    v4.7.8：`_gen_image_once` 的候选筛选原来只认池级声明，而报错提示又让用户
    配 key 级 `_REF_IMAGE_STYLE=extra_body` → 池模板没声明时照提示配完仍被排除
    （提示自指死循环）。这里把"池或其任一 key 声明"统一成一个判定。"""
    sup = ref_image_supported(info)
    if sup:
        return sup
    for kk in list_keys(pool, required=False):
        sup = ref_image_supported(info, kk)
        if sup:
            return sup
    return None


def ref_image_supported(info: dict, key: dict | None = None) -> dict | None:
    """多图参考（角色一致性）支持的声明式判定。未声明 → None。

    返回 {"style": "extra_body", "max": N}。同样支持 key 级覆盖
    （同池不同 key 接不同上游时）。"""
    key = key or {}
    style = key.get("ref_image_style") or info.get("ref_image_style")
    if style != "extra_body":
        return None
    mx = key.get("ref_image_max") or info.get("ref_image_max") or 4
    try:
        mx = int(mx)
    except (TypeError, ValueError):
        mx = 4
    return {"style": style, "max": mx}


def build_image_body(model: str, prompt: str, size: str, n: int = 1,
                     refs: list[str] | None = None) -> dict:
    """出图 request body。带参考图时**必须**走 extra_body（实测坑）：

    Agnes 的参考图/response_format 放 extra_body 才生效；放顶层会被**静默忽略**
    → 退化成文生图（返回 URL 从 /images/i2i/ 变 /images/t2i/），不报错、难排查。"""
    body: dict = {"model": model, "prompt": prompt, "size": size, "n": n}
    if refs:
        body["extra_body"] = {"image": list(refs), "response_format": "url"}
    return body


def image_to_uri_shrunk(path: str, max_bytes: int = 450_000,
                        max_side: int = 1536) -> str:
    """本地图片 → data URI；超体积时先转 JPEG（保尺寸、必要时缩边）再编码。

    实测教训（2026-09-11）：960KB PNG 直传 Agnes → 读超时（2m23s 失败）；
    同图缩到 220KB → 18s 成功。参考图/首帧/尾帧都吃这条上游约束。
    小文件（≤ max_bytes）走原路径，行为与 image_to_url_or_path 完全一致（零回归）。
    """
    p = Path(path)
    if not p.exists():
        return path                      # URL 或缺失：原样交由上层处理
    try:
        if p.stat().st_size <= max_bytes:
            return image_to_url_or_path(path)
        from PIL import Image
        import io
        with Image.open(p) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_side:
                r = max_side / max(w, h)
                im = im.resize((max(1, int(w * r)), max(1, int(h * r))))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=90, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[media_gen] [warn] 压缩 {p.name} 失败（{e}），按原图传", file=sys.stderr)
        return image_to_url_or_path(path)


def build_last_frame_fields(info: dict, key: dict | None = None) -> dict | None:
    """过渡镜（首尾帧双条件）：解析尾帧 payload 字段名与形式。

    池模板 / key env（MEDIA_<P>_n_LAST_FRAME_PARAM，list_keys 已并入 key dict）声明支持：
      - last_frame_param + last_frame_list：字段名 + 是否列表（各家叫法不同：
        tail_image / lastFrame / end_frame…）
      - last_frame: True 的简写：支持但字段名用默认 "last_frame"
    未声明支持 → 返回 None（调用方不传，老行为不变）。
    key 级覆盖优先于池级（同池不同 key 接不同上游时用）。
    返回 {字段名: None}——值由调用方填 URL/路径（与首帧同一编码方式）。"""
    key = key or {}
    param = key.get("last_frame_param") or info.get("last_frame_param")
    if not param:
        if info.get("last_frame") or key.get("last_frame"):
            param = "last_frame"      # 声明支持但未指定名 → 默认名
        else:
            return None
    return {param: None}


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
                       kind: str = "video", poll_path: str = "", key_n: int = 0) -> None:
    def _fn(s):
        s.setdefault("pending_tasks", {})[str(task_id)] = {
            "pool": pool, "out": out, "base": base, "submitted_at": time.time(),
            "kind": kind, "poll_path": poll_path,
            # key 序号（v4.7.8）：续等必须用提交时那把 key——多 key 池（agnes 跨域名）
            # 用 keys[0] 轮询必失败，任务永远收不回
            "key_n": int(key_n or 0),
        }
    _update_state(_fn)          # 跨进程安全：防并发覆盖丢 pending 记录

def _pop_pending_task(task_id: str) -> dict | None:
    rec: dict | None = None
    def _fn(s):
        nonlocal rec
        rec = s.get("pending_tasks", {}).pop(str(task_id), None)
    _update_state(_fn)
    return rec

def _extract_video_url(st: dict) -> str | None:
    """从轮询响应提取视频 URL（兼容智谱/Agnes/网关等字段风格）。
    本地桥接（LTXBridge 风）POST 同步阻塞、响应自带 local_path——直拷本地文件，
    不走网络、不受 http timeout 约束（#10）。
    同时接受相对路径 URL（如 /files/x.webp），由调用方 _abs_url 按 base origin 补全。"""
    if not isinstance(st, dict):
        return None
    lp = st.get("local_path")
    if isinstance(lp, str) and lp and os.path.exists(lp):
        return lp
    vr = st.get("video_result")
    if isinstance(vr, list) and vr and isinstance(vr[0], dict):
        u = vr[0].get("url")
        if isinstance(u, str) and u.startswith(("http", "/")):
            return u
    for k_ in ("video_url", "url"):
        if isinstance(st.get(k_), str) and st[k_].startswith(("http", "/")):
            return st[k_]
    data = st.get("data")
    if isinstance(data, dict):
        for k_ in ("video_url", "url"):
            if isinstance(data.get(k_), str) and data[k_].startswith(("http", "/")):
                return data[k_]
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        # 部分网关 data 是数组（OpenAI 兼容结构），此前仅认 dict 漏检（#9）
        for k_ in ("video_url", "url"):
            if isinstance(data[0].get(k_), str) and data[0][k_].startswith(("http", "/")):
                return data[0][k_]
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
    poll_url = build_poll_url(poll_url_base, info, video_id)   # URL 单次构造（不含时间变项）
    headers = {"Authorization": f"Bearer {k['key']}"}
    for st, perr in poll_tasks(poll_url, headers, interval=args.wait,
                               deadline=deadline, timeout=60):
        if st is None:
            print(f"[media_gen] 轮询异常: {perr}", file=sys.stderr)
            continue
        url = _abs_url(_extract_video_url(st), k.get("base", ""))
        if url:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            out = _final_out(out, url)
            _download(url, out)
            print(f"[media_gen] video OK via {pool} -> {out}")
            return
        status = str(st.get("task_status") or st.get("status") or "").upper()
        if status in ("FAIL", "FAILED", "ERROR"):
            die(f"视频任务失败: {json.dumps(st, ensure_ascii=False)[:300]}")
    print(f"[media_gen] 轮询超时 {pt // 60} 分钟", file=sys.stderr)
    _save_pending_task(pool, video_id, out, k.get("base", ""), key_n=k.get("n", 0))
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
    # 用**提交时那把 key**续等（v4.7.8）：多 key 池（agnes 3 把跨域名）用 keys[0]
    # 会 401/404 空转到 deadline；记录里的 key 已移除时退回第一把（旧记录兼容）
    target_n = int(rec.get("key_n") or 0)
    k = next((x for x in keys if x["n"] == target_n), keys[0]) if target_n else keys[0]
    print(f"[media_gen] 续等 {pool} 任务 {args.wait_task}（零扣分，不重新提交）", file=sys.stderr)
    _poll_video_task(pool, info, k, str(args.wait_task), out, args)
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

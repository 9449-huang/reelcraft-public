#!/usr/bin/env python3
"""[DEPRECATED] Agnes 生图 / 视频 一体化调用器（标准库，零依赖）。

⚠️ 已弃用：多 provider 路由 / 多 key 轮转 / 熔断 / per-key 节流 / QC 已由
 scripts/media_gen.py 全面接管。本文件仅保留旧 enhance 命令供历史回溯，
新流程请勿再调用本脚本。

用法：
  python agnes_gen.py image   --prompt "..." --size 1024x1024 --out shots/shot_01.png
  python agnes_gen.py video   --prompt "..." --image shots/shot_01.png --out clips/clip_01.mp4 --wait 60
  python agnes_gen.py enhance --system-file ../references/prompt-enhance-system.md --raw "..." [--i2v] [--role "..."] [--anchor "..."] [--palette "..."]

要求：环境变量 AGNES_API_KEY（可加 AGNES_BASE_URL 覆盖 base）。
视频限速：串行 + --wait 间隔（默认 60s，免费档 1 RPM）。
Key 绝不打印、绝不写盘。
"""
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
import urllib.error

def _load_env_file():
    """自动加载 ~/.workbuddy/agnes_key.env（若存在且变量未设置）。"""
    import pathlib
    env_file = pathlib.Path.home() / ".workbuddy" / "agnes_key.env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'export\s+([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"', line)
        if m and not os.environ.get(m.group(1)):
            os.environ[m.group(1)] = m.group(2)

_load_env_file()

BASE = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
POLL_BASE = os.environ.get("AGNES_POLL_BASE", "https://apihub.agnes-ai.com")

IMAGE_MODEL = "agnes-image-2.1-flash"
VIDEO_MODEL = "agnes-video-v2.0"
TEXT_MODEL = "agnes-2.5-flash"

def die(msg, code=1):
    print(f"[agnes_gen] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)

def key():
    k = os.environ.get("AGNES_API_KEY")
    if not k:
        die("未设置 AGNES_API_KEY。请 export AGNES_API_KEY=你的Key（到 platform.agnes-ai.com 申请）", 2)
    return k

def http(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key()}")
    req.add_header("Content-Type", "application/json")
    last_err = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            code = e.code
            body_txt = e.read().decode("utf-8", "ignore")[:400]
            if code in (429, 408, 500, 502, 503, 504, 520, 522, 524):
                wait = min(60, 2 ** (attempt + 1))
                print(f"[agnes_gen] HTTP {code}，{wait}s 后重试 (第{attempt+1}次) {body_txt[:120]}", file=sys.stderr)
                time.sleep(wait)
                last_err = f"http_{code}: {body_txt}"
                continue
            die(f"HTTP {code}: {body_txt}")
        except Exception as e:
            wait = min(30, 2 ** (attempt + 1))
            print(f"[agnes_gen] {e.__class__.__name__}: {e}，{wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
            last_err = str(e)
    die(f"重试耗尽: {last_err}")

def image(prompt, size, out):
    resp = http("POST", f"{BASE}/images/generations",
                {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1}, timeout=600)
    d = (resp.get("data") or [{}])[0]
    url = d.get("url")
    b64 = d.get("b64_json")
    if url:
        urllib.request.urlretrieve(url, out)
    elif b64:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
    else:
        die(f"图片响应无 url/b64: {json.dumps(resp)[:300]}")
    print(f"[agnes_gen] image ok -> {out}")

def image_to_url(path):
    """本地图片 -> data URL（API 不接受本地路径时用 base64 data URI）。"""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"

def video(prompt, image_path, out, wait_poll=15):
    payload = {"model": VIDEO_MODEL, "prompt": prompt}
    if image_path:
        payload["image"] = image_to_url(image_path) if image_path.startswith((".", "/")) or re.match(r"^[A-Za-z]:[\\/]", image_path) else image_path
    resp = http("POST", f"{BASE}/videos", payload, timeout=300)
    print(f"[agnes_gen] video task created: {json.dumps(resp)[:300]}", file=sys.stderr)
    video_id = None
    if isinstance(resp, dict):
        for k in ("video_id", "id", "task_id"):
            if resp.get(k):
                video_id = resp[k]
                break
        # 有些网关直接返回 url
        for k in ("video_url", "url"):
            if isinstance(resp.get(k), str) and resp[k].startswith("http"):
                urllib.request.urlretrieve(resp[k], out)
                print(f"[agnes_gen] video ok (direct) -> {out}")
                return
    if not video_id:
        die(f"无法从响应中取得 video_id: {json.dumps(resp)[:300]}")
    # 轮询
    deadline = time.time() + 600  # 单镜头最多 10 分钟
    while time.time() < deadline:
        time.sleep(wait_poll)
        try:
            req = urllib.request.Request(f"{POLL_BASE}/agnesapi?video_id={video_id}")
            req.add_header("Authorization", f"Bearer {key()}")
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[agnes_gen] 轮询异常: {e}", file=sys.stderr)
            continue
        print(f"[agnes_gen] poll: {json.dumps(st)[:200]}", file=sys.stderr)
        url = None
        if isinstance(st, dict):
            status = str(st.get("status", "")).lower()
            for k in ("video_url", "url"):
                if isinstance(st.get(k), str) and st[k].startswith("http"):
                    url = st[k]
            data = st.get("data")
            if isinstance(data, dict):
                for k in ("video_url", "url"):
                    if isinstance(data.get(k), str) and data[k].startswith("http"):
                        url = data[k]
            if status in ("failed", "error") and not url:
                die(f"视频任务失败: {json.dumps(st)[:300]}")
            if url:
                urllib.request.urlretrieve(url, out)
                print(f"[agnes_gen] video ok -> {out}")
                return
    die("轮询超时（10 分钟），请手动用 video_id 查询")

def enhance(system_file, raw, i2v=False, role="", anchor="", palette=""):
    with open(system_file, "r", encoding="utf-8") as f:
        text = f.read()
    # 拆 system/user 模板
    m = re.search(r"## system\s*(.*?)\s*## user", text, re.S)
    sys_part = m.group(1).strip() if m else text
    sys_part = sys_part.replace("{{CHARACTER_ANCHOR}}", anchor or "none")
    sys_part = sys_part.replace("{{PALETTE}}", palette or "none")
    user_part = (f"Shot role in sequence: {role or 'single shot'}\n"
                 f"Is I2V: {'yes' if i2v else 'no'}\n"
                 f"Rough brief: {raw}\n\nRewrite it now. Output only the final prompt paragraph.")
    resp = http("POST", f"{BASE}/chat/completions",
                {"model": TEXT_MODEL, "temperature": 0.7,
                 "messages": [{"role": "system", "content": sys_part},
                              {"role": "user", "content": user_part}]})
    content = resp["choices"][0]["message"]["content"].strip()
    # 剥掉开头解释
    content = re.sub(r"^(Here (is|are) (the|a) (rewritten )?prompt:?\s*)", "", content, flags=re.I)
    content = content.strip().strip('`').strip()
    print(content)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("image")
    p.add_argument("--prompt", required=True)
    p.add_argument("--size", default="1024x1024")
    p.add_argument("--out", required=True)

    v = sub.add_parser("video")
    v.add_argument("--prompt", required=True)
    v.add_argument("--image", default="")
    v.add_argument("--out", required=True)
    v.add_argument("--wait", type=int, default=15, help="轮询间隔秒")

    e = sub.add_parser("enhance")
    e.add_argument("--system-file", required=True)
    e.add_argument("--raw", required=True)
    e.add_argument("--i2v", action="store_true")
    e.add_argument("--role", default="")
    e.add_argument("--anchor", default="")
    e.add_argument("--palette", default="")

    args = ap.parse_args()
    if args.cmd == "image":
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        image(args.prompt, args.size, args.out)
    elif args.cmd == "video":
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        video(args.prompt, args.image, args.out, args.wait)
    elif args.cmd == "enhance":
        enhance(args.system_file, args.raw, args.i2v, args.role, args.anchor, args.palette)

if __name__ == "__main__":
    main()

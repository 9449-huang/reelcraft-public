#!/usr/bin/env python3
"""audio_triage.py — 出片听诊链（方案B）：每镜音频"要不要补朗读"机器粗筛，拍板留人。

背景：不同视频模型的音频能力未知（带人声/BGM/哑片都有），不能假设、也不能每镜人工听。
本模块把"听诊"收敛成一条命令：

    python scripts/media_gen.py triage clips/                # 只报告（scan，不落盘）
    python scripts/media_gen.py triage clips/ --pool <模型名> --update-profile
    python scripts/media_gen.py triage clips/S01.mp4         # 单镜

决策链（机器只粗筛，永远给人拍板）：
    probe has_audio?
      ├─ 无 → yes（哑片，旁白/音乐都需后期配）
      └─ 有 → 抽 8-10s 中段 → 硅基 SenseVoiceSmall 转写（/audio/transcriptions，
             OpenAI 兼容；通道扫 MEDIA_TTS_<n>_* 取第一个可用的 base/key）
           ├─ 转录中文文本 ≥2 字 → no（自带中文人声）
           ├─ 转录英文 → yes（非中文人声，建议补中文旁白）
           ├─ 转录空 → listen（纯音乐/环境音/噪声？机器不硬判）
           └─ API/抽帧失败 → listen（交人听）
    转录通道扫 MEDIA_TTS_<n>_*（起始空号跳过，配置后连续 3 空号停，与 cmd_tts 同口径）——
    key 不在 TTS_1 时不再误报"未配置"。
    拍板结果若 --pool --update-profile → 写 ~/.workbuddy/.audio_profiles.json
    （同 watermark/caps 档案模式：同一模型实测一次回写，下次免测）。

设计（深模块）：决策/档案读写是纯函数（测试不碰网络/文件系统外的真实现）；
probe/抽音频/转录是薄 IO 壳，错误一律降级为 listen（不因听诊失败拦流程）。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from ffmpeg_probe import find_ffmpeg
from postprocess import probe


def _scan_tts_slot(env: dict | None = None) -> tuple:
    """扫第一个可用的 MEDIA_TTS_<n>_BASE/KEY（纯函数可单测）。
    与 envcheck/cmd_tts 同口径：真值判断，起始空号跳过，配置后连续 3 空号停。
    返回 (n, base, key)；无可用通道返回 (0, "", "")。"""
    env = os.environ if env is None else env
    streak, tried = 0, False
    for n in range(1, 200):
        has = bool(env.get(f"MEDIA_TTS_{n}_KEY") or env.get(f"MEDIA_TTS_{n}_BASE"))
        if has:
            base = env.get(f"MEDIA_TTS_{n}_BASE", "").rstrip("/")
            key = env.get(f"MEDIA_TTS_{n}_KEY", "")
            if base and key:
                return n, base, key
            streak, tried = 0, True
        else:
            streak += 1
            if tried and streak >= 3:
                break
    return 0, "", ""

PROFILE_FILE = Path.home() / ".workbuddy" / ".audio_profiles.json"
STT_MODEL = "FunAudioLLM/SenseVoiceSmall"
SAMPLE_SECONDS = 9.0        # 抽音频中段时长（SenseVoice 建议 8-10s）
MIN_ZH_CHARS = 2            # 转录中文 ≥2 字才算"有人声"（单字可能是背景噪音/广播误录）


def _ffmpeg() -> str:
    return find_ffmpeg()


# ─── 决策（纯函数，测试的 seam）─────────────────────────
def triage_decision(has_audio: bool, transcript: str = "") -> dict:
    """听诊决策：{"verdict": "yes|no|listen", "reason": str}。
    yes=建议补朗读 / no=自带人声不用补 / listen=判不了给人听。"""
    if not has_audio:
        return {"verdict": "yes",
                "reason": "无音轨（哑片）——旁白/音乐都需后期配（tts 档）"}
    t = (transcript or "").strip()
    zh = re.findall(r"[\u4e00-\u9fff]", t)
    en = re.findall(r"[A-Za-z]+", t)
    if len(zh) >= MIN_ZH_CHARS:
        return {"verdict": "no", "reason": f"自带中文人声（转录 {len(zh)} 字）——native 档，不用补"}
    if en:
        return {"verdict": "yes", "reason": f"有音轨但非中文人声（转录含英文）——建议补中文旁白"}
    return {"verdict": "listen",
            "reason": "有音轨但转录为空/太短——可能纯音乐/环境音/噪声，机器不硬判，请听 8-10s 样段"}


# ─── audio_profiles.json 读写（同 caps 档案模式）────────
def load_profile(pool: str) -> dict:
    if not PROFILE_FILE.exists():
        return {}
    try:
        d = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        return (d or {}).get(pool) or {}
    except Exception:
        return {}


def record_profile(pool: str, entry: dict) -> None:
    """写一条该模型音频档案（verdict/has_audio/probed_at）。"""
    d = {}
    if PROFILE_FILE.exists():
        try:
            d = json.loads(PROFILE_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            d = {}
    entry.setdefault("probed_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    entry.setdefault("probed_at_ts", time.time())
    d[pool] = entry
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PROFILE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


# ─── IO 薄壳（真实执行层，错误一律降级 listen 不拦流程）──
def _extract_sample(clip: Path, out_wav: Path) -> bool:
    """抽视频中段 9s 音频 → 16k mono wav。失败返回 False。"""
    info = probe(str(clip))
    dur = float(info.get("duration") or 0)
    if dur <= 0:
        return False
    start = max(0.0, dur / 2 - SAMPLE_SECONDS / 2)
    r = subprocess.run([_ffmpeg(), "-y", "-loglevel", "error",
                        "-ss", f"{start:.2f}", "-i", str(clip), "-t", f"{SAMPLE_SECONDS:.1f}",
                        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_wav)],
                       capture_output=True)
    return r.returncode == 0 and out_wav.exists()


def _transcribe(wav: Path, base: str, key: str, model: str = STT_MODEL,
                timeout: int = 120) -> str:
    """POST /audio/transcriptions（multipart，OpenAI 兼容变体）。失败返回空串（→listen）。
    实测坑（2026-09-08）：硅基网关要求 **file 字段在前、model 字段在后且带
    Content-Type: text/plain**——OpenAI SDK 的默认顺序（model 先）会被网关 400
    "Error when parsing request"。"""
    boundary = "----wb-triage" + str(int(time.time() * 1000))
    raw = wav.read_bytes()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{wav.name}\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
        raw,
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n"
        f"Content-Type: text/plain\r\n\r\n{model}\r\n".encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(f"{base.rstrip('/')}/audio/transcriptions",
                                 data=body, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read() or b"{}")
        return str(d.get("text") or "")
    except Exception as e:
        print(f"[triage] 转录失败（{e}）——降级 listen", file=sys.stderr)
        return ""


def triage_one(clip: Path, pool: str = "", update: bool = False,
              refresh: bool = False) -> dict:
    """对单个成片跑完整听诊，返回决策 dict。pool+update → 写档案。
    pool（不管 update）且未 --refresh → 先查档案，命中直接用实测结论（免测，不烧转录额度）。"""
    if pool and not refresh:
        cached = load_profile(pool)
        if cached.get("verdict"):
            return {"file": clip.name, "has_audio": cached.get("has_audio"),
                    "verdict": cached["verdict"],
                    "reason": f"档案命中（{cached.get('detail', '')}，实测于 "
                              f"{cached.get('probed_at', '?')}，--refresh 重测）"}
    info = probe(str(clip))
    has_audio = bool(info.get("audio"))
    res = {"file": clip.name, "has_audio": has_audio}
    if has_audio:
        with tempfile.TemporaryDirectory(prefix="triage_") as td:
            wav = Path(td) / "sample.wav"
            if not _extract_sample(clip, wav):
                res.update({"verdict": "listen", "reason": "音频样段抽取失败（文件损坏？），交人听"})
            else:
                slot_n, base, key = _scan_tts_slot()
                model = os.environ.get("MEDIA_STT_MODEL", "") or STT_MODEL
                text = _transcribe(wav, base, key, model) if (base and key) else ""
                if not (base and key):
                    print(f"[triage] 未配置可用的 MEDIA_TTS_<n> 转录通道（硅基 key，当前扫到第 {slot_n} 号）"
                          "——无法自动听诊，全部交人听", file=sys.stderr)
                res["transcript"] = text[:80]
                res.update(triage_decision(True, text))
    else:
        res.update(triage_decision(False))
    if pool and update:
        record_profile(pool, {"verdict": res["verdict"], "has_audio": res.get("has_audio"),
                              "detail": res.get("reason", "")})
    return res


def cmd(args) -> None:
    targets: list[Path] = []
    for t in args.clips:
        p = Path(t)
        if p.is_dir():
            from mg_core import PRODUCT_EXTS, natkey
            targets += sorted((f for f in p.iterdir()
                               if f.suffix in PRODUCT_EXTS and f.name.startswith("clip_")),
                              key=lambda f: natkey(f.name))
        else:
            targets.append(p)
    if not targets:
        print("[triage] 未找到成片（传文件列表或含 clip_* 的目录）", file=sys.stderr)
        sys.exit(2)
    print(f"[triage] 听诊 {len(targets)} 镜"
          + (f"（{args.pool}，将写入档案）" if args.pool and args.update_profile else ""))
    n = {"yes": 0, "no": 0, "listen": 0}
    for c in targets:
        r = triage_one(c, pool=args.pool, update=args.update_profile,
                       refresh=args.refresh)
        n[r["verdict"]] += 1
        badge = {"yes": "补", "no": "不用补", "listen": "人听"}[r["verdict"]]
        au = "有音轨" if r["has_audio"] else "哑片"
        print(f"  [{r['verdict']:>6}] {c.name:24s} {au:4s} → {badge}")
        print(f"          {r.get('reason', '')}" +
              (f"（转录: {r['transcript']}）" if r.get("transcript") else ""))
    print(f"[triage] 汇总：补 {n['yes']} / 不用补 {n['no']} / 人听 {n['listen']}")
    sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description="出片听诊链：每镜要不要补朗读（方案B）")
    ap.add_argument("clips", nargs="+", help="成片文件，或含 clip_*.mp4/webp 的目录")
    ap.add_argument("--pool", default="", help="模型池名（配合 --update-profile 写档案）")
    ap.add_argument("--update-profile", action="store_true",
                    help="把本镜结果写进 ~/.workbuddy/.audio_profiles.json")
    ap.add_argument("--refresh", action="store_true",
                    help="忽略档案实测结论强制重测（配合 --pool）")
    cmd(ap.parse_args())


if __name__ == "__main__":
    main()

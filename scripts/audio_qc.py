# -*- coding: utf-8 -*-
"""audio_qc — 音频侧 QC（静音/削波/响度）+ 语音判定（VAD）。

补 qcgate 的盲区：`qcgate` 只查**画面**（黑帧/过曝/静帧），音频侧一直没人管——
旁白整段丢失、音轨没接上、增益爆表削波，都是 rc=0 但成片坏掉的静默失败。

两条能力：
1. **音频 QC**（纯 ffmpeg，零依赖）：`silencedetect` + `volumedetect` + `astats`
   → 静音占比 / 采样削波 / 平均响度 / 长静音段，四类机器可判项。
2. **语音判定 VAD**：优先 **silero-vad（ONNX）**，缺依赖或推理失败**如实回退**
   `ffmpeg silencedetect`（不静默失败——`backend` 字段会说明用了哪条）。

用途：① 成片/片段入库前体检；② `triage` 前置——本地 VAD 判"根本没人声"时
**直接给出补旁白建议，省掉一次远端 ASR 调用**。

设计（深模块）：解析与判定全是**纯函数**（测试不碰 ffmpeg/模型）；ffmpeg 与
模型推理是薄 IO 壳。

    python scripts/audio_qc.py clips/S01.mp4
    python scripts/audio_qc.py clips/ --json
    python scripts/audio_qc.py clips/ --backend ffmpeg --strict
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from mg_core import _ffmpeg
from postprocess import probe

MODELS_DIR = Path.home() / ".workbuddy" / "models"
SILERO_MODEL = "silero_vad.onnx"
SAMPLE_RATE = 16000
SILERO_WIN = 512              # silero v5 在 16kHz 下的窗长（样本数）
DIE_ARG = 2                   # 参数/输入错误

# 判定阈值（集中一处，便于按素材调）
SILENCE_WARN_RATIO = 0.5      # 静音占比 ≥50% → WARN
SILENCE_FAIL_RATIO = 0.85     # ≥85% → FAIL（音轨没接上/录制失败）
CLIP_DB = -0.1                # 峰值 ≥ 此值 → 有削波风险
SEVERE_CLIPS = 50             # 采样触顶处数 ≥ 此值 → FAIL
MIN_MEAN_DB = -45.0           # 平均响度 ≤ 此值 → WARN（近乎无声）
MAX_MEAN_DB = -6.0            # ≥ 此值 → WARN（可能爆音）
LONG_SILENCE = 5.0            # 单个静音段 ≥ 此秒数 → WARN


# ─── 解析（纯函数：测试的 seam）──────────────────────────
def parse_silencedetect(text: str) -> list[dict]:
    """解析 ffmpeg silencedetect 输出为静音段列表。

    末尾只有 `silence_start` 没有 `silence_end`（静音延伸到片尾）→ `end`/`dur`
    给 None，**不能丢**——那一段通常是"音轨断了"的关键证据。
    """
    out: list[dict] = []
    cur: dict | None = None
    pat = (r"silence_(start|end):\s*(-?[\d.]+)"
           r"(?:\s*\|\s*silence_duration:\s*(-?[\d.]+))?")
    for m in re.finditer(pat, text):
        kind, val, dur = m.group(1), float(m.group(2)), m.group(3)
        if kind == "start":
            if cur is not None:          # 上一个 start 没等到 end
                out.append(cur)
            cur = {"start": val, "end": None, "dur": None}
        else:
            if cur is None:              # 没有配对的 start（异常输入）
                out.append({"start": None, "end": val,
                            "dur": float(dur) if dur else None})
                continue
            cur["end"] = val
            cur["dur"] = float(dur) if dur else round(val - float(cur["start"] or 0.0), 3)
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def parse_volumedetect(text: str) -> dict:
    """解析 volumedetect 的 mean/max 音量（dB）。缺失给 None（不编造 0）。"""
    def _pick(pat):
        m = re.search(pat, text)
        return float(m.group(1)) if m else None
    return {"mean_db": _pick(r"mean_volume:\s*(-?[\d.]+)\s*dB"),
            "max_db": _pick(r"max_volume:\s*(-?[\d.]+)\s*dB")}


def parse_astats(text: str) -> dict:
    """解析 astats 的 **Overall** 段（不是逐声道段）。

    陷阱：astats 先按声道输出一遍（`Channel: 1` / `Channel: 2`），最后才是
    `Overall`。同名键在声道段是**该声道**的值——取错会把"某一声道削波"误报成
    整轨削波（或反过来漏报）。所以必须先切到 Overall 之后再解析。
    """
    if "Overall" not in text:
        return {}
    tail = text.split("Overall", 1)[1]

    def _num(pat):
        m = re.search(pat, tail)
        return float(m.group(1)) if m else None
    clips = re.search(r"Number of clips:\s*(\d+)", tail)
    return {"peak_db": _num(r"Peak level dB:\s*(-?[\d.]+)"),
            "rms_db": _num(r"RMS level dB:\s*(-?[\d.]+)"),
            "flat_factor": _num(r"Flat factor:\s*(-?[\d.]+)"),
            "clips": int(clips.group(1)) if clips else 0,
            "noise_floor_db": _num(r"Noise floor dB:\s*(-?[\d.]+)")}


# ─── 判定（纯函数：测试的 seam）──────────────────────────
def _sil_dur(seg: dict, total: float) -> float:
    """静音段时长；`end` 为 None（延伸到片尾）时用 total 兜。"""
    if seg.get("dur") is not None:
        return float(seg["dur"])
    start = seg.get("start")
    end = seg.get("end")
    if end is None:
        return max(0.0, total - float(start or 0.0))
    return max(0.0, float(end) - float(start or 0.0))


def qc_verdict(dur, silences, vol, stats, *,
               silence_warn_ratio: float = SILENCE_WARN_RATIO,
               silence_fail_ratio: float = SILENCE_FAIL_RATIO,
               clip_db: float = CLIP_DB, severe_clips: int = SEVERE_CLIPS,
               min_mean_db: float = MIN_MEAN_DB, max_mean_db: float = MAX_MEAN_DB,
               long_silence: float = LONG_SILENCE) -> list[tuple[str, str]]:
    """音频侧 QC 判定 → [(级别, 说明)]，空列表 = 全过。

    探测失败（时长/响度全 None）时**不产出假判定**——"没测到"不等于"没问题"，
    交由调用方的 duration_verdict 类逻辑去提示人工复核。
    """
    v: list[tuple[str, str]] = []
    d = float(dur) if dur else 0.0
    vol = vol or {}
    stats = stats or {}
    mean_db = vol.get("mean_db")
    max_db = vol.get("max_db")
    clips = int(stats.get("clips") or 0)

    # ① 削波（采样触顶 = 硬失真，不可逆）
    if clips >= severe_clips:
        v.append(("FAIL", f"严重削波：{clips} 处采样触顶——音频已失真，需重录或降增益"))
    elif clips > 0:
        v.append(("WARN", f"检测到 {clips} 处采样削波（轻微失真）"))
    if max_db is not None and max_db >= clip_db:
        v.append(("WARN", f"峰值 {max_db:.1f}dB 已达满刻度（≥{clip_db}dB）——有削波风险"))

    # ② 静音占比 + 长静音段
    if d > 0 and silences:
        durs = [_sil_dur(s, d) for s in silences]
        total_sil = max(0.0, min(sum(durs), d))
        ratio = total_sil / d
        if ratio >= silence_fail_ratio:
            v.append(("FAIL", f"≈{ratio:.0%} 时长是静音——音轨可能没接上/旁白整段丢失"))
        elif ratio >= silence_warn_ratio:
            v.append(("WARN", f"≈{ratio:.0%} 时长是静音（请确认是否符合预期）"))
        longest = max(durs) if durs else 0.0
        if longest >= long_silence:
            v.append(("WARN", f"存在 {longest:.1f}s 的长静音段（≥{long_silence}s）"))

    # ③ 响度（太低=近乎无声；太高=BGM 压过旁白/爆音）
    if mean_db is not None:
        if mean_db <= min_mean_db:
            v.append(("WARN", f"平均响度 {mean_db:.1f}dB 过低（≤{min_mean_db}dB）——近乎无声"))
        elif mean_db >= max_mean_db:
            v.append(("WARN", f"平均响度 {mean_db:.1f}dB 过高（≥{max_mean_db}dB）——可能爆音或 BGM 压过旁白"))
    return v


def speech_from_silences(dur, silences) -> tuple[list[dict], float]:
    """无模型回退路径：用静音段**反推**语音段（补集）。

    返回 (语音段列表, 语音占比)。silero 不可用时靠它，精度低于神经 VAD
    （能量阈值分不清音乐/噪声），但比"什么都不判"强。
    """
    d = float(dur) if dur else 0.0
    if d <= 0:
        return [], 0.0
    spans = []
    for s in silences or []:
        st = s.get("start")
        if st is None:
            continue
        st = max(0.0, min(float(st), d))
        en = s.get("end")
        en = d if en is None else max(0.0, min(float(en), d))
        if en > st:
            spans.append((st, en))
    spans.sort()
    segs: list[dict] = []
    cursor = 0.0
    for st, en in spans:
        if st - cursor > 1e-6:
            segs.append({"start": round(cursor, 3), "end": round(st, 3),
                         "dur": round(st - cursor, 3)})
        cursor = max(cursor, en)
    if d - cursor > 1e-6:
        segs.append({"start": round(cursor, 3), "end": round(d, 3),
                     "dur": round(d - cursor, 3)})
    total = sum(x["dur"] for x in segs)
    return segs, (round(total / d, 4) if d > 0 else 0.0)


def probs_to_segments(probs, win: float, *, threshold: float = 0.5,
                      min_speech: float = 0.25) -> list[dict]:
    """silero 逐窗概率 → 语音段（纯函数：测试不需要模型）。

    连续超阈窗口合并成段；短于 `min_speech` 的段丢弃——避免把咳嗽/环境噪声
    当成人声（下游"要不要补旁白"的判断经不起这种误报）。
    """
    segs: list[dict] = []
    run_start = None
    for i, p in enumerate(list(probs) + [0.0]):   # 末尾哨兵：闭合最后一段
        on = float(p) >= threshold
        if on and run_start is None:
            run_start = i
        elif not on and run_start is not None:
            st, en = run_start * win, i * win
            if en - st >= min_speech - 1e-9:
                segs.append({"start": round(st, 3), "end": round(en, 3),
                             "dur": round(en - st, 3)})
            run_start = None
    return segs


def speech_summary(segments, dur, *, min_total: float = 0.3) -> dict:
    """语音总量/占比/有无。碎片（总量 <0.3s）不算"有语音"。"""
    d = float(dur) if dur else 0.0
    total = sum(float(s.get("dur") or 0.0) for s in (segments or []))
    return {"ratio": round(total / d, 4) if d > 0 else 0.0,
            "total": round(total, 3),
            "has_speech": total >= min_total}


def _onnx_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def vad_backend(models_dir=None) -> str:
    """选后端：`silero`（有 onnxruntime + 模型）否则 `ffmpeg`。

    缺任何一半都**回退**而不是崩——VAD 是增强项，不该成为硬依赖。
    """
    md = Path(models_dir) if models_dir else MODELS_DIR
    if (md / SILERO_MODEL).exists() and _onnx_available():
        return "silero"
    return "ffmpeg"


# ─── IO 壳 ─────────────────────────────────────────────
def probe_duration(path) -> float:
    try:
        return float(probe(str(path)).get("duration") or 0.0)
    except Exception:
        return 0.0


def _run_silencedetect(path, *, noise_db: float = -35.0, min_dur: float = 0.35) -> list[dict]:
    cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
           "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return parse_silencedetect((r.stderr or "") + (r.stdout or ""))


def _run_volumedetect(path) -> dict:
    cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
           "-af", "volumedetect", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return parse_volumedetect((r.stderr or "") + (r.stdout or ""))


def _run_astats(path) -> dict:
    cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
           "-af", "astats", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return parse_astats((r.stderr or "") + (r.stdout or ""))


def _read_pcm16k(path) -> bytes:
    """抽出 16kHz 单声道 s16le PCM（silero 的输入形态）。"""
    cmd = [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    return r.stdout or b""


def _silero_probs(pcm: bytes, model_path) -> list[float]:
    """silero v5 ONNX 逐窗推理 → 每窗语音概率。按输入名自适应（不同版本命名有差）。"""
    import numpy as np
    import onnxruntime as ort

    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    in_names = [x.name for x in sess.get_inputs()]
    n = len(pcm) // 2
    samples = np.frombuffer(pcm[:n * 2], dtype=np.int16).astype(np.float32) / 32768.0
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(SAMPLE_RATE, dtype=np.int64)

    probs: list[float] = []
    for i in range(0, max(0, len(samples) - SILERO_WIN + 1), SILERO_WIN):
        chunk = samples[i:i + SILERO_WIN].reshape(1, -1)
        feed = {}
        for name in in_names:
            low = name.lower()
            if "state" in low:
                feed[name] = state
            elif low == "sr" or "sample_rate" in low:
                feed[name] = sr
            else:
                feed[name] = chunk
        outs = sess.run(None, feed)
        probs.append(float(np.asarray(outs[0]).reshape(-1)[0]))
        for j, name in enumerate([x.name for x in sess.get_outputs()]):
            if "state" in name.lower():
                state = outs[j]
    return probs


def detect_speech(path, *, models_dir=None, backend: str = "auto",
                  noise_db: float = -35.0, min_silence: float = 0.35,
                  threshold: float = 0.5, min_speech: float = 0.25) -> dict:
    """语音判定。返回 {has_speech, ratio, total, segments, backend, duration}。

    `backend="silero"` 显式要求而运行时不具备 → 抛错（不静默降级，避免"以为用了
    神经 VAD 其实在用能量阈值"）；`auto` 则自动选并如实标注。
    """
    p = Path(path)
    dur = probe_duration(p)
    be = vad_backend(models_dir)
    if backend == "ffmpeg":
        be = "ffmpeg"
    elif backend == "silero" and be != "silero":
        md = Path(models_dir) if models_dir else MODELS_DIR
        raise RuntimeError(
            f"--backend silero 但运行时不具备：需 onnxruntime 与模型 {md / SILERO_MODEL}"
            f"（安装：pip install onnxruntime numpy；模型放 {md}）")

    segs: list[dict]
    if be == "silero":
        md = Path(models_dir) if models_dir else MODELS_DIR
        try:
            probs = _silero_probs(_read_pcm16k(p), md / SILERO_MODEL)
            segs = probs_to_segments(probs, SILERO_WIN / SAMPLE_RATE,
                                     threshold=threshold, min_speech=min_speech)
        except Exception as e:      # 推理失败不能拦流程，如实回退并说明
            print(f"[audio_qc] silero 推理失败（{type(e).__name__}: {e}）"
                  f"→ 回退 ffmpeg VAD", file=sys.stderr)
            be = "ffmpeg"
            segs, _ = speech_from_silences(
                dur, _run_silencedetect(p, noise_db=noise_db, min_dur=min_silence))
    else:
        segs, _ = speech_from_silences(
            dur, _run_silencedetect(p, noise_db=noise_db, min_dur=min_silence))

    summ = speech_summary(segs, dur)
    return {"has_speech": summ["has_speech"], "ratio": summ["ratio"],
            "total": summ["total"], "segments": segs, "backend": be,
            "duration": round(dur, 3)}


def analyze(path, *, models_dir=None, want_vad: bool = True, **kw) -> dict:
    """单个文件的音频体检报告（QC + 可选 VAD）。"""
    p = Path(path)
    dur = probe_duration(p)
    sil = _run_silencedetect(p)
    vol = _run_volumedetect(p)
    stats = _run_astats(p)
    verdict = qc_verdict(dur, sil, vol, stats)
    rep = {"path": str(p), "duration": round(dur, 3),
           "silences": sil, "volume": vol, "stats": stats,
           "verdict": [{"level": lv, "msg": m} for lv, m in verdict]}
    if want_vad:
        try:
            sp = detect_speech(p, models_dir=models_dir)
            rep["speech"] = {k: sp[k] for k in ("has_speech", "ratio", "total", "backend")}
        except Exception as e:
            rep["speech"] = {"has_speech": None, "error": f"{type(e).__name__}: {e}"}
    return rep


# ─── CLI ───────────────────────────────────────────────
def _badge(verdict) -> tuple[str, int]:
    levels = {v["level"] for v in verdict}
    if "FAIL" in levels:
        return "FAIL", 2
    if "WARN" in levels:
        return "WARN", 0
    return "PASS", 0


def _targets(p) -> list[Path]:
    path = Path(p)
    if path.is_dir():
        from mg_core import PRODUCT_EXTS
        return sorted([x for x in path.iterdir()
                       if x.suffix.lower() in PRODUCT_EXTS and x.stat().st_size > 0])
    return [path]


def cmd(args) -> int:
    """执行体检（接受 media_gen 转发的 Namespace），返回退出码。

    与 `audio_triage.cmd` 同构，便于 `media_gen.py audio-qc` 直接转发。
    """
    if not Path(args.path).exists():
        print(f"[audio_qc] 路径不存在: {args.path}", file=sys.stderr)
        return DIE_ARG
    files = _targets(args.path)
    if not files:
        print(f"[audio_qc] 目录下没有可用产物: {args.path}", file=sys.stderr)
        return DIE_ARG

    want_vad = not getattr(args, "no_vad", False)
    as_json = getattr(args, "json", False)
    strict = getattr(args, "strict", False)
    reports, rc_any = [], 0
    for f in files:
        rep = analyze(f, want_vad=want_vad)
        badge, rc = _badge(rep["verdict"])
        if strict and badge == "WARN":
            rc = 2
        rc_any = max(rc_any, rc)
        rep["badge"] = badge
        reports.append(rep)
        if not as_json:
            print(f"[audio_qc] {f.name}  {badge}")
            for v in rep["verdict"]:
                print(f"  [{v['level']}] {v['msg']}")
            if not rep["verdict"]:
                print("  机器可判项全过（静音占比/削波/响度）")
            sp = rep.get("speech") or {}
            if sp.get("has_speech") is not None:
                print(f"  语音：{'有' if sp['has_speech'] else '未检出'}"
                      f"（占比 {sp['ratio']:.0%}，后端 {sp.get('backend')}）")
            elif sp.get("error"):
                print(f"  语音：判定失败（{sp['error'][:80]}）")

    if as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    return rc_any


def main() -> None:
    ap = argparse.ArgumentParser(
        description="音频侧 QC（静音/削波/响度）+ 语音判定（silero ‖ ffmpeg 回退）")
    ap.add_argument("path", help="音频/视频文件，或目录（批量）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败（exit 2）")
    ap.add_argument("--backend", choices=["auto", "silero", "ffmpeg"], default="auto",
                    help="VAD 后端（默认 auto：有 onnxruntime+模型用 silero，否则 ffmpeg）")
    ap.add_argument("--no-vad", action="store_true", help="跳过语音判定（只做 QC）")
    ap.add_argument("--noise", type=float, default=-35.0,
                    help="silencedetect 噪声门限 dB（默认 -35）")
    ap.add_argument("--min-silence", type=float, default=0.35,
                    help="静音最小时长秒（默认 0.35）")
    args = ap.parse_args()
    sys.exit(cmd(args))


if __name__ == "__main__":
    main()

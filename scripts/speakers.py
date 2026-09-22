#!/usr/bin/env python3
"""speakers.py — 说话人分离：一段音频里**谁在什么时候说话**（批十二，零 torch）。

sherpa-onnx 双模型：pyannote segmentation（切分语音段）+ 3D-Speaker campplus（声纹嵌入）
→ FastClustering 聚类 → 每段的说话人标签。纯本地 CPU。

用法：
    python scripts/speakers.py <音频/视频> [--num-speakers N] [--json]
    python scripts/media_gen.py speakers <音频/视频> [--num-speakers N]

输出：按时间排序的段列表 [{start, end, speaker}]（秒；speaker 按首次出现重编号为 0..n-1）。
退出码：0 成功 / 1 分离失败 / 2 依赖或参数问题（缺模型/缺 sherpa-onnx → 打印安装指引）。

模型（~/.workbuddy/models/sherpa-diarization/）：
    segmentation.onnx ← hf-mirror.com/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0
    embedding.onnx    ← ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/
                         speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mg_core
from mg_core import _ffmpeg, die
from word_axis import decode_pcm

MODELS_DIR = Path.home() / ".workbuddy" / "models" / "sherpa-diarization"
SEG_MODEL = "segmentation.onnx"
EMB_MODEL = "embedding.onnx"
DIE_ARG = mg_core.EXIT_USAGE
MIN_MODEL_BYTES = 1_000_000


def speakers_backend(models_dir=None) -> str:
    """分离后端探测："sherpa"（双模型齐）或 ""。重依赖在函数内 import（AST 守卫）。"""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return ""
    d = Path(models_dir) if models_dir else MODELS_DIR
    ok = all((d / n).is_file() and (d / n).stat().st_size > MIN_MODEL_BYTES
             for n in (SEG_MODEL, EMB_MODEL))
    return "sherpa" if ok else ""


def install_hint(models_dir=None) -> str:
    d = Path(models_dir) if models_dir else MODELS_DIR
    return (
        "[speakers] 需要说话人分离依赖与模型：\n"
        "  1) pip install sherpa-onnx        # ~2MB 轮子，onnxruntime 系，无 torch\n"
        f"  2) {d / SEG_MODEL}      # pyannote 切分，6MB：\n"
        "     https://hf-mirror.com/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/main/model.onnx\n"
        f"  3) {d / EMB_MODEL}  # 3D-Speaker campplus 中文声纹，28MB：\n"
        "     https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx\n"
        "     （github 直连不通时前缀加 https://ghproxy.net/）"
    )


def renumber_speakers(segs: list) -> list:
    """说话人重编号（纯函数）：输出按时间排序，speaker 按首次出现归零为 0..n-1。"""
    rows = sorted(({"start": round(float(s.get("start") or 0.0), 3),
                    "end": round(float(s.get("end") or 0.0), 3),
                    "speaker": s.get("speaker")} for s in segs),
                  key=lambda s: (s["start"], s["end"]))
    mapping: dict = {}
    out = []
    for s in rows:
        raw = s["speaker"]
        if raw not in mapping:
            mapping[raw] = len(mapping)
        out.append({"start": s["start"], "end": s["end"], "speaker": mapping[raw]})
    return out


def diarize(path, *, models_dir=None, num_speakers: int = 0, backend: str = "") -> dict:
    """音频 → {backend, num_speakers, duration, segments:[{start,end,speaker}]}。

    `backend="sherpa"` 显式要求而依赖/模型不齐 → **抛 RuntimeError(指引)**（不静默降级）；
    `backend="auto"`（默认）不具备 → 返回空 backend + error 指引（不抛，同 word_axis 口径）。
    `num_speakers=0` = 自动聚类（threshold 0.5）；>0 = 固定簇数。
    """
    import sherpa_onnx
    d = Path(models_dir) if models_dir else MODELS_DIR
    be = speakers_backend(d)
    if not be:
        if backend == "sherpa":
            raise RuntimeError(install_hint(d))
        return {"backend": "", "num_speakers": 0, "duration": 0.0, "segments": [],
                "error": install_hint(d)}

    pcm = decode_pcm(path)
    dur = float(pcm.size / 16000.0)
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                str(d / SEG_MODEL))),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(str(d / EMB_MODEL)),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=int(num_speakers) if int(num_speakers) > 0 else -1,
            threshold=0.5),
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    raw = sd.process(samples=[float(x) for x in pcm]).sort_by_start_time()
    segs = renumber_speakers([{"start": s.start, "end": s.end, "speaker": s.speaker}
                              for s in raw])
    n_spk = len({s["speaker"] for s in segs})
    return {"backend": "sherpa", "num_speakers": n_spk, "duration": round(dur, 3),
            "segments": segs}


def cmd(args) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"[speakers] 路径不存在: {path}", file=sys.stderr)
        return DIE_ARG
    try:
        r = diarize(str(path), num_speakers=int(args.num_speakers),
                    backend=getattr(args, "backend", "") or "auto")
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return DIE_ARG
    except Exception as e:                      # 模型推理失败：如实报错，不假装成功
        print(f"[speakers] 分离失败: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 1
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        return DIE_ARG

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f"[speakers] {path.name}  {r['duration']:.1f}s  后端={r['backend']}  "
          f"说话人={r['num_speakers']}  段数={len(r['segments'])}")
    for s in r["segments"]:
        print(f"  {s['start']:7.2f}s - {s['end']:7.2f}s  spk{s['speaker']}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="speakers.py",
        description="说话人分离（谁在什么时候说话；sherpa 双模型，纯本地 CPU）")
    ap.add_argument("path", help="音频/视频文件")
    ap.add_argument("--num-speakers", type=int, default=0,
                    help="说话人数量（0=自动聚类，默认 0；已知人数时指定更稳）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args()
    sys.exit(cmd(args))


if __name__ == "__main__":
    main()

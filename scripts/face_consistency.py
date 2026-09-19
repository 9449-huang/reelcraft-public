# -*- coding: utf-8 -*-
"""face_consistency — 跨镜**人脸一致性**度量（把 qcseq 的"颜色像不像"升级到"是不是同一个人"）。

为什么需要它：`qcseq` 用 HSV 直方图判跨镜一致性，**只能发现颜色跳变，发现不了换脸**——
而角色一致性（尤其漫剧/系列内容）最痛的恰恰是"同一角色到第 3 镜换了张脸"。

做法（全部 CPU，无 torch）：
    YuNet 人脸检测 → SFace 5 点对齐 → 128 维人脸嵌入 → 两两余弦
判据用 SFace 官方阈值 **0.363**（在 fp32 模型上标定；本仓 2026-09-15 用真实样本图复核过：
同一个人 ≈0.59，不同人 ≈0.04~0.33，阈值正好卡在中间）。

**过滤是前提**：画面里常有背景人群/误检小脸（实测一张 1024×512 的图能检出 7 张，
其中 6 张是 ~30px 的背景脸）。不做过滤直接比对，结果全是噪声——所以按
`min_score` + **短边** `min_side` 过滤，再取**面积最大**的当主体。

**只粗筛，拍板留人**：退出码与 `qcseq` 同口味——0=全过 / 1=有 WARN（报告性质，不拦流程）
/ 2=输入错误。

可选依赖（缺任一 → 打印安装指引并退出，不崩）：`opencv-python-headless` +
`~/.workbuddy/models/` 下的 `face_detection_yunet_2023mar.onnx` 与
`face_recognition_sface_2021dec.onnx`。

    python scripts/face_consistency.py clips/
    python scripts/face_consistency.py clips/ --ref shots/char_hero.png --json
    python scripts/face_consistency.py clips/ --threshold 0.363 --strict
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mg_core import PRODUCT_EXTS, _ffmpeg
from postprocess import probe

_MODULE_DIR = Path(__file__).resolve().parent
MODELS_DIR = Path.home() / ".workbuddy" / "models"
YUNET_MODEL = "face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "face_recognition_sface_2021dec.onnx"
SFACE_THRESHOLD = 0.363      # SFace 官方 LFW 标定阈值（fp32 模型）
MIN_SCORE = 0.6              # YuNet 置信度下限
MIN_SIDE = 48                # 人脸框**短边**下限（像素）——背景小脸的主要过滤手段
DIE_ARG = 2
# 图片后缀：这些**直接交给 cv2**，不过 ffmpeg（实测 ffmpeg 对图片配 -ss 会 rc=0 却不产出文件）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


# ─── 纯函数（测试的 seam）────────────────────────────────
def cosine(a, b) -> float:
    """余弦相似度（纯 Python，不依赖 numpy；零向量/长度不齐一律给 0.0 而不是 nan）。

    给 0.0 而不是抛错或 nan：下游是"低于阈值就报警"的比较，nan 会让比较**静默失败**
    （`nan < 0.363` 恒为 False → 漏报）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def filter_faces(faces, *, min_score: float = MIN_SCORE,
                 min_side: int = MIN_SIDE) -> list:
    """只留"能当主体"的脸：置信度够 + **短边**够。

    判据用短边而非面积：200×30 的宽脸条面积达 6000，但显然不是一张能认的脸。
    """
    out = []
    for f in faces or []:
        box = f.get("box") or []
        if len(box) < 4:
            continue
        if float(f.get("score") or 0.0) < min_score:
            continue
        if min(int(box[2]), int(box[3])) < min_side:
            continue
        out.append(f)
    return out


def pick_primary(faces):
    """主体 = 画面里面积最大那张脸（镜头通常把主角放在最近处）。"""
    if not faces:
        return None
    return max(faces, key=lambda f: int(f.get("area") or 0))


def pairwise_similarity(embs: dict) -> list:
    """两两余弦（只出 i<j 的组合，键名排序 → 输出顺序稳定，报告可 diff）。"""
    keys = sorted(embs)
    out = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            out.append({"a": keys[i], "b": keys[j],
                        "sim": round(cosine(embs[keys[i]], embs[keys[j]]), 4)})
    return out


def mean_similarity(embs: dict) -> dict:
    """每个条目与**其余条目**的平均相似度。只有一条时给 None（无从谈起，别编 1.0）。"""
    keys = sorted(embs)
    if len(keys) < 2:
        return {k: None for k in keys}
    out = {}
    for k in keys:
        others = [cosine(embs[k], embs[o]) for o in keys if o != k]
        out[k] = round(sum(others) / len(others), 4)
    return out


def outliers(means: dict, threshold: float) -> list:
    """低于阈值的条目（≈"这一镜的脸跟别人对不上"）。None 跳过。"""
    return [k for k in sorted(means)
            if means[k] is not None and means[k] < threshold]


def consistency_verdict(pairs, means, coverage, *,
                        threshold: float = SFACE_THRESHOLD,
                        extract_failed=()) -> list:
    """判定 → [(级别, 说明)]；空列表 = 全过。

    `coverage`: {条目: 该条目的可用人脸数}。不足两处可比对时**明说无法比对**，
    而不是静默 PASS——"没测到"不等于"一致"。
    `extract_failed`: 抽帧失败的条目。必须与"无人脸"分开说：抽帧失败是**测不了**，
    无人脸可能是**合理的背身镜头**——混为一谈会把工具故障说成内容没问题。
    """
    v: list[tuple[str, str]] = []
    failed = [str(x) for x in (extract_failed or [])]
    if failed:
        v.append(("WARN", f"抽帧失败（rc=0 但产物缺失/为空）：{'、'.join(failed)}"
                          f"——机器判不了，请人工确认画面"))
    usable = [k for k in sorted(coverage) if (coverage.get(k) or 0) > 0]
    if len(usable) < 2:
        v.append(("WARN", f"只有 {len(usable)} 镜检出可用人脸，无法做跨镜比对"
                          f"（共 {len(coverage)} 镜）——人脸太小/侧脸/背身镜头都会如此"))
    else:
        bad = outliers(means, threshold)
        if bad:
            detail = "、".join(f"{k}(均值 {means[k]:.3f})" for k in bad)
            v.append(("WARN", f"疑似换脸：{detail} 低于阈值 {threshold}"
                              f"——与其余镜对不上，请人眼确认是否同一角色"))
        multi = [k for k in sorted(coverage) if (coverage.get(k) or 0) > 1]
        if multi:
            v.append(("WARN", f"{'、'.join(multi)} 检出多张可用人脸——主体取的是最大那张，"
                              f"若主角不是最靠前的会误判，请复核或拆镜"))
    return v


def badge(verdict) -> tuple:
    """退出码契约（与 qcseq 同口味）：0=全过 / 1=有 WARN（报告性质）/ 2=FAIL。"""
    levels = {lv for lv, _ in (verdict or [])}
    if "FAIL" in levels:
        return "FAIL", 2
    if "WARN" in levels:
        return "WARN", 1
    return "PASS", 0


def _import_cv2():
    """导入 cv2 —— **导入期间把 scripts/ 从 sys.path 摘掉**。

    ⚠️ 本仓有 `scripts/copy.py`（文案旁线），而 cv2 的 bootstrap 内部会
    `import copy`。真机 `python scripts/face_consistency.py` 时 sys.path[0] 就是
    scripts/，标准库的 `copy` 会被 `copy.py` 顶掉 → cv2 直接崩：
    `AttributeError: module 'copy' has no attribute 'copy'`。

    测试进程里 unittest 早已导入标准库 copy，所以**看不出来**——只有真机 CLI 会中招
    （2026-09-15 实测：测试全绿，CLI 却静默降级成"未安装"）。摘掉 scripts/ 后导入，
    stdlib copy 就会正常进 sys.modules，此后全进程都不再受影响。
    """
    saved = list(sys.path)
    me = str(_MODULE_DIR)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != me]
    try:
        import cv2
        return cv2
    finally:
        sys.path[:] = saved


def _cv2_available() -> bool:
    try:
        _import_cv2()
        return True
    except Exception:
        return False


def face_backend(models_dir=None) -> str:
    """后端可用性：`sface`（cv2 + 两个模型齐）否则空串。

    缺任一半都返回空串——调用方据此打印安装指引，而不是崩。
    """
    md = Path(models_dir) if models_dir else MODELS_DIR
    if (md / YUNET_MODEL).exists() and (md / SFACE_MODEL).exists() and _cv2_available():
        return "sface"
    return ""


def install_hint(models_dir) -> str:
    """缺依赖时的安装指引（说清"装什么、放哪、从哪来"）。"""
    return (
        "人脸一致性需要 opencv-python 与两个 ONNX 模型：\n"
        f"  1) pip install opencv-python-headless\n"
        f"  2) 模型放 {models_dir}：\n"
        f"     {YUNET_MODEL}（约 227KB）\n"
        f"     {SFACE_MODEL}（约 38MB）\n"
        f"     直链（opencv_zoo 的模型是 Git LFS，只能用 media 端点）：\n"
        f"     https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        f"models/face_detection_yunet/{YUNET_MODEL}\n"
        f"     https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        f"models/face_recognition_sface/{SFACE_MODEL}")


# ─── IO 壳 ─────────────────────────────────────────────
def _frame_for(p, out_png):
    """取一帧用于人脸检测 → (可读路径 | None, 来源)。

    - **图片**（jpg/png/webp…）：直接用原文件。实测 `ffmpeg -ss 0.00 -i x.jpg
      -frames:v 1 out.png` 会 **rc=0 但根本不产出文件**（seek 越过唯一那一帧），
      所以图片一律不过 ffmpeg。
    - **视频**：抽**中段**帧（首帧常是转场/黑场，脸还没出来），且必须校验
      产物存在且非空——只看 rc 会把"抽帧失败"当成"没人脸"（本仓既有教训：rc=0 ≠ 对）。
    """
    pp = Path(p)
    if pp.suffix.lower() in IMAGE_EXTS:
        return pp, "image"
    try:
        dur = float(probe(str(pp)).get("duration") or 0.0)
    except Exception:
        dur = 0.0
    at = dur / 2 if dur > 0 else 0.0
    r = subprocess.run([_ffmpeg(), "-y", "-loglevel", "error",
                        "-ss", f"{max(0.0, at):.2f}", "-i", str(pp),
                        "-frames:v", "1", str(out_png)], capture_output=True)
    op = Path(out_png)
    if r.returncode != 0 or not op.exists() or op.stat().st_size <= 0:
        return None, "extract_failed"
    return op, "video"


def analyze(paths, *, models_dir=None, ref=None, min_side: int = MIN_SIDE,
            min_score: float = MIN_SCORE, threshold: float = SFACE_THRESHOLD) -> dict:
    """主流程：取帧 → 检测/嵌入 → 过滤取主体 → 两两比对 → 判定。"""
    md = Path(models_dir) if models_dir else MODELS_DIR
    be = face_backend(md)
    report = {"backend": be, "threshold": threshold, "min_side": min_side,
              "shots": [], "pairs": [], "means": {}, "outliers": [], "verdict": []}
    if not be:
        report["verdict"] = [("WARN", install_hint(md))]
        return report

    targets = ([{"path": str(ref), "role": "ref"}] if ref else []) + \
              [{"path": str(p), "role": "shot"} for p in paths]
    with tempfile.TemporaryDirectory(prefix="facechk_") as td:
        for i, t in enumerate(targets):
            png = Path(td) / f"f{i:02d}.png"
            # 身份键**必须带序号**：用 path 当键的话，同一文件传两次（或 --ref 与某镜
            # 同文件）会在 embs dict 里静默塌缩成一条 → 报告成"只有 1 镜"（2026-09-15 实测）。
            key = f"#{i + 1} {Path(t['path']).name}"
            shot = {"key": key, "path": t["path"], "role": t["role"], "n_faces": 0,
                    "n_detected": 0, "frame_ok": False, "frame_source": "",
                    "primary": None}
            src, why = _frame_for(t["path"], png)
            shot["frame_source"] = why
            shot["frame_ok"] = src is not None
            if src is not None:
                faces = _detect_and_embed(src, md, min_score=min_score)
                shot["n_detected"] = len(faces)
                kept = filter_faces(faces, min_score=min_score, min_side=min_side)
                shot["n_faces"] = len(kept)
                prim = pick_primary(kept)
                if prim is not None:
                    shot["primary"] = {"box": prim["box"], "score": round(prim["score"], 4),
                                       "area": prim["area"], "emb": prim["emb"]}
                    if len(faces) > 1:      # "同一个人是否另有其脸"（人群里有同人时很有用）
                        others = [cosine(prim["emb"], f["emb"])
                                  for f in faces if f is not prim]
                        shot["best_sim_all"] = round(max(others), 4) if others else None
            report["shots"].append(shot)

    embs = {s["key"]: s["primary"]["emb"] for s in report["shots"] if s["primary"]}
    coverage = {s["key"]: s["n_faces"] for s in report["shots"]}
    report["extract_failed"] = [s["key"] for s in report["shots"] if not s["frame_ok"]]
    report["pairs"] = pairwise_similarity(embs)
    report["means"] = mean_similarity(embs)
    report["outliers"] = outliers(report["means"], threshold)
    report["verdict"] = consistency_verdict(report["pairs"], report["means"], coverage,
                                            threshold=threshold,
                                            extract_failed=report["extract_failed"])
    return report


def _detect_and_embed(img_path, models_dir, *, min_score: float = MIN_SCORE) -> list:
    """一张图 → [{box, score, area, emb(128)}]。检测/对齐/嵌入都用 cv2 官方实现
    （自己不手搓 YuNet 解码与 5 点对齐——那是"微妙错但 rc=0"的高发区）。"""
    cv2 = _import_cv2()
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass
    md = Path(models_dir)
    img = cv2.imread(str(img_path))
    if img is None:
        return []
    det = cv2.FaceDetectorYN_create(str(md / YUNET_MODEL), "", (320, 320),
                                    score_threshold=float(min_score),
                                    nms_threshold=0.3, top_k=50)
    rec = cv2.FaceRecognizerSF_create(str(md / SFACE_MODEL), "")
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    if faces is None:
        return []
    out = []
    for f in faces:
        x, y, bw, bh = (float(v) for v in f[:4])
        emb = [float(v) for v in rec.feature(rec.alignCrop(img, f)).flatten()]
        out.append({"box": [int(x), int(y), int(bw), int(bh)],
                    "score": float(f[-1]), "area": int(bw * bh), "emb": emb})
    return out


def _targets(paths) -> list:
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out += sorted([x for x in path.iterdir()
                           if x.suffix.lower() in PRODUCT_EXTS and x.stat().st_size > 0])
        else:
            out.append(path)
    return out


# ─── CLI ───────────────────────────────────────────────
def cmd(args) -> int:
    """执行（接受 media_gen 转发的 Namespace），返回退出码。"""
    ref = getattr(args, "ref", "") or ""
    paths = _targets(args.paths)
    if not paths and not ref:
        print(f"[faces] 没有可用输入: {args.paths}", file=sys.stderr)
        return DIE_ARG
    if ref and not Path(ref).exists():
        print(f"[faces] 参考图不存在: {ref}", file=sys.stderr)
        return DIE_ARG

    r = analyze(paths, models_dir=getattr(args, "models_dir", None) or None, ref=ref or None,
                min_side=int(getattr(args, "min_side", MIN_SIDE) or MIN_SIDE),
                min_score=float(getattr(args, "min_score", MIN_SCORE) or MIN_SCORE),
                threshold=float(getattr(args, "threshold", SFACE_THRESHOLD)))
    bd, rc = badge(r["verdict"])

    if getattr(args, "json", False):
        slim = dict(r)
        slim["shots"] = [{k: v for k, v in s.items() if k != "primary"} |
                         ({"primary": {kk: vv for kk, vv in s["primary"].items() if kk != "emb"}}
                          if s["primary"] else {})
                         for s in r["shots"]]
        slim["verdict"] = [{"level": lv, "msg": m} for lv, m in r["verdict"]]
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        return rc

    print(f"[faces] {bd}  后端={r['backend']}  阈值={r['threshold']}  短边下限={r['min_side']}")
    for s in r["shots"]:
        tag = "参考" if s["role"] == "ref" else "镜 "
        if s["frame_ok"]:
            if s["primary"]:
                b = s["primary"]["box"]
                extra = (f"  场内最像 {s['best_sim_all']:.3f}"
                         if s.get("best_sim_all") is not None else "")
                print(f"  {tag} {s['key']}  检出 {s['n_detected']} 张 / 可用 {s['n_faces']} 张"
                      f"  主体框 {b}  conf {s['primary']['score']:.3f}{extra}")
            else:
                print(f"  {tag} {s['key']}  检出 {s['n_detected']} 张 / 可用 0 张"
                      f"  ——无可用人脸（侧脸/背身/太小）")
        else:
            print(f"  {tag} {s['key']}  ⚠ 抽帧失败（rc=0 但产物缺失/为空）")
    if r["pairs"]:
        print("  两两余弦：")
        for p in r["pairs"]:
            mark = "  ← 低于阈值" if p["sim"] < r["threshold"] else ""
            print(f"    {p['a']} ↔ {p['b']}  {p['sim']:+.4f}{mark}")
    for lv, msg in r["verdict"]:
        print(f"  [{lv}] {msg}")
    if not r["verdict"]:
        print("  跨镜人脸一致（机器粗筛通过）——美学/表演仍需人眼")
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(
        description="跨镜人脸一致性（YuNet 检测 + SFace 嵌入 + 余弦比对）")
    ap.add_argument("paths", nargs="+", help="成片文件或含产物的目录（批量）")
    ap.add_argument("--ref", default="", help="角色参考图（角色圣经里的设定图）——有则一并比对")
    ap.add_argument("--threshold", type=float, default=SFACE_THRESHOLD,
                    help=f"余弦阈值（SFace 官方 {SFACE_THRESHOLD}）")
    ap.add_argument("--min-side", type=int, default=MIN_SIDE,
                    help=f"人脸框短边下限像素（默认 {MIN_SIDE}，用于滤掉背景小脸）")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE,
                    help=f"YuNet 置信度下限（默认 {MIN_SCORE}）")
    ap.add_argument("--models-dir", default="", help="模型目录（默认 ~/.workbuddy/models）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    args = ap.parse_args()
    sys.exit(cmd(args))


if __name__ == "__main__":
    main()

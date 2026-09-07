#!/usr/bin/env python3
"""prompt_lint.py — shot JSON 提示词静态 lint（word 级约束机器化）。

此前"无 slop 词 / 词数合适 / i2v 不重述主体"全靠 agent 自觉，是系统唯一系统性缺口。
本脚本把 schema（references/prompt-atomic-schema.md）与 anti-slop 词表
（references/anti-slop-lexicon.md）变成可执行约束，batch/pipeline 出镜前强制过一遍。

检查项（对 shot JSON 的 t2i_prompt / i2v_prompt）：
  ① 禁词扫描：anti-slop 词表（绝对禁用 → FAIL；低效 mood 词 → WARN）
  ② 词数区间：t2i 120-220 词 / i2v 40-80 词（散文体；太短没细节、太长模型丢要素）
  ③ i2v 误写 subject：schema 规定 i2v 不写 subject/scene/style/color/material
    （首帧已锁定）——若 shot 含这些字段值，其显著 token 出现在 i2v_prompt 中 → WARN

用法：
  python prompt_lint.py shots/                      # 扫目录全部 shot JSON，exit 0=全过 / 1=有 FAIL
  python prompt_lint.py shots/shot_01.json --kind videos   # 单镜单角色（batch worker 用）
  python prompt_lint.py shots/ --json               # 机器可读输出
  python prompt_lint.py shots/ --no-slop-file       # 只用内置词表（不读 lexicon md）

接入（batch 默认强制）：batch --phase images/videos 出镜前先过 lint，
FAIL 的镜直接记 FAIL 不重试——prompt 问题重试 N 次也是白烧额度。
"""
from __future__ import annotations
import argparse
import json
import math
import re
import sys
from pathlib import Path

# ─── 词表（内置兜底；有 lexicon md 时按文件解析覆盖）────────
HARD_SLOP = [
    "beautiful", "gorgeous", "stunning", "breathtaking", "epic", "grand",
    "emotional", "touching", "artistic", "creative", "masterpiece",
    "best quality", "award-winning", "amazing", "wonderful", "perfect",
    "nice", "good", "great", "high quality", "ultra hd", "8k",
    "唯美", "大气", "震撼", "高级感",
]
MOOD_SLOP = [
    "calm and peaceful", "mysterious atmosphere", "nostalgic",
    "tense", "suspenseful", "lonely", "warm and cozy",
]
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "with",
    "to", "from", "by", "for", "is", "are", "was", "were", "its",
    "his", "her", "their", "this", "that", "as", "it", "be", "not",
}

# 词数区间（schema §1：t2i 120-220 散文；i2v 短——首帧锁定静态要素，只写动作/运镜/微变化）
WORD_RANGES = {"t2i": (120, 220), "i2v": (40, 80)}


def default_lexicon() -> tuple[list[str], list[str]]:
    """内置词表（lexicon md 缺失/解析失败时兜底）。"""
    return list(HARD_SLOP), list(MOOD_SLOP)


def parse_lexicon_md(path: Path) -> tuple[list[str], list[str]]:
    """从 anti-slop-lexicon.md 解析 (绝对禁用, 低效 mood) 两表第一列。
    表格行形如 `| beautiful / gorgeous | ... |`，第一列按 ` / ` 拆词；
    只取 ASCII 词与常见中文 slop 词，去空。解析失败回退内置。"""
    hard, mood = list(HARD_SLOP), list(MOOD_SLOP)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return hard, mood
    section = ""
    for ln in lines:
        if ln.startswith("## "):
            section = ln
            continue
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue
        if "绝对禁用" in section:
            target = hard
        elif "低效 mood" in section:
            target = mood
        else:
            continue
        for w in re.split(r"\s*/\s*|，", cells[0]):
            w = w.strip().lower()
            if w and (re.fullmatch(r"[a-z0-9][a-z0-9 \-]*", w) or any('\u4e00' <= c <= '\u9fff' for c in w)):
                if w not in target:
                    target.append(w)
    return hard, mood


def _wc(text: str) -> int:
    """词数（中英混排折算）：英文按词计数 + 中文字数按信息密度折算（≈1.8 字/词）。
    此前中文逐字计词——纯中文/中英混排 prompt 虚高超上限误杀（200 字中文=200 词，
    逼近 220 上限；500 字直接爆表）。折算后 220 词上限 ≈ 中文 400 字，语义相当。"""
    en = len(re.findall(r"[A-Za-z']+", text))
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    return en + math.ceil(zh / 1.8)


def lint_prompt(text: str, kind: str, hard: list[str], mood: list[str],
                ranges: dict | None = None) -> list[dict]:
    """lint 单个 prompt，返回违规列表 [{'level','msg','word'}]（空=通过）。"""
    if not text or not str(text).strip():
        return [{"level": "FAIL", "msg": f"{kind} prompt 为空"}]
    text = str(text)
    low = text.lower()
    out = []
    for w in hard:
        if w in low:
            out.append({"level": "FAIL", "msg": f"禁词 slop: {w!r}", "word": w})
    for w in mood:
        if w in low:
            out.append({"level": "WARN", "msg": f"低效 mood 词: {w!r}（用画面传达，见 anti-slop 表）", "word": w})
    rng = ranges or WORD_RANGES
    lo, hi = rng.get(kind, rng["t2i"])
    n = _wc(text)
    if n < lo:
        out.append({"level": "WARN", "msg": f"{kind} 词数 {n} < {lo}（太短没细节，模型丢要素）"})
    elif n > hi:
        out.append({"level": "FAIL", "msg": f"{kind} 词数 {n} > {hi}（太长丢要素；i2v 只写动作/运镜/微变化）"})
    return out


def _significant_tokens(value: str) -> set[str]:
    """字段值 → 显著 token（≥4 字母、非停用词、含字母），用于 i2v subject 误写检测。"""
    toks = set()
    for t in re.findall(r"[A-Za-z]{4,}", str(value).lower()):
        if t not in STOPWORDS:
            toks.add(t)
    return toks


def lint_shot(shot: dict, kind: str, hard: list[str], mood: list[str]) -> list[dict]:
    """lint 一个 shot JSON 的指定角色 prompt（kind: images→t2i / videos→i2v）。"""
    pf = "t2i_prompt" if kind == "images" else "i2v_prompt"
    text = shot.get(pf, "")
    out = lint_prompt(text, "t2i" if kind == "images" else "i2v", hard, mood)
    if kind == "videos" and text:
        # ③ i2v 误写 subject：schema 规定 i2v 不写 subject/scene/style/color/material
        # （首帧已锁定）。shot 里若有这些字段值，其显著 token 出现在 i2v_prompt → 提示。
        low = str(text).lower()
        for field in ("subject", "scene", "style", "color", "material"):
            val = shot.get(field) or shot.get(f"_atomic", {}).get(field)
            if not val:
                continue
            hits = [t for t in _significant_tokens(val) if t in low]
            if hits:
                out.append({"level": "WARN",
                            "msg": f"i2v 疑似重述 {field}（首帧已锁定，只写动作/运镜/微变化）："
                                   f"{hits[:5]}"})
    return out


def cmd(args) -> None:
    lex = args.lexicon if args.lexicon else (
        Path(__file__).resolve().parents[1] / "references" / "anti-slop-lexicon.md")
    hard, mood = (default_lexicon() if args.no_slop_file
                  else parse_lexicon_md(Path(lex)))
    target = Path(args.target)
    shots: list[tuple[str, dict]] = []
    if target.is_dir():
        import mg_core
        for j in mg_core.list_shot_files(target):
            try:
                shots.append((str(j), json.loads(j.read_text(encoding="utf-8"))))
            except Exception as e:
                shots.append((str(j), {"__error__": f"JSON 解析失败: {e}"}))
        if not shots:
            print(f"[prompt_lint] 未找到分镜 JSON: {target}/S*.json 或 shot_*.json", file=sys.stderr)
            sys.exit(2)
    else:
        try:
            shots.append((str(target), json.loads(target.read_text(encoding="utf-8"))))
        except Exception as e:
            print(f"[prompt_lint] 读取失败: {e}", file=sys.stderr)
            sys.exit(2)

    kind = args.kind
    fails = 0
    rows = []
    for path, shot in shots:
        if "__error__" in shot:
            rows.append({"shot": path, "level": "FAIL", "issues": [shot["__error__"]]})
            fails += 1
            continue
        issues = []
        for k in ("images", "videos"):
            if kind and k != kind:
                continue
            issues += lint_shot(shot, k, hard, mood)
        lv = "FAIL" if any(i["level"] == "FAIL" for i in issues) else (
            "WARN" if issues else "PASS")
        if lv == "FAIL":
            fails += 1
        rows.append({"shot": path, "level": lv, "issues": issues})

    if args.json:
        print(json.dumps({"lexicon": str(lex), "hard": len(hard), "mood": len(mood),
                          "shots": rows, "fails": fails}, ensure_ascii=False, indent=2))
    else:
        print(f"[prompt_lint] 词表: {Path(lex).name}（绝对禁用 {len(hard)} / mood {len(mood)}）")
        for r in rows:
            tag = r["level"]
            print(f"  [{tag}] {Path(r['shot']).name}")
            for i in r["issues"]:
                print(f"        [{i['level']}] {i['msg']}")
        print(f"[prompt_lint] {len(rows)} 镜, FAIL={fails}"
              f"{', 全过' if fails == 0 else '—— 修 prompt 后重跑，别让 slop 白烧额度'}")
    sys.exit(1 if fails else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description="shot JSON 提示词静态 lint（词级约束机器化）")
    ap.add_argument("target", help="shots 目录或单个 shot JSON")
    ap.add_argument("--kind", default="", choices=["", "images", "videos"],
                    help="只查某角色（images→t2i_prompt / videos→i2v_prompt；留空=都查）")
    ap.add_argument("--lexicon", default="", help="anti-slop 词表 md 路径（默认 references/anti-slop-lexicon.md）")
    ap.add_argument("--no-slop-file", action="store_true", help="不读 lexicon md，只用内置词表")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    cmd(ap.parse_args())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""dialogue.py — 剧本/对白层（批十四）：剧本文本 → vo_build 可吃的 vo_lines.json。

解决的问题：多角色漫剧的「谁说哪句、用什么音色」靠手工拼 JSON 又慢又错。
本脚本把**剧本体文本**（角色：台词）解析成结构化对白，按角色表自动分配音色，
输出直接喂 `vo_build plan` / `vo_build` 的 vo_lines.json。

剧本格式（每行一句，顺序即播放序）：
    # 井号开头 = 注释（也用于分场标题，会被解析进 acts）
    [方括号行 = 空镜/画外说明]（进 acts，不上 TTS）
    阿明：今天天气不错。
    小雨：是啊，一起去公园吧。
    阿明（激昂）：太棒了！           ← 括号内 = 该句 emotion（可选）
    [S01] 阿明：走吧                ← 行首 [Sxx] = 该句所属镜（可选，喂 plan 反推镜时长）

角色表（--voices，JSON）：{"阿明": "tongtong", "小雨": "xiaoyao"}
未映射的角色 → 用 --default-voice（缺省空 = provider 默认音色），**并在 stderr 明说**
（不静默全用默认——多角色剧全一个声音是事故）。

用法：
    python scripts/dialogue.py script.txt --out vo/vo_lines.json
    python scripts/dialogue.py script.txt --voices voices.json --default-voice tongtong --out vo/vo_lines.json
    python scripts/dialogue.py script.txt --check            # 只校验不落盘（lint 用）

输出（vo_lines.json，与 vo_build 既有 schema 对齐）：
    {"acts": [...方括号说明行...], "lines": [{"id","text","shot","voice","emotion"}]}
之后：
    python scripts/vo_build.py plan vo/vo_lines.json --out vo/plan.json --words --punct
    python scripts/vo_build.py vo/vo_lines_at.json --out vo/vo.m4a --total <plan 里的值>

退出码：0 成功 / 2 参数或剧本格式问题（含"没有任何台词"）/ 3 角色表读入失败。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 行首镜标记：[S01] / [shot_01] 之类
_SHOT_RE = None  # 延迟构造（见 _compile_res）——模块级正则放在函数里按需建，
# 避免 import 时就编译一批用不上的模式（AST 守卫只看 def，不看模块级常量）。

_ROLE_SPLIT = "："   # 中文冒号为主形态；英文冒号兜底（半角在中文输入法下常见）


def _compile_res():
    global _SHOT_RE
    import re
    if _SHOT_RE is None:
        _SHOT_RE = re.compile(r"^\[([A-Za-z0-9_\- ]{1,24})\]\s*")
    return _SHOT_RE


def _strip_emotion(text: str) -> tuple:
    """台词尾部的（情感）括号注记 → (text, emotion)。只剥**末尾**整段括号，
    句中括号不动（"他（终于）醒了"不该被当成情感注记）。"""
    t = str(text).strip()
    if t.endswith("）") and t.count("（") >= 1:
        i = t.rfind("（")
        inner = t[i + 1:-1]
        if inner and "：" not in inner and len(inner) <= 8:
            return t[:i].strip(), inner
    return t, ""


def parse_dialogue(text: str) -> dict:
    """剧本文本 → {acts, lines, warnings}（纯函数，可单测）。

    - `#` 行：`# 场一：公园` 形如 acts 的标题注释进 acts（不带 # 号）；
      纯分隔注释（# ---）不进 acts，直接丢弃
    - `[...]` 行：空镜/画外说明 → acts（保留方括号原文，方便 vo_build 原样展示）
    - `角色：台词` → lines[{speaker, text, emotion?, shot?}]
    - 角色名后若紧跟（情感）注记 → 该句 emotion
    - 无角色名但有台词的行（旁白体）→ speaker="旁白"
    - 识别不出的行 → warnings（不静默丢；调用方决定 die 还是容忍）
    """
    import re
    shot_re = _compile_res()
    acts, lines, warnings = [], [], []
    n = 0
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if body and not set(body) <= {"-", "=", "_", " ", "#"}:
                acts.append(body)
            continue
        if line.startswith("[") and line.endswith("]"):
            # 行首镜标记的方括号 [S01] 已被 shot_re 剥走，这里进来的只能是
            # 整行方括号 = 空镜/说明
            acts.append(line)
            continue
        shot = ""
        m = shot_re.match(line)
        if m:
            shot = m.group(1).strip()
            line = shot_re.sub("", line, count=1)
        n += 1
        if _ROLE_SPLIT in line:
            role, rest = line.split(_ROLE_SPLIT, 1)
            role = role.strip()
            rest = rest.strip()
            if not role:
                warnings.append(f"第 {n} 行冒号前为空：{raw[:40]}")
                continue
            if not rest:
                warnings.append(f"第 {n} 行 {role} 没有台词：{raw[:40]}")
                continue
            # 角色名尾部的（激昂）注记 → 剥成该句 emotion（v4.20 实测：
            # "阿明（激昂）：太棒了"的括号长在角色名上，不剥进 speaker）
            role, emo_role = _strip_emotion(role)
            text_clean, emotion = _strip_emotion(rest)
            emotion = emotion or emo_role
            rec = {"speaker": role, "text": text_clean}
            if emotion:
                rec["emotion"] = emotion
            if shot:
                rec["shot"] = shot
            lines.append(rec)
            continue
        # 英文冒号兜底（半角）：角色:台词（避免中文剧本被英文输入法坑）
        if ":" in line:
            role, rest = line.split(":", 1)
            if role.strip() and rest.strip():
                role = role.strip()
                rest = rest.strip()
                role, emo_role = _strip_emotion(role)
                text_clean, emotion = _strip_emotion(rest)
                emotion = emotion or emo_role
                rec = {"speaker": role, "text": text_clean}
                if emotion:
                    rec["emotion"] = emotion
                if shot:
                    rec["shot"] = shot
                lines.append(rec)
                continue
        # 无冒号 → 旁白体台词（有些剧本旁白不写角色名）
        lines.append({"speaker": "旁白", "text": line, **({"shot": shot} if shot else {})})
    return {"acts": acts, "lines": lines, "warnings": warnings}


def assign_voices(lines: list, voice_map: dict, default_voice: str = "") -> tuple:
    """按角色表给每句配音色（纯函数）。返回 (带 voice 的新列表, 未映射角色集)。

    未映射角色 → default_voice；default_voice 也空 → voice 留空（provider 默认），
    但**未映射角色集必须返回**，调用方要如实报告，不静默全默认。
    """
    out = []
    unmapped = []
    for ln in lines:
        rec = dict(ln)
        spk = str(ln.get("speaker") or "").strip()
        v = voice_map.get(spk, "")
        if not v:
            if spk not in unmapped:
                unmapped.append(spk)
            v = default_voice or ""
        if v:
            rec["voice"] = v
        out.append(rec)
    return out, unmapped


def to_vo_lines(parsed: dict, lines: list) -> dict:
    """结构化对白 → vo_build 的 vo_lines.json schema。

    id 规则：L01..L99（台词序号）；text/emotion/voice/shot 原样透传。
    lines 已是 assign_voices 的输出（带 voice），本函数只做 id 编号与组装。
    """
    out_lines = []
    for i, ln in enumerate(lines, start=1):
        rec = {"id": f"L{i:02d}", "text": str(ln.get("text") or "").strip(),
               "speaker": ln.get("speaker", "")}
        for k in ("voice", "emotion", "shot"):
            if ln.get(k):
                rec[k] = ln[k]
        out_lines.append(rec)
    return {"acts": parsed.get("acts", []), "lines": out_lines}


def die(msg: str, code: int = 1) -> None:
    print(f"[dialogue] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def cmd(args) -> int:
    src = Path(args.lines_src if hasattr(args, "lines_src") else args.script)
    if not src.exists():
        die(f"找不到剧本 {src}", 2)
    raw = src.read_text(encoding="utf-8")

    voice_map: dict = {}
    if args.voices:
        vp = Path(args.voices)
        if not vp.exists():
            die(f"找不到角色表 {vp}", 3)
        try:
            voice_map = json.loads(vp.read_text(encoding="utf-8"))
            if not isinstance(voice_map, dict):
                die(f"角色表必须是 JSON 对象（角色名→音色名），当前是 {type(voice_map).__name__}", 3)
        except json.JSONDecodeError as e:
            die(f"角色表不是合法 JSON：{e}", 3)

    parsed = parse_dialogue(raw)
    if not parsed["lines"]:
        die("剧本里没有一句台词（全是注释/空行？格式：角色：台词）", 2)
    for w in parsed["warnings"]:
        print(f"[dialogue] 警告：{w}", file=sys.stderr)

    lines, unmapped = assign_voices(parsed["lines"], voice_map,
                                    default_voice=args.default_voice)
    if unmapped:
        print(f"[dialogue] 未映射角色（将用 {'--default-voice ' + args.default_voice if args.default_voice else 'provider 默认音色'}）："
              f"{'、'.join(unmapped)}", file=sys.stderr)

    vo = to_vo_lines(parsed, lines)

    if args.check:
        print(f"[dialogue] 校验通过：{len(vo['lines'])} 句台词，{len(vo['acts'])} 条说明，"
              f"{len({l['speaker'] for l in vo['lines']})} 个角色")
        for ln in vo["lines"]:
            print(f"  {ln['id']}  {ln['speaker']:<6s} {ln.get('voice', '—'):<10s} {ln['text'][:30]}")
        return 0

    out = Path(args.out)
    if str(out) == "." or not str(out):
        die("--out 必须给输出路径", 2)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(vo, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dialogue] -> {out}  （{len(vo['lines'])} 句台词，{len(vo['acts'])} 条说明）")
    print(f"[dialogue] 下一步：python scripts/vo_build.py plan {out} --out "
          f"{out.parent / 'plan.json'} --words --punct")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="dialogue.py",
        description="剧本/对白层：剧本文本（角色：台词）→ vo_build 可吃的 vo_lines.json")
    ap.add_argument("script", help="剧本文本文件（角色：台词 每行一句）")
    ap.add_argument("--voices", default="", help="角色表 JSON：{\"角色\": \"音色名\"}")
    ap.add_argument("--default-voice", default="",
                    help="未映射角色的兜底音色（缺省 provider 默认）")
    ap.add_argument("--out", default="", help="输出 vo_lines.json 路径")
    ap.add_argument("--check", action="store_true", help="只校验不落盘")
    args = ap.parse_args()
    sys.exit(cmd(args))


if __name__ == "__main__":
    main()

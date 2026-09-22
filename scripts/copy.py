#!/usr/bin/env python3
"""copy.py — 广告文案批量出稿（AI 撒网 → 人工筛选）。

核心认知（实测得出）：**约束 > 模型**。
同一个 Agnes-2.5-flash，"随便写" 产出全是空话套话（"传承千年文脉""守护文化根魂"），
加了 7 条硬约束后能写出"笔挂起来，比握在手里活得长"这种句子。
所以本脚本的价值不在调模型，而在**内置那套验证过的约束模板**。

用法：
  python copy.py --brief "文房四宝公益广告，文化传承" --count 20
  python copy.py --brief "..." --shots shots.txt --count 30 --out candidates.txt
  python copy.py --brief "..." --extra-banned "国潮,非遗"   # 追加禁词

输出：纯列表（每行一条），供人挑选改写。
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path


# 密钥文件与加载实现收归 mg_core（单源）。
# v4.7.9：本地副本的正则是 `="(.*)"$`（要求行尾收引号），带行内注释的 key 行
# 会被**静默跳过**——而 mg_core 的 `="([^"]*)"` 容忍行内注释，两处已漂移。
from mg_core import (  # noqa: E402
    KEY_ENV_FILE as _KEY_ENV_FILE,
    LEGACY_KEY_ENV_FILE as _LEGACY_KEY_ENV_FILE,
    _load_env_file,
)

_load_env_file(_KEY_ENV_FILE)
_load_env_file(_LEGACY_KEY_ENV_FILE)

BANNED = "弘扬、瑰宝、绽放光彩、新时代、源远流长、博大精深、文化自信、璀璨"
# 这两个词本身没错，错在单独成句当口号。放开，但要求搭配具体细节。
ALLOW_BUT_GROUNDED = "传承、匠心"

CONSTRAINTS = """硬性要求：
1) 绝不描述画面（观众看得见的东西不许说），只抛主张、下判断、给态度
2) 优先用具体细节或数字支撑，细节比形容词有力
3) 每条不超过 15 字，短句，中文
4) 禁止空话：{banned}
5) “{allow}”可以用，但必须搭配具体细节，不许单独成句
   （如“匠心独运”不合格，“这双手磨了四十年”合格）
6) 数字必须是公认常识或可核实的，**不许编造精确工艺参数**
   （这条极重要：编的数字会被内行一眼戳穿）
7) 参考语气：慢，是一种功夫。/ 一张宣纸，能活一千年。/ 笔挂起来，比握在手里活得长。

只输出 {count} 条，每行一条，不加序号、分类、标题和任何解释。"""

SYSTEM = "你是广告文案，厌恶空话套话，擅长用具体细节替代形容词。"


def clean_text(text: str) -> str:
    """清洗模型输出：去序号（只剥"数字+分隔符/括号包数字"形态）、去空行、去 markdown 符号。"""
    lines = []
    for ln in text.splitlines():
        s = ln.strip().lstrip("-*·").strip()
        s = "".join(ch for ch in s if ch not in "**`")
        # 只剥"序号"形态（数字+分隔符 / 括号包数字），
        # 别裸剥数字开头的中文文案（如"30年的墨"→"年的墨"，#10）
        m = re.match(r"^(?:\d+[.、)）]\s*|[（(]\d+[）)]\s*)", s)
        if m:
            s = s[m.end():]
        if s:
            lines.append(s)
    return "\n".join(lines)


def backoff_delays(tries: int, base: float = 8.0, factor: float = 2.0) -> list:
    """重试等待表（秒）：**等待次数 = 尝试次数 − 1**——最后一次是终局，等了也白等。

    旧版是循环体末尾的 `time.sleep(8 * (i + 1))`：三次全灭会白等 24s
    （8/16/24 合计 48s），且线性硬编码，与 mg_core.http_call 的指数退避不同族。
    """
    return [base * (factor ** i) for i in range(max(0, int(tries) - 1))]


def chat(base: str, key: str, model: str, system: str, user: str,
         temperature: float, tries: int = 3) -> str:
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": 2000}
    delays = backoff_delays(tries)
    for i in range(tries):
        try:
            req = urllib.request.Request(
                base + "/chat/completions",
                data=json.dumps(body).encode(), method="POST")
            req.add_header("Authorization", f"Bearer {key}")
            req.add_header("Content-Type", "application/json")
            r = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
            return r["choices"][0]["message"]["content"]
        except Exception as e:                      # 429/5xx 退避
            print(f"  (重试 {i+1}/{tries}: {str(e)[:60]})", file=sys.stderr)
            if i < len(delays):                     # 最后一次不等
                time.sleep(delays[i])
    raise SystemExit("[copy] 调用失败，见上方错误")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", required=True, help="一句话需求（题材/主题/受众）")
    ap.add_argument("--shots", default="",
                    help="可选：画面列表文件（每行一镜），让文案贴合镜号；但严禁描述画面")
    ap.add_argument("--count", type=int, default=20, help="候选条数")
    ap.add_argument("--out", default="", help="输出文件（默认打印到 stdout）")
    ap.add_argument("--provider", default="agnes", help="文本模型 provider（默认 agnes）")
    ap.add_argument("--model", default="agnes-2.5-flash")
    ap.add_argument("--temperature", type=float, default=0.95)
    ap.add_argument("--extra-banned", default="", help="追加禁词，逗号分隔")
    ap.add_argument("--allow", default=ALLOW_BUT_GROUNDED,
                    help="允许但需落地搭配细节的词（默认 传承、匠心）")
    args = ap.parse_args()

    # key 从统一 env 读：MEDIA_AGNES_1_KEY / _BASE，MEDIA_ZHIPU_1_* …
    prefix = f"MEDIA_{args.provider.upper()}_1_"
    key = os.environ.get(prefix + "KEY", "")
    base = os.environ.get(prefix + "BASE", "").rstrip("/")
    if not (key and base):
        raise SystemExit(f"[copy] 未找到 {prefix}KEY/_BASE。请确认已在 ~/.workbuddy/media_keys.env 配置"
                         f"（脚本会自动 source；若仍读不到，重启终端或手动 source 该文件）")

    banned = BANNED + (("、" + args.extra_banned.replace(",", "、")) if args.extra_banned else "")
    user = f"为{args.brief}写{args.count}条字幕。\n"
    if args.shots:
        shots = " / ".join(
            ln.strip() for ln in open(args.shots, encoding="utf-8") if ln.strip())
        if shots:
            user += f"\n画面依次是：\n{shots}\n（仅供你理解内容，**严禁描述画面**）\n"
    user += "\n" + CONSTRAINTS.format(banned=banned, allow=args.allow, count=args.count)

    if args.provider == "zhipu":
        print("[copy] 提示：智谱免费档文本模型限流严重（实测持续 429），建议用 agnes",
              file=sys.stderr)

    text = chat(base, key, args.model, SYSTEM, user, args.temperature)

    out = clean_text(text)
    lines = out.splitlines()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out + "\n")
        print(f"[copy] -> {args.out}  ({len(lines)} 条)")
    else:
        print(out)
    print(f"\n[copy] 共 {len(lines)} 条候选。下一步：人工筛选改写，剔除编造数字的句子，"
          f"再写进 vo_lines.json", file=sys.stderr)


if __name__ == "__main__":
    main()

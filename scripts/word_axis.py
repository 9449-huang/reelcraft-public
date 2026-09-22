#!/usr/bin/env python3
"""word_axis.py — 词级时间轴（② 的核心引擎，v4.12.0）。

**为什么不是 whisper.cpp**（2026-09-21 实测）：whisper.cpp 的 Windows 二进制可下
（`whisper-bin-x64.zip`，经 ghproxy），但 **ggml 模型全源不可达**——官方下载脚本指向
`huggingface.co/ggerganov/whisper.cpp`（本机被墙 Tunnel 502）；`hf-mirror.com` 超时；
`ggml.ggerganov.com` 已空；ModelScope 试了 12 个候选仓库名全 404；gitee 搜索为空。
**有壳无芯。**

**为什么 ONNX Whisper**：ModelScope 上有 `onnx-community/whisper-base` 的 ONNX 导出，
而 `onnxruntime` 本机早已安装（v4.10.0 为 silero VAD 装的）。→ 同等能力、零 torch，
与本项目"凡能力走 ONNX 不引 torch"的既定策略一致。实测编解码前向均可在 CPU 跑通。

**分工**（纯函数与 IO 分离，便于单测）：
- 纯函数：`mel_filters` / `log_mel` / `timestamp_seconds` / `is_timestamp` /
  `interpolate_times` / `group_words` / `greedy_decode`（会话可注入）
- IO 壳：`decode_pcm`（ffmpeg）/ `_sessions` / `transcribe` / `cmd`

⚠️ 时间精度：**token 级插值**，不是 cross-attention DTW。whisper 的 `<|t.dd|>` 只在段
边界出现，中间文本 token 的时间用序号线性插值——这是行业通行做法，但不是 DTW 的精度。
当前 ONNX 导出不回吐注意力，想要 DTW 得换导出（记在文档里，不假装）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import mg_core
from mg_core import _ffmpeg

MODELS_DIR = Path.home() / ".workbuddy" / "models"
WHISPER_SUBDIR = "whisper-base-onnx"
ENC_MODEL = "encoder_model_int8.onnx"
DEC_MODEL = "decoder_model_merged_int8.onnx"
TOKENIZER_FILE = "tokenizer.json"
GEN_CONFIG = "generation_config.json"

SAMPLE_RATE = 16000
N_FFT = 400
HOP = 160
N_MELS = 80
CHUNK_SECONDS = 30           # whisper 的固定窗口；更长的音频按窗切分
N_HEADS = 8
HEAD_DIM = 64

SOT_TOKEN = 50258            # <|startoftranscript|>
EOS_TOKEN = 50257            # <|endoftext|>
TRANSCRIBE_TOKEN = 50359     # <|transcribe|>
NOTIMESTAMPS_TOKEN = 50363   # <|notimestamps|>——必须压制，否则模型不吐时间戳
TS_BASE = 50364              # <|0.00|>；之后每 1 个 token = 0.02s
TS_STEP = 0.02
TS_COUNT = 1501              # 0.00 ~ 30.00
MAX_DECODE_TOKENS = 224

LANG_DEFAULT = "zh"
DIE_ARG = mg_core.EXIT_USAGE

# ── sherpa（paraformer-zh）后端：中文逐字时间轴（v4.16）─────────
# 阶段一实测（2026-09-22）：1.13.8 有 cp313-win_amd64 轮子（pip 直连，零 torch）；
# paraformer-zh-2023-09-14 int8 给**可读汉字 token + 逐字时间戳**（29 字/29 时间戳，
# 5.6s 音频解码 0.5s）。⚠️ paraformer-**small**（2024-03-09）不给时间戳，勿用；
# ⚠️ zipformer-**ctc** 虽给时间戳但 token 是 byte-BPE 乱码（映射是查表非单一偏移），放弃。
SHERPA_SUBDIR = "sherpa-paraformer-zh"
SHERPA_MODEL = "model.int8.onnx"
PUNCT_SUBDIR = "sherpa-punct"          # 标点恢复模型（ct-transformer zh-en，294MB）
PUNCT_MODEL = "model.onnx"
SHERPA_TOKENS = "tokens.txt"
SHERPA_REPO = "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14"

_CJK_RANGES = (r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
               r"\u3040-\u30ff\uac00-\ud7af")
_CJK_ONE = re.compile(f"[{_CJK_RANGES}]")


# ─── 后端探测与安装指引（与 vad_backend/face_backend 同口味）──────────

def _models_path(models_dir=None) -> Path:
    """模型目录：显式给了就用它，否则 `<models>/whisper-base-onnx`。

    单源——`whisper_backend` / `install_hint` / `transcribe` 三处曾各写一遍默认，
    一旦漏改就会出现"探测说没有、实际下在那儿"的分裂。
    """
    return Path(models_dir) if models_dir else (MODELS_DIR / WHISPER_SUBDIR)


def whisper_backend(models_dir=None) -> str:
    """返回 "whisper" 或空串。缺任一文件/依赖都返回空——调用方据此降级并**如实标注**。"""
    md = _models_path(models_dir)
    try:
        for f in (ENC_MODEL, DEC_MODEL, TOKENIZER_FILE):
            p = md / f
            if not p.is_file() or p.stat().st_size <= 0:
                return ""
    except OSError:
        return ""
    try:
        import onnxruntime  # noqa: F401
        import tokenizers    # noqa: F401
    except Exception:
        return ""
    return "whisper"


def _sherpa_dir(models_dir=None):
    """sherpa 模型目录：--models-dir 按 **models 根**理解（其下找 sherpa 子目录，
    也容忍直接指向模型目录本身）；缺省 = ~/.workbuddy/models/sherpa-paraformer-zh。"""
    root = Path(models_dir) if models_dir else MODELS_DIR
    for c in (root / SHERPA_SUBDIR, root):
        try:
            if all((c / f).is_file() and (c / f).stat().st_size > 0
                   for f in (SHERPA_MODEL, SHERPA_TOKENS)):
                return c
        except OSError:
            continue
    return None


def sherpa_backend(models_dir=None) -> str:
    """返回 "sherpa" 或空串 —— **中文逐字时间轴**后端（paraformer-zh int8）。

    依赖 = `pip install sherpa-onnx`（onnxruntime 系，零 torch）+ 2 个文件；
    模型来源走 hf-mirror（HF 被墙的替代，实测可达）。
    """
    d = _sherpa_dir(models_dir)
    if not d:
        return ""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return ""
    return "sherpa"


def available_backend(models_dir=None) -> str:
    """当前机器**实际可用**的词轴后端（多个可用时取优先级最高者）。

    优先级：sherpa（中文逐字、真声学时间戳）> whisper（通用、英文好）。
    """
    return sherpa_backend(models_dir) or whisper_backend(models_dir)


def install_hint(models_dir) -> str:
    """缺依赖时的安装指引（两个后端各一段，说清"装什么、放哪、从哪来"）。"""
    md = _models_path(models_dir)
    repo = "https://www.modelscope.cn/api/v1/models/onnx-community/whisper-base/repo"
    sroot = Path(models_dir) if models_dir else MODELS_DIR
    return (
        "词级时间轴两个后端，装齐任一即可（都零 torch）：\n"
        "【中文逐字（sherpa paraformer-zh，推荐）】\n"
        "  1) pip install sherpa-onnx\n"
        "  2) 模型放 " + str(sroot / SHERPA_SUBDIR) + "（hf-mirror 可下）：\n"
        "     https://hf-mirror.com/" + SHERPA_REPO + "/resolve/main/" + SHERPA_MODEL + "\n"
        "     https://hf-mirror.com/" + SHERPA_REPO + "/resolve/main/" + SHERPA_TOKENS + "\n"
        "【通用（whisper ONNX，英文好）】\n"
        "  1) pip install onnxruntime tokenizers\n"
        f"  2) 模型放 {md}（HuggingFace 被墙，用 ModelScope 取）：\n"
        f"     {repo}?Revision=master&FilePath=onnx%2F{ENC_MODEL}\n"
        f"     {repo}?Revision=master&FilePath=onnx%2F{DEC_MODEL}\n"
        f"     {repo}?Revision=master&FilePath={TOKENIZER_FILE}\n"
        f"     （可选 {GEN_CONFIG}：取语言表与首步抑制表）\n"
        "  3) 装好后 --backend auto 会自动启用；缺任一项则回退句级对账")


# ─── mel 前端（纯 numpy；whisper 规格）────────────────────────────

def mel_filters(n_mels: int = N_MELS, n_fft: int = N_FFT,
                sr: int = SAMPLE_RATE):
    """Slaney 归一化三角 mel 滤波器组 → (n_mels, n_fft//2+1)。

    与 `librosa.filters.mel(sr, n_fft, n_mels)` 默认参数（htk=False,
    norm="slaney"）一致——whisper 官方的 mel_filters.npz 就是这么生成的。
    """
    import numpy as np

    def hz_to_mel(f):
        f = np.asarray(f, dtype=np.float64)
        min_log_hz, min_log_mel = 1000.0, 15.0
        logstep = np.log(6.4) / 27.0
        return np.where(f >= min_log_hz,
                        min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
                        f / (200.0 / 3.0))

    def mel_to_hz(m):
        m = np.asarray(m, dtype=np.float64)
        min_log_hz, min_log_mel = 1000.0, 15.0
        logstep = np.log(6.4) / 27.0
        return np.where(m >= min_log_mel,
                        min_log_hz * np.exp(logstep * (m - min_log_mel)),
                        (200.0 / 3.0) * m)

    fft_freqs = np.linspace(0.0, sr / 2.0, 1 + n_fft // 2)
    hz_pts = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(sr / 2.0), n_mels + 2))
    fb = np.zeros((n_mels, fft_freqs.size), dtype=np.float64)
    for i in range(n_mels):
        lo, ctr, hi = hz_pts[i], hz_pts[i + 1], hz_pts[i + 2]
        up = (fft_freqs - lo) / max(ctr - lo, 1e-10)
        down = (hi - fft_freqs) / max(hi - ctr, 1e-10)
        fb[i] = np.maximum(0.0, np.minimum(up, down))
    fb *= (2.0 / (hz_pts[2:] - hz_pts[:-2]))[:, None]     # Slaney 面积归一
    return fb.astype(np.float32)


def log_mel(pcm, *, n_mels: int = N_MELS, n_fft: int = N_FFT, hop: int = HOP,
            pad_seconds: float = CHUNK_SECONDS):
    """PCM float32（16k 单声道）→ log-mel (n_mels, 3000)。

    规格（padding 到 30s → 固定 3000 帧）：
      center-STFT（reflect pad n_fft//2）→ 功率谱 → 丢最后一帧 → mel 投影
      → log10(clamp(x, 1e-10)) → max(x, max−8) → (x+4)/4
    静音输入的结果恒为 **−1.5**（可由上式唯一推出），是钉住整条链路的锚点。
    """
    import numpy as np
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    want = int(SAMPLE_RATE * pad_seconds)
    x = np.pad(x, (0, want - x.size)) if x.size < want else x[:want]

    win = np.hanning(n_fft + 1)[:-1].astype(np.float32)      # periodic Hann
    pad = n_fft // 2
    xp = np.pad(x, (pad, pad), mode="reflect")
    n_frames = 1 + (xp.size - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    spec = np.fft.rfft(xp[idx] * win, axis=1)
    mag = (np.abs(spec) ** 2).T                              # (freq, frames)
    mag = mag[:, :-1]                                        # 丢末帧（whisper 规格）
    mel = mel_filters(n_mels, n_fft) @ mag
    lg = np.log10(np.maximum(mel, 1e-10))
    lg = np.maximum(lg, lg.max() - 8.0)
    return ((lg + 4.0) / 4.0).astype(np.float32)


# ─── 时间戳（纯函数）──────────────────────────────────────────

def is_timestamp(token_id) -> bool:
    """是否 whisper 的时间戳 token（<|0.00|> … <|30.00|>）。"""
    return TS_BASE <= int(token_id) < TS_BASE + TS_COUNT


def timestamp_seconds(token_id):
    """时间戳 token → 秒；非时间戳返回 None。"""
    if not is_timestamp(token_id):
        return None
    return round((int(token_id) - TS_BASE) * TS_STEP, 4)


def interpolate_times(tokens) -> list:
    """token 序列 → [(token_id, at)]（只含文本 token）。

    时间戳 token 只在段边界出现，中间文本 token 的时间用**序号线性插值**：
      · 前后都有戳 → 在两戳之间按序号比例
      · 只有前戳   → 取前戳
      · 只有后戳   → 从后戳往前倒推（每 token 一个 0.02s 步长）
      · 一个都没有 → 0.0
    """
    toks = [int(t) for t in tokens]
    stamps = [(i, timestamp_seconds(t)) for i, t in enumerate(toks) if is_timestamp(t)]
    out = []
    for i, t in enumerate(toks):
        if is_timestamp(t):
            continue
        prev = [s for s in stamps if s[0] < i]
        nxt = [s for s in stamps if s[0] > i]
        if prev and nxt:
            pi, pt = prev[-1]
            ni, nt = nxt[0]
            at = pt + (nt - pt) * (i - pi) / max(1, ni - pi)
        elif prev:
            at = prev[-1][1]
        elif nxt:
            at = max(0.0, nxt[0][1] - TS_STEP * (nxt[0][0] - i))
        else:
            at = 0.0
        out.append((t, round(float(at), 3)))
    return out


# ─── 分词（纯函数）──────────────────────────────────────────

def _is_cjk(text: str) -> bool:
    return bool(_CJK_ONE.match(str(text)[:1] or ""))


def _expand_token(text: str, at: float, next_at: float) -> list:
    """一个 token 的文本 → [(单元, at)]。

    多字 CJK（如「你好」）按字数在 [at, next_at) 内**均分**——否则一个 token 里的
    后面几个字会全挤在同一时刻，字幕高亮会跳。
    """
    chars = list(str(text))
    if len(chars) <= 1 or not all(_CJK_ONE.match(c) for c in chars):
        return [(str(text), float(at))]
    span = max(0.0, float(next_at) - float(at))
    return [(c, float(at) + span * i / len(chars)) for i, c in enumerate(chars)]


def group_words(pieces, end=None) -> list:
    """token 片段 → 词/字轴 [{w, at, dur}]（纯函数可单测）。

    · CJK 一字一词（字幕按字最可读）；拉丁/数字按空格成词；两者交界断开
    · dur = 下一个词的 at − 本词 at；末词用 `end`（音频时长）兜底，缺省 0.2s
    """
    toks = [p for p in (pieces or []) if str(p.get("t") or "").strip()]
    flat: list = []
    for i, p in enumerate(toks):
        cur = float(p["at"])
        nxt = float(toks[i + 1].get("at", cur)) if i + 1 < len(toks) else float(
            cur + 0.2 if end is None else end)
        flat += _expand_token(str(p["t"]), cur, nxt)

    words: list = []
    buf: list = []

    def flush():
        if not buf:
            return
        text = "".join(x[0] for x in buf).strip()
        if text:
            words.append({"w": text, "at": round(buf[0][1], 3)})
        buf.clear()

    for ch, at in flat:
        if _is_cjk(ch):
            flush()
            words.append({"w": ch, "at": round(at, 3)})
            continue
        if ch[:1].isspace() or (buf and _is_cjk(buf[-1][0])):
            flush()
        buf.append((ch, at))
    flush()

    for i, w in enumerate(words):
        nxt = words[i + 1]["at"] if i + 1 < len(words) else float(
            w["at"] + 0.2 if end is None else end)
        w["dur"] = round(max(0.02, nxt - w["at"]), 3)
    return words


def frame_rms(pcm, *, sr: int = SAMPLE_RATE, hop_ms: float = 10.0):
    """PCM → (每 hop_ms 一帧的 RMS 数组, 每帧秒数)。纯 numpy。"""
    import numpy as np
    x = np.asarray(pcm, dtype=np.float32).reshape(-1)
    n = max(1, int(sr * hop_ms / 1000.0))
    if x.size < n:
        return np.array([], dtype=np.float32), n / sr
    f = x[:x.size // n * n].reshape(-1, n)
    return np.sqrt((f ** 2).mean(axis=1)).astype(np.float32), n / sr


def snap_boundaries(times, rms, hop, *, reach: float = 0.40, depth: float = 0.70,
                    min_dur: float = 0.05) -> list:
    """把词边界吸附到最近的**能量谷**（纯函数）。

    `times` 是首尾在内的一串边界（通常来自均匀插值）。首尾不动，只调中间：
    在 ±reach × 相邻较短词跨度 内找能量最低点，**且该点必须足够深**
    （< depth × 窗口均值）才采纳，否则保持原位——宁可均匀，也不要被噪声带跑。

    为什么需要它：whisper 的时间戳 token 只在段边界出现（11s 语音实测只有
    0.00/11.00 两个锚），段内直接按 token 序号均分**完全不含声学信息**
    （停顿、拖长音全被抹平）。能量谷是免费拿得到的声学证据。
    """
    import numpy as np
    ts = [float(t) for t in (times or [])]
    if len(ts) < 3 or rms is None or len(rms) == 0:
        return ts
    out = [ts[0]]
    for i in range(1, len(ts) - 1):
        span = min(ts[i] - ts[i - 1], ts[i + 1] - ts[i])
        w = max(1, int(round(reach * span / hop)))
        c = int(round(ts[i] / hop))
        lo, hi = max(0, c - w), min(len(rms) - 1, c + w)
        if hi <= lo:
            out.append(ts[i])
            continue
        seg = rms[lo:hi + 1]
        k = int(np.argmin(seg))
        out.append((lo + k) * hop if float(seg[k]) < depth * float(seg.mean()) else ts[i])
    out.append(ts[-1])
    for i in range(1, len(out)):
        out[i] = max(out[i], out[i - 1] + min_dur)
    return out


def refine_words(words, pcm) -> list:
    """用音频能量谷精修词边界（就地改 at/dur 并返回）。"""
    if not words or len(words) < 3:
        return words
    rms, hop = frame_rms(pcm)
    if rms.size == 0:
        return words
    bounds = [w["at"] for w in words] + [words[-1]["at"] + words[-1]["dur"]]
    snapped = snap_boundaries(bounds, rms, hop)
    for i, w in enumerate(words):
        w["at"] = round(snapped[i], 3)
        w["dur"] = round(max(0.02, snapped[i + 1] - snapped[i]), 3)
    return words


# ─── 解码（会话可注入 → 能测"cache 有没有喂回去"）──────────────────

def greedy_decode(sess_dec, enc_hidden, prompt_ids, *, max_tokens: int = MAX_DECODE_TOKENS,
                  eos: int = EOS_TOKEN, suppress=()) -> list:
    """贪婪解码。**每步整句重算**（`use_cache_branch=False`）。

    ⚠️ 为什么不用 KV cache 支路（2026-09-21 实测）：merged 解码器的两条支路
    **数值不一致**——首步 argmax 相同，但 logits 最大绝对差 2.86，误差逐步累积，
    11 秒语音的转写从 "And so my fellow Americans, ask not what your country can do
    for you…" 变成 "And then, they're going to be arrested."；改成整句重算后
    **完全正确**。所以这里选精确但 O(n²) 的路径——字幕线的量级（单窗几十 token）
    代价可接受。哪天拿到带 QK 输出的导出、要上 DTW 时再回来修这条支路。

    `suppress` 只在**首步**生效（whisper 的 `begin_suppress_tokens` 语义）；每步都压制
    会让模型永远停不下来。
    """
    import numpy as np
    out_idx = {o.name: k for k, o in enumerate(sess_dec.get_outputs())}
    past_names = [i.name for i in sess_dec.get_inputs()
                  if i.name.startswith("past_key_values.")]
    enc_len = int(enc_hidden.shape[1])
    blank = {n: np.zeros((1, N_HEADS, 0 if ".decoder." in n else enc_len, HEAD_DIM),
                         dtype=np.float32) for n in past_names}

    ids = [int(t) for t in prompt_ids]
    generated: list = []
    for step in range(max(0, int(max_tokens))):
        feed = {"input_ids": np.array([ids], dtype=np.int64),
                "encoder_hidden_states": enc_hidden,
                "use_cache_branch": np.array([False])}
        feed.update(blank)          # 未用的 cache 输入仍需形状合法（支路会忽略内容）
        logits = sess_dec.run(None, feed)[out_idx["logits"]][0, -1]
        if step == 0 and suppress:
            logits = logits.copy()
            for s in suppress:
                logits[int(s)] = -1e9
        nxt = int(np.argmax(logits))
        if nxt == eos:
            break
        generated.append(nxt)
        ids.append(nxt)
    return generated


# ─── IO 壳 ────────────────────────────────────────────────

def decode_pcm(path):
    """任意音视频 → 16k 单声道 float32 PCM（经 ffmpeg 管道，不落中间文件）。"""
    import numpy as np
    r = subprocess.run([_ffmpeg(), "-v", "error", "-i", str(path),
                        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"解码失败 rc={r.returncode}: "
                           f"{r.stderr.decode('utf-8', 'replace')[:200]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def _lang_id(gen_config: dict, language: str) -> int:
    table = gen_config.get("lang_to_id") or {}
    for k, v in table.items():
        if str(k).strip("<|>") == str(language):
            return int(v)
    raise RuntimeError(f"模型不认识语言 {language!r}（可用示例："
                       f"{sorted(str(k).strip('<|>') for k in list(table)[:8])}）")


def _sessions(models_dir):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.log_severity_level = 3
    md = Path(models_dir)
    return (ort.InferenceSession(str(md / ENC_MODEL), so, providers=["CPUExecutionProvider"]),
            ort.InferenceSession(str(md / DEC_MODEL), so, providers=["CPUExecutionProvider"]))


def tokens_to_words(tokens, timestamps, end) -> list:
    """逐 token 时间戳 → 词轴 [{w, at, dur}]（纯函数）。

    paraformer-zh 的 token 是**逐字**（中文 1 token = 1 汉字），timestamps[i] 是第 i 个
    token 的**起始秒**；末 token 的结束 = `end`（音频时长）。
    - `<...>` 特殊符号与纯空白 token 跳过；
    - timestamps 与 tokens 数不一致时按**可得部分**截齐（没时间戳的 token 无法定位，
      宁可少不可编）；
    - dur 一律 >= 0（时间戳越界/乱序时钳 0，不产生负时长）。
    """
    ts = [float(x) for x in (timestamps or [])]
    end_val = float(end) if end else 0.0
    out = []
    for i in range(min(len(tokens or []), len(ts))):
        w = str(tokens[i] or "").strip()
        if not w or (w.startswith("<") and w.endswith(">")):
            continue
        at = ts[i]
        if at != at or at < 0:                       # NaN / 负
            continue
        nxt = ts[i + 1] if i + 1 < len(ts) else end_val
        hi = min(nxt, end_val) if end_val > 0 else nxt
        out.append({"w": w, "at": round(at, 3), "dur": round(max(0.0, hi - at), 3)})
    return out


_SHERPA_CACHE: dict = {}


def _sherpa_recognizer(md):
    """构造并缓存 OfflineRecognizer（模型 232MB，重复构造 = 每次重读磁盘）。"""
    import sherpa_onnx
    key = str(md)
    if key not in _SHERPA_CACHE:
        _SHERPA_CACHE[key] = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(md / SHERPA_MODEL), tokens=str(md / SHERPA_TOKENS),
            num_threads=2, decoding_method="greedy_search")
    return _SHERPA_CACHE[key]


def punct_backend(models_dir=None) -> str:
    """标点恢复后端探测：返回 "sherpa" 或 ""（model.onnx 缺/过小 → 空）。"""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return ""
    d = (Path(models_dir) if models_dir else MODELS_DIR) / PUNCT_SUBDIR   # 标点模型在 models **根**下，不在 whisper 子目录
    f = d / PUNCT_MODEL
    return "sherpa" if f.is_file() and f.stat().st_size > 1_000_000 else ""


def punctuate(text: str, models_dir=None) -> str:
    """标点恢复（sherpa ct-transformer zh-en）。缺依赖/模型 → RuntimeError(指引)。"""
    import sherpa_onnx
    d = (Path(models_dir) if models_dir else MODELS_DIR) / PUNCT_SUBDIR   # 标点模型在 models **根**下，不在 whisper 子目录
    f = d / PUNCT_MODEL
    if not f.is_file() or f.stat().st_size <= 1_000_000:
        raise RuntimeError(
            f"[word_axis] 标点恢复模型缺失（{f}）。下载：\n"
            "  https://hf-mirror.com/csukuangfj/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12/resolve/main/model.onnx\n"
            "  同仓 tokens.json 一起放到上面的目录（或去掉 --punct 用无标点转写）")
    key = ("_punct_inst", str(f))
    cached = _SHERPA_CACHE.get(key)
    if cached is None:
        cached = sherpa_onnx.OfflinePunctuation(
            sherpa_onnx.OfflinePunctuationConfig(
                sherpa_onnx.OfflinePunctuationModelConfig(str(f))))
        _SHERPA_CACHE[key] = cached
    return cached.add_punctuation(str(text))


def merge_punct_into_words(words: list, punctuated: str) -> list:
    """标点回填（纯函数）：标点模型在**字间**插入标点，必须挂回前一个字的词目。

    逐字符对齐：原字符序列与标点文本走双指针，对得上 → 落到所在词；
    对不上的是标点 → 追加到当前词的 `w` 尾部。任何字符不一致（模型异常改字）
    → **原样返回**，不编造。时间戳字段原样保留。
    """
    out = [dict(w) for w in words]
    concat = "".join(str(w.get("w") or "") for w in out)
    j = 0                                   # 指向 concat 的游标
    cur = -1                                # 当前词下标（游标所在词）
    ends = []
    pos = 0
    for w in out:
        pos += len(str(w.get("w") or ""))
        ends.append(pos)
    tail: list = [[] for _ in out]          # 每个词收集到的尾部标点
    for ch in str(punctuated):
        if j < len(concat) and ch == concat[j]:
            j += 1
            cur = next((k for k, e in enumerate(ends) if j <= e), len(out) - 1)
            if j > 0 and cur > 0 and ends[cur - 1] == j:
                cur -= 1
        else:
            idx = cur if cur >= 0 else len(out) - 1
            if idx < 0:
                return list(words)          # 空词表却来标点 → 异常，原样返回
            tail[idx].append(ch)
    if j != len(concat):
        return list(words)                  # 原字符没走完 → 对不上，原样返回
    for k, w in enumerate(out):
        if tail[k]:
            w["w"] = str(w.get("w") or "") + "".join(tail[k])
    return out


def transcribe_sherpa(path, *, models_dir=None, language: str = LANG_DEFAULT,
                      with_punct: bool = False) -> dict:
    """sherpa（paraformer-zh int8）→ 中文逐字时间轴。

    ★ **不做能量吸附**（whisper 路径才有 `refine_words`）：paraformer 的时间戳是
    模型自带的声学对齐（CIF 预测器），再吸附反而会破坏它——这也是两个后端
    `timing` 字段不同的原因，调用方如实展示。
    """
    d = _sherpa_dir(models_dir)
    if not d:
        raise RuntimeError(install_hint(models_dir))
    pcm = decode_pcm(path)
    dur = pcm.size / SAMPLE_RATE
    rec = _sherpa_recognizer(d)
    st = rec.create_stream()
    st.accept_waveform(int(SAMPLE_RATE), pcm)
    rec.decode_stream(st)
    r = st.result
    words = tokens_to_words(list(r.tokens or []), list(r.timestamps or []), dur)
    text = (r.text or "").strip()
    punct_used = ""
    if with_punct:
        punctuated = punctuate(text, models_dir)
        words = merge_punct_into_words(words, punctuated)
        text = punctuated
        punct_used = "sherpa"
    return {"backend": "sherpa", "text": text, "words": words,
            "duration": round(dur, 3), "language": language,
            "timing": "token-timestamps", "punct": punct_used,
            "model": SHERPA_SUBDIR + "/" + SHERPA_MODEL, "quant": "int8"}


def transcribe(path, *, models_dir=None, backend: str = "auto", language: str = LANG_DEFAULT,
               chunk: float = CHUNK_SECONDS, max_tokens: int = MAX_DECODE_TOKENS,
               punctuate: bool = False) -> dict:
    """音频 → {backend, text, words, duration, ...}。

    `backend="auto"`：**中文优先 sherpa**（paraformer 逐字、真声学时间戳），否则
    whisper（英文好）；都不可用 → 返回空 backend + error 指引（不抛）。
    `backend="whisper"/"sherpa"` 显式要求而运行时不具备 → **抛错**（不静默降级，免得
    "以为用了词级其实只有句级"）。
    """
    md = _models_path(models_dir)
    want = backend if backend in ("auto", "") else backend
    if want not in ("auto", "", "whisper", "sherpa"):
        raise RuntimeError(f"未知后端 {backend!r}（可选 auto/whisper/sherpa）")
    if want == "sherpa":
        # 依赖/模型校验在 transcribe_sherpa 内（**单一来源**，不重复判——
        # 重复判是死代码，破坏验证会抓到"防线删了测试还绿"）；缺了它抛 RuntimeError。
        return transcribe_sherpa(path, models_dir=models_dir, language=language,
                                  with_punct=punctuate)
    if want == "whisper" and not whisper_backend(md):
        # 显式要 whisper 但模型/依赖不齐：必须**在这里**报"缺什么"。
        # 否则会继续往下走到读 gen_config，抛出的却是"模型不认识语言 xx"这类
        # 误导性错误（v4.13 由测试暴露），排查会白走一大圈。
        raise RuntimeError(install_hint(md))
    if want in ("auto", ""):
        # auto：**中文优先 sherpa**（逐字、真声学时间戳）；whisper 兜底（英文好）。
        if language.startswith("zh") and sherpa_backend(models_dir):
            return transcribe_sherpa(path, models_dir=models_dir, language=language,
                                     with_punct=punctuate)
        if not whisper_backend(md):
            if sherpa_backend(models_dir):        # 非中文但只有 sherpa → 也用（如实标注）
                return transcribe_sherpa(path, models_dir=models_dir, language=language,
                                         with_punct=punctuate)
            return {"backend": "", "text": "", "words": [], "duration": 0.0,
                    "error": install_hint(md)}

    from tokenizers import Tokenizer
    gen = {}
    gp = md / GEN_CONFIG
    if gp.is_file():
        try:
            gen = json.loads(gp.read_text(encoding="utf-8"))
        except Exception:
            gen = {}
    suppress = tuple(dict.fromkeys(tuple(gen.get("begin_suppress_tokens") or ())
                                   + (NOTIMESTAMPS_TOKEN,)))
    prompt = [SOT_TOKEN, _lang_id(gen, language), TRANSCRIBE_TOKEN]
    tk = Tokenizer.from_file(str(md / TOKENIZER_FILE))
    sess_enc, sess_dec = _sessions(md)

    pcm = decode_pcm(path)
    dur = pcm.size / SAMPLE_RATE
    win = int(SAMPLE_RATE * chunk)
    n_chunks = max(1, math.ceil(dur / chunk)) if dur > 0 else 1

    pieces: list = []
    texts: list = []
    n_anchors = 0
    for ci in range(n_chunks):
        seg = pcm[ci * win:(ci + 1) * win]
        if seg.size == 0:
            break
        offset = ci * chunk
        hid = sess_enc.run(None, {"input_features": log_mel(seg)[None, :, :]})[0]
        toks = greedy_decode(sess_dec, hid, prompt, max_tokens=max_tokens, suppress=suppress)
        n_anchors += sum(1 for t in toks if is_timestamp(t))
        texts.append(tk.decode([t for t in toks if not is_timestamp(t)]))
        for tid, at in interpolate_times(toks):
            pieces.append({"t": tk.decode([tid]), "at": round(offset + at, 3)})

    words = refine_words(group_words(pieces, end=dur), pcm)
    return {"backend": "whisper", "text": " ".join(x.strip() for x in texts if x.strip()),
            "words": words, "duration": round(dur, 3),
            "language": language, "chunks": n_chunks,
            "timing": "anchors+energy-snapped", "anchors": n_anchors,
            "model": ENC_MODEL, "quant": "int8"}


# ─── CLI ─────────────────────────────────────────────────

def cmd(args) -> int:
    """执行（接受 media_gen 转发的 Namespace），返回退出码。"""
    p = Path(args.path)
    if not p.exists():
        print(f"[word_axis] 路径不存在: {p}", file=sys.stderr)
        return DIE_ARG
    md = getattr(args, "models_dir", "") or None
    be = available_backend(md)
    if not be:
        print(install_hint(md), file=sys.stderr)
        return DIE_ARG

    files = [p] if p.is_file() else sorted(
        f for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in (".mp4", ".webm", ".m4a", ".mp3", ".wav", ".aac"))
    if not files:
        print(f"[word_axis] 目录下没有可用音视频: {p}", file=sys.stderr)
        return DIE_ARG

    reports = []
    for f in files:
        try:
            rep = transcribe(f, models_dir=md, backend=getattr(args, "backend", "auto"),
                             language=getattr(args, "lang", LANG_DEFAULT),
                             max_tokens=getattr(args, "max_tokens", MAX_DECODE_TOKENS),
                             punctuate=bool(getattr(args, "punct", False)))
        except RuntimeError as e:
            print(f"[word_axis] {f.name}: {e}", file=sys.stderr)
            return DIE_ARG
        rep["file"] = str(f)
        reports.append(rep)
        if not getattr(args, "json", False):
            pn = f"  标点={rep['punct']}" if rep.get("punct") else ""
            print(f"[word_axis] {f.name}  {rep['duration']:.2f}s  "
                  f"{len(rep['words'])} 词  后端={rep['backend']}  语言={rep['language']}{pn}")
            print(f"  文本: {rep['text'][:120]}")
            for w in rep["words"][:12]:
                print(f"    {w['at']:7.2f}s +{w['dur']:.2f}  {w['w']}")
            if len(rep["words"]) > 12:
                print(f"    …（共 {len(rep['words'])} 词，--json 看全部）")

    if getattr(args, "json", False):
        print(json.dumps(reports if len(reports) > 1 else reports[0],
                         ensure_ascii=False, indent=2))
    return mg_core.EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(
        description="词级时间轴（ONNX Whisper，零 torch）——逐词字幕 / 精确对账 / 断句的地基")
    ap.add_argument("path", help="音频/视频文件，或目录（批量）")
    ap.add_argument("--lang", default=LANG_DEFAULT, help=f"语言代码（默认 {LANG_DEFAULT}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--backend", choices=["auto", "whisper", "sherpa"], default="auto",
                    help="auto=中文优先 sherpa（逐字）、whisper 兜底")
    ap.add_argument("--models-dir", default="",
                    help="模型目录：whisper 指 whisper-base-onnx 子目录；"
                         "sherpa 按 models 根找 sherpa-paraformer-zh（留空=默认位置）")
    ap.add_argument("--max-tokens", type=int, default=MAX_DECODE_TOKENS,
                    help="单窗最多解码 token 数（默认 224）")
    ap.add_argument("--punct", action="store_true",
                    help="标点恢复（v4.18：sherpa ct-transformer 中英，0.03s 级；"
                         "标点挂回逐字词目，时间戳不变。英文会得到中文式句号——模型偏中文）")
    sys.exit(cmd(ap.parse_args()))


if __name__ == "__main__":
    main()

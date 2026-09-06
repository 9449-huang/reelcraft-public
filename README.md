# ReelCraft

一句话需求 → 多 provider 生图/视频流水线（主力池可配，Agnes/智谱/魔塔模板内置）→ 声音设计（VO/TTS/字幕/BGM 混音）→ 比赛片规格后期 → 自检。

> **定位**：本项目按 **AI agent skill** 设计——在 WorkBuddy / Claude Code 等环境加载 SKILL.md 由 agent 执行（分镜、prompt、QC 目检、开工三问均由 agent 完成）。纯命令行用户可用下方「快速开始」的命令子集跑通最小闭环，但完整七步体验建议在 agent 环境使用。

面向比赛片（如 2026 黄山杯公益/商业赛道）与演示用 AIGC 视频，强调**多 key 轮转、双 key 并行、熔断、一镜多图选优、xfade/末帧链衔接、抽帧 QC 闭环、断点续跑**。

## 特性

- 多 provider 路由：主力池可配（`MEDIA_PRIORITY`，默认 Agnes）→ 智谱 CogView/CogVideoX（一级兜底）→ 魔塔 Qwen-Image-Edit（首帧小改）；单命令失败自动跨池兜底
- **通用池（v2.7）**：任意 OpenAI 兼容渠道填 4 行 env 即接入（`MEDIA_CUSTOM_1_*` 零改码）；`status` 自动探测能力（启发式，仅供参考）；产不出时报错三段式（现象/原因/改哪个字段）
- **成片三档模式（v2.7）**：full 全真视频 / hybrid 按镜混用（重点镜 i2v + 过场镜缓推，省额度不烂尾）/ stills 全缓推（0 视频调用也能出达标片）——探测结果报给用户选，绝不自动降级
- **prompt 口味卡（v2.7）**：通用骨架（导演思维）× 每池口味卡（句式/忌讳），换模型不重学；custom 渠道踩坑回写沉淀
- Key 角色声明（`_ROLES`）：一把 key 一条龙（默认，生图+视频都干）或按角色分工；模型覆盖（`_IMAGE_MODEL/_VIDEO_MODEL`）填名字即换更强模型，零改码
- 多 key 池轮转 + 熔断：401 黑名单 24h、429 冷却换 key、5xx 退避重试、审核拒绝改 prompt 不换 key
- 视频 per-key 节流（1 RPM/key）→ N key 并行吞吐 ×N
- 一镜多图选优、xfade 交叉溶解 / 末帧链衔接、Ken Burns 兜底
- 声音设计：分句 TTS → 精确字幕时间轴 → 旁白/BGM/环境音混音
- 文案批量撒网（`copy.py`）+ 硬约束去空话
- 后期统一比赛规格（H.264 / yuv420p / ≥720p）+ 自检
- 零第三方依赖（仅 Python 标准库 + ffmpeg）

## 目录

```
reelcraft/
├── SKILL.md                 # 完整工作流（7 步 + 失败处理 + 诊断表）
├── scripts/
│   ├── media_gen.py         # CLI 薄入口：image/video/edit/tts/qc/batch/status…
│   ├── mg_core.py           # 核心引擎：provider 表/路由/熔断/节流/异步轮询
│   ├── mg_batch.py          # batch 跨池混编 + harvest 收割
│   ├── mg_status.py         # status/plan-check/qc/last-frame
│   ├── postprocess.py       # 拼接/统一规格/烧字幕/kenburns/自检
│   ├── delogo_watermark.py  # 去固定角标水印（档案驱动）
│   ├── watermark_profiles.json # 水印档案（渠道→kind/框坐标，命中免测）
│   ├── vo_build.py          # 旁白分句合成 + 精确字幕打轴
│   ├── copy.py              # 文案批量出稿
│   ├── export_public.py     # 导出公开版（仅维护者用，剔除私有渠道痕迹）
│   └── ffmpeg_probe.py      # 跨平台定位 ffmpeg
├── tests/                   # 单元测试（python -m unittest discover tests，零网络）
├── references/              # prompt 框架 / 口味卡 / 模型能力 / 反套话词表 / 赛事规格 / 变更日志
└── media_keys.env.example   # 密钥模板（复制改名填真实 key）
```

## 快速开始

```bash
# 1. 配置密钥（复制模板，填真实 key）
mkdir -p ~/.workbuddy && cp media_keys.env.example ~/.workbuddy/media_keys.env
#   编辑 ~/.workbuddy/media_keys.env（目录不存在会自动创建）

# 2. 确认 ffmpeg 可用（脚本自动探测：环境变量 > PATH > imageio_ffmpeg > 托管路径）
#   Windows:  choco install ffmpeg
#   macOS:    brew install ffmpeg
#   Linux:    apt install ffmpeg

# 3. 体检
python scripts/media_gen.py status
python scripts/media_gen.py image --provider agnes --prompt "probe" --size 1024x1024 --out probe.png

# 4. 走完整流程（详见 SKILL.md）
python scripts/media_gen.py image  --provider agnes --prompt "..." --size 1344x768 --count 3 --out shots/shot_01.png
python scripts/media_gen.py video  --provider agnes --image shots/shot_01.png --out clips/clip_01.mp4 --num-frames 121
python scripts/postprocess.py concat clips/ --out final.mp4 --target-res 1280x720 --xfade 0.5
python scripts/postprocess.py check final.mp4
```

## 命令速查

| 命令 | 作用 |
|------|------|
| `media_gen.py image --prompt ... --size ... --out ... [--count N] [--pin-key N]` | 生图（`--count` 一镜多图选优；`--provider` 留空按 `MEDIA_PRIORITY` 自动选池+跨池兜底） |
| `media_gen.py video --prompt ... --image ... --out ... [--num-frames 121]` | 图生视频（默认主力池，失败自动跨池兜底） |
| `media_gen.py video --provider zhipu --image ... --video-size 1920x1080 --duration 5` | 图生视频（智谱兜底） |
| `media_gen.py edit --image ... --prompt ... --out ...` | 首帧小改（魔塔） |
| `media_gen.py last-frame <video> <out.png>` | 抽末帧（末帧链） |
| `media_gen.py batch <dir> --phase images\|videos --provider agnes --workers N [--qc] [--retries N] [--dry-run] [--video-size WxH] [--video-duration short\|medium\|long]` | 批量并行（跨池混编：N worker 各绑一个 (池,key)；size/duration 留空=每池各自默认；视频输出 `clip_<shot_id>.mp4`） |
| `media_gen.py qc <video> <out_dir>` | 抽首/中/尾 3 帧验收 |
| `media_gen.py tts --text ... --out ...` | 旁白 TTS |
| `media_gen.py status [--no-probe]` | key 健康 + /models 能力探测（猜的仅供参考） |
| `postprocess.py concat <clips/> --out ... [--xfade ...] [--voice ...] [--bgm ...] [--subtitles ...]` | 拼接+混音+烧字幕 |
| `postprocess.py check <final.mp4> [--min-res ...] [--max-duration ...] [--min-fps ...]` | 比赛规格自检 |
| `postprocess.py kenburns <img> <out.mp4> --duration 5` | Ken Burns 兜底（单镜） |
| `postprocess.py kenburns-all <dir> --outdir clips/ [--duration 5,4,6]` | stills 档批量缓推（0 API，断点续跑，对齐 concat 命名契约） |
| `delogo_watermark.py <video> --provider <channel> [--dry-run]` | 去固定角标水印（读档案自动缩放；`--dry-run` 只出红框自检图；产出 *_nowm.mp4 + check 图） |
| `postprocess.py extract <video> <out.png>` | 抽末帧（与 media_gen last-frame 等价） |
| `vo_build.py vo/vo_lines.json --out vo/vo.m4a --total 55.94 [--skip-tts] [--auto-shift]` | 旁白合成+精确打轴 |
| `copy.py --brief "..." --count 20 [--shots shots.txt] --out candidates.txt` | 文案批量出稿 |

## Provider 配置

密钥统一放在 `~/.workbuddy/media_keys.env`（旧 `agnes_key.env` 自动兼容），格式：

```bash
export MEDIA_AGNES_1_KEY="sk-..."
export MEDIA_AGNES_1_BASE="https://apihub.agnes-ai.com/v1"
export MEDIA_AGNES_1_POLL="https://apihub.agnes-ai.com"
# 多 key 序号连续：_2_ _3_ ... 遇缺号即停
export MEDIA_AGNES_2_KEY="sk-..."
export MEDIA_AGNES_2_BASE="https://api.agnes-ai.cn/v1"
export MEDIA_AGNES_2_POLL="https://api.agnes-ai.cn"
# 智谱 / 魔塔（兜底）
export MEDIA_ZHIPU_1_KEY="..."
export MEDIA_ZHIPU_1_BASE="https://open.bigmodel.cn/api/paas/v4"
export MEDIA_MODELSCOPE_1_KEY="..."
export MEDIA_MODELSCOPE_1_BASE="https://modelscope.cn/api/v1"
# 旁白 TTS（可选）
export MEDIA_TTS_1_KEY="..."
export MEDIA_TTS_1_BASE="https://host/v1"
export MEDIA_TTS_1_MODEL="cosyvoice-v1"
# 通用池：任意 OpenAI 兼容渠道（v2.7，零改码；接哪个能力填哪个模型名）
export MEDIA_CUSTOM_1_KEY="..."
export MEDIA_CUSTOM_1_BASE="https://host/v1"
export MEDIA_CUSTOM_1_IMAGE_MODEL="flux-dev"     # 生图能力必填
export MEDIA_CUSTOM_1_VIDEO_MODEL="wan-2.3"      # 视频能力必填
export MEDIA_CUSTOM_1_ROLES="image,video"        # 同步声明
```

`batch --workers N` 的 N 不得超过该池**承担该阶段角色的 key 数**（`_ROLES` 过滤后）。

### Key 角色与全局优先级（可选，不填 = 一条龙默认）

```bash
# 一条龙（默认）：一把 key 包办生图+视频，无需任何额外配置
# 分工：生图用 A 家、视频用 B 家
export MEDIA_A_1_ROLES="image"
export MEDIA_B_1_ROLES="video"
# 单把 key 换更强模型（零改码）
export MEDIA_AGNES_1_VIDEO_MODEL="agnes-video-v3.0"
# 主力池与跨池兜底顺序（默认 agnes,zhipu,modelscope）
export MEDIA_PRIORITY="agnes,zhipu"
```

## 安全

- **密钥永不打印、永不写产物、永不进 git**（`.gitignore` 已排除 `media_keys.env` / `.media_state.json` 等）
- 脚本仅显示 `sk-L***Slyr` 形式的掩码
- 请使用免费/官方允许的额度，遵守各 provider 速率限制
- 本工具仅生成内容，不替用户担保素材/BGM 版权

## License

MIT — 见 [LICENSE](LICENSE)。

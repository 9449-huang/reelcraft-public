# 模型能力表（v2.1）— 多 provider 版本

各 provider 模型参数白名单与限速。`scripts/media_gen.py` 中的 `PROVIDERS` 字典是权威定义；本文件仅供阅读与选型参考。

## Agnes（主力 · 速度限制型，无总量上限）
- Base URL: `https://apihub.agnes-ai.com/v1`（key A）/ `https://api.agnes-ai.cn/v1`（key B，独立 endpoint）
- 鉴权：Bearer KEY
- 文档参考：记忆里"Agnes AI 文档"

### 图像 `agnes-image-2.1-flash`
| 分辨率 | RPM | 备注 |
|---|---|---|
| 1024×1024 | 20 | 默认档 |
| 1024×576 | 20 | 16:9 |
| 1344×768 | 10 | **默认规格推荐**（超过 1280×720 下限） |
| 2048×1152 | 10 | 2K 16:9 |
| 3K / 4K | ≈1 | 极慢，慎用 |

返回：JSON `data[0].url`（cos-platform-outputs.agnes-ai.cn）。**经实测无水印**。

### 视频 `agnes-video-v2.0`
| 项 | 值 |
|---|---|
| 任务端点 | POST `/videos` |
| 任务轮询 | GET `{poll_base}/agnesapi?video_id=...`（每 15s 一次，最多 10min） |
| num_frames | **必须 8n+1** ∈ {9,17,25,33,41,49,57,65,73,81,89,97,105,113,121} |
| negative_prompt | 支持 |
| image（I2V 首帧） | 接受本地路径 / URL / data URI |
| RPM | **1**（视频硬瓶颈） |
| **输出分辨率（实测）** | **固定 1088×832（约 4:3）**，与首帧分辨率/比例无关（1312×736 首帧 → 1088×832 输出，已实测） |
| 实测耗时 | 121 帧 5s 视频全程 **2m15s**（含轮询），远好于 10min 上限 |

⚠️ **分辨率不达标（已实测确认）**：1088×832 < 1280×720 硬指标，且 4:3 比例偏方。
**解决方案（已验证）**：后期 `scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720`（中心裁切 + 1.18x 轻放大）→ 1280×720 达标，画质损失可控。`postprocess.py` 默认即此模式。
**勿用** decrease+pad（会出黑边）或直接放大到 1920×1080（1.77x 放大画质明显偏软）。

## 智谱 GLM（一级兜底 · 并发 30，无明确日限）

- Base URL：`https://open.bigmodel.cn/api/paas/v4`
- 鉴权：Bearer KEY
- OpenAI 兼容

### 图像 `CogView-3-Flash`（免费）
| 分辨率 | 备注 |
|---|---|
| 1024×1024 | 默认 |
| 1440×720 | 16:9，**符合默认规格下限** |

### 图像 `CogView-4`（付费，质量高）
- **首个能可靠生成汉字的开源文生图模型**
- 适合需要中文标语/书法的镜头

⚠️ **已确认问题**：免费档出图右下角带"AI 生成"水印（URL 域名 `maas-watermark-prod-new`，文件名含 `_watermark.png`）。**不能直接用于成片**——只能作概念图/风格参考，或后期去水印。

### 视频 `CogVideoX-Flash`（免费 · v2.1 已接入）
| 项 | 值 |
|---|---|
| 提交端点 | POST `/videos/generations`（base `https://open.bigmodel.cn/api/paas/v4`） |
| 轮询端点 | GET `/async-result/{id}`（path 式，与 Agnes 的查询参数式不同） |
| 模式 | i2v（`image_url`，URL/Base64，≤5M png/jpeg）/ t2v |
| 时长 | `duration` ∈ {5, 10} 秒（**无 num_frames 概念**） |
| 帧率 | `fps` ∈ {30, 60}（脚本默认 30） |
| size | 720x480 / 1024x1024 / 1280x960 / 960x1280 / **1920x1080** / 1080x1920 / 2048x1080 / 3840x2160（仅 i2v 支持；默认 1920x1080） |
| 输出 | `task_status: "SUCCESS"` → `video_result[0].url` |
| 优势 | **原生 1920×1080 16:9，无 Agnes 的 4:3 裁切放大问题**，直接超默认规格下限 |
| 定位 | Agnes 视频兜底（Agnes 挂了/出图比例不满意时切换）；注意与 Agnes 镜头风格差异，整片统一用一家 |

## 魔塔 ModelScope（二级兜底 · 图编辑，v2.1 已接入）

- Base URL：`https://api-inference.modelscope.cn/v1`
- 模型列表接口：`GET /v1/models`
- **当前 API-Inference 不提供文生图模型**（grep 后仅 `Qwen/Qwen-Image-Edit` 等编辑模型）
- 用途：图编辑（`media_gen.py edit` 子命令），不接入文生图主力链

### 图编辑 `Qwen/Qwen-Image-Edit-2509`
| 项 | 值 |
|---|---|
| 提交端点 | POST `{base}/images/generations`，header `X-ModelScope-Async-Mode: true` |
| 请求体 | `{model, prompt, image_url: [data URI 或 URL]}`（列表，支持多图编辑） |
| 轮询端点 | GET `{base}/tasks/{task_id}`，header `X-ModelScope-Task-Type: image_generation` |
| 状态值 | `SUCCEED`（注意与智谱 `SUCCESS` 拼写不同）/ `FAILED` |
| 输出 | `output_images[0]`（URL），**无水印，可直接入正片** |
| 典型用途 | 首帧小改：移物/调光/局部重绘，避免整图重 roll 破坏已选好的画面 |
| 耗时预期 | 约 30-120s（模型较大，轮询上限 5 分钟） |

## 路由优先级与风格一致性

```
图片链：
1. Agnes image-2.1-flash    主力（无限量，1344×768，--count 3 选优）
2. Agnes image-2.1-flash    key B（不同 endpoint，轮转）
3. 智谱 CogView-3-Flash     一级兜底（**带水印，作概念图**）
4. 智谱 CogView-4           特殊镜头（要中文字时）
5. 魔塔 Qwen-Image-Edit-2509  首帧小改（edit 子命令，无水印）

视频链：
1. Agnes agnes-video-v2.0   主力（121帧=5s，输出 1088×832 → 后期裁切放大到 720p）
2. 智谱 CogVideoX-Flash     兜底（--provider zhipu，原生 1920×1080，duration 5|10）
3. Ken Burns 缓推           最终兜底（关键帧变视频，postprocess kenburns）
```

⚠️ **关键约束**：不同 provider 出图风格差异显著。**同一片内不要中途切换 provider**，否则镜头之间会割裂。中途切换 → 全片关键帧重生成。
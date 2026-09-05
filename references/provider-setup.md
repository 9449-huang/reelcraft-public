# 扩容指南：多 key / 新 provider 接入（自 SKILL.md 外置）

> 加 key 只改 env 零改码；接新渠道首选通用池；想做一等公民再进 PROVIDERS 表。

## 扩容指南（多 key / 新 provider 接口）

**① 加同 provider 的更多 key**（如第 4、5 把 Agnes key）——只改 `~/.workbuddy/media_keys.env`，脚本零改动：

```bash
export MEDIA_AGNES_4_KEY="sk-..."
export MEDIA_AGNES_4_BASE="https://apihub.agnes-ai.com/v1"   # 每把 key 独立 _BASE/_POLL，可混用不同域名
export MEDIA_AGNES_4_POLL="https://apihub.agnes-ai.com"
export MEDIA_AGNES_4_ROLES="image,video"   # 可选：缺省一条龙；分工示例 "image"
export MEDIA_AGNES_4_IMAGE_MODEL="..."     # 可选：该 key 单独换模型（零改码）
```

- 之后 `batch --workers 4` 即四发并行（worker 数超过可用 (池,key) 对数时自动截断并提醒）
- key 池按 `_1_ _2_ _3_...` **序号连续枚举，遇缺号即停**——序号必须连续，不能跳号
- 改完跑 `python scripts/media_gen.py status` 验证全部加载（非默认的角色/模型覆盖会显示在行尾）

**② 接入新的生图/生视频 provider**——**首选通用池（零改码）**：

1. env 里加 `MEDIA_CUSTOM_1_KEY` / `_BASE`（OpenAI 兼容 base，`/v1` 结尾）+ **必填** `MEDIA_CUSTOM_1_IMAGE_MODEL` / `MEDIA_CUSTOM_1_VIDEO_MODEL`（接哪个能力填哪个，`_ROLES` 同步声明）
2. 可选：`MEDIA_CUSTOM_1_IMAGE_SIZES="1024x1024,..."` 自填白名单（超出仅警告）；视频默认按异步任务轮询风（`/videos/generations` + path 轮询），Sora 风渠道用 `_TASK_PATH=/_videos` 改
3. 跑 `status`（自动 /models 探测能力）→ probe 一张图/一段视频目检 → 按 `references/prompt_styles.md` custom 卡沉淀口味
4. 多把 custom key：`MEDIA_CUSTOM_2_...` 递增即可，轮转/熔断/节流/批量全套自动继承

> 想把某渠道做成一等公民（带实测 size 白名单/专用 payload）：再在 `media_gen.py` 顶部 `PROVIDERS` 表加条目——**照抄 agnes 条目改参数**，多 key 轮转 / 熔断 / per-key 节流 / 跨池兜底 / QC / 断点续跑全套自动继承；并把它加进 `MEDIA_PRIORITY`（不加入则 `--provider <pool>` 显式使用）。定制条目 = 沉淀实测参数，通用池 = 快速试接，两者不冲突

**③ 验证**：`status` 看加载；单 key 冒烟测试用 `--pin-key N`（如 `image --pin-key 3`），不扰动其它 key 的冷却计时。
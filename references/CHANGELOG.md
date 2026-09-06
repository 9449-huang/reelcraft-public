# ReelCraft 变更日志

> 历史演进记录，从 SKILL.md 外置（2026-09-06, v2.9）——SKILL.md 只保留当前有效指令，读历史到这来。

## v3.0（2026-09-06）

- **通用化定位**：具体赛事规格档案 `references/competition-spec.md` 移出公开版（export_public SKIP，私有仓保留备赛用）；SKILL/README/postprocess/model-capabilities/prompt-styles 等 6 文件约 25 处赛事归属表述中性化为"默认质量下限（可配置）"——720p/120s/H.264/yuv420p 数值与代码逻辑不变，仅去具体赛事归属
- **导出自检升级**：`export_public.py` 自检从"单一私有池零命中"升级为"私有痕迹零命中"（拦截清单见脚本内 FORBIDDEN：私有池名 + 具体赛事归属字样）
- **同步纪律教训**：export 的 DST 目录（reelcraft_public）是独立 git 仓——每轮"重导出"之后必须把 DST 中**实际变化**的文件也推送，只推源仓会漏（v3.0 版本号就漏推过一次，SKILL.md 云端停在 v2.9，靠逐文件内容比对才抓到）。发布后验证要"内容级"：逐文件比对本地↔云端（行尾归一化后），不能只看 commit 是否推成功

## v2.9（2026-09-06）

- **视频参数每池化**：`--video-size`/`--video-duration` 默认改空 = 走各池 `default_size`/`default_duration`（智谱 1920x1080、本地网关 1280x720/short），显式传参才覆盖；修复共享 CLI 默认导致智谱视频被白名单拒绝的回归
- **batch 透传**：`batch --video-size/--video-duration` 支持（原批量锁死 short@1280x720）
- **测试公开化**：公开仓补回 `tests/`（通用回归），私有池专属测试留 `tests/test_private_pools.py` 不导出；新增 `TestVideoDefaultSize` 钉死"每池默认必须在白名单内"
- **文档重构**：本变更日志从 SKILL.md 外置；README 目录树对齐实际文件（删除已弃用的 agnes_gen.py 条目，反映 mg_core/mg_batch/mg_status 拆分）；`prompt_styles.md` 更名 `prompt-styles.md`（命名与其他 references 统一为连字符）

## v2.8（2026-09-06）

- **LTX Bridge 接入**（本地算力机桥）：custom 池接入本地 OpenAI 兼容壳；修复 `call_with_failover` 漏传 role 导致 `_ROLES` 过滤失效的 bug；`/v1/videos` 用 `input` 字段（`_VIDEO_PROMPT_FIELD`）、产物相对路径 `.webp` 动图（`_abs_url` 补全 + `_final_out` 后缀纠正）
- **media_gen.py 拆四模块**：`mg_core.py`（引擎）/ `mg_batch.py`（批量）/ `mg_status.py`（查询质检）/ `media_gen.py`（薄入口），CLI 用法不变
- **LTX .webp 转码**：`postprocess.py webp2mp4`（Pillow 解帧绕过损坏 Exif）
- **失败镜补跑**：`batch --retry-failed`（读 batch_run.json 只重跑 FAIL/PENDING；PENDING 先 harvest，仍在生成的不重提交防重复扣费）
- **画风质检**：`postprocess.py stylegrid frames/ --cols 5`（N 镜首帧拼图，跳变一眼可见）
- **plan 校验**：`plan-check <plan.json>`（未知字段/枚举值校验，防拼错静默失效）

## v2.7（2026-09-05）

- **通用池**：任意 OpenAI 兼容渠道 4 行 env 接入（`MEDIA_CUSTOM_1_*`，零改码）；模型名必填，image 走 `/images/generations`、视频按异步任务轮询风
- **成片三档**：full / hybrid / stills（Step 1 问④，绝不自动降级；stills 用 `kenburns-all` 批量图→clip）
- **prompt 口味卡**：三张"怎么喂"小卡（轻量/高规格/custom 四档），通用骨架×每池口味分层
- custom 档位四档（ultra/high/mid/low）由用户自选，落 env `_TIER`（挂 key 不挂池）

## v2.6（2026-09-05）

- **水印探测-抹除旁线**：`watermark_profiles.json` 档案（clean/corner-delogo/unknown/fatal）+ `delogo_watermark.py --provider`；首遇新渠道 probe 片定位、抹除后回写档案

## v2.5（2026-09-05）

- **Key 方案确认**：开工六问落地（角色搭配/并行度/水印/成片模式/模型顺序/档位），答案落 plan.json
- **角色路由**：`_ROLES` 按 key 声明承担 image/video；`MEDIA_PRIORITY` 定主力池与跨池兜底顺序

## v2.1 – v2.4（2026-09-04 ~ 09-05）

- v2.1 一镜多图选优（`--count`）、智谱 CogVideoX 视频兜底、魔塔首帧小改（`edit`）
- v2.2 声音设计（tts/字幕/混音）、QC 闭环（`qc`/`batch --qc`）
- v2.3 文案批量出稿（`copy.py`）
- v2.4 batch 任意 N key 并行

## v2.0 及以前

- prompt 由对话模型直接撰写——**弃用 agnes-2.5-flash 写 prompt**：实测它写不好技术性 prompt（现在只用于文案批量，见 `copy.py`）
- 多 provider 路由、熔断、末帧链/xfade、断点续跑、比赛片规格后期

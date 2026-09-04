---
name: reelcraft
description: 一句话需求 → 多 provider 生图/视频流水线（主力池可配 + 智谱/魔塔兜底，图批量选优 + 首帧图编辑 + 视频双链兜底）→ 声音设计（VO/TTS/字幕/BGM 混音）→ 比赛片规格后期 → 自检。Use when user asks to 做个视频/出片/AIGC广告/多 provider 兜底 or 提供赛事规格要求生成参赛视频；强调多 key 轮转与**多 key 并行**、熔断、一镜多图选优、xfade/末帧链衔接、抽帧 QC 闭环、断点续跑。When NOT to use: 静态海报用 ppt-master 或 image-master，单帧修图用 buddy-image-processing。
---

# ReelCraft — 多 provider 视频流水线（v2.7）

### 何时使用
- 用户给出主题 + 时长 + 风格，要求生成一段比赛用/演示用 AIGC 视频
- 用户要求"用免费的 key 出片"
- 用户要求多 provider 兜底以防单点失败

### 何时**不要用**
- 静态海报/单图 → 用 `ppt-master` 或图片工具
- 单帧修图（去水印、风格化） → 用 `buddy-image-processing`

### 核心设计变更（v2.5）
0. **Key 方案确认 + 角色路由**（v2.5 新增）：开工前先问用户两件事——key 怎么搭配（**一条龙**一把 key 包办生图+视频，还是**分工**生图/视频用不同家）、开几发并行；每把 key 用 `_ROLES` 声明承担的角色，`MEDIA_PRIORITY` 决定主力池与跨池兜底顺序，`_IMAGE_MODEL/_VIDEO_MODEL` 填模型名即换更强模型（零改码）
1. **prompt（英文生图/视频提示词）由对话模型直接撰写**——当初弃用 agnes-2.5-flash 是因为它写不好技术性 prompt
2. **多 provider 路由**：Agnes 主力（无限量）→ 智谱（一级兜底）→ 魔塔（图编辑），单 provider 内多 key 轮转
3. **熔断**：401→换 key；429→冷却换 key；5xx→退避重试；**审核拒绝不换 key（改 prompt）**
4. **末帧链一致性**（有适用条件，见 Step 5）：同场景连续镜头用末帧链，跨物体切换用 xfade
5. **断点续跑**：已完成 clip 跳过，state 持久化跨会话
6. **比赛片规格后期**：统一 1280×720 / H.264 / yuv420p / +faststart，ffmpeg 自检
7. **一镜多图选优**（v2.1 新增）：`image --count N` 批量出 N 张候选，人工挑最好的一张进 i2v
8. **视频兜底**（v2.1 新增）：智谱 CogVideoX-Flash（免费，原生 1920×1080，无 Agnes 的 4:3 问题）
9. **首帧小改**（v2.1 新增）：魔塔 `edit` 子命令，移物/调光不用整图重 roll
10. **N key 并行**（v2.2 双 key → v2.4 任意 N）：`batch --workers N` 轮流把镜分给 N 把 key，per-key 节流，吞吐 ×N（有几把 key 就开几发）
11. **声音设计**（v2.2 新增）：VO 稿 → `tts` 子命令 → `--subtitles` 多段字幕 → `--voice/--bgm/--ambient-db` 混音
12. **QC 闭环**（v2.2 新增）：`qc`/`batch --qc` 每段抽首中尾 3 帧验收，不合格按诊断表单变量重拍
13. **文案批量出稿**（v2.3 新增）：`copy.py --brief --count` AI 撒网出候选 → 人工筛选改写。
    ⚠️ 注意与第 1 条区分：**prompt 归对话模型写（机器看的技术描述），文案走 AI 批量（人看的创意）**——
    实测证明约束到位时 agnes-2.5-flash 写中文文案质量很高，前提是给它硬约束
14. **水印探测-抹除旁线**（v2.6 新增）：档案 `scripts/watermark_profiles.json` 记各渠道水印状态（clean / corner-delogo / unknown / fatal）——命中档案免测直抹；首遇新渠道跑**固定镜头纯色画面** probe 片，抽帧目检定位（agent 自带视觉，0 API）后 `delogo_watermark.py --provider` 抹除并回写档案。只管小而静态的角标水印；动态/大面积记 fatal 换渠道
15. **通用池 + 能力探测 + 三档模式**（v2.7 新增）：任意 OpenAI 兼容渠道填 4 行 env 即接入（`MEDIA_CUSTOM_1_*`，零改码）；`status` 自动 GET /models 启发式猜能力（标注“猜的未验证”，实跑才算数）；成片三档 **full 全真视频 / hybrid 按镜混用（重点镜 i2v + 过场镜 kenburns） / stills 全缓推（0 视频调用）**——探测结果报给用户选，绝不自动降级
16. **prompt 口味卡**（v2.7 新增）：`references/prompt_styles.md` 三张"怎么喂"小卡——轻量卡（agnes/zhipu/qwen-edit 共用，按文生图/图编辑/视频三种活对号）／高规格卡（sora/kling 级大模型逐字段写全）／custom 两档（按模型实力选，判断不了问用户）——通用骨架保证"想得对"，口味卡保证"喂得对"；custom 池踩坑回写沉淀，越用越准

---

## Step 0 — 赛事规格锁定（比赛片必做）

向用户索取（最多 3 个问题）：
- 赛事全称 + 截止日期
- 投稿类别（决定能否用 AIGC、时长/分辨率/大小上限、人数限制）
- 主题方向 / 一句话创意

如果用户已发附件（如黄山杯通知），**抓取正文**（WebFetch + 必要时 WebSearch 找附件原文），落到 `references/competition-spec.md`。

**附 2026 黄山杯公益+商业双赛道关键规格**（已落盘，作为 `check` 默认阈值示例；其它赛事用 `--min-res/--max-duration/--min-fps` 调整）：
- 公益（皖市监函〔2026〕317）：**AIGC 数智类**限 1 人；视频 MP4/H.264/≥1280×720/≤300MB/≤120s
- 商业（皖市监函〔2026〕318）：其它媒体类含 AI 生成；演示视频 ≤60s；平面 CMYK 300dpi
- 截止：**2026-09-30 17:00**
- 13 主题 + 文房四宝专项单元

## Step 1 — 体检（5 分钟，必做）

不体检直接开工 = 赌。脚本一次跑完四个 provider 的探针：

```bash
python scripts/media_gen.py status                                    # key 健康
python scripts/media_gen.py image --provider agnes --prompt "probe" --size 1024x1024 --out probe_agnes.png
python scripts/media_gen.py image --provider zhipu --prompt "probe" --size 1024x1024 --out probe_zhipu.png
```

把生成的两张图用 Read 工具查看，**确认两个关键点**：
1. **水印**：智谱免费档右下角带"AI 生成"水印（已验证 → 不能直接投比赛），Agnes 不带（已验证）
2. **风格一致性预演**：两张图风格差异显著 → 同一片内不要中途切换 provider

体检报告写入对话日志，下一步决策依据。

### Key 方案确认（体检后、拆镜前，必问）

`status` 已报出各池 key 家底，向用户确认三件事（**只问一次**，答案落 `shots/plan.json`）：

**问① key 怎么搭配？**
- **A. 一条龙**（默认）：一把 key 包办生图+视频——env 里不填 `_ROLES` 即是（缺省 `image,video`）
- **B. 分工**：生图/视频用不同家的 key——env 给每把 key 填 `_ROLES`（如 `MEDIA_A_1_ROLES=image`、`MEDIA_B_1_ROLES=video`）

**问② 开几发并行？**
- 1 发 = 串行（约 2 分钟/镜）；N 发 = 吞吐 ×N（`batch --workers N`，N ≤ 承担该阶段角色的 key 数）
- `status` 显示有几把可用 key，用户答几就开几发

**问③ 视频源带水印吗？**
- 先查档案 `scripts/watermark_profiles.json`（命中渠道=实测框可抹 / agnes=clean / zhipu=视频未验证）
- 用户说不上来且档案 `unknown`/缺条目 → 跑 5s **probe 视频**：prompt 故意用**固定镜头+纯色背景**（蓝天/白墙，水印无所遁形），`qc` 抽首/中/尾 3 帧目检
- 有水印 → `delogo_watermark.py --provider <池> --dry-run` 目检框位 → 正式抹除，框坐标**回写档案**，同渠道永久免测
- 会动的/大面积/居中的水印 → 档案记 `fatal`，换渠道（勿硬抹）

**问④ 成片模式选哪档？**（按 key 家底 + 探测结果给建议，用户拍板）
- **full 全真视频**：视频 key 齐全时的默认，每镜走 i2v
- **hybrid 按镜混用**：重点镜（开场/高潮/转场）走 i2v，过场镜用 kenburns 缓推；请用户指定重点镜号（如 1,5,8）——省额度不烂尾
- **stills 全缓推**：没有视频 key 时的保底，全镜 kenburns（0 视频调用，纯本地），规格照样达标
- 模式落 plan.json；**绝不自动降级**——探测到有视频能力就默认 full，降级永远先问

确认后写入 `shots/plan.json`（`{"role_assign": "one-stop|split", "workers": N, "watermark": {"<池>": "clean|corner-delogo|fatal"}, "mode": "full|hybrid|stills", "hero_shots": [1,5,8]}`），后续步骤照办，不再重复问。
单命令 `image/video` 的 `--provider` 留空时按 `MEDIA_PRIORITY` 自动选池；某池全部 key 失败自动跨池兜底。

## Step 2 — 分镜拆解

按 `references/prompt-framework.md` 拆镜（比赛片 60s 建议 10-14 镜，可弹性到 ≤120s）。

**拆镜前先做 Director's Read**（framework 第 0 层）：每镜用一句话回答"这个镜头在故事里为什么存在"，写不出答案的镜头砍掉。把结论写进 shot JSON 的 `dramatic_function` 字段。

**连续性分类账**：拆镜同时建 `shots/ledger.json`（framework §3）——immutable（色板/光源/风格/道具外观，每镜强制重复）+ transient_state（世界状态，同场景镜头必须写入 prompt）。

每镜写到 `shots/shot_NN.json`：{景别, 主体外观锚点, 动作, 运镜, 光影色温, 风格, i2v_prompt, role, dramatic_function}。
**事件密度防火墙**：每镜最多 1-2 个事件（framework §2），超了拆镜，不要塞。

## Step 3 — Prompt 撰写（**由对话模型 agent 直接写**，不再调 agnes-2.5-flash）

按 `references/prompt-framework.md` 的六要素结构 + anti-slop 词汇表（`references/anti-slop-lexicon.md`），**用对话模型（袋袋）**为每镜撰写。**先查目标池的口味卡**（`references/prompt_styles.md`）——骨架保证“想得对”，口味卡保证“喂得对”（每池句式/语言/长度/忌讳不同，如 Qwen-Edit 要指令式、高规格视频模型必写音频提示）：
- 一段散文体英文 prompt（120-220 词），**主体+动作放开头**（顺序即优先级），覆盖 scene/subject/action/camera/light/style
- 一段 I2V prompt（40-80 词），只描述 motion + camera + atmosphere 微变，**不重写首帧内容**

**选轨（默认 A 轨）**：以上散文六要素服务**叙事镜头**。若该图需要**多次复现 / 换变量重出 / 交付他人改**
（海报、信息图、产品图、角色设定表、版式），改走 **B 轨**——先填原子字段再编译 prompt，见
`references/prompt-atomic-schema.md`。定稿后把选中图的参数反向抽成 `_atomic` 字段存进 shot JSON，
保证"同一张好图能再出一次"。**禁止**把 B 轨 15 字段全塞进 i2v prompt（会与首帧打架致运动糊掉）。

写完过一遍 framework §9 自查清单（11 项）。

**降级路径（已弃用）**：`scripts/agnes_gen.py` 仅保留旧 `enhance` 命令供历史回溯，**新流程请用 `media_gen.py`**，本脚本不再维护。

## Step 4 — 关键帧出图（多 provider 路由 + 批量选优）

```bash
# 推荐：一镜出 3 张候选，人工选优再进 i2v（免费档，选图不心疼）
python scripts/media_gen.py image \
  --prompt "<scene.subject...>" \
  --size 1344x768 \
  --count 3 \
  --out shots/shot_01.png        # 产出 shot_01_1.png ~ shot_01_3.png
# --provider 留空 = 按 MEDIA_PRIORITY 自动选池（默认 agnes 优先），失败自动跨池兜底
```

选优后把选中的那张改名为/复制为 `shots/shot_01.png`（i2v 入口），其余候选保留备查。

**首帧小改**（选中的图有局部瑕疵时，不要整图重 roll）：

```bash
python scripts/media_gen.py edit \
  --image shots/shot_01.png \
  --prompt "move the ink stick slightly to the left, keep everything else unchanged" \
  --out shots/shot_01_v2.png     # 魔塔 Qwen-Image-Edit-2509，输出无水印
```

**路由规则**（已写入 `scripts/media_gen.py`）：
- **主力池由 `MEDIA_PRIORITY` 决定**（默认 agnes 优先；换主力 = 改 env 一行，零改码）
- 同池内：key 1 失败 → 自动切 key 2（轮转）；401 黑名单 / 429 冷却 / 5xx 退避
- 单命令池内全部 key 失败 → 按 `MEDIA_PRIORITY` 自动**跨池兜底**（如 agnes → zhipu）
- 每把 key 的 `_ROLES` 决定它承担生图还是视频（缺省一条龙都干）；分工模式下路由只挑对口的 key
- `_IMAGE_MODEL/_VIDEO_MODEL` 填模型名即换（payload 层生效，零改码）
- 智谱带水印 → 用作"风格参考/概念验证"，**不入正片**（除非后期去水印）
- **开工前定好搭配，中途不换 provider**（会破坏风格一致性 → 全片关键帧需重生成）

**模型能力白名单**已硬编码在脚本：Agnes 支持 1024×1024/1024×576/1344×768/2048×1152；智谱支持 1024×1024/1440×720 等。**注意：Agnes video 输出固定 1088×832（已实测），与首帧分辨率无关**——出图仍用 1344×768（给视频模型更多细节余量），达标交给后期裁切放大（Step 6）。

## Step 5 — 图生视频（末帧链 / xfade 按镜头关系二选一）

**衔接方式选择（重要）**：
- **同场景连续镜头**（同一主体、机位延续，如"人物走近→特写"）→ **末帧链**：上一镜视频最后一帧抽出来当下一镜首帧，画面级一致性
- **跨物体/跨场景切换**（如砚台→墨锭→毛笔）→ **独立首帧 + Step 6 的 xfade 交叉溶解**：i2v 模型无法完成大幅场景跳变，强行末帧链会导致画面崩坏或几乎不动

```bash
# ① 每镜独立首帧（跨物体切换的主流路径）
python scripts/media_gen.py video  --provider agnes --prompt "<i2v shotN>" --image shots/shot_NN.png --out clips/clip_NN.mp4 --num-frames 121

# ② 末帧链（仅同场景连续镜头）——含 observed end state 规则
#    下一镜 i2v prompt 必须基于 clip 末帧"实际发生了什么"来写（模型常不按预期结尾），
#    并把 ledger.transient_state 里上一镜的世界状态写进 prompt
python scripts/media_gen.py last-frame clips/clip_01.mp4 shots/shot_02_seed.png
python scripts/media_gen.py video --provider agnes --prompt "<i2v shot2, 据实写>" --image shots/shot_02_seed.png --out clips/clip_02.mp4 --num-frames 121
```

**视频兜底链**（Agnes 不可用时）：
```bash
# 智谱 CogVideoX-Flash：免费，原生 1920x1080（无 4:3 问题），i2v，5s 或 10s
python scripts/media_gen.py video --provider zhipu --prompt "<i2v shotN>" \
  --image shots/shot_NN.png --out clips/clip_NN.mp4 --video-size 1920x1080 --duration 5
# 仍失败 → postprocess.py kenburns 关键帧缓推兜底
```

**硬约束**：
- Agnes 视频 **num_frames ∈ {9,17,25,...,121}**（8n+1），脚本白名单校验；智谱视频不用 num_frames，用 `--duration 5|10` + `--video-size`
- **negative_prompt**（Agnes）默认：`blurry, distorted faces, warped hands, extra limbs, text artifacts, watermark, camera shake, flickering, plastic skin, oversaturated`
- **节流**：Agnes 视频 1 RPM **按 key 计** / 智谱 5 RPM，脚本内自动等待
- **N key 并行**（批量生成提速，推荐）：N 把 key = N 路独立 1RPM，吞吐 ×N。
  用 `batch` 子命令自动分队列，worker 按 `_ROLES` 过滤后轮流分镜，**不会重复生成同一镜**：
  ```bash
  # 关键帧（可 --count 选优时不用并行，出图快）；batch 只跑单池，--provider 必填
  python scripts/media_gen.py batch shots60/ --phase images --provider agnes --workers 3
  # 视频：worker1→key#1、worker2→key#2、worker3→key#3，各跑各的 1RPM
  python scripts/media_gen.py batch shots60/ --phase videos --provider agnes --workers 3 --qc
  ```
  - `--workers N` 不得超过**承担该阶段角色的 key 数**（_ROLES 过滤后，超过直接报错）；有几把 key 就开几发
  - 节流为 **per-key 计时**（`tag = provider_keyN`），各路互不等待；state 文件加了线程锁
  - `--qc`：每段生成后自动抽首/中/尾 3 帧到 `clips/qc/`，供视觉验收（见"QC 闭环"）
  - `--retries N`：每镜失败自动重试 N 次（默认 2），仍失败记 FAIL
  - 视频产物命名 `clips/clip_{shot_id}.mp4`（与手动流程及 `postprocess concat` 的 `clip_*.mp4` 匹配规则一致，可直接拼接）；镜号请补零（shot_01…shot_12），保证拼接顺序正确
  - `--dry-run`：仅打印执行计划（每镜命令 + 输出路径）不实际生成，先核对队列分配
  - 每镜成功所用 provider/key 写入 `shots/batch_run.json`，便于追溯与混合 provider 拼接告警
  - 手动模式：`--pin-key N` 锁定某把 key 单跑（多 worker 队列不得重叠）
  - 加新 key / 新 provider：见"安全"节的**扩容指南**
- **断点续跑**：clip 文件已存在且非空则跳过

**降级路径（mode=hybrid / stills，见 Step 1 问④）**：
- **hybrid**：重点镜照常 i2v；过场镜不出视频，改用单张缓推补齐（逐镜跑）：
  `python scripts/postprocess.py kenburns shots/shot_03.png clips/clip_03.mp4 --duration 5`
- **stills**：全量批量缓推（0 API，断点续跑，文件名排序即镜序）：
  `python scripts/postprocess.py kenburns-all shots/ --outdir clips/ --duration 5,4,6`
  产出 `clips/clip_NN.mp4` 与主链命名契约一致，后续 concat/混音/字幕流程不变
- 混用提醒：`postprocess concat` 的混合 provider 告警同样适用于真视频/缓推混拼——属预期行为，不是错误

## Step 6 — 后期统一 + 自检

```bash
python scripts/postprocess.py concat clips/ \
  --out final.mp4 \
  --target-res 1280x720 \
  --xfade "0.5,0.5,1.0" \
  --freeze-last 6 \
  --voice vo/vo.mp3  --voice-delay 1.0 \
  --bgm bgm/bgm.mp3  --bgm-db -18  --ambient-db -10 \
  --subtitles subs.json \
  --slogan "文房四宝 · 皖美传承" --slogan-position left
# slogan 默认：楷体 64px 白字+阴影，左侧负空间垂直居中，距结尾 4s 淡入 0.8s
# 可调：--slogan-position bottom（底部居中）/--slogan-fade 秒 / --slogan-at 秒
# 字体自动探测（楷体>雅黑>黑体>宋体），可用环境变量 FFMPEG_FONT 覆盖
# --slogan 等价于在字幕列表末尾追加一条 dur=0（持续到片尾）的字幕
```

后期流程（脚本内，顺序固定）：
1. **concat / xfade 拼接** → `_merged.mp4`
   - `--xfade` 支持逗号列表逐转场指定长度（长度需 = 镜数-1），关键转场可给 1.0s 长溶解
   - **音频链同步**：各 clip 自带的 Agnes 环境音经 `acrossfade` 逐段对齐交叉，**不会丢音轨**
2. **统一规格**：scale(increase) + crop（填满裁切，不出黑边）+ fps=24 + libx264 yuv420p + +faststart
   - 默认 `--target-res 1280x720`：Agnes 1088×832 源仅 1.18x 放大，画质最好且达标
   - 1920×1080 可选（1.77x 放大，画质偏软），比赛只要求 ≥720p
   - `--freeze-last N` 末帧定格（落版余韵，建议 4-6s）
3. **声音混音**（`--voice` / `--bgm` 任一给出才触发）：视频流 copy，只重编码音频，秒级完成
   - 环境音 `-10dB` 垫底 → 旁白 `0dB`（`--voice-delay` 起始延迟）→ BGM `-18dB`（`-stream_loop -1` 自动循环铺满）
   - `amix:normalize=0`：电平完全由 `--*-db` 决定，某路结束不会抬升其余音轨
4. **烧字幕**（`--subtitles` JSON 列表 + `--slogan`）：多段 drawtext 链，淡入淡出由 alpha 表达式控制
5. **自检** → 比赛硬指标

`--subtitles` JSON 格式（`{at}` 出现秒、`{dur}` 持续秒、`{dur:0}`=持续到片尾）：
```json
[
  {"at": 0.6,  "dur": 2.6, "text": "壹 · 器之精", "pos": "center", "size": 54, "fade": 0.8},
  {"at": 14.2, "dur": 2.6, "text": "贰 · 艺之诚", "pos": "center", "size": 54, "fade": 0.8}
]
```
- `pos`：`center`（幕标题）/ `bottom`（对白字幕，默认）/ `left`（中式落版）
- 幕标题时刻按**实际转场后的时间轴**计算：第 k 镜起点 = 前面所有 clip 时长之和 − 转场时长之和

**踩坑记录（勿重蹈）**：
- 一旦显式写 `-map`，必须**同时映射视频和音频**，否则输出会变成纯音轨（画面整段丢失）
- 中文字体路径在 filter 内必须加单引号：`fontfile='C\:/Windows/Fonts/simkai.ttf'`
- drawtext 表达式内的逗号必须转义为 `\,`，否则被当作 filter 分隔符
- `volumedetect` 配 `-ss` 做分段电平测量不可靠（seek 误差），要精确就用 PCM 逐窗计算

**兜底**：某镜视频生成失败时，用关键帧出 Ken Burns 缓推片段顶替（已内置，规格与主链一致）：

```bash
python scripts/postprocess.py kenburns shots/shot_03.png clips/clip_03.mp4 --duration 5
```

**去网关水印**（档案驱动，探测流程见 Step 1 问③；小而静态的角标水印 delogo 边缘插值即可无痕去除）：

```bash
python scripts/delogo_watermark.py final.mp4 --provider <渠道>   # 按 watermark_profiles.json 实测框自动缩放
python scripts/delogo_watermark.py in.mp4 --dry-run           # 只出红框标注 3x 自检图，确认框位不抹除
python scripts/delogo_watermark.py in.mp4 --x 1133 --y 571 --w 54 --h 56   # 手动指定框
# 产出 *_nowm.mp4 + *_nowm_check.png（水印区 3x 自检图，务必目检有无残影/模糊斑）
# 新渠道实测出框坐标后回写 scripts/watermark_profiles.json；动态/大面积水印档案记 fatal（换渠道或上 ProPainter）
```

```bash
python scripts/postprocess.py check final.mp4
```

自检规则（阈值可配置，默认对齐黄山杯公益/商业赛道下限）：
- 分辨率 ≥ `--min-res`（默认 1280×720）✓
- 时长 ≤ `--max-duration`（默认 120s）✓
- 帧率 ≥ `--min-fps`（默认 24）✓
- 否则 exit 2，必须修复。其它赛事：如商业赛道 ≤60s 用 `--max-duration 60`

## 声音设计（VO 稿 → TTS → 字幕 → BGM）

**常见翻车**：成片交付了却发现“没声音、没字幕、画外音也没有”——因为分镜阶段只设计了画面。
**声音必须在 Step 2 分镜时同步规划**，四件事是一条依赖链：

```
VO 旁白稿（约 100-150 字，按幕分句）
   ├─→ TTS 朗读  → media_gen.py tts → vo.mp3
   ├─→ 字幕文案 = 旁白原文（拆句打时间点） → subs.json → --subtitles
   └─→ BGM 垫底（无版权音源，--bgm-db -18）
```

| 环节 | 命令 / 做法 | 备注 |
|---|---|---|
| ① 写 VO 稿 | 对话模型按分镜主线写，每句对应具体镜号，存 `vo/vo_lines.json` | 旁白是治“画面散”的特效药：声音把镜头串成线 |
| ② 分句合成 | `python scripts/vo_build.py vo/vo_lines.json --out vo/vo.m4a --total 55.94` | **按句** TTS，读回真实时长后 `adelay` 精确落位 |
| ③ 字幕自动打轴 | 同上，脚本直接产出 `subtitles_final.json` | 与朗读 100% 对齐，不会出现“字幕和声音对不上” |
| ④ 混音挂 BGM | `postprocess.py concat ... --voice vo/vo.m4a --bgm bgm.mp3` | 环境音默认 -10dB 垫底，不抢人声 |

### 文案怎么来：**AI 批量撒网 → 人工筛选改写**（不要自己硬写）

```bash
python scripts/copy.py --brief "文房四宝公益广告，匠人手艺" \
  --shots shots_list.txt --count 20 --out candidates.txt
# → 20 条候选 → 对话模型筛选改写 → 定稿进 vo/vo_lines.json
```

**实测结论：约束 > 模型。** 同一个 agnes-2.5-flash：
- "随便写" → 全是空话（"传承千年文脉""守护中华文化根魂"，还会混进英文、自加"创作思路"）
- 加 7 条硬约束 → "笔挂起来，比握在手里活得长""手上有茧，笔下才有根"

`copy.py` 内置的就是那套验证过的约束：① 绝不描述画面 ② 优先具体细节/数字 ③ ≤15 字短句
④ 空话黑名单 ⑤ "传承/匠心"可用但必须搭配具体细节（不许单独成句）
⑥ **数字必须可核实**（AI 会编"磨一百二十下""阴干六十天"这类假工艺参数，内行一眼看穿）
⑦ few-shot 参考语气。

**筛选时必做**：剔除疑似编造数字的句子；保留有反常识细节的（往往比人写的更有味道）。

### ⚠️ 文案第一原则：字幕不做画面解说，只抛主张

**画面负责证明，字幕负责下结论。** 写"一滴水，落进砚池"是废话——观众自己看得见。
要把"你看见了什么"改写成"**所以呢**"：下判断、给数字、抛态度。

| 画面 | ❌ 描述式（解说画面） | ✅ 主张式（广告文案） |
|---|---|---|
| 水入砚池 | 一滴水，落进砚池。 | 慢，是一种功夫。 |
| 匠人研墨的手 | 一双手，把它磨成了墨。 | 这双手，磨了四十年。 |
| 捞纸 | 一帘清水，捞起能活千年的纸。 | 一张宣纸，能活一千年。 |
| 笔尖悬滴 | 最后一滴，悬在笔尖，将落未落。 | 千年的功夫，都在这一滴里。 |

**自检**：写完每句问一句"这是观众已经看见的吗？"——是，就重写。
**有效手法**：给数字（四十年/百遍/一千年）比堆形容词有说服力；公益片留白 50% 左右，话说满就没余韵。

`vo_lines.json` 结构（`at` = 目标起始秒，按**转场后的实际时间轴**填）：
```json
{"acts": [{"at":0.6,"dur":2.6,"text":"壹 · 一水生墨","pos":"center","size":54}],
 "lines":[{"id":"L01","shot":"S01","at":0.8,"text":"一滴水，落进砚池。"}]}
```

**为什么必须分句合成**（`vo_build.py` 的存在理由）：
整段 TTS 只有一个总时长，每句落在哪一秒无法控制，字幕只能靠“字数 ÷ 语速”估算，结果必然对不齐。
分句方案每句独立成音、读回真实时长、按 `at` 用 `adelay` 落位，字幕时长直接取音频真实值。

```bash
# 有 TTS key：分句合成 + 拼接 + 打轴（一条命令）
python scripts/vo_build.py vo/vo_lines.json --out vo/vo.m4a --total 55.94
# 用户自录：录 11 条存 vo/lines/L01.wav… 然后只拼接打轴
python scripts/vo_build.py vo/vo_lines.json --out vo/vo.m4a --total 55.94 --skip-tts
# 某句太长挤到下一句：--auto-shift 自动顺延后续句子（--gap 0.3 控制间隔）
```
脚本会做：越界检查（实际时长 > 到下一句的间隔则警告）→ 静音底铺满总长 → 各句精确落位 →
输出 `subtitles_final.json` → 报告有声/留白占比（公益片留白 40-50% 较合适）。

**TTS 未配置 key 时**脚本会明确报错并给出配置指引（不是静默失败）。免费渠道优先级：
1. Gitee AI 的 Spark-TTS / CosyVoice（免费额度，需新 key）
2. 用户自录（版权零风险，比赛最稳）
3. 智谱 CogTTS（需付费资源包，用前先确认用户愿意花）

**BGM 版权**：必须无版权或明确可商用；用户提供音源，agent 不替用户担保曲目授权。

## QC 闭环（生成后验收：让 prompt 优化从开环变闭环）

只写不验 = 抽卡。每段视频生成后必须验收，不合格按诊断表**单变量重拍**：

```bash
# 单段抽首/中/尾 3 帧
python scripts/media_gen.py qc clips/clip_05.mp4 qc_frames/
# 或批量生成时自动抽（推荐）
python scripts/media_gen.py batch shots60/ --phase videos --workers 2 --qc
```

**验收标准**（逐条对照 shot JSON）：
1. 首帧是否复现 t2i 关键帧的构图/主体外观（漂移说明 i2v 没吃住首帧）
2. 中帧是否在执行 i2v_prompt 描述的**那一个**动作（执行了别的 = 运动指令被误读）
3. 尾帧是否有崩坏（手部畸形、面部扭曲、物体穿模、画面闪断）
4. 与上一镜的连续性（ledger 里的 immutable 不变量是否保持）

**单变量重拍**：一次只改一个变量，否则不知道哪改动起效——
先换 prompt 措辞 → 再调运动幅度 → 再换首帧（`--count` 重出选优）→ 最后才换 provider。

> 注：当前对话模型不支持读图时，`qc` 抽出的帧交用户目视，或由支持视觉的模型验收；
> 抽帧动作本身已固化在流程里，不会因为“看不了”而跳过这一步。

## Step 7 — 交付

- `final.mp4`（比赛片）+ `shots/`（关键帧）+ `clips/`（原始视频）+ `state.json`（过程记录）
- 用 `present_files` 展示 final.mp4
- 附分镜表（每镜的英文 prompt + i2v prompt + 实际选择）

---

## 失败处理（v2.1）

| 失败 | 处理 |
|---|---|
| 401/403 | key 永久黑名单 24h，自动换下一个 |
| 429 | key 冷却 60s 自动换；同池全部 key 冷却/失败 → 单命令按 `MEDIA_PRIORITY` 自动跨池兜底（batch 仅单池，会记 FAIL） |
| 5xx / 超时 | 指数退避重试 3 次 |
| 内容审核拒绝（ProviderFatal） | **不换 key、不降级**——换家也一样拒，应改 prompt |
| 视频轮询超时 10min | 提示手动用 video_id 查 |
| Agnes 视频整体不可用 | 切智谱 CogVideoX-Flash（`--provider zhipu`，原生 1080p）；再不行 Ken Burns |
| 智谱/魔塔返回带水印 | 不入正片，作概念图；正片用 Agnes 出图（魔塔 edit 输出无水印，可直接用） |
| TTS 报“未配置” | 按指引补 `MEDIA_TTS_1_KEY/_BASE/_MODEL`（见声音设计节）；或改用用户自录 |
| 成片无声 | 检查 concat 是否走了 xfade 分支且各 clip 自带音轨；`-map` 必须同时映射视频+音频 |
| 成片只有音轨没画面 | 字幕步骤漏了 `-map 0:v`（显式 map 一旦出现，未列出的流全部丢弃） |

### 按症状诊断（fail 诊断杠杆，源自 seedance-2.0）

生成"成功"但画面烂时，按症状对症下药；**每次只改一个变量**（单变量重拍，framework §8）：

| 症状 | 第一杠杆 | 第二杠杆 |
|---|---|---|
| 手/脸崩坏 | 降运动幅度（"slowly"加码）；手别当画面焦点 | negative 已有 warped hands；换景别避开手部特写 |
| 画面抖动/闪烁 | i2v prompt 删掉 camera 移动（locked camera） | 降事件数到 1 |
| 主体变形/漂移 | i2v prompt 补 "the camera holds still, the subject stays sharp in place" | 重出首帧（构图更稳的） |
| 运动幅度过大（瞬移） | 加 "very slowly, gradually, unhurried" | num_frames 降档（121→65） |
| 运动幅度过小（几乎不动） | i2v 动词换强动词（grind/lift/bloom） | 检查首帧是否本身太"满"（构图密=没空间动） |
| 风格跨镜漂移 | 核对 ledger.immutable 是否每镜逐词重复 | 该镜重出首帧 |
| 审核拒绝 | 改 prompt（移除疑似敏感词），**不换 provider** | — |

## 成本与时间预期

| 规格 | 镜头 | 图片调用 | 视频调用 | 串行耗时 | 双 key 并行 | 三 key 并行 |
|------|------|---------|---------|---------|------------|------------|
| 30s 快版 | 5 | 5×3（选优） | 5 | 30-40 分钟 | ~20 分钟 | ~13 分钟 |
| **60s 主打档**（推荐） | 10-14 | 10-14×3 | 10-14 | 60-90 分钟 | ~35-50 分钟 | ~25-35 分钟 |
| 90-110s 长版 | 16-20 | 16-20×3 | 16-20 | 100-150 分钟 | ~60-80 分钟 | ~40-55 分钟 |

实测参考（11 镜公益片）：关键帧 1m56s（全成），视频 19m25s（串行，约 2 分钟/镜），后期 1-2 分钟。12 镜商业片双 key 实测：出图 1m33s、视频 ~15 分钟（含一次 429 隔离重试）。

全程走免费额度，不消耗 WorkBuddy credits。

## 安全

- Key 存 `~/.workbuddy/media_keys.env`（旧 `agnes_key.env` 自动兼容）
- 变量命名 `MEDIA_<PROVIDER>_<n>_KEY`
- **永不打印、永不写产物、永不进 git**
- 脚本输出时只显示 `sk-L***Slyr` 形式
- state 文件 `~/.workbuddy/.media_state.json` 不含 key，只含冷却时间与统计（已加入 .gitignore）
- ffmpeg 由 `scripts/ffmpeg_probe.py` 跨平台自动探测（环境变量 FFMPEG > PATH > imageio_ffmpeg > 托管路径），无需硬编码绝对路径
- 密钥模板见仓库 `media_keys.env.example`，复制改名后填真实 key（切勿提交真实 key）

## 扩容指南（多 key / 新 provider 接口）

**① 加同 provider 的更多 key**（如第 4、5 把 Agnes key）——只改 `~/.workbuddy/media_keys.env`，脚本零改动：

```bash
export MEDIA_AGNES_4_KEY="sk-..."
export MEDIA_AGNES_4_BASE="https://apihub.agnes-ai.com/v1"   # 每把 key 独立 _BASE/_POLL，可混用不同域名
export MEDIA_AGNES_4_POLL="https://apihub.agnes-ai.com"
export MEDIA_AGNES_4_ROLES="image,video"   # 可选：缺省一条龙；分工示例 "image"
export MEDIA_AGNES_4_IMAGE_MODEL="..."     # 可选：该 key 单独换模型（零改码）
```

- 之后 `batch --workers 4` 即四发并行（workers ≤ 承担该阶段角色的 key 数，超了直接报错）
- key 池按 `_1_ _2_ _3_...` **序号连续枚举，遇缺号即停**——序号必须连续，不能跳号
- 改完跑 `python scripts/media_gen.py status` 验证全部加载（非默认的角色/模型覆盖会显示在行尾）

**② 接入新的生图/生视频 provider**——**首选通用池（零改码）**：

1. env 里加 `MEDIA_CUSTOM_1_KEY` / `_BASE`（OpenAI 兼容 base，`/v1` 结尾）+ **必填** `MEDIA_CUSTOM_1_IMAGE_MODEL` / `MEDIA_CUSTOM_1_VIDEO_MODEL`（接哪个能力填哪个，`_ROLES` 同步声明）
2. 可选：`MEDIA_CUSTOM_1_IMAGE_SIZES="1024x1024,..."` 自填白名单（超出仅警告）；视频默认按异步任务轮询风（`/videos/generations` + path 轮询），Sora 风渠道用 `_TASK_PATH=/_videos` 改
3. 跑 `status`（自动 /models 探测能力）→ probe 一张图/一段视频目检 → 按 `references/prompt_styles.md` custom 卡沉淀口味
4. 多把 custom key：`MEDIA_CUSTOM_2_...` 递增即可，轮转/熔断/节流/批量全套自动继承

> 想把某渠道做成一等公民（带实测 size 白名单/专用 payload）：再在 `media_gen.py` 顶部 `PROVIDERS` 表加条目——**照抄 agnes 条目改参数**，多 key 轮转 / 熔断 / per-key 节流 / 跨池兜底 / QC / 断点续跑全套自动继承；并把它加进 `MEDIA_PRIORITY`（不加入则 `--provider <pool>` 显式使用）。定制条目 = 沉淀实测参数，通用池 = 快速试接，两者不冲突

**③ 验证**：`status` 看加载；单 key 冒烟测试用 `--pin-key N`（如 `image --pin-key 3`），不扰动其它 key 的冷却计时。
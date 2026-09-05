# Prompt 口味卡（v2.7）—— 通用骨架 × 三张卡

> **用法**：SKILL.md Step 3 写 prompt 时，先用通用骨架（`prompt-framework.md` 的六要素/原子字段）把分镜的
> 导演意图组织好，再按目标池查下面的卡，把**写法**（句式/语言/长度/质量词/忌讳）调成该模型受用的样子。
> **骨架保证"想得对"，口味卡保证"喂得对"**——两者分离，接任何新渠道都不用重学导演思维。
>
> **三张卡**：卡一·轻量卡（弱模型够用）｜卡二·高规格卡（视频大模型吃得下细的）｜卡三·custom 四档（档位用户自选，落 `_TIER`）。
>
> **踩坑回写**：某池 QC 重拍时诊断出"某种写法总翻车"，就把一行经验追加到对应卡片末尾（标注日期）。
> custom 池刚接上按卡三起步，跑两镜即可沉淀出自己的专属卡。

---

## 卡一 · 轻量卡（agnes / zhipu / qwen-edit / custom 中低档 共用）

适合"写得简单就够好"的模型：轻量开源系、免费档、图编辑类。
按三种活对号入座，**主写法 + 差异注记**：

### 活 A · 文生图（agnes-image-2.x / zhipu CogView / custom 文生图）

- **主写法（agnes）**：英文自然散文段，六要素顺序展开（主体→动作→场景→光线→氛围→构图）
  - 质量词 `masterpiece, best quality, ultra-detailed` 适度点缀（2-3 个），堆多了反而抢戏
  - 长度 80-150 词为宜；超过 ~250 词细节权重被稀释，画面开始"各画各的"
  - 负面词表支持但作用有限，重点信息全放正向描述里
- **zhipu CogView 注记**：中英皆可、中文理解好；一句话一个要素，50-100 字，过长画面发糊；
  免费档右下角带水印 → 产物只作风格参考/概念验证，不入正片
- **Qwen-Image 注记**（custom 池，魔搭）：质量高于轻量系；中英皆可、吃长描述；
  **画面要出现文字就把文字内容用引号写进 prompt**（中文文字渲染是招牌，标题卡/字幕卡首选）；
  异步任务式出图（单张 20-40s，额度 500 次/天）→ 重点镜/标题卡用它，批量过场分镜仍走 agnes

### 活 A+ · 文生图加强写法（Qwen-Image 等强力文生图共用）

强者喂细的——词数花在细节上，不花在质量词上：

- 主体细节写满：材质/颜色/状态/姿态/表情
- 光线：时间 + 光源 + 方向 + 色温（"清晨侧逆光，暖金色"）
- 镜头：焦段 + 景别 + 景深（"85mm，中景，浅景深"）
- 构图：视角 + 层次（"低角度仰拍，前景书本、中景人物、远景黑板"）
- 长度不设上限：100-200 词也吃得下（轻量系 80-150 词的约束不适用）
- 画面文字：引号原样写入（"信息素养大赛"）
- 质量词零堆砌（masterpiece 之类全省，省下词数给细节）

### 活 B · 图编辑（modelscope Qwen-Image-Edit / custom 图编辑类）

- **指令式**：`把 X 改成 Y，其他保持不变`——一次只给一个改动
  （它是**改图**不是画图：写成场景描述会整个重绘，改图就失效了）
- 中英皆可；多图编辑可搬元素（"把图2的衣服换到图1的人身上"）

### 活 C · 视频（agnes-video-v2.0 / zhipu CogVideoX / custom 中低档视频）

- **主写法（agnes-video）**：首帧内容已定，prompt 只写**动作与镜头运动**（谁做什么、镜头怎么走）
  - 英文 40-80 词；动作分阶段写（"先…然后…"）比一段流更适合时序控制
  - 忌讳：不要重复描述首帧已有的静态外观（浪费权重）；num_frames 只接受 8n+1
- **zhipu CogVideoX 注记**：中文 prompt 常更好；简洁结构（主体+动作+镜头）；
  prompt 过长会被**截断而非报错**，注意核对成片；别塞英文电影黑话（dolly zoom 之类不一定吃）；
  size/duration 仅 i2v 支持（5s/10s）

---

## 卡二 · 高规格卡（sora / kling / 海螺 / seedance 级视频大模型）

这类模型吃得下细的提示词——**逐字段写全**，别用轻量卡应付（浪费模型能力）。

### 字段模板（英文 100-160 词，逐字段写）

| 字段 | 写什么 |
|---|---|
| Subject | 谁/什么 + 外观细节（1-2 句，材质/颜色/状态） |
| Action | 5-10 秒能演完的一个具体动作，按时间先后分步 |
| Scene | 地点 + 背景层次（前景/中景/远景） |
| Camera | 景别（close-up/medium/wide）+ 运镜（slow push-in / handheld / aerial）+ 可选焦段（35mm/85mm） |
| Style | 质感与影调（cinematic, natural light, film grain 之类 2-3 个） |
| Ambiance | 时间 + 光源 + 情绪氛围 |
| Audio | 环境音 + 音效 + 对白（引号原句 + 说话人） |

### 完整范例（清晨渔港，10 秒单镜）

> Subject: A weathered old fisherman in a faded blue raincoat coiling a thick rope
> on the deck of a small wooden trawler. Action: he pulls the rope hand over hand,
> then pauses and looks up toward the horizon. Scene: a misty harbor at dawn, other
> fishing boats and gulls soft-focus in the background, wooden pier planks in the
> foreground. Camera: medium shot, slow push-in from a slight low angle, 35mm lens,
> shallow depth of field. Style: cinematic, natural light, subtle film grain.
> Ambiance: cool blue dawn light breaking into soft gold, calm and hopeful.
> Audio: water lapping against the hull, distant gull cries, creak of the boat;
> he mutters "big catch today" in a low gravelly voice.

### 通用忌讳（大模型级都适用）

- **忌双义词**：kite（风筝/红鸢）教训——主体名词有歧义就补限定词（"a red kite toy on a string"）
- 单镜 5-10 秒别写镜头切换要求，切换是分镜层的事
- 不要堆轻量卡式的质量词（masterpiece 之类）——这个级别模型不需要，堆了占词数

### 平台注记

- **Audio 必写**：带原生音频是这类模型的招牌，环境音/音效/对白至少写全环境音
- 品牌名/真人名零出现，审核拒 = ProviderFatal（不换 key，改词）

---

## 卡三 · custom 四档（档位由用户自选，落 env `_TIER`）

### 第一原则：配比反比（全卡总纲，四档都是它曲线上的取样点）

**出片质量 = 模型能力 × 提示词质量**，两者分工**反着配**：

- 模型越强 → 画面可以越复杂，提示词越要**留白**（过度指定束缚大模型发挥，出片呆板）
- 模型越弱 → 画面必须越简单，提示词越要**细节拉满**（弱模型不会自己补全，没写的维度它就随机生成——随机就是违和感来源）

细则：①弱模型的"细腻"必须**具象参数化**（光位/材质/景别/运动速度/色温，像摄影师填拍摄单），抽象情绪词（氛围/诗意/电影感）对它是噪声；②细节写进**前 100 词**（文本编码器有截断上限），总长守各档甜蜜点。

### 问档话术（给用户看的，必须大白话）

档位由**用户自选**（默认不评估）；agent 只在用户点"帮我判断"时才评估（探测+模型知识+必要时试跑一发）。选完落 env，此后不再重复问。

> 问：你接的这个模型，你自己感觉算哪一档？
> - **顶级**——效果接近大片，一般是收费的旗舰 API（如 sora 这类视频旗舰）
> - **主流**——效果不错、收费的商业模型（如可灵、海螺、即梦）
> - **开源中等**——能看，但画面一复杂就容易崩（如 LTX 13B、CogVideoX-5B）
> - **入门/受限**——只适合简单画面，提示词得写很细帮它兑底（如自己电脑小显卡跑的小模型）
> - 帮我判断 ← 让 agent 评估

**话术总原则**：一切要用户选的东西（池名/档位/模式）都按此标准——池名报出来要带一句"是什么"（如"custom 池 = 你自己接入的任意模型"）；术语（_ROLES/_TIER/CFG）只写进 env/plan，不说给用户。

### 四档差异表（agent 写 prompt 时对号入座）

| 档 | `_TIER` | 典型模型 | 画面复杂度 | 细节密度 | 写法 |
|---|---|---|---|---|---|
| 顶级 | ultra | sora / kling / 海螺级 | 自由（多主体、复杂交互、8-12s） | 低-中 | 照卡二七字段，留白让模型发挥 |
| 主流 | high | 可灵 / 海螺 / seedance / Wan 14B / Qwen-Image(图) | 中高（主体 ≤3） | 中 | 卡二骨架收敛，禁画面内文字 |
| 开源中等 | mid | LTX 13B / CogVideoX-5B / Wan 1.3B | 中（单主体为主） | 中高 | 英文流水单段，具象细节多写，少留白 |
| 入门/受限 | low | 2B 蒸馏量化 / 本地小显存（LTX 2B fp8） | 极简（单主体单动作） | 拉满 | 零留白全参数化，见低档细则 |

### 低档细则（受限算力专项，源自 LTX 2B fp8 实测）

- 单焦点：1 主体 + 1 动作 + 1 种运镜，多主体必粘连
- 必崩区禁入：抽象词、否定词（蒸馏版 CFG 1.0-3.5，负向几乎无效）、画面内文字、精确计数、多人交互、剧烈物理（流体/火焰/布料/碰撞）
- 英文单段流水 60-120 词，细节写前 100 词
- 镜头语言要"可拍摄"：写 slow push-in 别写 cinematic
- 单镜 ≤5s，短镜快切 + 后期调色/转场盖瑕疵
- **优先 i2v**：起始帧用高档模型出图锁定外观，视频模型只做运动插值——受限算力的最优杠杆
- 固定 seed + 全片复用同一套描述措辞，一致性更好

### 中档细则

- 卡一活 C 为基础：具象细节写满（光线/材质/景别），但允许模型少量自主发挥
- 负向提示有效可用；英文优先（T5 系），国产模型中英各 probe 一次定夺
- 单镜 3-5s，事件 ≤1

### 通用流程（各档通用）

- 首片先 probe 一张/一段目检：画风、水印、分辨率、prompt 耐受度
- 踩坑回写：`[日期] 现象 → 调整`；两三轮后把本节替换成该渠道的专属卡
- `status` 的 /models 能力探测是**猜的**，口味以实跑为准

---

### 实测卡 · ltx-video（本地网关 LTXBridge，2026-09-05 沉淀）

- 接入：`MEDIA_CUSTOM_2_KEY=local`、`_BASE=http://127.0.0.1:8000/v1`、`_VIDEO_MODEL=ltx-video`、`_VIDEO_TASK_PATH=/videos`、`_VIDEO_PROMPT_FIELD=input`
- payload `{"model","input","image":dataURI}`，POST `/v1/videos`，**同步响应约 60s**（body 含 `url`（相对路径）+ `local_path`；media_gen 走 url 分支直接下载，无需轮询）
- 输出**固定 768×512 (3:2) / 24.39fps / 3-4s**（74-97 帧浮动）；`num_frames/width/height/duration` 一律被忽略；纯 t2v（无 image）返回空响应，**必须带首帧**
- 产物为动画 WebP 且 **Exif 损坏**（ffmpeg 报 invalid TIFF header），ffmpeg 直接转码失败——用 Pillow 解帧→ffmpeg 编码（工作区 tools/webp2mp4.py 有现成实现；临时帧目录放系统 temp，批量删除可能被安全机制拦截需容错）
- **i2v 不锁首帧像素**：把首帧当构图/内容参考，按 prompt 重绘成写实风 → 首帧务必用写实摄影风出图（agnes 卡一·活A photorealistic），不要给扁平插画/色块图
- 无水印（本地模型）；画质中档，768×512 裁 16:9 放大 1280×720 达标偏软
- prompt 喂法：照卡一·活C（短句、强动词、慢速控制词、单事件、locked camera 稳画面）

## 附：模型名 → 卡速查（status 探测的关键字即按此设计）

| 关键字命中 | 大概率能力 | 用哪张卡 |
|---|---|---|
| image / flux / dall / kolors / cogview / seedream | 文生图 | 卡一·活 A |
| edit（Qwen-Image-Edit 类） | 图编辑 | 卡一·活 B |
| video / sora / kling / wan / cogvideo | 视频 | sora/kling/hailuo/seedance 级 → 卡二；agnes/cogvideo 级 → 卡一·活 C |
| tts / speech / audio / cosyvoice | 语音合成 | （TTS 走 media_gen tts 子命令） |
| 以上都不命中 | 大概率纯文字模型 | 本流水线用不上，报告会明说 |

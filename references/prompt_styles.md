# Prompt 口味卡（v2.7）—— 通用骨架 × 三张卡

> **用法**：SKILL.md Step 3 写 prompt 时，先用通用骨架（`prompt-framework.md` 的六要素/原子字段）把分镜的
> 导演意图组织好，再按目标池查下面的卡，把**写法**（句式/语言/长度/质量词/忌讳）调成该模型受用的样子。
> **骨架保证"想得对"，口味卡保证"喂得对"**——两者分离，接任何新渠道都不用重学导演思维。
>
> **三张卡**：卡一·轻量卡（弱模型够用）｜卡二·高规格卡（视频大模型吃得下细的）｜卡三·custom 两档（按模型实力选）。
>
> **踩坑回写**：某池 QC 重拍时诊断出"某种写法总翻车"，就把一行经验追加到对应卡片末尾（标注日期）。
> custom 池刚接上按卡三起步，跑两镜即可沉淀出自己的专属卡。

---

## 卡一 · 轻量卡（agnes / zhipu / qwen-edit / custom-轻档 共用）

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

### 活 C · 视频（agnes-video-v2.0 / zhipu CogVideoX / custom 视频轻档）

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

## 卡三 · custom 两档（按接的模型实力选，判断不了就问用户）

### 选档规则（agent 在 Step 3 前先做，不猜）

- 模型名命中 **sora / kling / hailuo / seedance / wan** 等闭源或头部大模型 → **强档**
- 开源小模型、中转站杂牌、看不出深浅 → **轻档**（起步安全，翻车不心疼）
- 仍判断不了 → 直接问用户一句"你接的模型是什么级别？"，由用户定

### 轻档

- 写法**照抄卡一**（文生图/图编辑/视频对号入座），size 等参数以渠道文档为准（`_IMAGE_SIZES` 可自填白名单）
- 首片先 probe 一张/一段目检：画风、水印、分辨率、对 prompt 长度的耐受度

### 强档

- 写法**照抄卡二**字段模板，另注意两点差异：
  - **语言跟渠道走**：国内中转/国产大模型中文常更稳，先中英各 probe 一次定夺
  - **参数先小样试**：时长/分辨率/是否真支持 Audio 字段，probe 一段再上正片
- 踩坑回写：`[日期] 现象 → 调整`；两三轮后把本节替换成该渠道的专属卡
- 提示：`status` 的 /models 能力探测是**猜的**，口味以实跑为准

---

## 附：模型名 → 卡速查（status 探测的关键字即按此设计）

| 关键字命中 | 大概率能力 | 用哪张卡 |
|---|---|---|
| image / flux / dall / kolors / cogview / seedream | 文生图 | 卡一·活 A |
| edit（Qwen-Image-Edit 类） | 图编辑 | 卡一·活 B |
| video / sora / kling / wan / cogvideo | 视频 | sora/kling/hailuo/seedance 级 → 卡二；agnes/cogvideo 级 → 卡一·活 C |
| tts / speech / audio / cosyvoice | 语音合成 | （TTS 走 media_gen tts 子命令） |
| 以上都不命中 | 大概率纯文字模型 | 本流水线用不上，报告会明说 |

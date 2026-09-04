# 提示词框架（v2.4 · 融入 seedance-2.0 导演方法论 + 原子化双轨）

> 方法论来源：Emily2040/seedance-2.0 的导演层设计（Director's Read / anti-slop / continuity ledger / 事件密度），
> 与本 skill 的工程层（多 key/熔断/断点续跑）互补。
>
> **本文档 = A 轨（散文六要素，服务叙事镜头）**。
> 需要"可控/可复现"的设计类图（海报、信息图、产品图、角色设定、版式）走 **B 轨（原子化 schema）**，
> 见 `references/prompt-atomic-schema.md`。选轨规则见 §10。

## 第 0 层 — Director's Read（写 prompt 之前必须先回答）

**"Direct the scene, don't decorate it."** 每个 prompt 之前，先用一句话回答：

| 问题 | 示例（文房四宝 shot_03） |
|---|---|
| 这个镜头在故事里**为什么存在**？ | 承上启下：墨已研好，笔提起——把"工具展示"推向"创作发生" |
| 情绪**转折点**是什么？ | 从静到悬：一滴墨将落未落，屏息 |
| 观众看完这镜应该**感到什么**？ | 紧张的期待 |
| **一个可见的压抑/克制动作**是什么？ | 笔提起但墨滴不落——克制制造张力 |

- 回答不了的镜头 = 不该存在的镜头，砍掉或合并。
- 实用型/包装型/功能型镜头（如纯产品展示）走单独通道，**不要编造戏剧**——硬加"情感冲突"是 AI 味的主要来源。
- 把 Read 结论写进 shot JSON 的新字段 `dramatic_function`（一句话），落版镜可以是 "closing resolution"。

## 1. 六要素结构 + 顺序即优先级

```
[SCENE/环境] + [SUBJECT/主体与外观] + [ACTION/主动作] + [CAMERA/景别+运镜] + [LIGHT/光影色调] + [STYLE/风格氛围]
```

**顺序即方向**：模型对 prompt 开头的词权重最高。
- t2i：**主体和动作放开头**（"Extreme close-up of a traditional Chinese She inkstone..."），相机/光/风格殿后
- i2v：**运动放最前**（"The ink stick begins to grind in a slow circular motion..."），镜头行为其次
- 不要用形容词开头（"Beautiful extreme close-up..."）——形容词是装饰，不是方向。

其余写作规则：
1. **散文体，不用关键词堆砌**。120-220 词为宜（太短没细节，太长模型丢要素）。
2. **主体描述具体化**：不要 "a student"，要 "a young woman with short black hair in an oversized blue university hoodie"。
3. **光影具体化**：不要 "nice light"，要 "soft morning light through sheer curtains, warm 4500K"。
4. **一致性**：所有镜头重复相同的角色外貌锚点词和统一色板词（见 §3 连续性分类账）。

## 2. 事件密度防火墙（每镜 1-2 个事件）

**一个 5s 镜头最多承载 1-2 个事件**（事件 = 主体的一个状态变化）。

- ✅ "墨锭缓慢研磨，墨色在水中旋开" —— 1 个复合事件，OK
- ❌ "墨锭研磨、水变黑、笔提起、墨滴滴落、镜头拉远" —— 5 个事件，模型必然崩（运动糊成一团或漏做）
- 超了就**拆镜**，不要塞。宁多一镜，不少事件。

## 3. 一致性与连续性分类账（continuity ledger）

跨镜一致性分两层管理，写在 `shots/ledger.json`：

```json
{
  "immutable": {
    "palette": "muted ink-black and warm cream palette with soft teal-grey accents",
    "light": "warm 4500K soft diffused studio light",
    "style": "ultra-detailed macro product cinematography, refined Chinese literati aesthetic",
    "props": {
      "inkstone": "dark slate She inkstone with fine spiral carvings",
      "ink_stick": "black lacquered Huizhou ink stick, embossed golden dragon relief"
    }
  },
  "transient_state": [
    { "after": "shot_01", "state": "water in the inkstone half-darkened; the ink stick's grinding end is wet" },
    { "after": "shot_03", "state": "one droplet has fallen from the brush tip" }
  ]
}
```

规则：
- **immutable**（色板/光源/风格/道具外观）：每镜 prompt **强制重复**，一词不改。
- **transient_state**（世界状态）：AI **不会自己记住**"墨锭已经磨了一半"。同场景连续镜头、末帧链镜头的 prompt 必须把上一镜结束时的状态写进去（"the water in the inkstone is now dark with fresh ink"）。
- 跨物体切换镜头（xfade 衔接）可豁免 transient，但 immutable 永不豁免。

## 4. 景别与运镜词库

| 景别 | 英文 | 用途 |
|------|------|------|
| 大特写 | extreme close-up (face/hands/eyes only) | 情绪爆发、细节 |
| 特写 | close-up | 表情、小物件 |
| 中景 | medium shot (waist up) | 人物叙事主力 |
| 全景 | full shot / wide shot | 环境+人物 |
| 大远景 | extreme wide / establishing shot | 开场、收尾 |

| 运镜 | 英文 | 用途 |
|------|------|------|
| 推近 | slow dolly in / slow push-in | 聚焦、压迫感 |
| 拉远 | pull back / dolly out | 揭示环境、收尾 |
| 横移 | slow pan left / right | 巡视环境 |
| 跟拍 | tracking shot following the subject | 行走、运动 |
| 环绕 | orbit shot around the subject | 产品展示、英雄时刻 |
| 固定 | static shot, locked camera | 稳定叙事、对话 |
| 升/降 | slow crane up / down | 开场气势、收束 |

## 5. 图生视频（I2V）专用规则

首帧图片已锁定主体/构图/色彩，视频 prompt **只写三样**（40-80 词）：
1. **运动放最前**：主体动什么、怎么动（"she lifts her head slowly and smiles"）
2. 运镜：camera behavior（"camera holds still" / "camera drifts slowly forward"）
3. 氛围微变化：光线/粒子/衣物摆动（"morning dust floats in a sunbeam"）

**不要**在 I2V prompt 里重写主体外观、构图、配色——会打架。

**末帧链附加规则（observed end state）**：模型经常不按 prompt 预期结尾。下一镜的 i2v prompt 必须**基于实际生成内容**写——先看 clip 末帧描述实际发生了什么，再据实写下一镜（"the brush is fully lifted, the droplet still clinging"），不要照抄原计划。

## 6. 负向提示词（negative_prompt，视频 API 支持时填写）
固定模板，按需追加：
`blurry, distorted faces, warped hands, extra limbs, text artifacts, watermark, camera shake jitter, flickering, plastic skin, oversaturated`

## 7. anti-slop：禁用空泛词

写 prompt 时对照 `references/anti-slop-lexicon.md` 替换表。核心原则：
- **删除一切"评价性形容词"**（beautiful/stunning/epic/emotional/artistic）——它们不产生画面，只产生 AI 味。
- **用具体画面元素替代情绪词**："calm mood" → "dust motes drift unhurried through the light beam"。
- 保留**有效技术词**（macro / shallow depth of field / 4500K / rim light）——这些是摄影词汇，模型真正理解。

## 8. 重拍规范（选优 + 单变量控制）

两层配合使用：
1. **--count 3 批量选优**（首拍）：同 prompt 出 3 张，人工挑。随机种子不同，构图/光影有差异。
2. **单变量重拍**（重roll 已有镜头时）：一次只改**一个**变量——改了 prompt 就不动首帧，换了首帧就不动参数。同时改多个 = 永远不知道哪个改动起效，浪费额度。
3. 重拍前先看旧产物**具体哪里不对**（构图？光影？运动幅度？），对症下药，不要"感觉不对就重roll"。

## 9. 扩写前自查清单（每次出 prompt 前过一遍）

- [ ] Director's Read 有了？dramatic_function 一句话写进 JSON 了？
- [ ] 全英文？
- [ ] **主体+动作在 prompt 开头**？（顺序即优先级）
- [ ] 6 要素齐全？（环境/主体/动作/运镜/光影/风格）
- [ ] 事件数 ≤2？
- [ ] 角色锚点词/色板词与 ledger.immutable 完全一致？
- [ ] 同场景镜头写入了上一镜的 transient 状态？
- [ ] 光影写了色温或方向？
- [ ] 无 slop 词？（对照 anti-slop-lexicon）
- [ ] I2V prompt 没有重写首帧内容？
- [ ] 无敏感内容（真人肖像/暴力/品牌 Logo）？

## 10. 选轨：A 散文六要素 vs B 原子 schema

| 判断 | 走哪轨 |
|---|---|
| 这一镜要讲故事 / 有运动 / 是分镜或视频镜头 | **A 轨 = 本文档**（散文六要素） |
| 这一图要多次复现 / 换变量重出 / 交付他人改 | **B 轨 = `prompt-atomic-schema.md`**（填字段 → 编译 prompt） |
| 海报 / 信息图 / 产品图 / 角色设定表 / 版式 | **B 轨** |
| 拿不准 | 先 A 轨出概念，`--count 3` 选优后按 B 轨把参数固化到 `_atomic` |

**禁止**：把 B 轨的 15 个字段全塞进 i2v prompt——会与首帧打架，导致运动糊掉。
i2v 只写「运动 + 运镜 + 氛围微变化」三样，其余字段进 shot JSON 的 `_atomic` 作元数据。

定稿后**不要只留散文**：把选中那张的参数反向抽成字段存进 `_atomic`，
否则同一张好图第二次再也复现不出来（这是本流水线最大的隐性成本）。

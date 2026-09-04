# Anti-Slop 词汇表 — 空泛词替换

> 原则：**评价性形容词不产生画面，只产生 AI 味**。写 prompt 时逐词对照本表。
> 有效技术词（摄影/光学词汇）不算 slop，放心用。

## 一、绝对禁用（不产生任何画面信息）

| ❌ 禁用 | 为什么 | ✅ 替代（写具体画面） |
|---|---|---|
| beautiful / gorgeous | 纯评价，模型无感 | 写"什么让你觉得美"：`soft rim light tracing the carved stone edge` |
| stunning / breathtaking | 同上 | `shallow depth of field isolating the subject against creamy bokeh` |
| epic / grand | 空泛气势 | `extreme wide shot, the lone figure occupies one-tenth of the frame` |
| emotional / touching | 让模型"演情绪"→表情崩坏 | 写可见动作：`her fingers tighten slightly on the letter's edge` |
| artistic / creative | 模型不知道你指什么 | 指明风格流派：`in the style of Song dynasty ink wash painting` |
| masterpiece / best quality / award-winning | 生成器 slop 词，浪费 token | 删掉，把 token 留给画面细节 |
| amazing / wonderful / perfect | 同上 | 删掉 |
| nice / good / great | 模糊修饰 | 删掉或具体化 |
| high quality / ultra HD / 8K | slop 高发区 | 用技术词替代：`ultra-detailed macro product cinematography`（保留这一个，模型响应好） |
| AI 味中文词直译：唯美 / 大气 / 震撼 / 高级感 | 同英文对应项 | 具体化为构图/光/动作 |

## 二、低效 mood 词（能不用就不用，用了就补画面）

| ⚠️ 弱 | ✅ 用画面传达 |
|---|---|
| calm and peaceful mood | `dust motes drift unhurried through the light beam, nothing moves but the ink` |
| mysterious atmosphere | `deep shadows swallow the left half of the frame, a single shaft of light` |
| nostalgic feeling | `faded warm tones, slight film grain, worn fabric edges` |
| tense / suspenseful | `the droplet stretches but does not fall, the camera holds its breath` |
| lonely | `a single teacup on a six-seat table, the other five seats empty` |
| warm and cozy | `low evening sun through gauze curtains, steam curling from the cup` |

## 三、有效技术词白名单（放心用，模型真理解）

- **镜头/光学**：macro, extreme close-up, shallow depth of field, bokeh, fisheye, tilt-shift
- **光**：rim light, backlight, soft diffused light, golden hour, warm 4500K, high-key / low-key lighting, chiaroscuro
- **质感**：glossy reflections, matte texture, subsurface scattering (蜡/玉/皮肤), fine fibers, wet sheen
- **风格锚**：in the style of Song dynasty ink wash painting, documentary photography, macro product cinematography, literati aesthetic
- **运动描述**（i2v）：slowly, gradually, imperceptibly, unrushed — 具体速度词优于 "smooth motion"

## 四、自检方法

prompt 写完后扫一遍：**每个形容词问自己"删掉它，画面会少什么？"**——答不上来的都是 slop，删。

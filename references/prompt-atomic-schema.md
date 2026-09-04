# 原子化提示词 Schema（双轨制 · 融入 Awesome GPT Image 2 方法论）

> **来源**：`freestylefly/awesome-gpt-image-2`（MIT，~20k stars）。
> 它把 530+ 逆向案例提炼为 **13 个一级模板类别 / 40+ 模板**，共用一套**原子化字段**，
> 核心主张是 **Prompt-as-Code**：把提示词从"散文抽奖"变成"可填、可换、可复现的参数表"。
>
> **与本文档的关系**：`prompt-framework.md` 是**散文六要素**（服务叙事镜头），本文件是**原子 schema**（服务可控出图）。两者不是替代关系，是双轨。

---

## 1. 双轨制：什么时候用哪一套

| | **A 轨 · 散文六要素** | **B 轨 · 原子 schema** |
|---|---|---|
| 适用对象 | **叙事镜头**：分镜概念图、i2v 视频镜头 | **可控设计图**：海报、信息图、产品图、封面、角色设定表、文档版式 |
| 输出形态 | 120–220 词英文散文 | 先填 JSON/字段表，再编译成 prompt |
| 核心诉求 | 故事张力、运动、情绪转折 | **可复现**（同一张图能再出一次）、变量可换 |
| 典型场景 | "墨滴将落未落"的镜头 | 比赛海报换标题重出 5 版、角色设定表、PPT 封面 |
| 参考文档 | `prompt-framework.md` | 本文件 |

**决策口诀**：
- 这一镜要"**讲故事/有运动**" → A 轨。
- 这一图要"**多次复现/换变量/交付给别人改**" → B 轨。
- 拿不准 → 先 A 轨出概念，定稿后再用 B 轨把关键参数固化下来（见 §6）。

> ⚠️ **不要把 B 轨用在 i2v 视频上**。视频镜头只写「运动 + 运镜 + 氛围微变化」三样（见 §4），
> 把 15 个字段全塞进 i2v prompt 会与首帧打架，是运动糊掉的主要成因。

---

## 2. 15 个原子字段

填表时**不要求全填**——按模板类别挑 6–10 个即可，空白字段不写进 prompt。

| 字段 | 含义 | 生图（t2i）用法 | 视频（i2v）用法 |
|---|---|---|---|
| `type` | 输出物类型（建立全局上下文） | `Movie Poster` / `Character Concept Art` | ❌ 不写（由镜头定义） |
| `subject` | 核心主体 | 主角/产品/角色 | ❌ 不写（首帧已锁定） |
| `action` | 动作/正在发生的事件 | 静态图也要写**动词**（防"明信片化"） | ✅ **必写，且放最前** |
| `scene` | 场景环境 | 背景、环境上下文 | ❌ 不写（首帧已锁定） |
| `style` | 视觉/画风/美学方向 | 风格锚 + 参考流派 | ❌ 不写（首帧已锁定） |
| `layout` | 布局、网格、构图 | 版式/分栏/网格/镜头角度 | 只写运镜（见下） |
| `material` | 材质细节 | 磨砂/金属/丝绸/混凝土 | ❌ 不写 |
| `lighting` | 光影方案 | 柔光/侧光/轮廓光/色温 | ✅ 只写**微变化**（"dust floats in a sunbeam"） |
| `camera` | 焦段、光圈、机型、比例 | `50mm` `f/1.4` `Eye-level` `9:16` | ✅ 只写**运镜行为**（dolly in / static） |
| `color` | 主色/辅助色/强调色 | 色板 + HEX | ❌ 不写（走 ledger.immutable） |
| `typography` | 标题/副标题/字体风格 | **硬编码**标题文字（不要让它自由发挥） | ❌ 视频内文字基本不可控，避开 |
| `content` | 结构化内容数据 | 信息图模块、试色矩阵、分镜格 | ❌ 不写 |
| `structure` | 输出物分区/交付清单 | 顶部/中部/底部、多格编号 | ❌ 不写 |
| `constraints` | 禁止项与避坑 | ✅ **必写**（见 §5） | 只写 negative_prompt |
| `output` | 用途、分辨率、比例 | `2K` `9:16` `commercial photography` | ✅ 只写时长/帧数（走 CLI 参数） |

**关键差异**：A 轨要求"主体动作放开头"，B 轨的 `subject`/`action` 只是字段之一——
**字段顺序不等于权重**，B 轨靠 `constraints` 和硬编码文字来约束模型，不靠词序。

---

## 3. 13 个模板类别 → 本 skill 适配优先级

| 类别 | 对本流水线的价值 | 说明 |
|---|---|---|
| **场景与叙事** `tpl-scene` | 🔴 高 | 直接对应分镜概念图。强制写**动词/正在发生的事件** + 镜头语言（Low angle / Dutch angle）——这和 Director's Read 是同一件事的两种表达 |
| **摄影与写实** `tpl-photo` | 🔴 高 | 写实镜头、真实照片类 PPT 配图。核心是**具体摄影参数 + 真实瑕疵**（皮肤纹理、颗粒、raw look） |
| **人物与角色** `tpl-character` | 🔴 高 | **角色一致性**，与 `ledger.immutable` 完全同构。含 4×4 动作分解网格模板 |
| **历史与古风题材** `tpl-history` | 🔴 高 | 文房四宝/古风类项目。强制明确**朝代、服饰器物、建筑纹样** + `No modern elements` |
| **海报与排版** `tpl-poster` | 🟠 中 | 比赛海报、视频封面。核心是主视觉 + 硬编码标题 + 信息克制 |
| **插画与艺术** `tpl-illustration` | 🟠 中 | 风格锚点。要点：**提取大师特征，不要直接写大师名** |
| **图表与信息可视化** `tpl-infographic` | 🟠 中 | 信息图可喂 PPT。要点：**限制模块数（3–5 或 6–8）**，防信息溢出 |
| **文档与出版物** `tpl-document` | 🟠 中 | 结构优先：栏数/页边距/留白；**模拟文本填正文，只写死大标题** |
| **商品与电商** `tpl-product` | 🟡 低 | 产品展示镜头可用（材质+光影提商业质感） |
| **品牌与标志** `tpl-brand` | 🟡 低 | 品牌包/Logo，视频流水线少用 |
| **建筑与空间** `tpl-architecture` | 🟡 低 | 空间镜头可用（`Eye-level perspective` 控透视） |
| **UI 与界面** `tpl-ui` | ⚪ 极少 | 仅在需要"界面演示镜头"时用。中文文字可读是主要坑 |
| **其他应用场景** `tpl-other` | ⚪ 兜底 | 通用任务；支持主方案 + 备选方案 A/B |

---

## 4. 视频专用：原子 schema → shot JSON 映射

**i2v prompt 只写三样**（与 `prompt-framework.md` §5 一致），schema 其余字段不进 prompt，
而是**留在 shot JSON 里作为元数据**，用于复现和 batch 追溯：

```json
{
  "shot_id": "S03",
  "dramatic_function": "承上启下：墨已研好，笔提起",
  "i2v_prompt": "The brush lifts slowly from the inkstone, a single droplet stretching but not falling. Camera holds still. Morning dust drifts through the light beam.",

  "_atomic": {
    "action": "brush lifts; droplet stretches but does not fall",
    "camera": "static shot, locked camera",
    "lighting_delta": "dust motes drift through the sunbeam",
    "locked_by_first_frame": ["subject", "scene", "style", "color", "material"]
  },

  "_reproduce": {
    "first_frame": "frames/S03.png",
    "num_frames": 121,
    "negative": "blurry, warped hands, camera shake, flickering, plastic skin"
  }
}
```

规则：
- `_atomic.locked_by_first_frame` 列出的字段**禁止出现在 i2v prompt 里**——写了就会和首帧打架。
- 需要换效果时，改 `_atomic` 字段 → 重新编译 prompt，这就是"**单变量重拍**"的结构化版本
  （比手改散文更可靠：你知道自己改的是哪个变量）。

---

## 5. constraints 避坑清单（按类别，直接抄进 prompt 的禁止项）

| 类别 | 必写 constraints |
|---|---|
| 场景与叙事 | 必须含正在发生的**动词/事件**；禁止静态明信片式风景 |
| 摄影与写实 | 加真实瑕疵（skin texture / film grain / raw look）；禁止"太完美像假人"；写具体焦段光圈 |
| 人物与角色 | 前置角色一致性（脸型/服装/比例/发型）；禁止换脸、禁止新增角色、禁止复杂背景 |
| 古风题材 | 明确**朝代 + 服饰器物 + 建筑纹样**；`No modern elements`（防星巴克式穿越） |
| 海报 | `single poster only`；信息克制；标题硬编码；禁止文字压住主体 |
| 信息图 | 模块数限制 3–5 或 6–8；短文案；禁止信息溢出 |
| 插画 | 锁笔触；提取大师特征而非写大师名；禁止 AI 默认塑料风 |
| 文档 | 结构优先（栏数/页边距/留白）；正文用模拟文本；只写死大标题 |
| UI/界面 | 中文文字可读、禁止乱码、禁止平台特征混搭 |
| 品牌/Logo | 纯白背景；`No gradients`；矢量可缩放 |

---

## 6. 实操：两步工作流（A 轨出概念 → B 轨固化）

1. **A 轨出概念**：按 `prompt-framework.md` 写散文 prompt，`--count 3` 批量选优，人工挑中意的构图/光影。
2. **B 轨固化**：把选中那张的参数反向抽成字段表（subject/style/lighting/camera/color/constraints），
   存进 shot 的 `_atomic`。之后要"同风格再来一张"或"换个标题"，只改对应字段，不重抽卡。

> 这一步解决本流水线最大的隐性成本：**好图出了一次，第二次再也复现不出来**。

---

## 7. 与 anti-slop-lexicon 的分工

| 文档 | 管什么 | 典型动作 |
|---|---|---|
| `anti-slop-lexicon.md` | **词级**：删掉不产生画面的形容词 | "beautiful" → "soft rim light tracing the carved edge" |
| `prompt-framework.md` | **句级**：叙事镜头的六要素与顺序 | 主体动作放开头、事件数 ≤2 |
| 本文件 | **结构级**：可控出图的参数表与禁止项 | 填字段 → 编译 prompt → 可复现 |

三者是同一套方法论的三个层次，**不冲突，叠着用**。

# 失败处理与按症状诊断（自 SKILL.md 外置）

> agent 遇到生成失败/画面烂时查这里：先对失败类型，再按症状单变量重拍。

## 失败处理（v2.1）

| 失败 | 处理 |
|---|---|
| 401/403 | key 永久黑名单 24h，自动换下一个 |
| 429 | key 冷却 60s 自动换；同池全部 key 冷却/失败 → 单命令按 `MEDIA_PRIORITY` 自动跨池兜底（batch 中该 worker 连续失败 ≥3 自动退场，FAIL 镜可用单命令跨池兜底补跑） |
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


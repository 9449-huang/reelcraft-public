# ReelCraft 变更日志

> 历史演进记录，从 SKILL.md 外置（2026-09-06, v2.9）——SKILL.md 只保留当前有效指令，读历史到这来。

## 版本速查表（结构化索引）

> 回滚定位用：撞上"自己造的 bug"→ 用表里 commit 直接 `git diff <上一版commit>..<坏版本commit>` 看引入点，
> 或在 `git log` 里 `git show <commit>` 逐项核对。**回滚 = `git checkout <commit>^ -- scripts/ tests/` 拿回上一版文件**。
> 测试数 = 该版本全量 unittest 通项（skipped 不计）。

| 版本 | 日期 | commit | 测试数 | 一句话概括 | 引入了什么隐患（回滚重点） |
|---|---|---|---|---|---|
| **v4.4** | 2026-09-09 | bcc45cd | 215 | #5 成本账本（JSONL 调用记录 + `ledger` 报表）+ #6 声音链反向（`vo_build plan` 按 TTS 真实时长反推每镜时长） | 账本挂点在 call_with_failover/cmd_tts/cmd_edit，测试必须 patch LEDGER_FILE+STATE_FILE 到 tmp（真实 state 冷却条目会造"所有 key 失败: None"假故障） |
| **v4.3** | 2026-09-09 | d537af6 | 203 | B6 SKILL 瘦身：585→551 行，文案方法论外置 copy-guide.md，TTS 细节归 voice-guide，修 audit 重复行 | 纯文档+export 规则清理；被删的口味卡句移除对应替换规则，防回潮靠 grep 自检 |
| **v4.2** | 2026-09-09 | f723245 | 203 | 字幕链：srt 导入 + 样式预设档案（news/movie/variety）+ 测试拆 5 文件 + SLOW 门控 | drawtext 渲染串曾有空段 `::` 双冒号 bug（首版端到端失败根因，已修并钉死测试）；测试拆文件后跑法变 `python -m unittest discover tests` |
| **v4.1** | 2026-09-09 | 41c77fe | 194 | 文档质量批（上承 v3.1.16）：实测断言加时间戳 + 水印/能力复述改指针 + 术语速查表 + 中英概念词统一 | 纯文档改动零代码；SKILL.md 头部新增术语表（约 30 行，B6 瘦身时的候选对象） |
| v3.1.16 | 2026-09-08 | b09f4bf | 194 | 声音链三件套：vo_build fit 声画对账 + pick 选优半自动 + BGM 床闪避混音；双轴复核收尾（测量法 P1 + webp 哨兵） | vo_build 录音查找扩 .m4a；fit/pick 只建议不拍板；export 新增 CHANGELOG 赛事词替换规则 |
| v3.1.15 | 2026-09-08 | f77cdb9 | 179 | 移除 references/competition-spec.md（赛后脱敏收尾）+ 引用清理 | 文件在 git 历史 f3ed235..e486a2d 可恢复；export SKIP 名保留防将来重建漏脱敏 |
| v3.1.14 | 2026-09-08 | e486a2d | 179 | 二次复核修复：triage 档案读路径（--pool 命中免转录）+ tts 空体校验 | --pool 现在会跳过听诊直用档案结论（旧行为每次重转录）；响应 <64B 视为失败（某些网关返回极短真音频会被误拦，--refresh 重验） |
| v3.1.13 | 2026-09-08 | 51fd23d | 176 | 复核修复批：cmd_tts 原子写+CLI voice 优先+槽位动态扫描 + probe 进程内缓存 + triage 多 key + purge 容错 + envcheck ASCII | voice 优先级反转（CLI 显式参数现在赢 env）；probe 缓存同进程内不感知外部文件变化（长流水线场景慎用同 path 重 probe） |
| v3.1.12 | 2026-09-08 | 53ad152 | 171 | 方案B triage 听诊链（SenseVoice ASR 判补不补朗读）+ audio_profiles + audit 有声列 | triage 转录走硅基（file 字段必须在 model 前，已钉死）；听诊只粗筛，拍板留人 |
| v3.1.11 | 2026-09-08 | b866f3a | 163 | 复核修复批：watermark 提升 stop + qcseq 错误中断 + 中文词数折算 + negative 词界 + 导出防护 | watermark 自动提 stop 改变运行边界；qcseq exit2 现在会中断（此前静默放行） |
| v3.1.10 | 2026-09-07 | e8bd3cf | 150 | TTS 升级：硅基流动 CosyVoice2 主力 + 多 key failover + 情感/逐句声音参数 + 声音风格卡 | tts 默认音色从 Cherry 改为 env 驱动（旧写法失效）；emotion 只对 CosyVoice 系生效 |
| v3.1.9 | 2026-09-07 | 21dccea | 144 | envcheck 环境自检 + clean 产物治理（#65/#66） | clean --purge 不可逆；envcheck 本地服务探测受本机服务状态影响 |
| v3.1.8 | 2026-09-07 | 7083c10 | 131 | qcseq 跨镜一致性粗检 + caps sync 文档单源化 + 导出残留清理 | qcseq 阈值判据可能误杀（组合判据已防明度误判）；md 快照区块勿手改 |
| v3.1.7 | 2026-09-07 | 86816b1 | 117 | pipeline 补完 end-to-end + prompt_lint + 负面分档 + 统一入口/审计 | run/audit 是新入口；lint 默认强制（旧 shots 目录需 --no-lint） |
| v3.1.6 | 2026-09-07 | f2f03f8 | 87 | PENDING→exit4 链路 + cmd_video 认 .webp + caps 写读对齐 + P2 四项 | batch 退出码语义变化（0/1/4）；pipeline lint 前需 --no-lint 场景 |
| v3.1.5 | 2026-09-07 | b2fd4b0 | 73 | 一键编排 pipeline.py（#1）+ Windows glob 去重 bug | pipeline 是新增编排入口，改动面大 |
| v3.1.4 | 2026-09-07 | aa3fe7a | 66 | QC 硬门禁 qcgate（#4） | 门禁阈值可能误杀（WARN 不硬拦，设计如此） |
| v3.1.3 | 2026-09-07 | 609f574 | 63 | 能力单源化 mg_caps（#2）+ ffmpeg 管线真冒烟（#6） | caps 写读 key 错位（v3.1.6 才修）；effective 非唯一出口 |
| v3.1.2 | 2026-09-07 | 41c491b | 53 | copy.py main 截断 P0 + 锁静默降级 + #2/#3/#4 遗漏点 | — |
| v3.1.1 | 2026-09-06 | 827f206 | — | status 纳 TTS 池 + 小样合成探测 | TTS 探测会真发合成请求（耗少量额度） |
| v3.1 | 2026-09-06 | 59d570d | — | 本地 Edge TTS 接入 key 池 | TTS 依赖本地服务（localhost:5050）在线 |
| v3.0 | 2026-09-06 | 8d3a975 | 27 | 通用化中性化（去赛事归属） | 公开仓剔除私有池，私有能力不在公开版 |
| v2.9 | 2026-09-05 | c37fa8e | — | 公开仓体检修复 | — |

## v4.4（2026-09-09）

两个功能拓展（TDD 红→绿，seam 先定：ledger_append 文件行为 / ledger_summarize 纯函数 / plan_axis 纯函数 / 两个 CLI）：

- **#5 成本账本**：每次真实 API 调用追加一行 JSONL 到 `~/.workbuddy/.media_ledger.jsonl`
  （`{ts,provider,key,op,ok,ms,err}`）。挂点三处：`call_with_failover`（image/video，含 batch
  多进程各 subprocess）、`cmd_tts`（成功 + 每把 key 失败降级）、`cmd_edit`（成功）。
  `media_gen.py ledger` 出汇总：总/成功/失败 + **白烧次数**（失败调用=免费档下被限速吃掉的
  真时间）+ 按渠道/操作/天分组，`--days N` 过滤、`--json` 机器可读。
  设计取舍：**append-only JSONL 而非 state 读改写**——追加天然适合并发，坏行只影响自己
  （summarize 容错跳过，不炸整个 state）。
- **#6 声音链反向**：`vo_build.py plan <lines> --out plan.json` ——VO 先行，逐句 TTS/录音取
  真实时长 → `plan_axis` 自动排 `at`（gap 间隔）→ 反推每镜需求时长（+pad 呼吸）→
  直接给出 `kenburns-all --duration` 逗号串。与 `fit` 互为镜像：**fit 是"镜定时长 → 对账 VO"，
  plan 是"VO 定时长 → 生成镜时长"**（stills/hybrid 档画面跟着声音走）。
  落盘 `vo_lines_at.json` 可喂回正向 `vo_build` 合成成片，两个方向闭环。
- 测试 203 → **215**（+12：账本纯函数 5 + 挂点 2 + plan_axis 4 + plan 端到端 1）。
- 教训（测试基建）：挂点测试必须**同时** patch `LEDGER_FILE` 和 `STATE_FILE` 到 tmp——
  真实 state 里残留的 key 冷却条目会让 failover 全跳过，报 "所有 key 失败: None" 的假故障。

## v4.3（2026-09-09）

B6 SKILL 瘦身（纯文档，代码零改动，203 测试不变；方法论：SKILL.md 每次会话全文进 context，行数即持续 token 开销）：

- **文案方法论外置**：第一原则正反例表 + copy.py 七条约束详解 + 筛选必做 → 新建
  `references/copy-guide.md`（约 55 行），SKILL 留 8 行骨架 + 指针。
- **TTS 渠道细节归位**：音色名/情感词表/降级提醒压缩为优先级清单，细节指向
  voice-guide.md（复述=会过期的 cache，v4.1 方法论）。
- **去重**：audit 命令重复两行（笔误 bug）、kenburns 兜底两处、路由规则与核心设计
  重复的跨池兜底描述、核心设计 14/15/16 三条收紧。
- **export_public 规则清理**：被瘦身删掉的"高规格卡（私有池大模型句式）"对应替换规则
  移除（miss 只 warn 无害，但留着误导）；防私有池名回潮继续靠 grep 自检兜底。
- 585 → **551 行**（-34，约 6%）；验证：references/ 9 个指针全部对应存在文件、
  导出零命中无 warn、203 测试全绿。

## v4.2（2026-09-09）

字幕链 + 测试结构批（TDD：红→绿；结构优化 #1 + 功能拓展 #4）：

- **srt 字幕导入（#4 前半）**：`postprocess.py concat --subtitle` 现在直接认 `.srt` 文件
  （此前只认内部 JSON 格式）。`parse_srt` 纯函数：标准 `HH:MM:SS,mmm --> HH:MM:SS,mmm`
  时间戳、多行文本合并、块序号容错、非法时间戳块丢弃（TestSrtParse ×4）。
  utf-8-sig 读取，剪映/Premiere 导出的 srt 拿来即用。
- **字幕样式预设档案（#4 后半）**：`--subtitle-preset news|movie|variety` 三风格
  （`scripts/subtitle_presets.json`）——news=新闻条底框白字、movie=电影底幕、
  variety=综艺黄字黑描边。`apply_preset` 条目级字段优先于预设（单条字幕想特殊，
  JSON 里写 own 字段即可覆盖），未知预设 ValueError 直接 die（TestSubtitlePreset ×4）。
- **drawtext 渲染串重构**：修复空 shadow 段产生 `::` 双冒号的 bug（首版端到端测试
  红的根因——ffmpeg 报 Invalid argument）。改为 extras 列表逐段拼冒号，空段绝不输出。
  端到端测试：lavfi 造 4s 底片 + srt + news 预设烧录，抽帧验白色像素 >50（真跑 ffmpeg）。
- **测试拆分（#1，B5）**：test_media_gen.py（2332 行 194 测试 49 类）机械拆为 5 文件——
  test_mg_core（18 类）/ test_cli_tools（11 类）/ test_postprocess（11 类）/
  test_vo_build（7 类）/ test_pipeline（2 类）。零逻辑改动，类边界原样搬。
- **SLOW 门控**：真跑 ffmpeg 的慢测试标 `@unittest.skipUnless(not _slow)`，
  默认快跑 ~2s（skip 50），`SLOW=1` 全量 ~49s（203 项，skip 4）。
  日常改码快反馈、提交前全量验，两档节奏。
- 测试 194 → **203 项**（+9：srt×4 + 预设×4 + 端到端×1）。
- 验证：快/SLOW 双档全绿、AST 过、导出自检私有词零命中。

## v4.1（2026-09-09）

文档质量批（代码零改动，测试 194 不变；方法论参照 writing-for-agents：环境是 source of truth，文档复述=会过期的 cache）：

- **实测断言加时间戳（④）**：SKILL.md / model-capabilities.md / troubleshooting.md / prompt-styles.md 全量扫
  "已验证/实测/无限量/带水印"字样逐条补"2026-09"标记；model-capabilities.md 头部加全局快照声明
  （所有实测结论以 `caps probe --real` 落盘值为权威，7 天过期）。
- **复述改指针（③）**：SKILL.md Step1 水印确认（原硬编码"智谱带/Agnes 不带"）→ 改查
  `watermark_profiles.json`（档案自带 probed_at）；Step4 路由段、troubleshooting 水印行同改；
  "能力白名单已硬编码"段改"以 caps show 实测为准"。
- **术语速查表（①）**：SKILL.md 头部加 19 行术语表（池/key/六问/三档/末帧链/口味卡/slop/exit 4/
  harvest/qcgate/单变量重拍/Director's Read/ledger…），新会话 agent 扫一遍表即可上岗；
  README 加面向外部用户的 9 行简版——黑话门槛从"考古 20 分钟"降到"读表 3 分钟"。
- **中英统一（⑥）**：概念词首次出现处补中文对译（Director's Read=导演读法、ledger=连续性分类账、
  harvest=收割、kenburns=缓推）；CLI 命令名/API 名保留英文原文（grep 可检索性优先，rename 是破坏）。
- 验证：194 测试全绿、caps sync --check 过、导出自检私有词零命中。

## v3.1.16（2026-09-08）

改进批第一批（TDD：红→绿，每项先测后码；三 CLI 均 lavfi 造片端到端真跑）：

- **`vo_build.py fit <vo_lines> <clips/>`（A1' 声画对账）**：VO 时间轴 vs 逐镜 clip 时长对账表——
  每镜 VO 需求（末句 at+dur+gap − 首句 at）对比 clip 实测时长，verdict 四档
  ok/warn/info（空镜合法）/missing（镜缺成片）；warn 镜给建议（kenburns 补长或 VO 提速比）。
  纯函数 fit_report 可单测（TestVoFit ×4）。
- **`postprocess.py pick <images...>`（A3 选优半自动）**：count N 张候选图启发式打分
  （清晰度×2 + 对比度 − 亮度偏离惩罚），排序只做参考——**最终人选人做**，脚本明说。
  score_images 纯函数（TestPick ×2）。
- **`vo_build.py --bgm <file>`（拓展#1 BGM 床）**：BGM 循环补齐到成片长 + 基础音量
  -14dB（--bgm-duck 调），sidechaincompress 以 VO 总线为 key——有旁白处 BGM 自动压低，
  无旁白处保持底床。bgm_filter_chain 纯函数（TestBgmMix ×2）。
- 顺手：vo_build 录音查找扩展名 .mp3/.m4a/.wav（TTS 产物就是 m4a，旧版 --skip-tts 找不到自己的产物）。
- 修复：export_public 新增 references/CHANGELOG.md 赛事词替换规则（v3.1.15 条目的具体赛事截止表述
  差点带进公开版，自检拦截后补规则；其余赛事提及本就在 private 块内）。
- 测试 179→**187 项**（+4 fit +2 pick +2 bgm）。
- **复盘修复（P1）**：首版 BGM 链 sidechaincompress 输入顺序反（VO 成了被压缩方）且 VO 未进最终
  amix——成片只剩 BGM 没旁白；第一版端到端只验 rc=0 漏网。重构为 bgm_assembly 纯函数
  （BGM 主输入 + VO asplit 出片/触发两路 + VO 必进 amix）+ volumedetect 内容级测试。
  **教训：音频混音 rc=0 ≠ 对，必须验内容**。最终 189 项。
- **双轴复核收尾（b09f4bf，194 项）**：①测试硬编码绝对路径改相对（他机不挂）；②e2e 断言收紧为
  电平差（VO 段 vs 纯 BGM 段 >6dB）——顺藤摸出**测量法 P1**：`-ss` 放 `-i` 后是输出侧 seek，
  只在 mux 层丢帧，volumedetect 吃全量样本报**整文件均值**（静默段假响 67dB），旧断言因此假绿；
  改输入侧 seek（`-ss` 在 `-i` 前）后链路电平实测健康：VO -24.1dB vs BGM -38.2dB（闪避真实生效）。
  ③cmd_fit 静态 webp probe=0 被丢 → 误报 missing；改哨兵 -1.0 报 info（图片镜不缺成片，
  VO 需求照算供 kenburns 配 --duration），新增回归测试钉死。④fit_report 删未使用的 total 参数。
  P3：kenburns-all 注明 NN=图序≠shot_id；duck_db 注释改"全程基础音量"。

## v3.1.15（2026-09-08）

改进分析落地（用户拍板只做 #7 删除项）：

- **移除 `references/competition-spec.md`**：赛事截止日期临近，赛事敏感内容整文件删除（git 历史 f3ed235 起全程可恢复）。SKILL.md Step 0 的规格附件指引改为存会话工作区（不再落 skill 目录）。
- export_public 的 SKIP_FILES 保留 `competition-spec.md` 条目并加注释——将来若再建同名文件自动防泄漏，不因删文件而丢防护。
- 测试 179 全绿、导出自检私有词零命中不变。

## v3.1.14（2026-09-08）

外部二批复核（6 条：2 误报/1 部分/3 成立中的两条真问题）修复：

- **triage 档案读路径**（P2）：v3.1.12 只写不读——“同模型下次免测”承诺未兑现，每镜重复烧转录额度。
  现在 `--pool` 传入即自动查档案，命中直接用实测结论（不 probe 不转录，reason 标注实测时间）；
  换模型版本/结论存疑时 `--refresh` 强制重测。media_gen 转发同步加 --refresh。
- **tts 空体校验**（P3）：网关半死时常返 200+空体——旧版会落盘 0KB 废 mp3 并报 OK，静默流入
  下游拼接。现在响应 <64 字节视为失败进 failover（next key）。
- 测试 176→**179 项**（档案命中免转录/refresh 绕过/空体 failover；顺带修旧用例假音频过短被新阈值误拦）。

## v3.1.13（2026-09-08）

改代码 skills 双轴全量复核（v3.1.8..v3.1.12 共 13 commit +1852 行）后的修复批，8 条（2×P2+6×P3）：

- **cmd_tts 原子写 + 旧文件警告**（P2）：成功路径 tmp+os.replace（被杀不留半截）；全 key 失败且 out 是旧文件时 stderr 明确警告"下游勿复用"。
- **probe 进程内缓存**（P2）：postprocess.probe 按 path 缓存——audit 逐镜 + triage 双 probe 同文件从 2 次降 1 次，20 镜 audit 少起 20 个 ffmpeg 进程。
- **purge_trash 容错**（P2）：Windows 文件被占用（播放器/杀软）单文件跳过不炸，其余继续删。
- **envcheck badge ASCII 化**（P3）：envcheck 恰是"环境坏了才跑"的工具，裸 cmd 无 PYTHONUTF8 时 emoji 会替真报告炸掉，改 [OK]/[WARN]/[FAIL]。
- **triage 转录通道多 key 扫描**（P3）：旧版只读 MEDIA_TTS_1（key 配 TTS_2 时误报"未配置"），现扫 MEDIA_TTS_<n>_* 取第一个可用，与 cmd_tts/envcheck 同口径。
- **CLI --voice 优先于 env**（P3）：旧版 env VOICE 赢 CLI 显式参数，反转后 CLI 显式传入优先，env 只作各 key 默认。
- **TTS key 槽位动态扫描**（P3）：旧版 range(1,20) 封死 19 把，现 _scan_tts_slots()（起始空号跳过、断档容忍、连续 3 空号停、空值不算已配）。
- **plan_clean 死代码清理 + envcheck 空值口径**（P3）：删 rel_names 死变量与冗余 elif；scan_key_env 判"已配"改真值（空字符串=未配，与 mg_core.list_keys 一致）。
- 测试 171→**176 项**（TestReviewFixBatch ×9：槽位扫描/voice 优先/旧文件警告/probe 缓存/triage TTS_2/空值断档/purge 容错/ASCII badge）。

## v3.1.12（2026-09-08）

方案B 出片听诊链（承接声音方案讨论：每个视频模型音频能力未知，不能假设、不能每镜人工听）：

- **`media_gen.py triage <clips>`（audio_triage.py）**：机器粗筛"这镜要不要补朗读"，拍板留人——
  probe 无音轨 → yes（哑片，需后期配）；有音轨 → 抽中段 9s → **硅基 SenseVoiceSmall 转写**
  （/audio/transcriptions，通道复用 MEDIA_TTS_1 硅基 key，零新依赖）→ 中文人声 → no；
  英文 → yes；转录空/失败 → listen（纯音乐/环境音？机器不硬判，交人听）。
- **audio_profiles.json**（~/.workbuddy，同 watermark/caps 档案模式）：`--pool <名> --update-profile`
  实测一次回写，同一模型下次免测。
- **audit 加"有声/哑片"列**：逐镜 probe 音轨状态，汇总"有声 n / 哑片 m"，直接回答"该不该开 triage"。
- **实测坑（已钉死到代码注释）**：硅基网关 multipart 要求 **file 字段在前、model 字段在后且带
  Content-Type: text/plain**——OpenAI SDK 默认顺序（model 先）会被网关 400 "Error when parsing
  request"。真实闭环验证：用 v3.1.10 情感引导生成的旁白片 → SenseVoice 转写回 23 字中文 → 判 no ✓。
- 测试 163→**171 项**（TestAudioTriageDecision ×6 + TestAudioProfile ×2）。

## v3.1.11（2026-09-08）

外部复核修复批（逐条核验后只修真问题；误报项不改并记录理由）：

- **P2-1 水印静默跳过**：`--watermark` 已给但 stop_after 不含 watermark → 自动提升 stop 到
  watermark + 打印提示（此前"以为做了 dry-run，实际一行没跑"）。
- **P2-2 qcseq 真错误被吞**：区分 exit——1=WARN 提示放行 / ≥2=真错误（输入错/抽帧失败）
  中断 pipeline，不再当色调跳变提示放过。
- **P2-5 prompt_lint 中文按字计词失真**：改中英信息密度折算（中文 ≈1.8 字/词）——
  纯中文/中英混排不再虚高爆 220 上限；200 字中文 ≈167 词语义相当。
- **P3-1 negative_for_shot 子串误档**：英文关键词改词边界（`(?<![a-z])…(?![a-z])`）——
  "naturally" 不再误中 nature、"produce" 不再误中 product；中文关键词保持子串。
- **P3-7 qcseq 结果落盘**：pipeline_run.json 记 qcseq rc/note，audit 显示"上次 qcseq"。
- **P3-2/P3-3 导出加固**：目标目录名必须含 reelcraft_public（防 argv 误配 rmtree 误删）；
  语法校验动态收集全部 scripts/*.py（新增脚本不漏，"N 脚本通过"不再是写死文案）。
- **P2-6 UTF-8 运行前提文档化**：SKILL.md 加"中文 Windows 必读"——需 PYTHONIOENCODING=utf-8
  或 UTF-8 终端（裸 GBK 控制台跑会因 ✅/⚠️ 等非 GBK 字符 print 崩溃）。
- **误报澄清（未修）**：P1"三处 subprocess 无 encoding"全部不实——delogo:55/mg_core:329
  (run_capture)/vo_build:48 均已带 encoding="utf-8"（v3.1.6 修过，跨行写法）；GBK 崩的真凶是
  print emoji，由 P2-6 文档覆盖。P2-3 测试数/P2-4 envcheck 入日志/P3-6 envcheck 无单测 均为
  误报/口径误解（encheck 已有 TestEnvcheck 8 项）。
- **P3-5 子链端到端真跑补课**：lavfi 造 2 片带音轨 → qcseq（真跳变 WARN）→ vo_build
  （硅基 TTS 温柔+激昂 2 句）→ concat --voice/--subtitles/--slogan（audio=True）→
  delogo --dry-run 红框标注，全链零视频 API 冒烟通过。
- 测试 150→**163 项**（词数折算×4 / negative 词界×4 / watermark 提升×2 / qcseq 中断×2 / 导出防护×1）。

## v3.1.10（2026-09-07）

TTS 声音线升级（用户拍板：接硅基流动 + 方案 A+C）：

- **主力渠道切硅基流动 CosyVoice2-0.5B**：`MEDIA_TTS_1_*` = `api.siliconflow.cn/v1`
  （OpenAI 兼容 /audio/speech），本地 Edge TTS 降为 `MEDIA_TTS_2` 兜底。实测出片
  ✓（69KB/4.3s 情感引导生效）。key 只进 `~/.workbuddy/media_keys.env`，不进 repo。
- **TTS 多 key 自动 failover**：`cmd_tts` 从"只读 TTS_1"改为按序号扫描（遇配置后
  连续空号停），key#n 失败自动降级下一把；**每把 key 可独立 `_VOICE`，降级时音色
  自动跟随切换**（硅基挂 → Edge 女声兜底）。全挂 exit 3 明示。
- **情感参数 `--emotion`**：CosyVoice 系情感走文本引导——`你能用{情感}的情感说吗，`
  拼进正文（`_tts_emotion_prefix` 纯函数，6 项新测试）。
- **vo_build 逐句声音属性**：vo_lines.json 每行可带 `voice`/`emotion`/`speed`，
  覆盖全局默认——高潮句激昂档、独白温柔档，情绪节奏机器化。
- **声音风格卡 `references/voice-guide.md`**（新）：场景→语速/情感/音色组合表 +
  情感词表（含 `[laughter]` 等官方标记）+ 自定义音色说明 + vo_lines 完整示例。
- 测试 144→**150 项**（TestTtsFailover ×4 + TestVoBuildPerLine ×2）。

## v3.1.9（2026-09-07）

运维侧两件套（#65/#66）——不出片，但让"多轮试拍"不翻车：

- **#65 envcheck 开跑前体检**：`media_gen.py envcheck`（= envcheck.py）13 项检查——
  python/ffmpeg/libx264/PIL/中文字体/各池 key env/本地服务 TCP 探测/caps 可读性。
  设计纪律：本地 base 挂 → fail；远程 API 不探（能力看 caps，网络抖动不当环境故障）；
  **key 值绝不进结果**（单测钉死，envcheck 常被截图贴日志）；`scan_key_env` 修
  `list_keys` 遇"KEY 在 BASE 缺"静默吞后续序号的盲区（断档显式报）。
  exit：0 全过（warn 允许）/ 1 有 fail。
- **#66 clean 产物治理**：`pipeline.py clean <shots>`（audit 同胞）。默认 **scan-only**
  只列清单；`--yes` 把可再生产物（frames/clips/qc_frames/临时文件）移入
  `.trash/<时间戳>/`（**Trash, not delete，可反悔**）；`--purge` 才真删。
  源（shots JSON/plan/vo_lines）、账单（batch_run.json）、成片（final*）永不触碰。
- **测试污染修复**：TestEnvcheck 曾写坏并误删用户真实 `.media_caps.json`
  （冒烟时 envcheck 报"无实测记录"暴露）——重定向 tmp 修复，实测记录已从 md
  快照恢复。测试 131→**144 项**。

## v3.1.8（2026-09-07）

优化批 ⑤⑥ —— QC 闭环补上"跨镜视角"、能力文档从手抄变生成物：

- **优化⑤ qcseq 跨镜首帧一致性粗检**：postprocess 新增 `qcseq` 子命令——抽每段首帧 →
  HSV 直方图（H12×S3×V3 桶）相邻 Bhattacharyya 对比。WARN = BC 阈值下 **且** 色相/饱和度
  确实漂移（组合判据：纯 BC 判据会把"同色相不同明度"误杀成 BC=0，已用单测钉死）；
  彩色↔黑白（ΔS 跳变）也会报。WARN 报告不拦流程（跑偏镜重拍/整体调色是人的决策）；
  pipeline concat 阶段自动跑（--no-qcseq 关），`media_gen run` 同步透传。实测快照：
  stylegrid 看全貌人眼判，qcseq 给机器定量。测试 125→**131 项**（含 PIL 造图纯函数测试）。
- **优化⑥ caps sync 文档单源化**：`model-capabilities.md` 内嵌 `<!-- caps-auto -->` 区块，
  由 `~/.workbuddy/.media_caps.json` 生成——实测更新文档自动跟上，不再出现"第三个矛盾源"。
  `caps sync` 重写区块（区块外叙述机器不碰）；`caps sync --check` drift 校验（不一致 exit 1）。
- **导出残留清理（v3.1.7 遗留）**：v3.1.7 新增的测试/示例把私有池名字样带回公开仓
  （自检必挂）——测试 tag/帮助文案改中性名，私有档测试方法加 `# private-begin` 标记，
  SKIP_DIRS 补 `.pytest_cache`。重新导出自检 [OK] 零命中。

## v3.1.7（2026-09-07）

优化批 ②④⑦⑧⑨⑩⑪ —— 把"最后一公里"（词级约束 / 声音 / 去水印 / 文档回滚定位）机器化：

- **优化④ pipeline 补完 end-to-end**：阶段扩为 images→videos→harvest→kenburns→**sound**→concat→**watermark**。
  sound 阶段有 `shots/vo_lines.json` 才跑 vo_build（旁白+字幕，产物自动带进 concat，纯画面成片仍合法）；
  concat 透传 plan.json 的 xfade/freeze_last（PLAN_KNOWN_KEYS 已补此二键，plan-check 不再 warn）；
  watermark 阶段 `--watermark <provider> --watermark-dry-run` 只列待处理档/出红框自检图，目检后再真抹。
- **优化⑦ CHANGELOG 结构化**：文件头加「版本速查表」（版本/日期/commit/测试数/一句话/回滚重点），
  撞上自己造的 bug 可直接 `git diff <上一版commit>..<坏版本>` 定位、`git checkout <commit>^ -- scripts/` 回滚。
- **优化⑧ 负面模板按题材分档**：mg_core 新增 `negative_for_shot()` + `NEGATIVE_TEMPLATES`（人物/风景/产品三套），
  batch 出镜时按 shot 的 type/subject 关键词自动选档；未命中兜底原通用 negative；`--negative` 显式覆盖。
- **优化⑨ 测试补三块盲区**：prompt 决策路径（batch lint 快照 PASS/FAIL）、xfade 管线（lavfi 两段实跑 concat --xfade）、
  水印旁线（真实 watermark_profiles.json 读取 + 跨分辨率框缩放）。测试 95→**117 项**。
- **优化⑩ 统一 CLI 入口**：`media_gen.py run <shots>`（转发 pipeline.py，参数单一事实源在 pipeline 侧，subprocess 转发
  零重复实现 + 退出码透传）与 `media_gen.py audit <shots>`——用户不再需要记 pipeline.py 脚本名。
- **优化⑪ plan.json 审计视图**：`pipeline.py audit <shots>`（= `media_gen.py audit`）一键盘点——
  每镜 出图/出片/失败 状态（读 batch_run.json 标签对照 frames/clips 产物）+ 汇总 + 可执行下一步清单
  （harvest 在途 / --retry-failed 补 FAIL / images 补缺帧 / 全出片→concat）。
- 维护纪律追加：**编辑任何脚本后跑 AST 结构校验**（本批再犯一次：改 mg_core 时误删 natkey 函数体，
  AST 立即抓出——纪律已生效）；测试 112→117 项。

## v3.1.5（2026-09-07）

架构整改第三批 —— **#1 一键编排 `pipeline.py`**（"流程依赖 agent 手工串联"）：

- **新增 `scripts/pipeline.py`**：把 Step 2-6 的既有命令按 `plan.json` 串起来——
  `images`（全镜出图）→ `videos`（full 全镜 / hybrid 只 `hero_shots`，靠 batch `--only`）→ `harvest` →
  `kenburns`（只补缺失 clip 的镜）→ `concat`（后期+自检→final）。
  **只做编排、不做审美决策**：mode / 重点镜 / 池顺序 / 水印全读 `plan.json`（那是问过用户的结果）；
  **plan.json 缺 mode 或 hybrid 缺 hero_shots → 直接 die 退回 Step 1，绝不默认 full 替用户拍板**
  （"绝不自动降级"在此同样成立）。videos 有镜超时在途 → **exit 4** 交还 agent 走超时三选协议（不自动重试防重复扣费）。
  出片后**停在 QC 门前**（视觉验收必须人看）。每阶段幂等断点续跑，执行清单落 `shots/pipeline_run.json`。
  `--dry-run` 预览各阶段命令；`--stop-after` 提前停；声音/字幕/xfade 参数透传给后期。
- **`batch` 加 `--only`**（只跑指定镜号，按 shot_id）：hybrid 模式喂 `hero_shots` 用。
- **抓出一个 Windows 专属 bug**：分镜枚举 `glob("S*.json")+glob("shot_*.json")` 在 **Windows 大小写不敏感**的
  glob 下两个 pattern 都匹配 `shot_01.json` → 每镜被算两次（mg_batch 双跑 / pipeline 双缓推）。
  Linux/macOS 不复现，只 Windows 静默出错。抽 `mg_core.list_shot_files()` 单源去重（按解析路径），
  mg_batch 与 pipeline 共用；SKILL「维护本 skill 时」加第 4 条纪律"跨平台 glob 警惕大小写"。
- 测试 66 → **73 项**：`TestPipelineOrchestration`（5，dry-run 不烧钱——hybrid 只跑 hero / stills 跳过视频 /
  full 无 --only 无缓推 / **缺 mode 必须 die 不能猜** / hybrid 缺 hero 必须 die）+ `TestListShotFiles`（2，跨平台去重）。
- SKILL 加"新用户导览"一键入口 + 「一键编排 pipeline.py」专章。

## v3.1.4（2026-09-07）

架构整改第二批 —— **#4 QC 硬门禁半自动**（"QC 未做满机器可验收"）：

- **新增 `postprocess.py qcgate <video>`**：单段机器门禁，与 `check`（只查规格）互补——
  补上"画面是不是坏的"这类机器能判项：
  ① 规格（分辨率/时长/帧率，同 check 阈值）；
  ② 抽 6 帧（沿时长均布）算 **近黑帧占比 / 过曝占比 / 相邻帧运动量**：
  - ≥80% 帧近黑 → **FAIL**（生成失败/审核拒/纯色）
  - ≥60% 帧过曝、或近乎完全静帧 → WARN（可能故意的淡入淡出/kenburns，交人眼）
  - 输出 PASS/WARN/FAIL + exit code（0 过 / 2 拦），`--strict` 把 WARN 也升 FAIL
  - 机器**判不了**的（手部/面部崩坏、主体漂移）**不硬判**——仍走 `qc` 抽帧人眼，避免误杀好片
- **batch 加 `--qcgate`**：videos 阶段每镜出片后就地过门禁，FAIL 的镜记为 `FAIL` →
  `--retry-failed` 自动重跑；`--qcgate-strict` 可选把 WARN 也纳入重跑。
  只拦"明显生成失败"，不打断美学层面的重 roll 决策
- 抽帧指标用 PIL `tobytes()`（灰度每字节一像素），弃 `getdata()`（Pillow 14 将移除）
- 测试 63 → **66 项**：`TestFFmpegPipeline` 补 qcgate 好片 PASS / 纯黑片 FAIL / 低分辨率规格 FAIL（lavfi 造片零 API，实跑 postprocess.py qcgate）

## v3.1.3（2026-09-07）

架构整改第一批（用户拍板优先级：#2 能力单源化最高、#6 管线测试次之）：

- **#2 能力单源化（声明=候选，实测=权威）**：新增 `scripts/mg_caps.py` + `media_gen.py caps` 子命令（show / probe / clear）。整改前"这个池到底支持什么"有三个互相矛盾的答案（PROVIDERS 硬编码 / status 的 /models 关键字猜 / 真跑才知道但不落盘）；现在收敛成一条链：声明（PROVIDERS，候选与默认）→ `caps probe <pool> --kind image|video --real`（真发一次最小请求，subprocess 调 media_gen 自身，端到端零重复实现）→ 落盘 `~/.workbuddy/.media_caps.json`（权威）→ `effective()` 唯一出口。**新后端接入从此零改码**：配 env → probe --real 验一次 → 后续所有决策自动采用。/models 探测降级为"线索"（method=models-guess 不当权威）；实测 7 天过期自动回落声明。实测即抓真实现象：请求 1024x576、agnes 实际出 1312x736（`caps show` 会标 ⚠ 请求≠实测）
- **#6 ffmpeg 管线真冒烟测试**：新增 `TestFFmpegPipeline`（4 项）——lavfi testsrc+sine 零 API 造片，实跑 `postprocess.py concat` 三条真实路径：同规格 -c copy 直切 / 混分辨率转码 / **同分辨率不同 codec（h264 vs vp9）必须转码**（#4 回归钉：旧签名只比分辨率这条会变红）/ **filter 分支带音轨不丢**（#4 回归钉：旧代码写死 a=0）。此前 postprocess 的拼接分支只有"读代码觉得对"
- **测试修复**：TestCapsSource 漏 import time（3 处 NameError）+ test_clear 数据漏 probed_at_ts 被误判过期（断言错在测试数据，代码逻辑正确）
- **copy.py**：`open().write()` 未关文件（ResourceWarning）改 with 语句
- SKILL.md：能力探测表述升级为"能力单源"，新增 Step 1.5（新池/久未验证先 `caps probe --real`；问②清点以实测为准）
- 测试 53 → **63 项**（新增 6 caps 单测 + 4 ffmpeg 管线冒烟），全绿

## v3.1.2（2026-09-07）

复核 v3.1.1 那轮 11 条修复（外部审查报告）后的补漏，全部由"改代码 skills"两轴复核（Standards / Spec）抓出：

- **P0 `copy.py` main() 被拦腰截断（本轮修复自己造的）**：#10 抽 `clean_text` 时 `def` 写成了 0 缩进，插进 `main()` 体内 → main 在 122 行提前结束，调用 chat / 写文件那段变成 `return` 之后的死代码，CLI 静默 exit 0 不产出任何文案；死代码还引用了已变成 `clean_text` 局部变量的 `lines`（光调缩进也修不好）。**测试 46 项全绿却完全没发现**——因为只 import 了 `clean_text` 单函数，没人跑 `main()`。修复：函数提到 main 之前，调用链复原，条数改由 `out.splitlines()` 统计。**这是同类事故第二次**（v3.1 修 postprocess 时把 `_escape_drawtext` 误插进 `cmd_concat` 尾部），故新增纪律：编辑任何脚本后必须做 AST 结构校验 + CLI 冒烟；纯函数测试之外至少有一条端到端 main 层测试
- **#1 锁的静默降级**：`_FileLock` 用 `msvcrt.LK_LOCK`，高争用时内核盲重试约 9–10s 后抛 `OSError`，被 `except` 吞掉 → `fd=None` 降级无锁、且句柄从不关闭（泄漏）。等于跨进程保护在 batch 高并发下悄悄失效却无人知晓。改：非阻塞 `LK_NBLCK` + 自旋到 30s 预算自持，失败关句柄并向 stderr 告警（同锁只报一次）。**实测**：8 进程 ×100 次 `_update_state` 累加 = 800/800 无丢更新；拆掉锁的对照组直接撞 `PermissionError`（连原子写都互冲突）
- **#2 排序漏点**：`postprocess` 三处仍字典序（stylegrid 帧收集、webp2mp4 目标、kenburns 帧序列），与 natkey 注释自称冲突 → 全改 natkey；`mg_batch` 打印在生成中的镜号同理
- **#3 拼接漏点**：`cmd_concat` 仍只 glob `clip_*.mp4`，.webp 分镜进不了成片 → 收集逻辑抽出 `_collect_clips()`（可单测），扩展名统一走 `PRODUCT_EXTS`；含非 mp4 分片时强制重编码（webp 动图进 concat demuxer 不可靠）
- **#4 一致性签名只比分辨率**：同分辨率不同编码（h264 vs vp9）仍走 `-c copy` 直拼 → probe 增补 `codec`、签名改三元组；filter 重编码分支原写死 `a=0` 会静默丢环境音（xfade 分支是保音轨的，两分支不对称）→ 按 `all(audio)` 补音频链
- **#5 文档不一致**：SKILL.md 字体变量写作 `FFMPEG_FONT`，代码是 `_FFMPEG_FONT`
- **清理**：删除已无调用者的 `_save_state()`（读改写竞态入口，留着会被误用回退）；`_download` 临时名带 pid（两 subprocess 同 out 曾互串）+ 失败清理残骸；节流文件惰性清理超过 1 天的 tag；`PRODUCT_EXTS` 常量收敛（此前 mg_batch 两处手写 `.mp4/.webp`）
- **测试 46 → 53**：新增 copy.main 端到端落盘冒烟、_FileLock 句柄释放、_download 失败无残骸、`_collect_clips` 自然序+webp、probe codec 解析

## v3.1.1（2026-09-06）

- **status 纳入 TTS 池**：v3.1 把本地 TTS 排成第一渠道但 status 里完全不可见（TTS 是 cmd_tts 硬编码单 key，不入 PROVIDERS 通用枚举）——本次在 cmd_status 补 TTS 专项段：配置行（key 掩码 + base + model，未配则并入 no_keys 提示）+ 小样合成探测。**探测不走 /models**（本地 Edge TTS 等服务 /models 返回空列表会误报），改发一次极短 /audio/speech 小样验证链路真通；音色双降级 Cherry→zh-CN-XiaoxiaoNeural（实测本地 Edge 服务拒 OpenAI 音色名 500，云端 OpenAI 系相反，双降级两边都覆盖）
- **env.example TTS 段合并**：云端/本地两套示例合并为一段，补"音色命名随服务商"说明

## v3.1（2026-09-06）

- **TTS 接入**：本地 OpenAI Edge TTS 服务（`localhost:5050`，`/v1/audio/speech`，模型 tts-1/tts-1-hd/gpt-4o-mini-tts）配进 `MEDIA_TTS_*` key 池——零代码改动（cmd_tts 本就是 OpenAI 兼容实现）；Edge 音色（zh-CN-XiaoxiaoNeural/YunxiNeural 等）与 `--speed` 实测可用（speed 真实生效，1.5 倍 17KB vs 1x 31KB）；SKILL 声音设计渠道优先级更新：本地 TTS 排第一（免费无限、不耗 key）

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

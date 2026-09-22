# ReelCraft 变更日志

> 历史演进记录，从 SKILL.md 外置（2026-09-06, v2.9）——SKILL.md 只保留当前有效指令，读历史到这来。

## 版本速查表（结构化索引）

> 回滚定位用：撞上"自己造的 bug"→ 用表里 commit 直接 `git diff <上一版commit>..<坏版本commit>` 看引入点，
> 或在 `git log` 里 `git show <commit>` 逐项核对。**回滚 = `git checkout <commit>^ -- scripts/ tests/` 拿回上一版文件**。
> 测试数 = 该版本全量 unittest 通项（skipped 不计）。

| 版本 | 日期 | commit | 测试数 | 一句话概括 | 引入了什么隐患（回滚重点） |
|---|---|---|---|---|---|
| **v4.16.0** | 2026-09-22 | 1cc8e0e | 654 | **② 中文词轴升级：word_axis 接入 sherpa（paraformer-zh）**——中文从"锚点+插值"升级为**逐字真声学时间戳**。阶段一（模型可得性验证，先验证后写码）实测：`sherpa-onnx` 1.13.8 有 `cp313-win_amd64` 轮子（2.2MB，pip 直连，零 torch）；`sherpa-onnx-paraformer-zh-2023-09-14` int8（232MB，hf-mirror 可下）给**可读汉字 token + 逐字时间戳**（29 字/29 时间戳，5.6s 音频解码 0.5s）；两模型文本互证（同 wav 转写一致）。实现：`sherpa_backend`/`available_backend`/`tokens_to_words`（纯函数）/`transcribe_sherpa`，`transcribe` 分派 auto=中文优先 sherpa、whisper 兜底；`vo_build plan --words` 加 `--word-backend`，auto 双缺时把安装指引递出去（die 2） | 新增可选依赖 `sherpa-onnx`（函数内延迟 import，AST 守卫 BANNED 已收录）；**默认行为变化**：中文词轴从 whisper 换 sherpa（效果提升，但模型多占 232MB）；★ **模型选择的三条实测教训**：paraformer-**small**（2024-03-09）不给时间戳勿用；zipformer-**ctc** 给时间戳但 token 是 byte-BPE（映射是查表非单一偏移，反推成本高）放弃；paraformer 的时间戳是 CIF 声学对齐，**不做能量吸附**（`timing=token-timestamps` 与 whisper 的 `anchors+energy-snapped` 如实区分）；显式 sherpa 的依赖校验只在 `transcribe_sherpa` **单一来源**（dispatch 里重复判=死代码，受控破坏当场抓到） |
| **v4.15.0** | 2026-09-22 | 2ecd644 | 635 | **字幕渲染 drawtext → ASS/libass（词轴终于上屏）**：①新增 7 个 ASS 纯函数（`ass_time`/`ass_text`/`ass_color`/`karaoke_text`/`ass_style_spec`/`cues_to_ass`/`ass_filter`）：cue（含 v4.13 词轴）→ 完整 ASS 文档 → `ass=` 烧录，**逐词高亮（`\k`）首次真正画到屏幕上**（drawtext 结构上做不到）；②默认通道改 ASS，`--subtitle-render drawtext` 留作回滚，`--no-karaoke` 关逐词；③★ **顺带修掉 drawtext 一个静默失败**：文案含裸 `%` 会触发 `Stray % near ...` → **整条字幕一个字都不画、rc 仍为 0**（旧版把 `%` 写成 `\%` 一样触发），正解是滤镜加 `expansion=none`；④`subtitle_presets.json` 增 `ass` 子字典（字体/字号/高亮色；底框→BorderStyle=3）；⑤逃生阀两个入口都能到（`pipeline.py` 与 `media_gen run`），clean 清单纳入 `_subs.ass` | 新增 CLI `--subtitle-render {ass,drawtext}` / `--subtitle-font` / `--no-karaoke`；**默认行为变化**：字幕由 drawtext 换 ASS —— 字体回退/描边/底框观感与原通道不逐像素一致，出问题用 `--subtitle-render drawtext` 秒退；ASS 的 `%` **不特殊**（照抄 drawtext 转义会多出反斜杠）；`ass=` 路径同样必须过 `ff_path()`；`_MANIFEST` 已同步（postprocess 39→47 函数）；**回滚本版要连 `caf5c93` 一起退**（media_gen 门面转发，单独一条补丁提交） |
| **v4.14.0** | 2026-09-22 | 3378570 | 593 | **③ ffmpeg 能力升级（零新依赖）**：①**filter 路径转义收单源** `ff_path()`——实测四种写法只有"单引号+转义冒号"或"双反斜杠"能活，裸 Windows 路径必崩（rc=4294967274）；②**去水印 v2**：`delogo_watermark --mode mask` 走 removelogo（mask 声明"这里永远是 logo"）；③**防抖**：`postprocess stabilize`（vidstab 两遍 detect→transform）；④**画质门禁**：`qcgate --ref <参考>` 用 ssim 量化劣化（**取不到值判 WARN 而非 PASS**）；⑤`concat --qsv` 核显编码——**本机实测三链路（纯编码/+drawtext/+subtitles）都出片且字幕真烧上**，此前"QSV 不能配字幕"的结论被推翻（根因是路径没转义）；⑥`concat --voice-speed`（rubberband 变速不变调，**有字幕时拒绝**以防字幕轴失效） | 新增 `ff_path`/`parse_ssim_stats`/`ssim_verdict`/`resolve_encoder`/`voice_speed_filter`/`qsv_available`/`cmd_stabilize` 与 `delogo.mask_filter`/`removelogo_filter`；`--qsv`/`--voice-speed`/`--ref`/`--min-ssim`/`--mode` **全部默认不启用**（旧调用零影响）；⚠️ `ff_path` **返回值自带单引号**，调用方再套引号会崩；⚠️ `unsharp` **不是** vidstabtransform 的选项（别凭记忆写参数） |
| **v4.13.0** | 2026-09-22 | 73b516f | 557 | **词级时间轴接线：plan_axis 带词轴 + 字幕按词切**：①`plan_axis` 接受句内词轴（相对句首）→ 转成片绝对时间挂在 line 上；越界词夹回句内并计入 `word_stats`（**不许静默夹**）；②新纯函数 `split_line_cues`——有词轴时**只在词边界断行**（限宽按显示宽度：CJK 计 2、拉丁计 1；单条 ≤40 宽 / 6s；尾片 <0.3s 并回前一条），文本优先**按词定位从原文切片**（标点不丢），对不上原文则回退词拼接；无词轴时输出与旧版逐字一致；③`vo_build plan --words` 对每句录音跑词轴并写进 `vo_lines_at.json`，缺模型/依赖 **die(2) 给安装指引**（绝不静默降级）；④修 `word_axis.transcribe` 显式点名后端却不校验模型存在 → 抛"模型不认识语言 xx"这类误导性错误 | 新增 plan 参数 `--words/--word-lang/--word-models-dir`（默认关，旧调用零影响）；`plan_axis` 返回值多一个 `word_stats` 键、有词轴时 line 多一个 `words` 键（旧字段不变）；**字幕条数会因按词切行而变多**（下游若假设"字幕条数 == 句数"会失效）；`--words` 缺模型时 rc=2 而非静默跳过 |
| **v4.12.0** | 2026-09-22 | c599a48 | 539 | **批六：词级时间轴（② 的核心引擎）**——新旁线 `scripts/word_axis.py`，从零实现 ONNX Whisper 前端与解码（**零 torch**）：①**mel 前端**（Slaney 三角滤波器组 + center-STFT + 丢末帧 + `log10(clamp(1e-10))` + `max(x,max−8)` + `(x+4)/4`，纯 numpy；静音恒为 −1.5 是链路锚点）；②**贪婪解码**；③**时间戳插值** `<|t.dd|>` 锚点 + **能量谷吸附**精修词边界（谷不够深就不动）；④**分词**（CJK 一字一词、多字 token 在内均分、拉丁按空格）；⑤`media_gen` 之外可独立 CLI（`--json`/`--lang`/`--models-dir`）。**★ 路线裁定**：whisper.cpp **缺件放弃**（二进制可下，但 ggml 模型在 HF/镜像/ggml.ggerganov.com/ModelScope/gitee **全源不可达**）→ 改走 ModelScope 的 `onnx-community/whisper-base`（int8 编解码 + tokenizer ≈79MB），复用本机已装的 onnxruntime | 新增脚本 `word_axis.py`（`_MANIFEST` 已登记）+ 新测试文件 `test_word_axis.py`；新增可选依赖 `tokenizers`（~3MB，**无 torch**）与 `~/.workbuddy/models/whisper-base-onnx/`（79MB，**不入仓**）；缺任一项 → rc=2 + 安装指引。⚠️ **解码故意走"每步整句重算"（O(n²)）**：merged 解码器的 KV-cache 支路实测与全量重算**数值不一致**（首步 argmax 相同但 logits 最大差 2.86，累积后 11s 转写从 JFK 名言变成 "they're going to be arrested"）——**改回 cache 支路会让转写变错**；⚠️ 默认**压制 `<|notimestamps|>`（50363）**，否则模型自选无时间戳模式（首步 p=0.688）→ 一个锚点都不吐、词轴全挤在 0.0s；⚠️ 词时间是"锚点+能量吸附"**不是 DTW**（本导出不回吐 cross-attention，真 DTW 需换导出）；11s 音频实测 6.9s 转完 |
| **v4.11.0** | 2026-09-21 | 5459ac5 | 501 | **批五：健壮性四小项 + "记录字段"收尾（不新增能力，只消歧义）**：①`copy.py` 重试退避抽成纯函数 `backoff_delays`，**等待次数 = 尝试次数 − 1**（旧版循环体末尾无条件 `sleep(8*(i+1))`，三次全灭白等 24s、累计 48s；且线性硬编码与 `mg_core.http_call` 的指数退避不同族）；②`delogo_watermark.die` 由 `sys.exit(msg)`（传字符串走隐式行为）改为显式 `print(前缀, stderr)` + `sys.exit(int code)`，与另四份 `die` 同形；③退出码约定收单源 `mg_core.EXIT_OK/FAIL/USAGE/TIMEOUT`，`audio_qc`/`face_consistency` 的 `DIE_ARG` 改为引用（实测全库 `sys.exit` 分布 0/1/2/4——**没有 3**）；④`export_public` 的 DST 支持 `argv` > `$REELCRAFT_PUBLIC_DIR` > 内置默认，并在 main 打印源/目标（旧版硬编码单一路径，工作区搬家会静默写到幽灵目录）；⑤`role_assign`/`tier_map` 在文档与 `plan-check` 中显式标为**记录字段（不驱动行为）** | `copy.py` 多出公开函数 `backoff_delays`（`_MANIFEST` 已登记）；重试总等待由 48s 降到 24s（**失败路径更快抛出**，调用方若依赖长等待需注意）；`delogo` 的 die 输出格式变了（多了 `[delogo] ERROR:` 前缀，**若有人 grep 旧输出需同步**）；`export_public.resolve_dst()` 新增（模块级 `DST` 改为经它解析，行为对未设 env/argv 的本机默认**保持不变**）；`audio_qc`/`face_consistency` 新增 `import mg_core`（无循环：mg_core 不反向引用二者） |
| **v4.10.2** | 2026-09-19 | 5763f82 | 484 | **门禁接线（把做好的能力真正接进一键流程）**：①`audio-qc`(v4.10.0) 与 `faces`(v4.10.1) 此前只在 `media_gen` 里能手动调，在 `pipeline` 里出现 **0 次**——跑 `media_gen run` **根本不执行**（"功能存在 ≠ 功能生效"，与 v4.7.9~v4.9.0 那批"决策零生效"同族，只是这次在输出侧）。现在 concat **前**依次自动跑 `audio-qc` → `faces` → `qcseq`，结果写进 `pipeline_run.json`、`audit` 会读出来；②新增 `qc_gate_action()` 统一消化**两族退出码**（报告族 qcseq/faces：1=WARN；门禁族 qcgate/audio-qc：非 strict 时 WARN 也返 0）——共同硬判据只有 `rc>=2`；③开关 `--no-audio-qc` / `--audio-qc-strict` / `--no-faces`，`media_gen run` 同步转发；④faces 缺 cv2/模型 → **跳过并说明**（不因可选依赖拦流程，也不假装通过）；⑤顺手修：`test_days_filter_drops_old` 是颗**时间炸弹**（硬编码 `2026-09-09` 又断言"7 天内"→ 到 2026-09-16 之后必红）改成相对时间 | ⚠️ **默认行为有变**：以前 `run` 不跑这两道门禁，现在会跑（这正是本版目的；想回到旧行为用 `--no-audio-qc --no-faces`）。手搓 `pipeline` Namespace 的调用方需补三个属性 `no_audio_qc` / `audio_qc_strict` / `no_faces`（test_postprocess 两处已补）。`audio_qc.py` 的 WARN 仍返 0（与 qcgate 同族，**未改**其契约） |
| **v4.10.1** | 2026-09-15 | 90a7eac | 473 | **跨镜人脸身份一致性（补 qcseq 只比色调的错配）**：新旁线 `scripts/face_consistency.py`——YuNet 检测（227KB）+ SFace 128 维嵌入（37.8MB）+ 余弦比对，判"**是不是同一个人**"；`media_gen.py faces <path\|dir>` 直达，`--ref 设定图` 以角色圣经基准比对。判定全是**纯函数**（`cosine`/`filter_faces`/`pick_primary`/`pairwise_similarity`/`mean_similarity`/`outliers`/`consistency_verdict`），检测/嵌入是薄 IO 壳。三态退出码 0/1/2（与 qcseq 同口味）。**另：`_MANIFEST` 结构快照从 7 个核心脚本扩展到全部 17 个**，并加"新脚本必须登记"守卫 | 新增子命令 `faces`；**依赖 `opencv-python-headless` + 两个 ONNX 模型**（放 `~/.workbuddy/models/`），缺任一则打安装指引 + rc=1（**不假装通过**）；⚠️ **本仓 `scripts/copy.py` 会遮蔽标准库 `copy`**，故 cv2 一律经 `_import_cv2()` 助手（导入期摘掉 `scripts/`）——否则真机 CLI 静默降级而测试全绿；判定阈值 0.363 是 SFace 官方 LFW 标定值，改它等于改松紧；新增脚本未登记 `_MANIFEST` 会让结构测试红灯（**这是有意的**） |
| **v4.10.0** | 2026-09-15 | 6f3c956 | 427 | **音频侧 QC + 语音判定（补 qcgate 只查画面的盲区）**：①新旁线 `scripts/audio_qc.py`——静音占比 / 采样削波 / 平均响度 / 长静音段四类机器可判项（`silencedetect`+`volumedetect`+`astats`，**纯 ffmpeg 零依赖**），`media_gen.py audio-qc <path\|dir>` 直达；②**语音判定 VAD**：优先 **silero-vad ONNX**（onnxruntime，CPU 秒级），缺依赖/推理失败**如实回退** ffmpeg 能量法并在 `backend` 字段标注（不假装）；③`triage` 加 **VAD 前置**——本地判"根本没语音"时直接给"补旁白"结论，**省掉一次远端 ASR 额度**；④可选重依赖机制：onnxruntime/numpy 一律**函数内延迟 import**，模型放 `~/.workbuddy/models/` | 新增子命令 `audio-qc`；`triage_decision()` 新增**可选**参数 `speech`（默认 None → 行为与旧版逐字一致）；triage 每镜输出新增 `vad` 字段；**语义提醒：silero 对纯音乐/正弦会判"非语音"，而能量法会判"有语音"——这是升级不是 bug**（它识的是语音而非能量）；AST 守卫「可选重依赖不得顶层 import」+「silero 必须有 InferenceSession 调用（反空壳）」 |
| **v4.9.0** | 2026-09-14 | 5596911 | 388 | **成本门禁 + 角色圣经（漫剧第一步）**：①**事前调用门禁**——批量前预估 `镜数 × (1+重试)` 上限并在超限时**拒跑**（`--max-calls` / plan.json 的 `max_calls`），补上 ledger 只有事后记账的缺口；②**角色圣经** `<shots>/characters.json`——角色设定图定义一次、shot 写 `characters:["hero"]` 引用，与自带 `ref_image` 合并去重；id 查不到报 MISS（不静默丢一致性）；无该文件时行为与旧版完全一致 | `--max-calls` 默认 0 = 不设门禁（旧调用零影响）；plan.json 的 `max_calls` 非正整数会被忽略；角色圣经缺失/损坏/非 dict 一律当空（不炸 batch）；`mock` 掉的 `make_cmd` 调用方需多传一个可选参数 `bible`（有默认值，位置参数不变） |
| **v4.8.0** | 2026-09-14 | b59d7c7 | 376 | **补齐决策链 + 单源收编 + 五处崩溃/误判**：①`video_pool_order` 接入（问⑤落盘的池顺序此前**无任何代码消费**，只在 PLAN_KNOWN_KEYS 里"合法"）→ 新增 `resolve_pool_order`，转成 batch 的 `--provider a,b,c`；②`_ffmpeg` 收单源（postprocess/vo_build 各带 `_ffmpeg_cache` 副本、audio_triage 那份**漏了缓存**每次重探测）→ 统一 `mg_core._ffmpeg`；③TTS 槽位枚举单源（audio_triage 本地副本与 mg_core 口径漂移：status 报已配、triage 报未配）；④qcgate 抽帧 `duration` 兜底 5.0 → **时长未知只抽首帧**（原 30s 片只测开头 5s，后半段黑帧/静帧漏检却打 PASS）；⑤delogo：`probe_size` 逐行跳过 mjpeg（带封面图的 mp4 原取首个匹配 → 拿 320x240 算框位）+ `clamp_box` 每维独立钳制（原 OR 一刀切，框比画面大时钳完仍越界却打印"已钳制"）；⑥envcheck `runner` 返回 None → `r.stdout` 崩；⑦prompt_lint `_atomic: null` → `None.get` 崩 | CLI 未变；`_ffmpeg` 现在从 mg_core 懒加载缓存（首次调用即探测，行为不变）；qcgate 在时长未知时**帧数变少**（只 1 帧，靠 duration_verdict 的 WARN 提示人工复核）；delogo 遇"框 ≥ 画面"现在**直接 die**（原来静默钳制成越界框）；新增守卫「非 mg_core 不得定义 `_ffmpeg`/`_load_env_file`」 |
| **v4.7.9** | 2026-09-14 | 8d5dda1 | 357 | **脱敏绕过 + 决策零生效 + 单源收编**：①export 遇非 UTF-8 文件原直接 `continue`（跳过标记块删除）→ 私有内容原样进公开仓且自检命中不了；改为**拒绝导出**（`non_utf8_marker_error`，拒绝 > 泄漏）；②pipeline 声称"workers/池顺序/水印读自 plan.json"实际只读 CLI 默认值 → 问⑤落盘的 `workers_image/workers_video/watermark` **零生效**；新增 `resolve_workers`/`resolve_watermark` + CLI 默认改 `None`（`media_gen run` 的转发链同步，否则恒真默认值覆盖 plan）；③单源收编：`copy.py` 的 env 加载（本地副本正则 `="(.*)"$` 要求行尾收引号 → **带行内注释的 key 行静默读不到**，与 mg_core 已漂移）、`PRODUCT_EXTS` 三处复刻（pipeline×2 + vo_build，守卫新抓到 vo_build 那处）+ 三条新守卫（产物白名单字面量/env 加载实现/workers 默认值） | CLI `--workers-image/--workers-video` 默认值由 3 改 **None**（不给则读 plan.json，再缺省 3）——手搓 Namespace 的旧脚本若依赖"未给=3"需显式传；非 UTF-8 文件现在会让导出**非零退出**（先转 UTF-8）；`video_pool_order` 仍未接（本轮只解决 workers/watermark） |
| **v4.7.8** | 2026-09-12 | 8632ae4 | 342 | **三项 P1（静默丢钱/死循环）**：①caps 探针加内层 `--poll-timeout`——外层 `run_capture` 到点会**杀进程**，任务来不及落盘 → 已受理（已扣费）任务永久丢失，且提示的 harvest 无记录可收；②`_save_pending_task` 记 `key_n`，`--wait-task` 续等改用**提交时那把 key**——多 key 池（agnes 跨域名）用 `keys[0]` 会 401/404 空转到 deadline；③`list_keys` 补读 `_REF_IMAGE_STYLE/_REF_IMAGE_MAX/_KEYFRAMES_STYLE` + 新增 `ref_image_candidates()`（池或任一 key 声明即可作候选）——此前报错提示让用户配这些 env 却无人读，是**自指死循环**，key 级覆盖分支也从不触发 | 探针内层超时 = 外层 - 60s（下限 60）：外层配得过小时内层仍是 60s；续等记录里的 key 已从 env 移除时退回 `keys[0]`（旧记录兼容）；`ref_image_candidates` 会枚举 key（多一次 env 扫描，仅在有参考图时触发） |
| **v4.7.7** | 2026-09-12 | b4bb94c | 333 | **三个 P0 修复（全库复测实证发现）**：①`mg_batch` 补 `import mg_core`——harvest 一跑 `NameError`（v4.7.6 引入，328 测试全绿没抓到：harvest 零行为测试）；②`vo_build` 首次 TTS 判空——`f=None` 时 `f.exists()` AttributeError，且崩在**调用 TTS 之前**；③concat 单路音频改 `apad=whole_dur=<成片时长>`——原单路无 apad，`-shortest` 把画面截到旁白长度（实证 10s 画面+3s 旁白 → 4.02s 成片，rc=0） | ⚠️ **apad 必须带 `whole_dur`**：无参 apad 造无限音频流、本 ffmpeg 版本 `-shortest` 不终止它 → 整个 concat **挂起**（实证 40s 未结束）；多路分支的 pad 也随之从 `,apad` 改为 `,apad=whole_dur=X`（语义等价、更精确）；新增 AST 守卫「用了本仓模块名却没 import」；harvest / concat 单路 / vo_build 首次三条路径**首次有行为测试** |
| **v4.7.6** | 2026-09-11 | 5879399 | 328 | **批四（查询入口 + 轮询单源）**：①mg_status 新增能力档案汇总段（声明/实测✅/实测❌/过期/声明未测，延迟 import mg_caps 防循环，caps 损坏不拖垮 status）；②轮询循环收归 mg_core 单源——`build_poll_url`（path/query URL 构造）+ `fetch_task_state`（收割单查）+ `poll_tasks`（生成器骨架），`_resolve_async_task`/`_poll_video_task`/`cmd_edit`/harvest 双函数五处调用方改造，网络语义（interval/headers/终态/超时动作）原样留在调用方 | `build_poll_url` 缺省 style 为 query（与旧 `_poll_video_task` else 分支一致，agnes 依赖——改默认会坏 agnes 轮询）；新增 AST 守卫"while 内 urlopen 只许在 mg_core"；harvest 视频侧现在按池 poll_style 构造 URL（旧版硬编码 path 拼接，custom query 池的旧落盘任务受影响——记录里 poll_path 优先缓解） |
| **v4.7.5** | 2026-09-11 | 6600194 | 313 | **批三（P1 清尾 + 两项核验为误报）**：轮询路径支持 local_path 直拷（取产物统一入口）、vo_build 缺 at 字段前置校验（TTS 前拦）、pipeline 透传 --negative/--video-size/--video-duration/--qc、clean 纳入 _norm/_mixed/_with_text 中间文件 | pipeline 新增四个可选参数（默认不追加，旧调用零影响）；字体冒号转义与 cmd_edit 无 key 两项审计 P1 经真机核验为**误报**，不修（详见正文） |
| **v4.7.4** | 2026-09-11 | c22d26b | 305 | **批二（结构 + 能力）**：caps 能力级真探针（ref_image/keyframes 真跑落档）、--only × 过渡桥兼容（hybrid 核心场景）、概念收敛（is_transition/find_product 收归 mg_core + 三道单源守卫）、探针执行器去重 | 过渡镜在 --only 下的语义变了（邻居可接就跑）——只想跑纯普通镜的老脚本不受影响；caps 记录新增 `<kind>:cap:<cap>` 键（旧读取逻辑不受影响） |
| **v4.7.3** | 2026-09-11 | 237e16a | 294 | **批一修复（钱 + 假合格）**：POST 读超时不再重试/换 key（防重复扣费）、zhipu payload 并入统一构造器（--last-frame 显式报错 / --negative 显式警告）、时长未知改 WARN 不再假 PASS、TTS 槽位扫描收归单源、--ref-image 可变默认值修复 | `http_call` 的重试语义变了：POST"发出后失败"现在抛 RequestUncertain（exit 4），**不再自动重试 3 次**——依赖旧行为盲重试的脚本要适配；zhipu + --last-frame 从"静默忽略"变"报错退出码 2" |
| **v4.7.2** | 2026-09-11 | 41a2bcc | 262 | **全库复测（code-review 五路并行 + 逐条核验）**：修 4 个 P0 静默失败——concat 混编丢音轨、静帧+仅旁白 ffmpeg 无限挂起、batch 的 MISS/STALE 被当成功吞掉、导出脱敏自检三处绕过 | 详见 `reelcraft复测审计报告.md`；`audio_plan` 三态（none/all/mixed）语义别退回 all()；blocked 镜现在会非零退出码（旧脚本若忽略退出码需适配） |
| **v4.7.1** | 2026-09-11 | 4db3de1 | 253 | **收口 v4.7 尾巴 + 上结构性防线**：`ref_image` 进 batch（角色一致性可用于主流程）；过渡镜与普通镜共用执行器 `run_shot_once`（补回 qcgate/qc、消除 70 行重复）；**新增 `tests/test_structure.py`**（顶层函数清单快照 + 子进程入口守卫 + CLI --help 冒烟） | 结构快照 `_MANIFEST` 变更需**先核对结构再更新**（否则防线失效）；`--help` 冒烟走 SLOW 门控；pass2 仍单线程（过渡镜多时慢） |
| **v4.7** | 2026-09-11 | 149c6c4 | 242 | **实测驱动的能力解锁**：Agnes 免费池原生支持多图参考（`--ref-image`，角色一致性）与首尾帧 keyframes（过渡镜不再需付费池）；大图自动转 JPEG 防上传超时；video payload 提为纯函数 | 参考图**必须走 extra_body**（顶层会被静默忽略、退化成 t2i，无报错）；`image_to_uri_shrunk` 仅在 >450KB 时改编码（小图行为不变）；keyframes 需首尾帧齐全，只给尾帧会 die |
| **v4.6** | 2026-09-11 | 339803b | 229 | 过渡镜一等公民（方案二）：`transition` 字段 + batch 两趟调度（pass1 普通镜→抽 from 末帧→pass2 过渡镜）+ STALE 失效链 + audit 待邻出片；**顺带修 P0：make_cmd worker 子命令曾指向无 `__main__` 的 mg_batch.py（真实 batch 静默空跑记 OK）** | 过渡镜 seed（frames/<sid>_seed.png）mtime 比 from clip 旧判 STALE——系统时间回拨会误判；hybrid `--only` 不含过渡镜时需手工跑 batch（两趟调度只在全量 videos 阶段触发）；仍需接支持双条件的池才能真跑 |
| **v4.5** | 2026-09-10 | 48a8a06 | 222 | 过渡镜（首尾帧双条件）：`video --last-frame` + batch 透传 + 池/key 级字段名配置 | 免费池（agnes/zhipu）不支持双条件——功能就绪但需接支持的池才能真跑；`last_frame` 图缺失时 batch 报 MISS 跳过不提交 |
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

## v4.7.5（2026-09-11）

**批三：清掉审计报告"已核验未修"里的 P1 剩余项**（TDD 红→绿 + 真机核验）。
本轮亮点是**两项审计 P1 被真机核验推翻**——先验证再动手的老规矩又救了一次。

**① 轮询路径支持 local_path（P1-1，钱+失败类）**
`_extract_video_url` 认得 local_path 并原样返回，但 `_download` 对它 urlopen →
ValueError（urlopen 不吃 Windows 盘符路径）。同步路径（cmd_video 的 LTXBridge
分支）有 shutil.copyfile 保护，轮询路径没有——同一条链路两种行为。
现在 `_download` 是**取产物的统一入口**：非 http(s) 一律本地直拷（copyfile +
原子替换 + 失败清残骸），URL 走原带超时下载。受控破坏验证过（禁用本地分支 → 红灯）。

**② vo_build 缺 at 字段前置校验（P1-5）**
`lines[i]["at"]` 直下标——手写 vo_lines_at.json 少个 at 就 KeyError 裸奔 exit 1。
校验放在 TTS/录音检查**之前**：错误的数据不该开始烧合成额度。报错指认到行 id。

**③ pipeline 透传（P1-6）**
视频阶段 batch 命令只透传了 provider/only/qcgate——`--negative/--video-size/
--video-duration/--qc` 被静默丢弃（"以为用了自定义 negative，实际是默认值"）。
现在四个参数全透传；**未给参数不追加**（batch 各池默认行为不变）；getattr 防御
旧调用方手工构造的 Namespace。

**④ clean 纳入 concat/字幕中间文件（P2）**
`_norm.mp4 / _mixed.mp4 / _with_text.mp4` 散落在 clips/ 里，clean 治理面扫不到，
越积越多。加入 `_CLEAN_FILES`（trash not delete 语义不变）。

**⑤ 两项审计 P1 核验为误报（不修，记档防复发）**
- **字幕冒号转义**：真机 ffmpeg 实测——单引号 fontfile 内**不转义反而失败**
  （rc=4294967274 filter 解析错），现有 `C\:/...` 转义才是正确做法。审计
  报告自己标了"需真机验证"，这条标救了一命——不验证就把好代码改坏了。
- **cmd_edit 无 key 裸奔**：key 从 `~/.workbuddy/media_keys.env` **文件**加载
  （非进程 env），`list_keys(required=True)` 已 die(2) 带指引。真机复现的
  HTTP 400 是测试图格式错（b"x" 不是图），不是校验缺失。

测试 305 → **313**（+8：local_path 2 + at 校验 2 + 透传 2 + clean 1 + 文档性 1）；
快测 13s 全绿。
## v4.7.4（2026-09-11）

**批二：结构性防线 + 能力制度化**（改代码 skills，TDD 红→绿；三件事全落在"同一类问题"上——
概念没有单一真相源）。

**① caps 能力级真探针（把"实测过"制度化）**
池级冒烟只证"能出片"，不证"参考图真锁脸 / 首尾帧真插值"——v4.7 踩过的坑（顶层 image 被静默忽略、
大图直传超时）只有真跑才知道。新增：
- `caps probe <pool> --kind <k> --cap ref_image|keyframes --real`：64px fixture 图现场生成，
  真跑一次，结果落 `.media_caps.json` 的 `<kind>:cap:<cap>` 键（不覆盖池级冒烟）
- `caps show` 显示能力行：`实测✅ / 实测❌ / 声明未测（附可执行命令）`
- 未声明就拒绝探测（白烧一次额度）；`_execute_smoke` 从 `_real_smoke` 抽出共用
  （探针执行器写两遍必漂移——本批主题的自我实践）
- 真机验证：agnes/ref_image 探针 35.6s 出片落档 ✅

**② --only × 过渡桥兼容（hybrid 核心场景）**
过渡镜**不按 --only 硬滤**：邻居在本次运行 → pass2 接上；from 镜不在运行但有历史 clip →
seed 照抽；from 完全带不动 → 排除并提示（不留在计划里刷 MISS）。
hybrid 流程从此能直接出"重点镜 + 过渡镜"，不用再手工全量 batch。

**③ 概念收敛（单一真相源）**
- `is_transition` / `find_product` 收归 mg_core：此前过渡镜判定 mg_batch 一份 + pipeline 内联两处、
  clip 查找两份实现（`_find_clip` / `_find_dep_clip`）
- 三道新守卫（test_structure）：共享概念禁止本地重定义 / 旧内联名禁止复活 /
  （承 v4.7.3）可变默认值与裸编码器
- 受控破坏验证守卫有效性（假 def → 红灯）

测试 294 → **305**（+11：能力探针 6 + only×过渡 3 + 单源守卫 2）；快测 15s / SLOW 全绿。

## v4.7.3（2026-09-11）

**批一：把审计报告里"还在冒烟"的高价值项清掉**（改代码 skills TDD 红→绿，真机验证）。

**① POST 读超时 → 不再重试/换 key（重复扣费，最贵的一条）**
旧 `http_call` 的通用 `except` 对读超时也照常重试 3 次——但 POST 可能已被服务端受理，
每次重提都是一笔新任务。新增 `classify_network_error()`（纯函数）+ `RequestUncertain` 异常：
- `uncertain`（POST/PUT/PATCH 已发出后超时/被掐断）→ **不重试、不换 key、不跨池**，直接抛出；
  `call_with_failover` 落账（这可能是一笔已产生的费用）后 raise；上层 `die(..., 4)` 走超时在途协议。
- `retry`（连接被拒 / DNS 失败 / GET 一切）→ 照旧重试，不误伤可用性。
URLError 会把真实原因包在 `.reason` 里，必须拆开判——否则读超时被误判成普通连接错误。

**② zhipu payload 并入 `build_video_payload`（消灭分池分支漂移）**
cmd_video 里 zhipu 另起一份 payload 构造 → `--last-frame` / `--negative` 被**无声丢弃**、
`image_url` 走未压缩编码（大图直传会读超时）。现在所有池走唯一入口：
zhipu + `--last-frame` 显式 die(2)；不支持的 `--negative` 显式 warn（每池一次，batch 不刷屏）。

**③ 时长未知 ≠ 合规（假合格）**
`probe` 拿不到 Duration 时旧逻辑兜底成 0 → `0 <= max` 恒真 → check/qcgate 的时长校验**恒 PASS**。
新增 `duration_verdict()` 三态（ok/warn/fail）：warn 显式打出"时长无法判定"，
qcgate `--strict` 下升级为 FAIL。**webp 动图合法地没有 Duration，所以默认不拦只喊。**

**④ TTS 槽位扫描收归单源**：`scan_tts_slots` 移入 mg_core，media_gen 保留别名、mg_status 改用共享实现
——修复 status 在 key#1 空缺时误报"tts 未配置"（口径漂移）。

**⑤ `--ref-image` 可变默认值**：`default=[]` → `default=None`（消费侧 `or []`）；
新增 AST 守卫 `TestNoMutableArgparseDefaults` 防全库复发。

**⑥ 新守卫**：`TestShrunkEncoderCoverage` —— 输入图编码必须走压缩版
（裸编码器只允许在 mg_core 内部出现），防止"大图超时"这类覆盖缺口再冒出来。

测试 262 → **294**（+32）；快测 12s / SLOW 全绿；真机验证：agnes 出图 29s 正常（新分类不伤正常路径）。

## v4.7.2（2026-09-11）

**全库复测审计**（用户要求"把整个生视频 skills 复测核实一遍"）。方法：改代码 skills 的
code-review 双轴精神 → 适配为全库审查：5 路并行只读子代理分域审（带 Fowler 坏味道基线）
+ **逐条核验后才采信** + 真机复现。完整报告见工作区 `reelcraft复测审计报告.md`。

修掉 4 个 P0（全部是"rc=0 但结果是错的"这类静默失败）：

- **concat 混编音轨被静默丢弃**：`all(audio)` 判定下，只要有任一分片无音轨（hybrid 模式
  的常态：真视频带环境音 + kenburns 静帧片段）整卷音轨被丢。→ 新增 `audio_plan()`
  三态判定 + `audio_src()` 给无音轨片补等长静音；`-c copy` 快路径在音轨不一致时改走重编码。
- **静帧成片 + 仅加旁白 → ffmpeg 无限挂起**：`amix=inputs=1`（amix 要求 ≥2 输入）不报错、
  不退出，永久挂起。→ 单路直通（acopy），单路时不再 apad。
  **修的过程二次踩坑**：首版把 `run()` 留在 elif 分支内 → rc=0 但旁白被静默丢弃，
  靠 ffprobe 内容级检查才发现。
- **batch 的 MISS/STALE 被当成功吞掉**：缺帧/缺依赖的镜不进账单、不算失败、退出码 0
  → 成片静默缺段且 `--retry-failed` 抓不到。→ 新增 `skip_kind()`/`record_skip()`，
  blocked 进结果与账单并计入退出码；pipeline 分开报 blocked/failed；修 audit 标签切片乱码。
- **导出脱敏自检三处静默绕过**：只扫内容不扫文件名、非 UTF-8 文件直接 continue、
  标记块删除遇非 UTF-8 静默跳过。→ 文件名一并扫描、非 UTF-8 用 replace 继续扫并列出待转码。

测试 253 → **262**（+9：audio_plan/audio_src 3 + 混编与单路旁白端到端 3 + blocked 分类 3）。
另修 `probe` 重复调用（每片 2 次 → 1 次）。

报告同时列出**已核验但本轮未修**的 P1/P2 清单（轮询 local_path、大图压缩未全覆盖、
响应超时重试可能重复扣费、status 与 cmd_tts 的 TTS 槽位漂移、probe 时长假 PASS、
pipeline 未透传 --video-size/--negative 等）。

## v4.7.1（2026-09-11）

收口 v4.7 的尾巴 + 把反复出事故的两类问题变成防线。

- **`ref_image` 进 batch（补 v4.7 尾巴）**：v4.7 只加了单命令 `image --ref-image`，
  批量主流程用不了。现在 shot JSON 写 `ref_image`（字符串或列表）即自动透传，
  相对路径按 shots 根解析；参考图缺失 → MISS 不提交。
  真机验证：batch images 带角色参考出片成功，角色特征完整保留。
- **过渡镜与普通镜共用执行器 `run_shot_once`**：pass2（过渡镜串行）原先手写了一份
  与 worker 重复约 70 行的重试/记结果逻辑，且**漏掉 qcgate/qc 质量门禁**。
  现抽为单一实现，两处共用 → 门禁补回、重复消除、以后加镜头级能力只需改一处
  （locality 修复）。
- **新增结构性防线 `tests/test_structure.py`**（对 AST 校验与 dry-run 盲区的补位）：
  1. **顶层函数清单快照**（7 个核心模块）：def 被插进别的函数体中间时，它会从
     top-level 清单消失/多出 → 立刻红灯。用受控破坏验证过能抓到。
  2. **子进程入口守卫**：扫 `parent / "X.py"` 引用，目标必须存在且带 `__main__`
     ——正是 mg_batch 静默空跑那次事故的形状。同样用受控破坏验证过。
  3. **CLI `--help` 冒烟**（SLOW 门控）：带 argparse 的脚本真跑一遍帮助。
- 测试 242 → **253**（+11：batch 参考图 3 + run_shot_once 3 + 结构防线 5）。

## v4.7（2026-09-11）

**实测驱动的能力解锁**：调研 GitHub 开源漫剧项目时，从 `vvlife/agnes-comic-drama` 的源码
发现 Agnes 免费池可能支持多图参考与首尾帧，遂写探针实测（`_probe_agnes/`）——**两条都被证实**，
且顺带发现一个静默坑。v4.5/v4.6 的"免费池不支持双条件"边界**被推翻**。

- **实测结论（零成本）**：
  - `extra_body.image=[...]`（≤4 张）→ 走 `/images/i2i/`，**角色特征完整保留**（真图生图）
  - 顶层 `image` → 走 `/images/t2i/`，**静默忽略无报错**（退化成文生图）——最坑的一条
  - 视频 `extra_body={"image":[首,尾],"mode":"keyframes"}` → **真插值**（末帧≈第二关键帧）
  - 大图直传会超时：960KB PNG → 2m23s 读超时；同图缩至 220KB → 18s 成功
- **新增 `image --ref-image`**（可重复）：参考图生图，用于角色/风格一致性。池能力声明
  `ref_image_style: extra_body` + `ref_image_max`（Agnes = 4）。
- **新增 `video --last-frame` 的 keyframes 分支**：`keyframes_style == "extra_body"` 的池
  （Agnes）自动走 `extra_body` 双帧插值，**不传顶层首帧**（双帧必须成组）；custom 池的
  `last_frame_param` 路径老行为不变。
- **新增 `mg_core.image_to_uri_shrunk`**：本地图 >450KB 时先转 JPEG（保尺寸、必要时缩边）
  再编码；小文件走原路径（零回归）。修的是"大图上传超时"这个真机才暴露的问题。
- **重构 `media_gen.build_video_payload`**：payload 构造提为纯函数（与 zhipu 分支解耦），
  三种首尾帧承载方式的差异第一次可被单测钉死。
- 测试 229 → **242**（+13：池声明/`keyframes_supported`/`ref_image_supported`/
  `build_image_body` 共 5 + `image_to_uri_shrunk` 4 + `build_video_payload` 分支 4，含 keyframes 无首帧 die）。
- **端到端真机验证**（非 dry-run，v4.6 的教训）：`--ref-image` 用 960KB 原图出片成功（17s）；
  `--last-frame` keyframes 出片成功（2m19s，抽帧确认末帧=第二关键帧）。
- 教训沉淀：**dry-run 和单元测试都覆盖不了"上游对参数/体积的真实反应"**——API 行为类改动
  必须真跑一发；第三方 repo 的源码是**可验证线索**而非结论，值得写探针去证。

## v4.6（2026-09-11）

过渡镜一等公民（讨论定案的"方案二：分镜 Schema + 编排依赖"）：v4.5 半自动方案里
"过渡镜何时能跑"靠人判断，本版升级为**编排自动理解 DAG 依赖**——过渡镜必须等
from 镜出片 + to 镜出图才能跑，batch 自动排两趟。

- **Schema**：过渡镜 = 独立 shot JSON（如 `S01T`），写
  `transition: {"from": "S01", "to": "S02"}`，无需 t2i_prompt（首帧来自邻居）。
  **natkey 排序天然把 `S01T` 落在 S01 与 S02 之间**（`['s',1,'t']` 恰在
  `['s',1]` 与 `['s',2]` 之间，已实测验证）——concat/qcseq 的 clip 排序
  **零改动**，这是方案二最大的雷区（时间轴连锁）被绕开的根因。
- **两趟调度（mg_batch.cmd_batch）**：videos 阶段 pass1 只跑普通镜（原 worker
  机制不动）→ 每个过渡镜用 `media_gen last-frame` 抽 from clip 末帧落
  `frames/<sid>_seed.png` → pass2 单线程逐镜跑过渡镜（`--image seed
  --last-frame frames/<to>.png`）。缺依赖报 MISS 跳过不提交。
- **失效链（STALE）**：from 镜重拍后 clip 比 seed 新 → make_cmd 报
  `STALE` 提示，pass2 的抽帧逻辑自动重抽（mtime 比较）。邻居重拍不会带着
  旧 seed 出"接不上"的过渡片。
- **孤儿校验**：transition 的 from/to 指向不存在的镜 → 提前 die 列出全部
  缺失依赖（否则静默 MISS 死循环，用户无从排查）。
- **pipeline 联动**：kenburns 阶段跳过过渡镜（hybrid 模式下过场镜缓推，
  但过渡镜等邻居出片走 batch pass2 出真过渡——缓推会破坏首尾帧衔接语义）；
  audit 视图对未出片过渡镜显示 `待邻出片（S1→S1T→S2）`。
- **顺带修 P0（历史潜伏）**：make_cmd 的 worker 子命令曾用 `Path(__file__)`
  构造——mg_batch.py 从 media_gen 拆出后无 `__main__` 入口，**真实 batch
  （非 dry-run）每镜子进程静默空跑 exit 0，全队记 OK 但零生成**。现有测试全
  是 dry-run 一直没抓到（本次手测 `python mg_batch.py video ...` 复现确认）。
  修：子命令一律指向 `parent / "media_gen.py"`（image/video/qc 三处 + pass2）。
  教训：**dry-run 测试覆盖不了子进程入口正确性，改子命令构造必须真跑一发**。
- **边界**：hybrid `--only hero` 场景下两趟调度不触发（plan 里只有 hero 列表），
  过渡镜需用户手工跑全量 `batch --phase videos`；真跑仍需接支持双条件的池。
- 测试 222 → **229**（+7：过渡镜 images 排除/MISS/两趟/孤儿/STALE 5 +
  pipeline kenburns 跳过 + --only 语义钉死 2）。
- 落地过程抓到自己的编辑事故：把模块级函数插进 cmd_batch 函数体中间把函数
  切成两半（AST 合法但语义死代码，batch 静默结束）——**AST 校验只能保语法
  不能保结构，函数插入后必须核对 top-level 函数清单 + 端到端跑一发**。

## v4.5（2026-09-10）

过渡镜（首尾帧双条件，讨论定案的"半自动方案一"）：解决"镜头之间非常独立和分割"的
结构性短板——xfade 是后期叠化的假过渡，首尾帧双条件让模型**生成中间演化过程**，才是
长镜头感的正解。

- **设计原则（半自动）**：过渡镜就是一个普通 shot JSON（加 `last_frame` 字段），
  何时插、插在哪人拍板——编排/时间轴/断点续跑体系**零改动**（爆炸半径缩 80%，
  这正是全量方案 3 的高风险区，先绕开）。
- **`mg_core.build_last_frame_fields(info, key)`**（纯函数）：解析尾帧字段名/列表形。
  哲学沿用 image_param/image_list——池模板或 key env（`MEDIA_<P>_n_LAST_FRAME_PARAM`，
  可选 `_LAST_FRAME_LIST=1`）声明支持才传，未声明返回 None 老行为不变；
  key 级覆盖优先（同池不同 key 接不同上游）。
- **CLI**：`video --last-frame <图>` 进 payload（与首帧同一 data-URI/URL 编码）；
  池未声明支持时 die 带配置指引（不静默丢弃）。
- **batch 透传**：shot JSON 的 `last_frame` → `--last-frame`；尾帧图缺失时报
  `MISS (last_frame ... not found)` 跳过**不提交**（提交即扣额度）。
- **现状边界**：免费池（agnes/zhipu）不支持双条件——功能就绪，真跑需 custom 池
  接支持首尾帧的服务（可灵/Vidu 级）。轻量模型 morph 能力弱，过渡质量强依赖模型档次。
- 测试 215 → **222**（+7：build_last_frame_fields 纯函数 5 + batch 透传/MISS 2）。

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
| **v4.16.1** | 2026-09-22 | af7fad3 | 654 | **README 事实错误修复**：①删「零第三方依赖」（v4.10 起已不成立：audio-qc/faces/word-axis 需可选依赖）改为「核心零依赖 + 可选增强」，补 requirements.txt 与安装节；②特性/目录补 audio-qc·faces·word-axis·ASS 逐词高亮·pipeline | 纯文档提交，零代码改动；公开仓导出同步后同样生效 |
| **v4.17.0** | 2026-09-22 | bc6ef0e | 665 | **批十一（④ 字幕重断句——SubtitleEdit 三规则）**：新纯函数 `fix_cue_timing`——①**CPS 超标 → 延长显示**（说话速度拆行救不了；把行尾 cue 延长进后方空隙，至 next_at−gap、至多 width/max_cps）；②**重叠 → 收缩前条**（MIN_CUE_DUR 钳制会造重叠，下条锚定语音不能推；缩无可缩如实计数不假装修好）；③words 一律不动（真实语音时间戳）。main 字幕输出接线 + `--max-cps`（默认 20 宽度/秒≈10 汉字/秒，0 关）/`--cue-gap`（默认 0.08≈2 帧） | 纯函数 11 条测试 + 端到端（造分句录音验证 main 真应用）；受控破坏 6/6（含抓到**测试入参别名**让 words 断言恒真的坑）；665 快测 + SLOW 全量 202s 全绿；导出零命中 |
| **v4.18.0** | 2026-09-22 | 6d3c4db | 682 | **批十二（标点恢复 + 说话人分离，零 torch）**：①`word_axis --punct`——sherpa ct-transformer（294MB，hf-mirror 可下）中英标点，0.03s 级推理；新纯函数 `merge_punct_into_words` 把标点**挂回逐字词目**（双指针字符对齐，对不上原样返回不编造，时间戳不变）；②新旁线 `scripts/speakers.py`——说话人分离（pyannote 切分 6MB + 3D-Speaker campplus 中文声纹 28MB 经 ghproxy + FastClustering），`[{start,end,speaker}]` 按首次出现归零；多角色漫剧对白对到角色的地基。★ 模型可得性先行（批十纪律）：三个模型全部下到手并真跑通才写码 | 标点模型无 int8（294MB fp32）；英文会得中文式句号（模型偏中文）；paraformer 分离结果**不做能量吸附**（与词轴同理由）；speakers 暂为独立 CLI（media_gen 门面被并行会话占用，接线留待合并后） |
| **v4.19.0** | 2026-09-22 | 76d551d | 685 | **批十三（--punct 接进声音链）**：`vo_build plan --punct`（隐含 --words）——标点挂回逐字词目随稿落盘，字幕自然带句读。核心防线：whisper 后端静默忽略 punctuate 参数（无标点能力）→ rep["punct"] 为空即 die(2)+指引——「要了标点却拿到无标点轴」是最典型的静默失败形态 | 3 条接线测试（内容级：标点真落 vo_lines_at.json / punctuate 真传到 / whisper 静默忽略必须拦）；受控破坏 3/3；真机冒烟：0.wav 29 字全挂标点（"啊，"“呢，"“嗯。"）；685 快测全绿。坑：Edit 插入测试类中间把原类第 4 个测试误划进新类（\_args 默认 words=False 所致）——独立类回填法 |
| **v4.20.0** | 2026-09-22 | bcdb5c0 | 706 | **批十四（剧本/对白层）**：新旁线 `scripts/dialogue.py`——剧本文本（角色：台词）→ vo_build 可吃的 vo_lines.json：`#` 注释/分场、`[空镜说明]` 进 acts、行首 `[Sxx]` 标镜、`角色（激昂）：` 括号注记=该句 emotion（长在角色名上也要剥）、半角冒号兜底；角色表 JSON 配音色，**未映射角色 stderr 点名不静默**（多角色全一个声音是事故）；空剧本 die(2)、角色表坏/非对象 die(3) | 21 条测试（纯函数全覆盖+CLI 壳+防线）+ 受控破坏 4/4；706 快测全绿；真机端到端：剧本→dialogue→plan --words --punct→词轴带标点+镜聚合全通；导出零命中（34 脚本）。坑：英文冒号兜底要放在中文冒号判定之后且先查 rest 非空（"阿明："空台词必须收 warning 不能喂 TTS 空音频） |

# 音频测试方案

**被测对象**：Audiokinetic Cube（Wwise 2025.1.10.9233 随附示例工程）
**方案性质**：可执行的测试设计。用例的前置条件、操作步骤、判定标准均基于实测工程结构编写；
标注「待执行」的部分尚未在实机跑过，本文不填结果。

---

## 一、被测工程实测结构

方案里每条用例都指向具体对象，先把对象清单落实。以下数据全部读自
`Cube\cube\soundbanks\Windows\` 的 SoundBank 定义文件，非估算。

### 1.1 SoundBank 构成

| Bank | 大小 | 占比 | Event | 内存音频 | 流式音频 |
|---|---|---|---|---|---|
| Music.bnk | 21,958,516 B | 67.8% | 7 | 76 | 0 |
| Main.bnk | 6,773,477 B | 20.9% | 82 | 109 | 1 |
| DCP_the_core.bnk | 3,294,886 B | 10.2% | 15 | 14 | 1 |
| Init.bnk | 3,966 B | 0.01% | — | — | — |
| **合计** | **32,030,845 B** | | **104** | **199** | **2** |

两个结论直接决定测试重点：

- **Music.bnk 一个 Bank 占了 67.8%**，却只承载 7 个 Event。音乐资源的内存
  代价与它的功能覆盖面严重不成比例，这是内存与加载时长风险的第一顺位。
- **199 个音频里只有 2 个是流式**（`Cube_Main_Theme`、DCP 地图主题）。
  其余全部常驻内存，意味着 Bank 一加载内存就到位，没有渐进加载的缓冲余地。

### 1.2 全局对象（Init.bnk）

Init.bnk 只有 3,966 B，但它是**所有其他 Bank 的前置依赖**，承载全部总线与
游戏同步器定义：

- **Audio Bus 21 个**，两条主干：
  `Main Audio Bus\Environmental\SFX\{Magic, Items, Main Character, Monsters}`
  与 `Main Audio Bus\Music\Interactive Music\Interactive Music Main\{Explore, Combat, Boss, Story, Victory, Defeated}`
- **Auxiliary Bus 4 个**：`env_corridor` / `env_small_room` / `env_medium_room` / `env_large_room`，
  各挂一个 Wwise RoomVerb
- **额外混响 ShareSet 2 个**：`Medium_Room1` / `Underground_Parking1`（Wwise Matrix Reverb）
- **动态处理**：`Main Audio Bus` 上一个 Wwise Peak Limiter（Default 自定义）、
  一个 `Hard_Knee_Minus_3dB_RMS` Compressor、一个 Wwise Meter
- **State Group 4 个**：`PlayerLife`（Alive/Defeated/None）、`PlayerInWater`（Yes/No/None）、
  `PlayerHasSuperGem`（Yes/No/None）、`Music_State`（Gameplay/Boss/Story/Victory/None）
- **Switch Group 2 个**：`Health_Status`（Healthy / Flesh_Wound / Badly_Injured / Nearly_Defeated）、
  `Gameplay_Switch`（Explore / Combat）
- **Game Parameter 2 个**：`PlayerHealth`、`Stinger_Sidechain`
- **Audio Device 2 个**：`System`、`No_Output`

### 1.3 Event 命名规律

82 个 Main Event 不是平铺的，有两组强规律，直接决定用例的组织方式：

**魔法投射物的三段式生命周期**——同一种魔法拆成 `Fire_` / `Hit_` / `End_`：

```
Fire_FireGem_Player     Hit_FireGem_Player      End_FireGem_Player
Fire_Fireball_Monster   Hit_Fireball_Monster    End_Fireball_Monster
Fire_Iceball_Monster    Hit_Iceball_Monster     End_Iceball_Monster
Fire_Slimeball_Monster  Hit_Slimeball_Monster   End_Slimeball_Monster
Fire_LightningGem_*     （无 Hit_/End_ 配对）
Fire_PoisonGem_*        （无 Hit_/End_ 配对）
Fire_IceGem_*           （无 Hit_/End_ 配对）
```

配对不完整本身就是测试信号：`Fire_` 起的循环声若没有对应 `End_`，
投射物销毁时声音不会停，表现为 voice 泄漏。哪些魔法缺 `End_`、
缺的那些是设计如此还是漏配，是用例 TC-02 要回答的问题。

**九个生物 × 四类行为**——`Foot_` / `Grunt_` / `Pain_` / `Defeated_`：

```
生物：Player, Bauul, Goblin, HellPig, Knight, Ogre, Rat, Rhino, Slith
```

`Foot_` 全部依赖 `Material` Switch Group（9 个 Switch：Sand / Concrete /
Stone / Wood / Gravel / Metal / Tile / Water / Grass）。
9 生物 × 9 材质 = **81 个脚步组合**，是本工程组合爆炸最严重的一处，
也是 Switch fallback 缺失最容易藏身的地方——取不到 Switch 时 Wwise 不报错，
只是没声音，功能测试极易漏过。

### 1.4 交互音乐（Music.bnk）

7 个 Event：`Music`（主入口）、`Story_Start` / `Story_End`、`Boss_Start`、
`Monsters_Aware` / `Monsters_Unaware`、`Map_Completed`。

- **Trigger 2 个**：`Cymbal_Swell`、`Health_Cue`（Stinger 用）
- **Game Parameter 3 个**：`PlayerHealth`、`EnemyAware`、`CC #1: Modulation`
- **Modulator**：1 个 LFO（`Synth Vibrato`）+ 2 个 Envelope
- **Source Plug-in**：2 个 Wwise Synth One（Melody Synth / Arpeggio Synth）

音乐分轨的文件名编码了排布信息：`Explore-Theme_138bpm4-4_L16M-P1M`
= 138 bpm、4/4 拍、16 小节、弱起 1 小节。**这为音乐切换的时机判定提供了
可计算的期望值**：138 bpm 4/4 下一小节 = 1.739 s，所以「Exit Cue 在小节线」
这种要求可以换算成毫秒去量，而不是靠听感描述。

---

## 二、测试用例集

优先级定义：**P0** 阻塞发布 / **P1** 影响体验须修 / **P2** 打磨项。

### TC-01 Event 触发完整性（P0）

**前置条件**：Profile 构建；Wwise Authoring 连接目标进程；Init.bnk +
Main.bnk + Music.bnk 已加载。

**操作步骤**
1. 从 `Main.txt` / `Music.txt` / `DCP_the_core.txt` 提取全部 104 个 Event 名。
2. 逐一触发（游戏内触发，或 Authoring 的 Soundcaster 直接投递）。
3. Profiler 开 Capture，记录每个 Event 的 Voice 起始。

**判定标准**
- 每个 Event 至少产生 1 个 physical voice，或有明确的设计理由说明它不该出声
  （例如纯 State 切换类 Event）。
- Profiler 无 `Event not found` / `No Sound Object` 类告警。
- 「无声」必须区分三种成因：Event 未定义、Event 定义了但 Sound 缺 media、
  Switch/State 取不到导致落空。三者的修法不同，报缺陷时必须指明是哪一种。

**说明**：104 个 Event 全量手工触发不现实，实际按 1.3 节的规律分组抽样——
三段式魔法取全部 `Fire_`，生物类每类行为取 3 个生物，其余全覆盖。

### TC-02 投射物生命周期配对（P0）

**前置条件**：同 TC-01；Profiler 的 Voice Count 图表可见。

**操作步骤**
1. 对每种魔法，连续触发 `Fire_` 10 次，不触发对应 `Hit_` / `End_`
   （对着空处放，或让投射物自然超时销毁）。
2. 观察 Voice Count 曲线在 10 次触发后是否回落到基线。
3. 对有 `End_` 配对的魔法，重复一次并显式触发 `End_`，对比曲线。

**判定标准**
- 投射物销毁后 physical voice 数须回到触发前的基线。
- 若不回落：确认该魔法的 Sound 是否设为 Loop，以及是否缺 `End_` 事件。
  这是 voice 泄漏，长时间战斗会顶满 voice 上限导致后续音效被抢占。
- 记录 `Fire_LightningGem_*` / `Fire_PoisonGem_*` / `Fire_IceGem_*`
  三组缺 `End_` 配对的实际行为，判断是 one-shot 设计（正常）还是漏配（缺陷）。

### TC-03 Material Switch 覆盖与 fallback（P1）

**前置条件**：可控制角色走过全部 9 种材质地形，或用 Soundcaster 手动设 Switch。

**操作步骤**
1. 对 `Foot_Player`，依次把 `Material` 设为 9 个 Switch 值各触发 5 次。
2. 把 `Material` 设为一个**未定义值**（或不设，保持初始状态），触发 `Foot_Player`。
3. 对 `Foot_Goblin` / `Foot_Ogre` / `Foot_Rhino` 重复步骤 1（抽样验证非 Player 生物）。

**判定标准**
- 9 种材质都要出声，且**互不相同**——同一段音频挂在多个 Switch 下
  是常见配置错误，听感上表现为「换了地面声音没变」。可用 Profiler 的
  Voice 名或我方工具的内容哈希比对来客观确认，而非靠听。
- 未定义 Switch 值时必须有 fallback 出声。**静默是缺陷**，因为它在
  Wwise 侧不产生任何错误，只有专门测才能发现。
- Water 材质需与 `PlayerInWater` State 交叉验证（见 TC-05）。

### TC-04 交互音乐状态转换（P1）

**前置条件**：Music.bnk 已加载；Profiler 开 Game Sync Monitor。

**操作步骤**
1. 触发 `Music` 进入播放，确认当前 `Music_State`。
2. 按下列路径逐条切换 State，每次记录切换指令时刻与音乐实际改变时刻：
   `Gameplay → Story`（触发 `Story_Start`）
   `Story → Gameplay`（触发 `Story_End`）
   `Gameplay → Boss`（触发 `Boss_Start`）
   `Gameplay → Victory`（触发 `Map_Completed`）
   `任意 → None`
3. 切 `Gameplay_Switch` 的 `Explore ⇄ Combat`（触发 `Monsters_Aware` /
   `Monsters_Unaware`），重复 10 次，其中 3 次在 1 s 内快速来回。

**判定标准**
- 转换须落在 Exit Cue 上，不得出现硬切、爆音、两段音乐重叠。
- **时机可量化**：素材标注给了 bpm 与拍号，138 bpm 4/4 一小节 1.739 s，
  90 bpm 4/4 一小节 2.667 s。若设计要求「小节线切换」，实测延迟应落在
  0 到一个小节长之间；超出即为 Exit Cue 配置问题。用这个数去卡，
  而不是写「感觉切得有点晚」。
- 快速来回切换不得出现状态错乱（停在中间态、音乐彻底静音、
  或 `Explore` 与 `Combat` 同时在响）。
- Profiler 中 State 变更时刻与音乐段落切换时刻的差值须记录进报告。

### TC-05 State 组合矩阵（P1）

**前置条件**：可独立控制 3 个 State Group。

**操作步骤**
`PlayerLife`（Alive/Defeated）× `PlayerInWater`（Yes/No）×
`PlayerHasSuperGem`（Yes/No）= 8 种组合，逐一进入并触发
`Foot_Player`、`Fire_FireGem_Player`、`Pain`、`Jump`。

**判定标准**
- 8 种组合都不得出现无声或异常电平。
- `Defeated` + `PlayerInWater=Yes` 这类不常见组合是重点——
  正常流程走不到的组合最容易漏配。
- `PlayerHasSuperGem=Yes` 时的魔法音效应有可听差异，
  若与 `No` 完全一致，需确认是设计如此还是 State 未生效。

### TC-06 RTPC 边界（P1）

**前置条件**：可连续改变 `PlayerHealth`。

**操作步骤**
1. `PlayerHealth` 从 100 线性降到 0，耗时 10 s，全程 Profiler 抓 Capture。
2. 记录 `Health_Status` Switch 在哪些 `PlayerHealth` 值上发生跳变
   （4 档：Healthy / Flesh_Wound / Badly_Injured / Nearly_Defeated）。
3. 在每个跳变点附近 ±1 反复越界 5 次。
4. `PlayerHealth` 从 0 升回 100，重复记录。

**判定标准**
- 4 档的分界值须与设计文档一致，且**升降方向对称**
  （若无迟滞设计，同一分界值双向应一致）。
- 边界反复越界不得出现音效抖动、Stinger 连续重复触发
  （`Health_Cue` Trigger 会在此处被打到）。
- `PlayerHealth` 驱动的音乐层混音（Music.bnk 里它是 Game Parameter）
  变化须连续，不得阶跃。

### TC-07 Voice 上限与 Playback Limit（P0）

**前置条件**：Profile 构建；Profiler 的 Voice Count（physical / virtual 分列）可见。

**操作步骤**
1. 记录空闲基线的 physical / virtual voice 数。
2. 极限施压：同一位置连续触发 `Fire_Fireball_Monster` 与 `Foot_*`
   共 50 次以上，尽量在 1 s 内打满。
3. 观察 physical voice 是否被限制在某个上限，超出的是否转为 virtual。
4. 换 `Rumble`（长音）与 `Teleport` 重复。

**判定标准**
- physical voice 须有上限约束，不得无限增长。
- 超限时的行为须符合 Playback Limit 设置的策略
  （Kill Oldest / Kill Newest / Use Virtual），且**听感上不能出现
  正在播的重要音效被突然掐断**。
- virtual voice 转回 physical 时不得从头重播（除非配置为 From Beginning）。
- 记录压力下的 CPU 占用与 total voice 峰值。

### TC-08 Bank 加载与卸载（P0）

**前置条件**：可控制 Bank 加载时序。

**操作步骤**
1. 冷启动，只加载 Init.bnk，触发 Main.bnk 里的 Event，记录行为。
2. 依次加载 Main.bnk → Music.bnk → DCP_the_core.bnk，每步记录
   Profiler 的 Bank memory 与加载耗时。
3. 播放中卸载 Music.bnk，观察正在播的音乐。
4. 卸载 Init.bnk（其他 Bank 仍加载），观察行为。
5. 重复加载 / 卸载 Music.bnk 20 次，观察内存是否回到基线。

**判定标准**
- 缺 Bank 时触发 Event 须安全失败（无声 + 日志告警），不得崩溃。
- Music.bnk 单独占 21.9 MB，其加载耗时须单独计量并记录——
  这是低端机加载卡顿的主要来源。
- 卸载正在播放的 Bank 不得崩溃；正在播的声音应按设计停止或播完。
- 20 次加卸载后内存回到基线 ±5% 以内，否则是泄漏。
- **Init.bnk 是全局前置依赖**（21 个 Bus + 全部 State/Switch 定义），
  它缺失或卸载后的行为必须单独验证，不能与其他 Bank 混在一起测。

### TC-09 流式播放（P1）

**前置条件**：Profiler 的 Streaming 视图可见。

**操作步骤**
1. 触发 `Cube_Main_Theme`（Main.bnk 唯一流式音频）与 DCP 地图主题。
2. 观察 Streaming 视图的 buffer 状态与 starvation 计数。
3. 在流式播放中同时施加 TC-07 的 voice 压力。
4. 若可行，在低速存储或限制 IO 的条件下重复。

**判定标准**
- 无 starvation（buffer 掏空导致的断音）。
- 首次播放的启动延迟须记录；若配了 prefetch，验证 prefetch 长度是否够。
- 压力下流式音频不得被抢占或断流。

### TC-10 混响区域切换（P1）

**前置条件**：能在 4 个 `env_*` 区域间移动。

**操作步骤**
1. 依次进入 `env_corridor` / `env_small_room` / `env_medium_room` /
   `env_large_room`，每个区域内触发 `Foot_Player` 与 `Fire_FireGem_Player`。
2. 在两个区域交界处来回穿越 10 次，其中 3 次快速穿越。
3. 在区域内触发长音（`Rumble`），播放期间跨区域移动。

**判定标准**
- 4 个区域的混响须有可辨差异，且与空间尺度相符
  （small_room 的 decay 不应长于 large_room）。
- 区域切换须平滑过渡，不得出现混响突变或 send 电平跳变。
- 长音跨区域时混响应跟随变化，不应保持进入时的区域设定。
- 记录每个 Auxiliary Bus 的 send 电平，确认没有区域漏配 send
  （漏配表现为该区域完全干声，容易被当成「这个房间就是这样」而漏过）。

### TC-11 总线电平与限幅（P1）

**前置条件**：Profiler 可读 `Main Audio Bus` 的 Meter（Init.bnk 里挂了
Wwise Meter）。

**操作步骤**
1. 单独播放各类音效，记录 `Main Audio Bus` 峰值。
2. 叠加：音乐 + 战斗音效 + 脚步 + 语音同时最密集的场景，持续 30 s。
3. 记录 Peak Limiter 的实际介入频率与压缩量。

**判定标准**
- 叠加场景下总线不得削波。
- Peak Limiter 若频繁重度介入，说明上游电平配置过高——
  依赖限幅器兜底会压掉动态，属于混音配置问题而非限幅器问题。
- 各子总线（SFX / Music / Voice）的相对电平须与设计一致。

### TC-12 素材层回归（P0，已执行）

**前置条件**：`audio_qa` 工具 + ffmpeg。

**操作步骤**
```bash
python audio_qa.py scan <Originals 目录> -o baseline.json --md baseline.md
# 素材改动后
python audio_qa.py scan <Originals 目录> -o current.json
python audio_qa.py compare baseline.json current.json --md diff.md
```

**判定标准**：无新增 FAIL。工具退出码非 0 即阻断。

**已执行结果**：209 个素材，PASS 54 / INFO 117 / WARN 29 / FAIL 9。
9 条 FAIL 全部为 `dc_offset` / `clipping` / `true_peak_overshoot`，
已用 ffmpeg 原生 `astats` / `ebur128` 逐条交叉验证。
判据的适用边界与六轮误报治理过程见 `判据设计与误报治理.md`。

### TC-13 音频中断与焦点丢失（P1）

**操作步骤**
1. 播放中切到其他应用（Alt+Tab），再切回。
2. 播放中拔掉 / 插入音频输出设备。
3. 播放中切换默认输出设备（耳机 ⇄ 扬声器）。
4. 系统音量归零再恢复。
5. 播放中把 Audio Device 切到 `No_Output`（Init.bnk 里定义了这个设备），再切回。

**判定标准**
- 恢复后音频须正常，不得静音、不得只剩部分总线出声。
- 设备切换不得崩溃；采样率不同的设备间切换须正确重采样。
- 后台时的行为须符合设计（静音或继续），且切回后状态一致。

### TC-14 长时间稳定性（P1）

**操作步骤**：连续运行 2 h，脚本循环触发 Event 与 State 切换，
每 10 min 记录 Profiler 的 voice 数、CPU、内存。

**判定标准**
- 内存无单调增长趋势。
- voice 数在每轮循环结束后回到基线。
- CPU 占用不随时间上升。
- 无累积性的音质劣化（音调偏移、噪声累积）。

---

## 三、移动端兼容性

Cube 本身是 Windows 示例工程，本节给的是**方法与判据**，
在有 Android 构建时可直接套用。工具链已在本机 Windows 侧验证过。

### 3.1 设备音频状态取证

```bash
# 当前音频状态：输出设备、采样率、活跃 AudioTrack、焦点归属
adb shell dumpsys audio

# Wwise 与 Android 音频层的日志
adb logcat -s AudioTrack:V AudioFlinger:V Wwise:V AK:V

# 底层输出流的实际采样率与缓冲区大小
adb shell dumpsys media.audio_flinger | grep -A5 "Output thread"
```

`dumpsys audio` 的价值在于它给出**设备侧的事实**，
与 Wwise Profiler 给出的引擎侧数据互相印证。两边不一致时，
问题出在引擎与平台音频层的交接处，这类缺陷单看一边都看不出来。

### 3.2 采样率与重采样

Cube 素材实测有 5 种格式组合（48 kHz / 44.1 kHz × 24 bit / 16 bit）。
Android 设备的原生输出采样率因机型而异（常见 48 kHz，部分老机 44.1 kHz）。

**判定标准**
- 用 `dumpsys media.audio_flinger` 读出设备实际输出采样率，
  与素材采样率不一致时必然发生重采样，需确认重采样在何处发生
  （Wwise 内部 / Android AudioTrack / 硬件），以及是否引入可听劣化。
- 重点测 44.1 kHz 素材在 48 kHz 设备上的表现，以及反向情况。
- 重采样的 CPU 代价须计入性能预算。

### 3.3 输出延迟

**操作步骤**
1. `adb shell dumpsys media.audio_flinger` 读 buffer size 与
   frame count，算出理论延迟下限。
2. 实测：录制屏幕操作与音频输出，测量按下到出声的间隔（需外部录音设备
   或高帧率录屏 + 波形对齐）。
3. 对比同一构建在 3 台以上不同档位设备上的结果。

**判定标准**
- 打击感强的音效（`Fire_*`、`Hit_*`、`Jump`）的端到端延迟须在可接受范围。
- **延迟的机型差异比绝对值更值得报**：同一构建在不同设备上差出一倍，
  说明依赖了设备的低延迟路径而没有 fallback 策略。
- 若设备不支持低延迟输出路径，须确认引擎侧有对应处理。

### 3.4 低端机内存与加载

Music.bnk 21.9 MB 占 Bank 总量 67.8%，这个比例在移动端是明确风险。

**操作步骤**
1. 在 2 GB / 3 GB / 4 GB RAM 三档设备上分别测 Bank 加载耗时与峰值内存。
2. `adb shell dumpsys meminfo <package>` 取音频相关内存占用。
3. 触发系统内存压力（后台开多个应用），观察音频是否被回收或降级。
4. 测冷启动到第一声出声的总耗时。

**判定标准**
- 低端机上 Music.bnk 加载不得造成可感知卡顿（须给出具体毫秒阈值）。
- 内存压力下不得出现音频进程被杀。
- 若必须裁剪，优先考虑把 Music.bnk 的常驻音频改流式——
  当前 76 个音乐音频全部常驻内存，一个都没走流式，这是最直接的优化空间。

### 3.5 机型音频特性差异

**操作步骤**：在至少 5 台覆盖不同芯片平台与 Android 版本的设备上跑
TC-01 / TC-04 / TC-07 / TC-13 的核心子集。

**重点关注**
- 多声道下混：立体声素材在单扬声器设备上的下混是否丢内容
  （工具的 `fake_stereo` 检查可提前在素材层发现相位问题）。
- 系统音效增强（各厂商的「音效」开关）开启时是否破坏混音意图。
- 通话 / 通知打断后的恢复行为（对应 TC-13，移动端触发频率远高于 PC）。
- 蓝牙音频的延迟与断连恢复。

---

## 四、风险分析

按「出问题的概率 × 发现的难度」排序。**难发现的风险比高频风险更值得前置投入**，
因为高频问题自己会冒出来，难发现的会一路带到线上。

### 风险 1：Init.bnk 的全局爆炸半径（高影响 / 中概率 / 易发现）

Init.bnk 只有 3,966 B，却定义了 21 个 Audio Bus、4 个 Auxiliary Bus、
4 个 State Group、2 个 Switch Group、2 个 Game Parameter。
**任何总线结构或游戏同步器的改动都会重新生成它，影响面是全工程。**

- 触发条件：改 Bus 层级、增删 State/Switch、调总线上的 Effect。
- 表现：可能是全局静音、可能是某一类音效整体丢失、可能只是电平偏移。
- 应对：Init.bnk 的二进制变更必须触发全量回归，不能只测改动涉及的模块。
  把 Init.bnk 的哈希纳入 CI，变了就跑全量。

### 风险 2：Switch/State 取值落空的静默失败（中影响 / 高概率 / **难发现**）

这是本工程最值得投入的一处。`Foot_*` 有 9 生物 × 9 材质 = 81 个组合，
`Material` Switch 取不到值时 Wwise **不报错，只是没声音**。

- 触发条件：新增材质但漏配、代码传的 Switch 名与 Wwise 侧不一致、
  State 未初始化。
- 为什么难发现：没有错误日志，没有崩溃，功能测试走主流程不一定踩到那个材质。
  测试员听不到脚步声，很可能以为「这个地面本来就没声」。
- 应对：**不能靠听，要靠 Profiler 的 Voice 记录做覆盖率统计**。
  把 81 个组合列成矩阵逐格打勾，缺的格子就是缺陷。这类工作适合脚本化——
  用 Soundcaster 批量投递 Switch + Event 组合，把 Profiler 输出解析成矩阵。

### 风险 3：Music.bnk 的体量（高影响 / 高概率 / 易发现）

21.9 MB 占 67.8%，承载 7 个 Event、76 个常驻内存音频、0 个流式音频。

- 触发条件：低端机、内存压力、冷启动。
- 表现：加载卡顿、内存超预算、极端情况被系统回收。
- 应对：优先在低端机上量化加载耗时（TC-08 步骤 2、3.4）。
  流式化是明确的优化方向，但要连带测 starvation（TC-09）——
  把常驻改流式是在用 IO 风险换内存，不是免费的。

### 风险 4：投射物 voice 泄漏（中影响 / 中概率 / **难发现**）

`Fire_` / `Hit_` / `End_` 三段式里，`LightningGem` / `PoisonGem` / `IceGem`
缺 `Hit_` 与 `End_` 配对。

- 触发条件：投射物未命中就销毁、销毁路径没走到 `End_`、
  同类魔法连发。
- 为什么难发现：单次触发听不出异常，要连续几十次才顶到 voice 上限，
  而顶满的表现是**别的音效被抢占**——现场看到的是「脚步声偶尔没了」，
  根因在几十秒前的魔法泄漏，因果隔得很远。
- 应对：TC-02 用 Voice Count 曲线是否回落到基线来判定，
  这是客观信号，不依赖听感。把「voice 数回落到基线」做成自动化断言。

### 风险 5：音乐转换时机（中影响 / 中概率 / **难发现且难复现**）

交互音乐的 State 转换依赖 Exit Cue，正确性与**触发时刻在小节里的位置**有关。

- 触发条件：在小节末尾、Exit Cue 前后极短窗口内切 State；快速来回切换。
- 为什么难：同一操作在不同时刻的结果不同，缺陷复现率低，
  报上去容易被判成「无法复现」。
- 应对：**用素材标注把时机量化**。文件名给了 bpm 与拍号
  （138 bpm 4/4 → 一小节 1.739 s），期望的转换点可以算出来，
  实测偏差可以量。报缺陷时附 Profiler Capture 与
  「State 变更时刻 / 音乐切换时刻 / 差值 / 该差值对应几拍」，
  比「有时候切得很怪」可执行得多。快速来回切换（TC-04 步骤 3）
  是提高复现率的手段。

### 风险 6：素材层缺陷混入（低影响 / 中概率 / 易发现）

已由 TC-12 覆盖并自动化，209 个素材 9 条 FAIL 均已定位。
纳入 CI 后这条风险基本关闭——这也是把它排在最后的原因：
**能自动化的风险不该占人工测试的预算。**

### 优先级结论

人工测试预算应集中在风险 2、4、5 —— 三者都属于「静默失败或难复现」，
自动化难做、走主流程测不到，但都能找到客观判定信号
（Switch 覆盖矩阵、Voice Count 回落、时机毫秒偏差）。
风险 1、3 影响大但容易发现，做好触发条件的把关即可；
风险 6 交给 CI。

---

## 五、缺陷报告要求

音频缺陷比功能缺陷更容易被判成「无法复现」或「主观感受」，
所以报告的门槛要更高。每条缺陷必须包含：

1. **构建信息**：版本号、Profile / Release、Bank 版本或哈希。
2. **精确复现路径**：到 Event / Switch / State 级别，
   不写「在洞穴里走路」而写「`Material=Stone` + `env_large_room`
   触发 `Foot_Player`」。
3. **客观证据**：Profiler Capture 文件、voice 数曲线、Meter 读数、
   录音波形。**至少一项可被第三方独立核验的量化数据。**
4. **期望与实际的量化对比**：不写「延迟感觉有点大」，
   写「State 变更后 2.31 s 音乐才切换，138 bpm 4/4 下超过一个小节
   （1.739 s），若设计为小节线切换则超出 0.57 s」。
5. **影响范围**：只这一处，还是同类配置都有。
   这一条决定优先级，也决定修的时候要不要顺手扫一遍同类。
6. **区分成因**：无声要说清是 Event 未定义、media 缺失，
   还是 Switch/State 落空——三者派给不同的人修。

---

## 六、方案的边界

如实说明本方案未覆盖的部分，以及原因：

- **实机执行结果尚未填入**。TC-01 到 TC-11、TC-13、TC-14 是设计，
  不是报告。用例的前置条件与判定标准基于实测工程结构编写，
  但执行需要运行 Cube 并操作 Wwise Profiler。TC-12 已执行并有完整结果。
- **移动端一节是方法而非实测**。Cube 无 Android 构建，
  命令与判据可直接套用，但未在真机上跑过。
- **主观音质评价不在本方案内**。混音是否好听、音效是否贴合，
  属于音频设计的判断，测试能做的是把可量化的部分量化，
  并为主观评价提供可复现的对照条件。
- **性能预算的具体阈值待定**。voice 上限、加载耗时、延迟的
  合格线需要项目侧给出目标机型与性能预算，本方案给出的是
  测量方法与须记录的指标。

# Wwise Profiler 抓取清单（Cube 2025.1.10）

这份清单的目的不是「把 Profiler 各个面板截一遍」，而是**每张截图对应一个具体的判定**：
截之前先知道要看哪个数、这个数说明什么、什么值算异常。没有判定的截图不截。

全程只读不写：不改 Wwise 工程、不改 SoundBank、不改安装目录任何文件。
唯一的输入是游戏内控制台命令。

---

## 0. 前置：两件事必须先确认

### 0.1 必须用 Profile 版，不是 Release 版

```
C:\Audiokinetic\Cube_2025.1.10.9233\Cube\cube\cube-profile.bat
```

内容是 `..\win32\profile\bin\cube.exe -w1024 -h768 -t %*`。

**Release 版连不上 Profiler**——`AK::Comm::Init()` 只在 Profile/Debug 配置里编译进去
（`Cube\cube_source\src\sound.cpp:263`），Release 版整段被宏剔掉。
如果 Wwise 里 Remote 列表是空的，第一件要查的就是启动的是不是 profile 版。
这一点本身就值得在报告里写一句：**性能数据只能在 Profile 版上采集，
而 Profile 版的开销不等于玩家实际开销**，两者不能混为一谈。

### 0.2 连接顺序

1. 先启动 `cube-profile.bat`，进到主菜单（不用进地图）
2. 再开 `Wwise.exe`，打开 `Cube\WwiseProject\Cube.wproj`
3. 菜单 **Layouts → Profiler**（快捷键 F6）
4. 工具栏 **Remote...** → 列表里选 `localhost` 的 Cube → Connect
5. 连上后标题栏会显示已连接的目标名

连不上时按这个顺序排查：Profile 版 → 防火墙放通 → Cube 已经在运行 → 端口没被占。

---

## 1. 控制台是主要的触发手段

按 **`** （反引号，keymap.cfg 里绑到 `saycommand /`）打开控制台，
输入命令后回车。清单里的所有 Event 触发都走这条路，不靠打怪碰运气。

### 1.1 直接触发任意 Event

```
/akevent Fire_FireGem_Player
```

`akevent` 的实现（`sound.cpp:543`）是把 Event 发在 `player1` 这个游戏对象上：

```cpp
void akevent(char *name)
{
	CHECK_SOUND_ENGINE;
	snd_event( name, player1 );
}
COMMAND(akevent, ARG_1STR);
```

**这条命令让所有 82 个 Main.bnk Event 都可以精确、可重复地触发**，
这是能做出可复现用例的前提。注意它总是发在 player1 上，
所以**衰减（Attenuation）与 3D 定位测不出来**——那些需要靠移动听者位置来测。

### 1.2 音量命令实际是在设 RTPC

```
/musicvol 128
/soundvol 0
/voicevol 255
```

`sound.cpp:552-576` 里这三个命令并不改引擎主音量，而是分别设
`MUSICVOLUME` / `SFXVOLUME` / `VOICEVOLUME` 三个 Game Parameter：

```cpp
void musicvol( int vol ) { SoundEngine::SetRTPCValue( GAME_PARAMETERS::MUSICVOLUME, (AkRtpcValue) vol ); }
```

取值 0-255。这给了一个现成的 **RTPC 边界值测试入口**——不需要改工程就能测
RTPC 曲线在两端的行为。

### 1.3 进地图

```
/sp dcp_the_core/enter
```

`sp` 是 `defaults.cfg` 里的 alias：`mode -2; map $arg1`（经典单人模式）。
这张图是 DCP 做的官方示例关，专门给音频演示用的，有 12 个语音触发点。

---

## 2. 要抓的画面与对应判定

下面 7 项，每项给：**怎么触发 / 看哪个面板 / 判定什么 / 什么算异常**。
建议按顺序做，前面几项不进地图就能做，最省时间。

### 抓取 1：初始化后的 Bank 内存基线

**触发**：启动 profile 版，停在主菜单，不进地图。

`sound.cpp:272-274` 在初始化时无条件加载两个 Bank：

```cpp
AKVERIFY(SoundEngine::LoadBank("Init.bnk", bankID) == AK_Success);
AKVERIFY(SoundEngine::LoadBank("main.bnk", bankID) == AK_Success);
```

**看**：Profiler 的 **Advanced Profiler → Memory** 标签页。

**判定**：主菜单状态下常驻内存是多少。这是所有后续对比的基线。

**参考数据**（磁盘上的 Bank 大小，用来对照内存占用是否合理）：

| Bank | 字节 | 占比 |
|---|---|---|
| Music.bnk | 21,958,516 | 68% |
| Main.bnk | 6,773,477 | 21% |
| DCP_the_core.bnk | 3,294,886 | 10% |
| Init.bnk | 3,966 | 0.01% |

**异常信号**：主菜单就把 Music.bnk 载进来了。Music.bnk 占 Bank 总量 68%，
主菜单不需要交互音乐，如果它在这时就常驻，那是明确的内存优化点。
从代码看初始化只载 Init 和 main，Music.bnk 应当由地图或音乐系统按需加载——
**这一条需要实测确认，不要预设结论**。

> 顺带一个可写进缺陷报告的观察：源码里写的是 `"main.bnk"` 小写，
> 而磁盘上的文件名是 `Main.bnk`。Windows 文件系统不区分大小写所以能跑通，
> 但同一套代码在 Linux / Android 上会加载失败。这是**真实的跨平台隐患**，
> 属于「在 Windows 上永远测不出来」的那一类问题。

### 抓取 2：Voice Count 的物理与虚拟之分

**触发**：进地图后连续快速触发同一个 Event：

```
/sp dcp_the_core/enter
/akevent Fire_Fireball_Monster
（连打十几次）
```

**看**：Profiler 主窗口顶部的 **Voices** 图表 + **Advanced Profiler → Voices** 列表。

**判定**：**Physical Voices 与 Total Voices 分别是多少**。这两个数不能混着看：
- Total（含 Virtual）= 逻辑上正在播的声音数
- Physical = 真正占用 DSP 与解码资源的声音数

Virtual Voice 是被 Playback Limit 或 Virtual Voice Behavior 降级掉的，
不吃 CPU 但仍在计时。**报性能数据时只说「同时 40 个声音」是没有信息量的**，
必须说清是 40 个物理声还是 40 个里只有 8 个物理声。

**异常信号**：Physical Voice 数随触发次数无上限增长——说明没配 Playback Limit，
在实机上会直接吃满音频线程。

### 抓取 3：Playback Limit 的抢占行为

**触发**：同上，密集触发同一 Event。

**看**：**Advanced Profiler → Voices**，观察被 kill / virtualize 的条目。

**判定**：超限时引擎丢的是哪一个声音——最老的、最新的、还是音量最低的？
（对应 Wwise 里的 *Discard Oldest / Discard Newest / Lowest Volume*）

**异常信号**：`Discard Newest` 配在打击音效上。玩家刚按下攻击键的那一声被丢掉，
听起来就是「输入没响应」，是很典型的体验缺陷，但在功能测试里不会被记为 bug。

### 抓取 4：Music_State 切换的对齐行为

**触发**：`Music.txt` 里 `Music_State` 有 5 个状态：
`Gameplay` / `Boss` / `Story` / `Victory` / `None`。
对应的 Event 是 `Story_Start` / `Story_End` / `Boss_Start` / `Map_Completed`。

```
/akevent Story_Start
（等几秒）
/akevent Boss_Start
（不等，紧接着再切）
/akevent Story_End
```

**看**：**Game Sync Monitor**（Profiler 里的 Game Object 3D Viewer 旁边那个标签），
配合主窗口的 **Music** 时间轴。

**判定**：状态切换发生在小节/拍边界上，还是立即切？
Cube 的音乐分轨文件名带小节信息（`_138bpm4-4_L16M-P1M` = 138 bpm、4/4、16 小节、
弱起 1 小节），138 bpm 4/4 一小节 = 1.739 s。
**如果切换点没落在 1.739 s 的整数倍上，交互音乐的对齐就被破坏了。**

**异常信号**：连续快速切 State 时出现两段音乐重叠、或者过渡段被截断。
这是交互音乐最常见的回归类型，且只在「切得比过渡时长更快」时才暴露。

### 抓取 5：Material Switch 落空时的行为

**触发**：`Main.txt` 里 `Material` Switch Group 有 9 个值：
`Sand` / `Concrete` / `Stone` / `Wood` / `Gravel` / `Metal` / `Tile` / `Water` / `Grass`。

脚步声在 `sound.cpp:162` 按材质设 Switch：

```cpp
SoundEngine::SetSwitch( SWITCHES::MATERIAL::GROUP, materialid, (AkGameObjectID) ent );
```

在地图里走过不同贴图的地面，同时触发脚步：

```
/akevent Foot_Player
```

**看**：**Game Sync Monitor**（当前 Switch 值）+ **Voices** 列表（实际播放的素材）。

**判定**：每种材质是否都有对应素材、Switch 值切换是否跟得上地形变化。

**异常信号**：Switch 落到一个没配素材的值上时**静音**而不是回退到默认。
这类缺陷的特征是「加了新地形之后某些地面走上去没声音」，
功能测试很容易漏，因为没有报错、没有崩溃，只是安静。

### 抓取 6：RTPC 边界值

**触发**：`PlayerHealth` 是 Music.bnk 与 Main.bnk 共用的 Game Parameter，
`Health_Status` Switch Group 有 4 档：
`Healthy` / `Flesh_Wound` / `Badly_Injured` / `Nearly_Defeated`。

用现成的音量 RTPC 做边界测试最省事：

```
/soundvol 0
/soundvol 1
/soundvol 255
/soundvol 256
（超范围）
```

**看**：**Game Sync Monitor** 里的 RTPC 实际值 + **Voices** 的音量列。

**判定**：0 和 255 两端的行为、以及超范围输入被如何钳位。

**异常信号**：`soundvol 0` 之后 Voice 仍然是 Physical 而不是被 virtualize——
说明音量降到 0 但资源没释放，在移动端是白烧电。

### 抓取 7：Streaming 与地图语音

**触发**：`dcp_the_core/enter` 这张图里有 12 个语音触发点，
`enter.cfg:70-81` 用 `level_trigger_N` alias 绑定：

```
alias level_trigger_3 [akevent DCP_Enter01; echo "intruder alert!"]
alias level_trigger_1 [akevent DCP_Enter02; echo "drainage system access granted"]
...
```

可以直接在控制台按名字触发，不必在图里找触发器：

```
/akevent DCP_Enter01
/akevent DCP_Signal01
```

**看**：**Advanced Profiler → Streaming** 标签页。

**判定**：哪些素材走流式、Streaming 的缓冲状态。
Main.bnk 里只有 1 条 Streamed Audio（`Cube_Main_Theme`），
其余 109 条都是 In Memory Audio；DCP_the_core.bnk 是 14 条内存 + 1 条流式。

**异常信号**：Streaming 出现 starvation（缓冲耗尽）。这在本机 SSD 上基本不会发生，
**所以这一项的真实价值是记录「本机测不出来」**——
流式缺陷要在低速存储或高 I/O 竞争下才暴露，这是本机环境的能力边界，
报告里应当如实写明，而不是写「流式无问题」。

---

## 3. 顺手能做的一项：不用 Profiler 也能验的跨平台缺陷

抓图之外，有一条不依赖 Profiler、可以直接从源码论证的缺陷，
适合写成一条正式的缺陷报告：

**标题**：Bank 文件名大小写不一致，在大小写敏感的文件系统上会导致初始化失败

**证据**：
- `Cube\cube_source\src\sound.cpp:274`：`SoundEngine::LoadBank("main.bnk", bankID)`
- 磁盘实际文件：`Cube\cube\soundbanks\Windows\Main.bnk`
- 该调用被 `AKVERIFY` 包裹，失败即断言中止

**影响**：Windows / macOS 默认不区分大小写，问题不可见；
Linux 与 Android 区分，会在音频初始化阶段直接失败。

**为什么值得报**：这是一类特定的缺陷——**在开发平台上 100% 不复现，
在目标平台上 100% 复现**。它说明「本机测过没问题」不能作为跨平台结论，
也是测试环境矩阵必须覆盖多种文件系统的理由。

---

## 4. 截图与记录规范

每张截图配三行文字，缺一行这张图就不算完成：

```
触发：/akevent Fire_FireGem_Player  ×12（间隔 <100 ms）
读数：Total Voices 峰值 31，其中 Physical 8
判定：Playback Limit 生效，超限走 virtualize 而非 kill；与 Wwise 工程配置一致
```

命名：`profiler_01_bank基线.png`、`profiler_02_voice计数.png`，序号对应本清单章节。

**记录里必须区分三种话**，不能混：
- **实测到的**：Profiler 上读到的数
- **推断的**：从数推出的结论（要写清推理链）
- **没测的**：本机环境测不了的（如 Streaming starvation、移动端延迟）

第三类尤其要写。一份说清自己边界的报告，比一份看起来什么都覆盖了的报告更可信。

---

## 5. 时间安排

前 6 项都不需要在游戏里跑动，控制台就能触发，**大约 40 分钟**能全部抓完。
第 7 项要进地图，加 20 分钟。

如果时间只够做三项，按这个优先级：
**抓取 2（Voice Count）→ 抓取 1（Bank 内存）→ 抓取 4（Music State）**。
这三项覆盖了性能、内存、交互音乐三条主线，也是复核时最容易被追问的三块。

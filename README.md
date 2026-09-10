# audio_qa — 游戏音频质量检查与自动化回归工具

面向游戏音频**素材库**与**运行时录制**的质量检查、跨库回归比较与缺陷证据生成。单文件 CLI，纯 Python 标准库 + `ffmpeg` 子进程，无第三方运行时依赖。

> 设计立场写在前面：**工具只提供证据，不代替判定。** 每一条判据都写明了自己的适用边界；凡是需要人下结论的地方（结论栏、间隙定性、阈值校准），工具一律留空或标注为草案。误报怎么修的记录在 [`判据设计与误报治理.md`](判据设计与误报治理.md)。

## 判据一览

| 判据 | 实现方式 | 判定口径 |
|---|---|---|
| 格式一致性 | 读取采样率 / 位深 / 声道并全库比对 | 离群标 WARN |
| 削波 | 扫描连续 ≥3 个满刻度样点 | 区分「制作期削波」与「有损源解码钳位」，后者降级 INFO |
| 直流偏移 | 样点均值占满刻度比例 | 超阈值判 FAIL |
| 首尾异常静音 | 头尾窗口电平扫描 | 一次性音效与音乐分轨分别判定 |
| loop 接缝 | 首尾跳变 / 素材自身 p99 斜率 | 与自身斜率比，避免高频素材误报 |
| LUFS / True Peak | `ffmpeg ebur128` + 4 倍过采样真峰值 | 无显式规范时只做库内一致性 |
| 重复素材 | SHA-256 内容哈希 + 命名规范化 | 分三类：冗余副本 / 命名等价 / 导出缺陷 |
| 段内静音间隙（dropout） | 全声道同时低于门限且两侧有信号的间隙 | **默认关闭**，仅显式给下限时启用；只报 WARN，须与视频核对 |
| 声道一致性 | 逐声道峰值比对 | 多声道中途哑掉单独报 |

## 快速开始

```bash
# 扫描素材库，输出 JSON（可作回归基线）+ Markdown（人读）
python audio_qa.py scan <素材目录> --json out.json --md out.md

# 查运行时录制里的断流（录制片段；0 = 关闭）
python audio_qa.py scan captures/perf/clips --ext mkv,mp4 --dropout-min-ms 80 --md dropout.md

# 两次扫描的回归比较
python audio_qa.py compare baseline.json current.json --md diff.md

# 现场记录助手：一个录制目录 → 客观测量 + 待填报告骨架（结论栏留空）
python scripts/field_session.py captures/target-game/genshin-1.0/bug_01_concurrency --case bug_01 --dropout-min-ms 80

# 语音-字幕对齐检查（四类差异 + offset 统计；阈值标注为待校准草案）
python scripts/asr_align.py --baseline baseline.tsv --asr asr.json --out align.md


# 测试
python -m pytest tests -q      # 89 passed
```

## 工具组成

| 文件 | 作用 |
|---|---|
| `audio_qa.py` | 单文件 CLI：九类判据、跨库回归比较、Markdown/JSON 双报告、FAIL 项的 AI 报告草稿层 |
| `scripts/field_session.py` | 现场记录助手：把一段录制目录变成客观测量 + 待填报告骨架（间隙带时间码、命名不合规会被点名） |
| `scripts/asr_align.py` | 语音-字幕对齐：`missing_speech` / `missing_subtitle` / `misordered` / `offset` 四类差异，offset 中位数·P90·最大偏差；已确认与疑似分开计数 |
| `scripts/runtime_evidence.py` | Wwise 运行时取证：Live Edit 注入故障 → 录制 → 恢复 → 校验工程哈希 |
| `inject_faults.py` | Wwise 工程故障注入：3D 衰减、播放上限、限幅行为，可回滚 |
| `waapi_probe.py` | WAAPI 探针：读取 Profiler 侧数值快照（声部、CPU、Bank、Streaming、RTPC） |

## 真实扫描结果

在真实素材上跑出来的结果（不是示例数据）：

| 素材库 | 文件数 | PASS | INFO | WARN | FAIL |
|---|---:|---:|---:|---:|---:|
| Cube 官方工程 `Originals` | 209 | 54 | 117 | 29 | 9 |
| 外部音效库 | 147 | 27 | 69 | 8 | 43 |

跨库回归比较：`0 added / 0 removed / 0 changed / 0 regressions`。报告见 [`captures/cube-live.md`](captures/cube-live.md)、[`captures/assets-live.md`](captures/assets-live.md)、[`captures/cube-diff.md`](captures/cube-diff.md)。

运行时断流判据自检：合成录音中插入 300 ms 全程静音，工具在 `1.200 s` 检出 1 处、最长 300 ms，与构造值一致（[`captures/perf/selftest.md`](captures/perf/selftest.md)）。

## 边界声明

1. **受控故障 ≠ 商业游戏缺陷**：`inject_faults.py` / `runtime_evidence.py` 对官方 Cube 工程做的实验是受控运行时故障，用于证明「改配置 → 观察 → 回滚」的定位能力，不作为任何商业游戏的缺陷结论。
2. **dropout 判据默认关闭**：素材里的静音常是设计的一部分（对话句间、环境声留白、音乐休止），默认开启会让整个素材库变成假 WARN 库；且加载与转场处的静音属设计行为，工具只报 WARN，定性必须回到画面。
3. **ASR 阈值是待校准草案**：`250 / 150 ms` 是待标定值；相似度用标准库 `difflib` 字符级比对，短句一字之差就可能误报，每条差异必须人工回听复核。报告中「已确认差异」与「疑似差异」分别计数、不合并。
4. **未完成的采集不填占位结果**：目标游戏侧的兼容矩阵、负载压测、已知问题复测在方案中明确标为「待执行 / 待采集」，仓库里不出现占位数字。
5. **第三方内容不随之发布**：官方 Wwise 工程与外部音效素材受各自许可约束，见 [`.gitignore`](.gitignore) 中的说明。

## 许可

仓库尚未附许可文件（待定）。第三方素材与官方示例工程的版权归各自权利人所有。


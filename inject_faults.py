#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inject_faults —— Wwise 工程故障注入，用于验证测试用例真的能发现问题

用途
    一份测试方案说「这条用例能查出衰减距离配错」，凭什么？
    唯一站得住的证明是：把故障注进工程，跑一遍用例，看它是否报警。
    本脚本把可控故障写进 Wwise 工程的 .wwu（纯 XML），供 Profiler 侧验证，
    并提供一键回滚。

安全边界
    只操作 --project 指向的工程副本，绝不碰 Wwise/Cube 官方安装目录。
    脚本启动时会检查目标路径是否落在 C:\\Audiokinetic 下，若是则直接拒绝。
    每次注入前把待改文件原样备份到 .inject_backup/，restore 逐字节还原。

注入点（均为实测存在于 Cube 工程中的属性）
    attenuation   Attenuations/*.wwu 的 RadiusMax        —— 衰减最大距离
    playlimit     Containers/*.wwu 的 MaxSoundPerInstance —— 同时播放上限
    limitbehavior Containers/*.wwu 的 MaxReachedBehavior  —— 达上限时的抢占策略

用法
    python inject_faults.py list     --project <dir>
    python inject_faults.py inject   --project <dir> --fault attenuation
    python inject_faults.py status   --project <dir>
    python inject_faults.py restore  --project <dir>

注入之后需要重新生成 SoundBank 才会体现到运行时：
    WwiseConsole.exe generate-soundbank <Cube.wproj> --platform Windows

退出码
    0 成功   1 拒绝执行（安全检查未过 / 无备份可回滚）   2 脚本自身错误
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

VERSION = "0.1.0"

BACKUP_DIR = ".inject_backup"
MANIFEST = "manifest.json"

# 官方安装目录前缀，命中即拒绝。故障注入是破坏性操作，
# 必须保证它只作用在工程副本上——原始工程要留着做对照基线。
FORBIDDEN_PREFIXES = (
    Path("C:/Audiokinetic"),
    Path("C:/Program Files/Audiokinetic"),
    Path("C:/Program Files (x86)/Audiokinetic"),
)


# --------------------------------------------------------------------------
# .wwu 属性读写
#
# Wwise 把属性存成两种形态，同一个属性名在不同对象上可能用不同形态：
#   <Property Name="X" Type="int16" Value="10"/>                    内联
#   <Property Name="X" Type="int16"><ValueList><Value>10</Value>...  包裹
# 只处理一种形态会漏改，所以两种都认。
# --------------------------------------------------------------------------
def _prop_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        r'<Property\s+Name="' + re.escape(name) + r'"'      # 属性名
        r'(?P<attrs>[^>]*?)'                                 # Type= 等其它属性
        r'(?:'
        r'\s*/>'                                             # 内联，可能带 Value=
        r'|'
        r'\s*>(?P<body>.*?)</Property>'                      # 包裹形态
        r')',
        re.DOTALL,
    )


_VALUE_ATTR_RE = re.compile(r'\sValue="(?P<val>[^"]*)"')
_VALUE_TAG_RE = re.compile(r"(?P<open><Value>)(?P<val>.*?)(?P<close></Value>)", re.DOTALL)


@dataclass
class PropHit:
    """一次属性命中：所属对象、当前值、在文本中的位置。"""

    owner_type: str
    owner_name: str
    prop: str
    value: str
    start: int
    end: int


# 对象开标签，用来回溯某个 Property 属于哪个对象
_OBJECT_RE = re.compile(
    r'<(?P<type>Sound|RandomSequenceContainer|SwitchContainer|BlendContainer'
    r'|ActorMixer|MusicSegment|MusicTrack|MusicSwitchContainer|MusicPlaylistContainer'
    r'|Attenuation)\s+Name="(?P<name>[^"]*)"'
)


def _owner_of(text: str, pos: int) -> tuple[str, str]:
    """回溯 pos 之前最近的对象开标签。

    .wwu 是层级 XML，属性写在对象的 <PropertyList> 里，
    所以「之前最近的对象标签」就是它的归属。用正则而非 XML 解析是有意的：
    改完要逐字节写回，DOM 往回序列化会重排属性顺序、改动缩进与自闭合形式，
    产生大量与故障无关的 diff，回滚和 code review 都会变得没法看。
    """
    last = ("", "")
    for match in _OBJECT_RE.finditer(text, 0, pos):
        last = (match.group("type"), match.group("name"))
    return last


def find_props(text: str, prop: str) -> list[PropHit]:
    hits: list[PropHit] = []
    for match in _prop_pattern(prop).finditer(text):
        body = match.group("body")
        if body is None:
            attr = _VALUE_ATTR_RE.search(match.group("attrs") or "")
            value = attr.group("val") if attr else ""
        else:
            tag = _VALUE_TAG_RE.search(body)
            value = tag.group("val").strip() if tag else ""
        owner_type, owner_name = _owner_of(text, match.start())
        hits.append(
            PropHit(owner_type, owner_name, prop, value, match.start(), match.end())
        )
    return hits


def set_prop(
    text: str,
    prop: str,
    new_value: str,
    owners: Iterable[str] | None = None,
) -> tuple[str, list[PropHit]]:
    """把 prop 改成 new_value，返回新文本与实际发生变化的命中列表。

    owners 给定时只改这些对象名下的属性。故障注入必须可归因：
    一次实验只动一个对象，Profiler 里看到的现象才能挂到这一处改动上。
    值本来就等于 new_value 的命中不计入 changed——否则会记出一堆空改动，
    让 status 看起来改了很多，实际运行时行为没变。
    """
    allow = set(owners) if owners is not None else None
    changed: list[PropHit] = []

    def repl(match: re.Match[str]) -> str:
        whole = match.group(0)
        body = match.group("body")
        owner_type, owner_name = _owner_of(text, match.start())
        if allow is not None and owner_name not in allow:
            return whole
        if body is None:
            attrs = match.group("attrs") or ""
            attr = _VALUE_ATTR_RE.search(attrs)
            if attr is None:
                # 没有 Value= 的内联属性（值取默认），补一个
                old = ""
                new_attrs = attrs.rstrip() + f' Value="{new_value}"'
            else:
                old = attr.group("val")
                new_attrs = attrs.replace(attr.group(0), f' Value="{new_value}"', 1)
            out = f'<Property Name="{prop}"{new_attrs}/>'
        else:
            tag = _VALUE_TAG_RE.search(body)
            if tag is None:
                return whole
            old = tag.group("val").strip()
            new_body = body.replace(
                tag.group(0), f"{tag.group('open')}{new_value}{tag.group('close')}", 1
            )
            out = f'<Property Name="{prop}"{match.group("attrs")}>{new_body}</Property>'
        changed.append(
            PropHit(owner_type, owner_name, prop, old, match.start(), match.end())
        )
        return out

    return _prop_pattern(prop).sub(repl, text), changed


def ensure_bool_prop(
    text: str,
    prop: str,
    value: str = "True",
    owners: Iterable[str] | None = None,
) -> str:
    """把布尔开关设成 value；属性不存在时不新建（保持改动最小）。

    Playback Limit 有 MaxSoundPerInstance（数值）和 UseMaxSoundPerInstance（开关）
    两个属性，只改数值而开关是 False 的话运行时不生效，注入就成了空操作。
    owners 必须与数值属性用同一份作用域，否则会给整个文件里的对象都打开限制开关，
    那是个比目标故障大得多的改动。
    """
    new_text, _ = set_prop(text, prop, value, owners=owners)
    return new_text


# --------------------------------------------------------------------------
# 故障定义
# --------------------------------------------------------------------------
@dataclass
class Fault:
    key: str
    title: str
    files: str                  # 相对工程根的 glob
    prop: str                   # 主属性名，list 用它报当前值
    targets: tuple[str, ...]    # 只改这些对象——一次实验只动一处，现象才可归因
    describe: str               # 这个故障在游戏里表现成什么
    detects: str                # 哪条用例应当抓到它
    apply: Callable[[str], tuple[str, list[PropHit]]]


# 注入目标。挑选原则：属性当前值与注入值不同（否则是空改动），
# 且在 Cube 可达的场景里能稳定触发。
ATTENUATION_TARGET = "Object Attenuation"   # RadiusMax 实测 70，挂在通用 3D 对象上
PLAYLIMIT_TARGET = "Footsteps"              # MaxSoundPerInstance 实测 10，多怪同屏必然打满
LIMITBEHAVIOR_TARGET = "Pain"               # MaxReachedBehavior 实测 1，连续受伤可反复触发


def _fault_attenuation(text: str) -> tuple[str, list[PropHit]]:
    """Object Attenuation 的衰减最大距离由 70 改到 5。

    改到 5 之后，离开 5 米范围声音就被完全衰减掉——玩家会感觉
    「怪物明明在旁边却没声音」。这是个典型的配置类缺陷：素材没问题、
    Event 没问题、代码没问题，单纯是衰减曲线的作用域配错了，靠听素材永远发现不了。

    工程里另有 Main Attenuation(155) / Teleporter(40) / Jump(10) 三条曲线，
    刻意不动：全改的话 Profiler 上的现象无法归因到某一条曲线，
    这份证据就失去了「改动 A 导致现象 B」的因果链。
    """
    return set_prop(text, "RadiusMax", "5", owners=(ATTENUATION_TARGET,))


def _fault_playlimit(text: str) -> tuple[str, list[PropHit]]:
    """Footsteps 的同时播放上限由 10 压到 1，并确保开关打开。

    多个怪同时走路只剩一个脚步声——Profiler 的 Voice Count 会看到
    physical voice 被压平，而 Game Sync 侧一切正常。这类缺陷在
    单人测试环境下几乎不可能复现，必须靠多目标场景 + Profiler 才能定位。

    只改 Footsteps：Bauul Foot(20)、PoisonGem Magic(4) 保持原值作对照，
    同一次录屏里就能看到「被限制的声音」与「未被限制的声音」并存。
    Under_Water / Gem_Pickup_Voice / Pain 上限本来就是 1，改了等于没改。
    """
    owners = (PLAYLIMIT_TARGET,)
    text, changed = set_prop(text, "MaxSoundPerInstance", "1", owners=owners)
    text = ensure_bool_prop(text, "UseMaxSoundPerInstance", "True", owners=owners)
    return text, changed


def _fault_limitbehavior(text: str) -> tuple[str, list[PropHit]]:
    """Pain 达上限时的行为由 1（丢弃新实例）改成 0（杀掉最旧实例）。

    改完之后连续受伤时前一声痛呼会被硬切——表现为爆音或声音突然消失。
    这个故障的价值在于它证明「同一个上限值配不同抢占策略，听感缺陷完全不同」，
    所以用例里必须把上限值与抢占策略当成两个独立参数分别验证。

    Under_Water / Gem_Pickup_Voice 同样是 1，保持不动作对照。
    """
    return set_prop(text, "MaxReachedBehavior", "0", owners=(LIMITBEHAVIOR_TARGET,))


FAULTS: dict[str, Fault] = {
    "attenuation": Fault(
        key="attenuation",
        title=f"{ATTENUATION_TARGET} 衰减最大距离 70 → 5",
        files="Attenuations/*.wwu",
        prop="RadiusMax",
        targets=(ATTENUATION_TARGET,),
        describe="超出 5 米即完全衰减，近处怪物听不见声音",
        detects="3D 定位用例：绕目标由近及远走，观察 Voice Graph 的距离衰减曲线",
        apply=_fault_attenuation,
    ),
    "playlimit": Fault(
        key="playlimit",
        title=f"{PLAYLIMIT_TARGET} 同时播放上限 10 → 1",
        files="Containers/*.wwu",
        prop="MaxSoundPerInstance",
        targets=(PLAYLIMIT_TARGET,),
        describe="多目标同时发声时只剩一个实例，其余被静默丢弃",
        detects="并发用例：多怪同时行动，看 Profiler 的 physical voice 是否被压平",
        apply=_fault_playlimit,
    ),
    "limitbehavior": Fault(
        key="limitbehavior",
        title=f"{LIMITBEHAVIOR_TARGET} 达上限行为 1（丢弃新实例）→ 0（抢占最旧实例）",
        files="Containers/*.wwu",
        prop="MaxReachedBehavior",
        targets=(LIMITBEHAVIOR_TARGET,),
        describe="连续触发时前一声被硬切，表现为爆音或声音突然消失",
        detects="连续触发用例：快速重复受伤，听是否有硬切，看 Voice Graph 的实例生命周期",
        apply=_fault_limitbehavior,
    ),
}


# --------------------------------------------------------------------------
# 安全检查与备份
# --------------------------------------------------------------------------
def assert_safe_target(project: Path) -> None:
    resolved = project.resolve()
    for forbidden in FORBIDDEN_PREFIXES:
        try:
            resolved.relative_to(forbidden.resolve())
        except (ValueError, OSError):
            continue
        raise SystemExit(
            f"拒绝执行：目标位于官方安装目录内\n"
            f"  目标：{resolved}\n"
            f"  受保护前缀：{forbidden}\n"
            f"请先把 WwiseProject 复制到工作目录，再对副本注入故障。"
        )
    if not (resolved / "Cube.wproj").is_file():
        # 不限定必须叫 Cube.wproj，但至少要有一个 .wproj，否则大概指错了目录
        if not list(resolved.glob("*.wproj")):
            raise SystemExit(f"拒绝执行：{resolved} 下没有 .wproj，看起来不是 Wwise 工程目录")


def backup_root(project: Path) -> Path:
    return project / BACKUP_DIR


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(project: Path) -> dict[str, Any]:
    path = backup_root(project) / MANIFEST
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(project: Path, data: dict[str, Any]) -> None:
    root = backup_root(project)
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def backup_file(project: Path, rel: str) -> None:
    """首次改动前备份；已备份过就不覆盖，保证 restore 总能回到最初状态。"""
    src = project / rel
    dst = backup_root(project) / rel
    if dst.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def resolve_files(project: Path, pattern: str) -> list[Path]:
    return sorted(project.glob(pattern))


def cmd_list(args: argparse.Namespace) -> int:
    project = Path(args.project)
    assert_safe_target(project)
    print(f"工程：{project.resolve()}\n")
    for fault in FAULTS.values():
        files = resolve_files(project, fault.files)
        print(f"[{fault.key}] {fault.title}")
        print(f"  表现：{fault.describe}")
        print(f"  应由哪条用例发现：{fault.detects}")
        print(f"  涉及文件：{len(files)} 个（{fault.files}）")
        # 把当前值连同「是否为注入目标」一起报出来。工程里同名属性往往挂在
        # 多个对象上，只有标 ← 的那几行会被改，其余是刻意留下的对照组。
        total = 0
        targeted = 0
        for path in files:
            for hit in find_props(path.read_text(encoding="utf-8"), fault.prop):
                total += 1
                is_target = hit.owner_name in fault.targets
                targeted += is_target
                mark = "  ← 注入目标" if is_target else ""
                print(
                    f"    {hit.owner_type:24} {hit.owner_name:32} "
                    f"{fault.prop}={hit.value}{mark}"
                )
        print(f"  命中 {total} 处，其中 {targeted} 处为注入目标，{total - targeted} 处保持原值作对照\n")
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    project = Path(args.project)
    assert_safe_target(project)
    fault = FAULTS.get(args.fault)
    if fault is None:
        raise SystemExit(f"未知故障：{args.fault}（可选：{'、'.join(FAULTS)}）")

    files = resolve_files(project, fault.files)
    if not files:
        raise SystemExit(f"没有匹配的文件：{fault.files}")

    manifest = load_manifest(project)
    injected = manifest.setdefault("injected", {})
    if fault.key in injected:
        print(f"故障 {fault.key} 已处于注入状态，先 restore 再重新注入")
        return 1

    records: list[dict[str, Any]] = []
    for path in files:
        rel = path.relative_to(project).as_posix()
        original = path.read_text(encoding="utf-8")
        new_text, changed = fault.apply(original)
        if not changed or new_text == original:
            continue
        backup_file(project, rel)
        path.write_text(new_text, encoding="utf-8")
        for hit in changed:
            records.append(
                {
                    "file": rel,
                    "owner_type": hit.owner_type,
                    "owner_name": hit.owner_name,
                    "prop": hit.prop,
                    "old_value": hit.value,
                }
            )
        print(f"  改动 {rel}：{len(changed)} 处")

    if not records:
        print("没有任何属性被改动——检查故障定义是否与工程实际结构匹配")
        return 1

    injected[fault.key] = {
        "title": fault.title,
        "changes": records,
        "files": sorted({r["file"] for r in records}),
    }
    save_manifest(project, manifest)

    print(f"\n已注入 [{fault.key}] {fault.title}")
    print(f"  改动 {len(records)} 处，涉及 {len(injected[fault.key]['files'])} 个文件")
    print(f"  备份在 {backup_root(project)}")
    print("\n下一步：重新生成 SoundBank 才会体现到运行时")
    wproj = next(iter(project.glob("*.wproj")), None)
    if wproj is not None:
        print(f'  WwiseConsole.exe generate-soundbank "{wproj}" --platform Windows')
    print(f"\n验证完成后回滚：python inject_faults.py restore --project {args.project}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project = Path(args.project)
    assert_safe_target(project)
    manifest = load_manifest(project)
    injected = manifest.get("injected", {})
    if not injected:
        print("当前无注入故障（工程为干净状态）")
        return 0
    print(f"已注入 {len(injected)} 个故障：\n")
    for key, info in injected.items():
        print(f"[{key}] {info['title']}")
        for change in info["changes"]:
            print(
                f"    {change['file']} :: {change['owner_type']} "
                f"{change['owner_name']} :: {change['prop']} 原值 {change['old_value']}"
            )
        print()
    # 顺带校验备份是否还在
    missing = [
        rel
        for info in injected.values()
        for rel in info["files"]
        if not (backup_root(project) / rel).is_file()
    ]
    if missing:
        print("警告：以下备份文件缺失，restore 无法完整还原：")
        for rel in missing:
            print(f"    {rel}")
        return 1
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    project = Path(args.project)
    assert_safe_target(project)
    root = backup_root(project)
    manifest = load_manifest(project)
    injected = manifest.get("injected", {})
    if not injected:
        print("无注入记录，无需回滚")
        return 0

    files = sorted({rel for info in injected.values() for rel in info["files"]})
    missing = [rel for rel in files if not (root / rel).is_file()]
    if missing:
        print("备份缺失，拒绝执行部分回滚（否则工程会停在半修复状态）：")
        for rel in missing:
            print(f"    {rel}")
        return 1

    for rel in files:
        shutil.copy2(root / rel, project / rel)
        print(f"  还原 {rel}")

    # 记录清空，备份文件保留——重复实验时不必再复制一次工程
    manifest["injected"] = {}
    save_manifest(project, manifest)
    print(f"\n已还原 {len(files)} 个文件，工程回到注入前状态")
    print("记得重新生成 SoundBank，否则运行时仍是带故障的 Bank")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inject_faults",
        description="向 Wwise 工程副本注入可控故障，用于验证测试用例的有效性",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add_project(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--project",
            required=True,
            help="Wwise 工程目录（必须是副本，不能是官方安装目录）",
        )

    p_list = subs.add_parser("list", help="列出可注入故障与当前属性值")
    add_project(p_list)
    p_list.set_defaults(func=cmd_list)

    p_inject = subs.add_parser("inject", help="注入指定故障")
    add_project(p_inject)
    p_inject.add_argument("--fault", required=True, choices=sorted(FAULTS))
    p_inject.set_defaults(func=cmd_inject)

    p_status = subs.add_parser("status", help="查看当前注入状态")
    add_project(p_status)
    p_status.set_defaults(func=cmd_status)

    p_restore = subs.add_parser("restore", help="回滚所有注入")
    add_project(p_restore)
    p_restore.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as exc:
        if exc.code and isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())

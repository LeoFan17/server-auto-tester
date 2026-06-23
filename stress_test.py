#!/usr/bin/env python3
"""
stress_test.py — CPU / 内存自动化压测工具

依赖：stress（apt install stress 或 yum install stress）
      cpu_mem_check.py（同目录，旧脚本作为采集模块）

用法：
    python3 stress_test.py --mode view              # 只看状态
    python3 stress_test.py --mode stress --profile 2 # 标准交付压测
    python3 stress_test.py --mode stress --force    # 强制压测（跳过状态检查）
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List

# ── 导入旧脚本的采集模块 ──
try:
    from cpu_mem_check import (
        collect_cpu,
        collect_mem,
        check_alerts,
        format_human,
        run_cmd,
    )
except ImportError:
    print("错误：未找到 cpu_mem_check.py，请确保它和本脚本在同一目录。")
    sys.exit(2)


# ═══════════════════════════════════════════════════════
# 预设方案定义（运维可根据实际机器调整）
# ═══════════════════════════════════════════════════════

PRESETS = {
    1: {
        "label": "快速冒烟",
        "cpu_workers": 2,
        "mem_bytes": "512M",
        "duration": "30s",
    },
    2: {
        "label": "标准交付",
        "cpu_workers": "physical",       # 运行时解析为物理核数
        "mem_bytes": "auto_80",          # 运行时解析为可用内存的 80%
        "duration": "5m",
    },
    3: {
        "label": "极限烤机",
        "cpu_workers": "logical",        # 运行时解析为全部逻辑核
        "mem_bytes": "auto_95",          # 运行时解析为可用内存的 95%
        "duration": "30m",
    },
}


# ═══════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════

def _find_stress() -> str:
    """检查 stress 命令是否可用"""
    path = run_cmd(["which", "stress"]).strip()
    if not path:
        print("错误：未找到 stress 命令。请执行 sudo apt install stress 或 sudo yum install stress")
        sys.exit(2)
    return path


def _resolve_workers(cpu_info: dict, spec) -> int:
    """把预设中的 'physical'/'logical' 解析为具体数字"""
    if spec == "physical":
        return cpu_info.get("physical_cores", 4)
    elif spec == "logical":
        return cpu_info.get("logical_cores", 8)
    return int(spec)


def _resolve_mem(mem_info: dict, spec: str) -> str:
    """把预设中的 'auto_80'/'auto_95' 解析为 stress 可用的 --vm-bytes 值"""
    if spec.startswith("auto_"):
        pct = int(spec.split("_")[1]) / 100.0
        available_bytes = int(mem_info.get("available_gb", 1) * 1024 ** 3)
        target_bytes = int(available_bytes * pct)
        # stress 接受 512M / 1G 这种格式
        if target_bytes >= 1024 ** 3:
            return f"{round(target_bytes / 1024**3, 1)}G"
        else:
            return f"{target_bytes // 1024**2}M"
    return spec


def _parse_duration_to_seconds(duration: str) -> int:
    """把 30s / 5m / 1h 转为秒数"""
    mapping = {"s": 1, "m": 60, "h": 3600}
    unit = duration[-1]
    value = int(duration[:-1])
    return value * mapping.get(unit, 1)


def _sample_cpu_light() -> float:
    """轻量采集 CPU 总使用率（只用 top -bn1，不跑 lscpu/dmidecode）"""
    out = run_cmd(["top", "-bn1"])
    for line in out.splitlines():
        if line.startswith("%Cpu"):
            us = sy = 0.0
            for part in line.split(","):
                part = part.strip()
                if part.endswith("us"):
                    us = float(part.split()[0]) if part.split()[0].replace(".", "").isdigit() else 0.0
                elif part.endswith("sy"):
                    sy = float(part.split()[0]) if part.split()[0].replace(".", "").isdigit() else 0.0
            return round(us + sy, 1)
    return 0.0


def _sample_mem_light() -> float:
    """轻量采集内存使用率（只读 /proc/meminfo）"""
    out = run_cmd(["cat", "/proc/meminfo"])
    total = available = 0
    for line in out.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])  # kB
        elif line.startswith("MemAvailable:"):
            available = int(line.split()[1])
    if total == 0:
        return 0.0
    return round((total - available) / total * 100, 1)


# ═══════════════════════════════════════════════════════
# 终端表格输出
# ═══════════════════════════════════════════════════════

def _print_stress_result_table(
    profile_id: int,
    label: str,
    duration_sec: int,
    sample_count: int,
    peak_cpu: float,
    avg_cpu: float,
    peak_mem: float,
    avg_mem: float,
    cpu_threshold: float,
    mem_threshold: float,
    passed: bool,
) -> None:
    """打印压测结果汇总表格"""

    def judge(val, th):
        return "✓ 通过" if val < th else "✗ 超标"

    sep = "─" * 62
    print(f"\n{sep}")
    print(f"  压测结果汇总 — 方案{profile_id}「{label}」")
    print(f"  开始: {datetime.now().isoformat(timespec='seconds')}  |  时长: {duration_sec}s  |  采样: {sample_count}点")
    print(sep)
    print(f"  {'指标':<16s}│ {'峰值':>10s} │ {'均值':>10s} │ {'判定'}")
    print(f"  {'─'*16}┼{'─'*12}┼{'─'*12}┼{'─'*16}")
    print(f"  {'CPU 使用率':<16s}│ {peak_cpu:>8.1f}% │ {avg_cpu:>8.1f}% │ {judge(peak_cpu, cpu_threshold)}")
    print(f"  {'内存使用率':<16s}│ {peak_mem:>8.1f}% │ {avg_mem:>8.1f}% │ {judge(peak_mem, mem_threshold)}")
    print(sep)
    status = "✓ 全部通过" if passed else "✗ 存在超标项"
    print(f"  最终判定: {status}")
    print(f"{sep}\n")


def _print_json_result(
    profile_id: int,
    label: str,
    duration_sec: int,
    sample_count: int,
    peak_cpu: float,
    avg_cpu: float,
    peak_mem: float,
    avg_mem: float,
    cpu_threshold: float,
    mem_threshold: float,
    passed: bool,
) -> None:
    print(json.dumps({
        "profile": profile_id,
        "label": label,
        "duration_seconds": duration_sec,
        "sample_count": sample_count,
        "peak_cpu_percent": peak_cpu,
        "avg_cpu_percent": avg_cpu,
        "peak_mem_percent": peak_mem,
        "avg_mem_percent": avg_mem,
        "cpu_threshold": cpu_threshold,
        "mem_threshold": mem_threshold,
        "passed": passed,
        "status": "PASS" if passed else "FAIL",
    }, indent=2, ensure_ascii=False))


# ═══════════════════════════════════════════════════════
# 模式：仅查看
# ═══════════════════════════════════════════════════════

def mode_view(args) -> int:
    cpu = collect_cpu()
    mem = collect_mem()
    alerts = check_alerts(cpu, mem, args.cpu, args.mem, args.load)

    if args.json:
        print(json.dumps({
            "cpu": cpu,
            "memory": mem,
            "alerts": alerts,
            "alert_count": len(alerts),
            "status": "WARNING" if alerts else "OK",
        }, indent=2, ensure_ascii=False))
    else:
        print(format_human(cpu, mem, alerts))

    return 1 if alerts else 0


# ═══════════════════════════════════════════════════════
# 模式：压测
# ═══════════════════════════════════════════════════════

def mode_stress(args) -> int:
    # 1. 找到 stress
    stress_path = _find_stress()

    # 2. 取预设方案
    profile = PRESETS.get(args.profile, PRESETS[2])
    label = profile["label"]

    # 3. 压测前采集一次完整信息（用于解析核数、内存量）
    print("→ 正在采集系统信息...")
    cpu_full = collect_cpu()
    mem_full = collect_mem()

    # 4. 解析预设中的动态值
    cpu_workers = _resolve_workers(cpu_full, profile["cpu_workers"])
    mem_bytes = _resolve_mem(mem_full, profile["mem_bytes"])
    duration_sec = _parse_duration_to_seconds(
        args.stress_time if args.stress_time else profile["duration"]
    )

    # 覆盖参数
    if args.stress_cpu is not None:
        cpu_workers = args.stress_cpu
    if args.stress_mem is not None:
        mem_bytes = args.stress_mem

    print(f"  方案: {args.profile}「{label}」")
    print(f"  CPU 工作线程: {cpu_workers}  |  内存压力: {mem_bytes}  |  时长: {duration_sec}s")

    # 5. 状态检查
    alerts = check_alerts(cpu_full, mem_full, args.cpu, args.mem, args.load)
    if alerts and not args.force:
        print("\n⚠  系统当前不满足压测条件，存在以下告警：")
        for a in alerts:
            print(f"    [{a['severity']}] {a['message']}")
        print("  如需强制执行，请加 --force 参数。")
        return 3

    if alerts and args.force:
        print(f"\n⚠  检测到 {len(alerts)} 条告警，但 --force 已启用，继续压测。")

    # 6. 启动 stress
    stress_cmd = [
        stress_path,
        "--cpu", str(cpu_workers),
        "--vm", "1",
        "--vm-bytes", mem_bytes,
        "--timeout", f"{duration_sec}s",
    ]
    print(f"\n→ 启动压测: {' '.join(stress_cmd)}")

    proc = subprocess.Popen(
        stress_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 7. 定时采集
    sample_interval = 3  # 秒
    samples: List[Dict] = []
    start_time = time.time()

    print(f"  采样中（间隔 {sample_interval}s）...")
    while proc.poll() is None:
        cpu_pct = _sample_cpu_light()
        mem_pct = _sample_mem_light()
        samples.append({
            "ts": time.time() - start_time,
            "cpu": cpu_pct,
            "mem": mem_pct,
        })
        print(f"    [{len(samples):>3d}]  CPU: {cpu_pct:>5.1f}%  │  内存: {mem_pct:>5.1f}%", end="\r")
        time.sleep(sample_interval)

    print()  # 换行，结束进度行

    # stress 退出后的收尾
    proc.wait()

    if not samples:
        print("错误：压测期间未采集到任何数据。")
        return 2

    # 8. 聚合
    peak_cpu = max(s["cpu"] for s in samples)
    avg_cpu = round(sum(s["cpu"] for s in samples) / len(samples), 1)
    peak_mem = max(s["mem"] for s in samples)
    avg_mem = round(sum(s["mem"] for s in samples) / len(samples), 1)

    # 9. 判定
    passed = (peak_cpu < args.cpu) and (peak_mem < args.mem)

    # 10. 输出
    if args.json:
        _print_json_result(
            args.profile, label, duration_sec, len(samples),
            peak_cpu, avg_cpu, peak_mem, avg_mem,
            args.cpu, args.mem, passed,
        )
    else:
        _print_stress_result_table(
            args.profile, label, duration_sec, len(samples),
            peak_cpu, avg_cpu, peak_mem, avg_mem,
            args.cpu, args.mem, passed,
        )

    return 0 if passed else 1


# ═══════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════

def _interactive_mode() -> str:
    """无 --mode 参数时的交互式选择"""
    print("请选择运行模式：")
    print("  [1] 查看模式 — 仅采集并显示当前 CPU/内存状态")
    print("  [2] 压测模式 — 运行 stress 并汇总压测结果")
    while True:
        choice = input("请输入 1 或 2: ").strip()
        if choice == "1":
            return "view"
        elif choice == "2":
            return "stress"


def main():
    parser = argparse.ArgumentParser(
        description="CPU/内存压测工具（依赖 stress + cpu_mem_check.py）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
预设方案:
  1 = 快速冒烟  (CPU:2线程, 内存:512M, 时长:30s)
  2 = 标准交付  (CPU:物理核数, 内存:可用80%, 时长:5m)
  3 = 极限烤机  (CPU:逻辑核数, 内存:可用95%, 时长:30m)

示例:
  python3 stress_test.py --mode view
  python3 stress_test.py --mode stress --profile 2
  python3 stress_test.py --mode stress --profile 3 --force
  python3 stress_test.py --mode stress --profile 2 --stress-time 10m
        """,
    )
    parser.add_argument("--mode", choices=["view", "stress"], default=None,
                        help="运行模式（不指定则交互选择）")
    parser.add_argument("--profile", type=int, choices=[1, 2, 3], default=2,
                        help="预设方案编号（默认 2=标准交付）")
    parser.add_argument("--force", action="store_true",
                        help="强制压测，跳过状态检查")
    parser.add_argument("--json", action="store_true",
                        help="JSON 输出（对接 CI）")
    parser.add_argument("--cpu", type=float, default=80.0,
                        help="CPU 告警/通过阈值 %%（默认 80）")
    parser.add_argument("--mem", type=float, default=85.0,
                        help="内存告警/通过阈值 %%（默认 85）")
    parser.add_argument("--load", type=float, default=1.5,
                        help="负载/核心告警比例（默认 1.5）")
    parser.add_argument("--stress-cpu", type=int, default=None,
                        help="覆盖方案中的 CPU 线程数")
    parser.add_argument("--stress-mem", default=None,
                        help="覆盖方案中的内存量，如 512M / 1G")
    parser.add_argument("--stress-time", default=None,
                        help="覆盖方案中的时长，如 30s / 5m / 10m")
    args = parser.parse_args()

    # 未指定 mode 时交互选择
    if args.mode is None:
        args.mode = _interactive_mode()

    if args.mode == "view":
        return mode_view(args)
    else:
        return mode_stress(args)


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""
cpu_mem_check.py — 基于系统命令的 CPU / 内存检查工具（subprocess 版）

依赖：lscpu, free, dmidecode（后者需要 root）

用法：
    python3 cpu_mem_check.py                 # 人类可读输出
    python3 cpu_mem_check.py --json          # JSON 输出（对接 CI）
    python3 cpu_mem_check.py --cpu 80 --mem 85

退出码：
    0  一切正常
    1  存在告警
    2  采集失败
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from typing import List, Dict, Optional


# ═══════════════════════════════════════════════════════
# 第 1 层：执行命令的工具函数
# ═══════════════════════════════════════════════════════

def run_cmd(cmd: list, timeout: int = 15) -> str:
    """
    安全地跑一条命令，返回 stdout 字符串。
    命令失败时返回空字符串，不崩整个脚本。
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # 有些命令（如 dmidecode 无权限）会返回非零 exit code
        # stderr 里有报错，但我们只关心 stdout 能否解析
        return result.stdout
    except FileNotFoundError:
        # 命令本身不存在（比如 dmidecode 没装）
        return ""
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════
# 第 2 层：采集 CPU 信息（lscpu + dmidecode）
# ═══════════════════════════════════════════════════════

def collect_cpu() -> dict:
    """
    返回 CPU 信息字典。
    先用 lscpu（不要求 root），再用 dmidecode 补充（需要 root）。
    """
    info: Dict[str, any] = {}

    # ── lscpu ──
    lscpu_out = run_cmd(["lscpu"])
    lscpu_map = _parse_lscpu(lscpu_out)

    info["model"] = lscpu_map.get("Model name", "Unknown")
    info["architecture"] = lscpu_map.get("Architecture", "Unknown")
    info["physical_cores"] = _parse_int(lscpu_map.get("Core(s) per socket", 0)) \
                              * _parse_int(lscpu_map.get("Socket(s)", 1))
    info["logical_cores"] = _parse_int(lscpu_map.get("CPU(s)", 0))
    # MHz 字段可能是当前频率
    freq_str = lscpu_map.get("CPU max MHz", lscpu_map.get("CPU MHz", ""))
    info["max_frequency_mhz"] = _parse_float(freq_str) if freq_str else None

    # ── dmidecode 补充（需要 root，拿不到就跳过）──
    dmidecode_out = run_cmd(["dmidecode", "-t", "processor"])
    if dmidecode_out:
        # dmidecode 里有更准的型号描述
        for line in dmidecode_out.splitlines():
            if "Version:" in line and ":" in line:
                candidate = line.split(":", 1)[1].strip()
                if candidate and "Not Specified" not in candidate:
                    info["model"] = candidate
                    break
            if "Max Speed:" in line:
                # 格式: "Max Speed: 4000 MHz"
                parts = line.split(":")
                if len(parts) >= 2:
                    speed_str = parts[1].strip().split()[0]
                    val = _parse_float(speed_str)
                    if val:
                        info["max_frequency_mhz"] = val

    # ── CPU 使用率：读 /proc/loadavg（比调 top 快）──
    load_out = run_cmd(["cat", "/proc/loadavg"])
    loads = load_out.strip().split() if load_out else []
    info["load_1min"] = _parse_float(loads[0]) if len(loads) >= 1 else 0.0
    info["load_5min"] = _parse_float(loads[1]) if len(loads) >= 2 else 0.0
    info["load_15min"] = _parse_float(loads[2]) if len(loads) >= 3 else 0.0

    # ── CPU 使用率百分比：用 top -bn1 采样 ──
    top_out = run_cmd(["top", "-bn1"])
    info["usage_percent_total"] = _parse_cpu_usage_top(top_out)
    info["usage_percent_per_core"] = []  # top -bn1 不太方便逐核，主动放弃

    info["timestamp"] = datetime.now().isoformat()
    return info


def _parse_lscpu(output: str) -> dict:
    """把 lscpu 的 'Key: Value' 格式解析为字典"""
    result = {}
    for line in output.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            result[key.strip()] = val.strip()
    return result


def _parse_cpu_usage_top(output: str) -> float:
    """
    从 `top -bn1` 输出中提取 CPU 总使用率。
    示例行： %Cpu(s):  12.3 us,  2.1 sy, ...
    返回用户态+系统态之和。
    """
    for line in output.splitlines():
        if line.startswith("%Cpu"):
            # 提取 us 和 sy 的数值
            parts = line.split(",")
            us = 0.0
            sy = 0.0
            for part in parts:
                part = part.strip()
                if part.endswith("us"):
                    us = _parse_float(part.split()[0])
                elif part.endswith("sy"):
                    sy = _parse_float(part.split()[0])
            return round(us + sy, 1)
    return 0.0


# ═══════════════════════════════════════════════════════
# 第 3 层：采集内存信息（free 命令）
# ═══════════════════════════════════════════════════════

def collect_mem() -> dict:
    """
    返回内存信息字典。
    用 `free -b`（字节）避免单位歧义，再转 GB。
    """
    info: Dict[str, any] = {}

    # free -b 输出字节，方便统一解析
    free_out = run_cmd(["free", "-b"])

    # 解析 Mem 行和 Swap 行
    mem_line = ""
    swap_line = ""
    for line in free_out.splitlines():
        if line.startswith("Mem:"):
            mem_line = line
        elif line.startswith("Swap:"):
            swap_line = line

    if mem_line:
        fields = mem_line.split()
        # free -b 输出格式：Mem: total used free shared buff/cache available
        # 字段位置是固定的
        if len(fields) >= 7:
            total = _parse_int(fields[1])
            used = _parse_int(fields[2])
            available = _parse_int(fields[6]) if len(fields) >= 7 else (total - used)
            info["total_gb"] = round(total / (1024**3), 2)
            info["used_gb"] = round((total - available) / (1024**3), 2)
            info["available_gb"] = round(available / (1024**3), 2)
            info["usage_percent"] = round((total - available) / total * 100, 1) if total > 0 else 0.0
        else:
            info["total_gb"] = info["used_gb"] = info["available_gb"] = info["usage_percent"] = 0.0
    else:
        info["total_gb"] = info["used_gb"] = info["available_gb"] = info["usage_percent"] = 0.0

    if swap_line:
        fields = swap_line.split()
        if len(fields) >= 3:
            swap_total = _parse_int(fields[1])
            swap_used = _parse_int(fields[2])
            info["swap_total_gb"] = round(swap_total / (1024**3), 2)
            info["swap_used_gb"] = round(swap_used / (1024**3), 2)
            info["swap_percent"] = round(swap_used / swap_total * 100, 1) if swap_total > 0 else 0.0
        else:
            info["swap_total_gb"] = info["swap_used_gb"] = info["swap_percent"] = 0.0
    else:
        info["swap_total_gb"] = info["swap_used_gb"] = info["swap_percent"] = 0.0

    info["timestamp"] = datetime.now().isoformat()
    return info


# ═══════════════════════════════════════════════════════
# 第 4 层：告警判定
# ═══════════════════════════════════════════════════════

def check_alerts(cpu: dict, mem: dict, cpu_threshold: float,
                 mem_threshold: float, load_factor: float) -> list:
    """根据阈值生成告警列表"""
    alerts = []

    cpu_usage = cpu.get("usage_percent_total", 0.0)
    if cpu_usage >= cpu_threshold:
        severity = "CRITICAL" if cpu_usage >= 95 else "WARNING"
        alerts.append({
            "severity": severity,
            "target": "cpu",
            "metric": "usage_percent",
            "value": cpu_usage,
            "threshold": cpu_threshold,
            "message": f"CPU 总使用率 {cpu_usage:.1f}%（阈值 {cpu_threshold}%）",
        })

    logical = cpu.get("logical_cores", 1)
    load_1 = cpu.get("load_1min", 0.0)
    if logical > 0 and load_1 / logical >= load_factor:
        ratio = round(load_1 / logical, 2)
        alerts.append({
            "severity": "WARNING",
            "target": "cpu",
            "metric": "load_ratio",
            "value": ratio,
            "threshold": load_factor,
            "message": f"1分钟负载/核心数 = {ratio}（阈值 {load_factor}），"
                       f"负载 {load_1:.2f} / 核心 {logical}",
        })

    mem_usage = mem.get("usage_percent", 0.0)
    if mem_usage >= mem_threshold:
        severity = "CRITICAL" if mem_usage >= 95 else "WARNING"
        alerts.append({
            "severity": severity,
            "target": "memory",
            "metric": "usage_percent",
            "value": mem_usage,
            "threshold": mem_threshold,
            "message": f"内存使用率 {mem_usage:.1f}%（阈值 {mem_threshold}%），"
                       f"已用 {mem.get('used_gb', 0)}GB / 总量 {mem.get('total_gb', 0)}GB",
        })

    swap_pct = mem.get("swap_percent", 0.0)
    if swap_pct >= 50:
        alerts.append({
            "severity": "WARNING",
            "target": "memory",
            "metric": "swap_percent",
            "value": swap_pct,
            "threshold": 50.0,
            "message": f"Swap 使用率 {swap_pct:.1f}%（阈值 50%）",
        })

    return alerts


# ═══════════════════════════════════════════════════════
# 第 5 层：输出格式化
# ═══════════════════════════════════════════════════════

def format_human(cpu: dict, mem: dict, alerts: list) -> str:
    sep = "─" * 62
    lines = [sep, "  CPU / 内存 检查报告", f"  采集时间: {cpu['timestamp']}", sep]

    lines.append(f"\n  ▸ CPU")
    lines.append(f"    型号          : {cpu['model']}")
    lines.append(f"    架构          : {cpu['architecture']}")
    lines.append(f"    物理核心      : {cpu['physical_cores']}")
    lines.append(f"    逻辑核心      : {cpu['logical_cores']}")
    if cpu.get("max_frequency_mhz"):
        lines.append(f"    最高频率      : {cpu['max_frequency_mhz']:.0f} MHz")
    lines.append(f"    总使用率      : {cpu['usage_percent_total']:.1f}%")
    lines.append(f"    负载 (1/5/15) : {cpu['load_1min']:.2f} / {cpu['load_5min']:.2f} / {cpu['load_15min']:.2f}")

    lines.append(f"\n  ▸ 内存")
    lines.append(f"    总量          : {mem['total_gb']} GB")
    lines.append(f"    已用          : {mem['used_gb']} GB")
    lines.append(f"    可用          : {mem['available_gb']} GB")
    lines.append(f"    使用率        : {mem['usage_percent']:.1f}%")
    lines.append(f"    Swap 总量     : {mem['swap_total_gb']} GB")
    lines.append(f"    Swap 已用     : {mem['swap_used_gb']} GB")
    lines.append(f"    Swap 使用率   : {mem['swap_percent']:.1f}%")

    if alerts:
        lines.append(f"\n  ▸ 告警 ({len(alerts)} 条)")
        for a in alerts:
            icon = "!!" if a["severity"] == "CRITICAL" else "! "
            lines.append(f"    {icon} [{a['severity']:8s}] {a['message']}")
    else:
        lines.append(f"\n  ▸ 告警: 无")

    lines.append(f"\n{sep}")
    return "\n".join(lines)


def format_json(cpu: dict, mem: dict, alerts: list) -> str:
    return json.dumps({
        "cpu": cpu,
        "memory": mem,
        "alerts": alerts,
        "alert_count": len(alerts),
        "status": "WARNING" if alerts else "OK",
    }, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════

def _parse_int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def _parse_float(s: str) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


# ═══════════════════════════════════════════════════════、
# 入口
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="CPU/内存检查（subprocess 版）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--cpu", type=float, default=80.0, help="CPU 告警阈值 %（默认 80）")
    parser.add_argument("--mem", type=float, default=85.0, help="内存告警阈值 %（默认 85）")
    parser.add_argument("--load", type=float, default=1.5, help="负载/核心告警比例（默认 1.5）")
    args = parser.parse_args()

    try:
        cpu = collect_cpu()
        mem = collect_mem()
    except Exception as e:
        print(json.dumps({"status": "ERROR", "error": str(e)}), file=sys.stderr)
        return 2

    alerts = check_alerts(cpu, mem, args.cpu, args.mem, args.load)

    if args.json:
        print(format_json(cpu, mem, alerts))
    else:
        print(format_human(cpu, mem, alerts))

    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
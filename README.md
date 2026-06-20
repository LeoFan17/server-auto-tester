# cpu-mem-check

Linux 服务器 CPU / 内存健康检查工具，适用于交付验证、日常巡检、CI 流水线集成。

## 功能

- 采集 CPU 型号、核心数、频率、使用率、系统负载
- 采集内存总量、使用量、可用量、Swap 状态
- 可配置告警阈值，输出告警列表
- 支持人类可读和 JSON 两种输出格式
- 退出码对接 CI（0=正常，1=有告警，2=采集失败）

## 环境要求

- Python 3.6+
- Linux 系统（依赖 `lscpu`、`free`、`top`、`/proc/loadavg`）
- `dmidecode` 可选（需要 root，用于补充 CPU 型号和频率信息）

## 快速开始

```bash
# 人类可读输出
python3 cpu_mem_check.py

# JSON 输出（适合 CI 解析）
python3 cpu_mem_check.py --json

# 自定义告警阈值（CPU 80%  /  内存 85%）
python3 cpu_mem_check.py --cpu 80 --mem 85

# 调整负载告警比例（负载/核心数）
python3 cpu_mem_check.py --load 2.0
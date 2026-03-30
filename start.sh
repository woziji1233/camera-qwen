#!/bin/bash
# 生产车间视频监控系统启动脚本

# 设置环境变量
export PYTHONPATH="/tmp/production-monitor/backend:$PYTHONPATH"
export QWEN_API_KEY="${QWEN_API_KEY:-your-api-key-here}"

# 创建必要目录
mkdir -p /tmp/production-monitor/hls
mkdir -p /tmp/production-monitor/alerts

# 启动后端服务
cd /tmp/production-monitor/backend
python3 main.py

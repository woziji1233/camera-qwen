#!/usr/bin/env python3
"""
简易测试服务器 - 无需第三方依赖
"""
import http.server
import socketserver
import json
import os
from pathlib import Path
import yaml

PORT = 8000

CONFIG = {
    "cameras": [],
    "nvr": {"host": "192.168.1.64", "port": 554, "username": "admin", "password": "admin123"}
}

# 初始化25个摄像头
for i in range(1, 26):
    is_discharge = i <= 2
    CONFIG["cameras"].append({
        "id": i,
        "name": f"出料口-{chr(64+i)}" if is_discharge else f"摄像头-{i:02d}",
        "location": f"车间{(i-1)//10 + 1}-{(i-1)%10 + 1}区",
        "status": "online",
        "alarm_status": "alarm" if is_discharge else "normal",
        "analyze_type": "discharge" if is_discharge else "general",
        "alarm_enabled": is_discharge,
        "rtsp_channel": i
    })

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/cameras":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(CONFIG["cameras"]).encode())
            
        elif self.path == "/api/alarms":
            alarms = {cam["id"]: {"active": cam["alarm_status"]=="alarm", "message": "测试告警"} 
                     for cam in CONFIG["cameras"] if cam["alarm_status"] == "alarm"}
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(alarms).encode())
            
        elif self.path == "/ws":
            self.send_response(400)
            self.end_headers()
            
        else:
            # 前端文件
            file_path = "/tmp/production-monitor/frontend/index.html"
            if os.path.exists(file_path):
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")

print(f"启动测试服务器: http://localhost:{PORT}")
print("按 Ctrl+C 停止")

with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
    httpd.serve_forever()

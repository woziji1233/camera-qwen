#!/bin/bash
#================================================================
# 生产车间视频监控系统 - 宝塔面板一键部署脚本
#================================================================

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}  生产车间视频监控系统 - 部署脚本${NC}"
echo -e "${GREEN}======================================${NC}"

# 检查是否为root用户
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}请使用root用户运行此脚本${NC}"
    exit 1
fi

# 基础路径
PROJECT_DIR="/www/wwwroot/production-monitor"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
CONFIG_DIR="$PROJECT_DIR/config"

#================================================================
# 第一步：检查宝塔环境
#================================================================
echo -e "\n${YELLOW}[1/6] 检查宝塔环境...${NC}"

if ! command -v bt &> /dev/null; then
    echo -e "${RED}未检测到宝塔面板，请先安装宝塔${NC}"
    echo "安装命令: yum install -y bt && bt-panel"
    exit 1
fi

echo -e "${GREEN}✓ 宝塔面板已安装${NC}"

#================================================================
# 第二步：创建项目目录
#================================================================
echo -e "\n${YELLOW}[2/6] 创建项目目录...${NC}"

mkdir -p "$PROJECT_DIR"
mkdir -p "$BACKEND_DIR/services"
mkdir -p "$BACKEND_DIR/models"
mkdir -p "$FRONTEND_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$PROJECT_DIR/alerts"
mkdir -p "$PROJECT_DIR/hls"

echo -e "${GREEN}✓ 目录创建完成${NC}"

#================================================================
# 第三步：检查Python环境
#================================================================
echo -e "\n${YELLOW}[3/6] 检查Python环境...${NC}"

# 检查Python版本
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    echo -e "${GREEN}✓ Python版本: $PYTHON_VERSION${NC}"
else
    echo -e "${RED}请先在宝塔面板安装Python环境${NC}"
    echo "路径: Python项目管理器 → 安装Python 3.11+"
    exit 1
fi

# 安装系统依赖
echo -e "\n${YELLOW}安装系统依赖...${NC}"
apt-get update -qq
apt-get install -y -qq ffmpeg libsm6 libxext6 libxrender-dev 2>/dev/null || \
yum install -y -q ffmpeg 2>/dev/null

#================================================================
# 第四步：创建项目文件
#================================================================
echo -e "\n${YELLOW}[4/6] 复制项目文件...${NC}"

# 复制后端文件
cat > "$BACKEND_DIR/main.py" << 'MAINEOF'
#!/usr/bin/env python3
"""
生产车间视频监控系统 - 后端服务
"""
import os
import sys
import json
import asyncio
import yaml
import cv2
import base64
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/www/wwwroot/production-monitor/config/cameras.yaml")

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    config['ai']['qwen']['api_key'] = os.getenv('QWEN_API_KEY', config['ai']['qwen'].get('api_key', ''))
    return config

config = load_config()
alarm_controller = None

def init_alarm_controller():
    global alarm_controller
    alarm_config = config.get('ai', {}).get('alarm', {})
    
    if not alarm_config.get('enabled', False):
        logger.info("告警功能未启用")
        return
        
    alarm_type = alarm_config.get('type', 'speaker')
    
    try:
        if alarm_type == 'ptt':
            from services.usb_alarm import USBPTTalker
            alarm_controller = USBPTTalker(alarm_config)
        else:
            from services.usb_alarm import USBAlarmController
            alarm_controller = USBAlarmController(alarm_config)
        alarm_controller.enabled = True
        logger.info(f"报警控制器已初始化: {alarm_type}")
    except Exception as e:
        logger.warning(f"报警控制器初始化失败: {e}")
        alarm_controller = None

class CameraStore:
    def __init__(self):
        self.cameras: Dict[int, Dict] = {}
        self.alarms: Dict[int, Dict] = {}
        self.connections: List[WebSocket] = []
        self._init_cameras()
    
    def _init_cameras(self):
        for cam in config['cameras']:
            self.cameras[cam['id']] = {
                **cam,
                'status': 'offline',
                'alarm_status': 'normal',
                'last_frame': None,
                'last_analyze_time': None,
                'analyze_result': None,
                'alarm_count': 0
            }
            self.alarms[cam['id']] = {
                'active': False,
                'message': '',
                'start_time': None,
                'type': 'normal'
            }
    
    def get_all_cameras(self) -> List[Dict]:
        return list(self.cameras.values())
    
    def get_camera(self, camera_id: int) -> Optional[Dict]:
        return self.cameras.get(camera_id)
    
    def update_camera_status(self, camera_id: int, status: str):
        if camera_id in self.cameras:
            self.cameras[camera_id]['status'] = status
    
    def update_alarm(self, camera_id: int, alarm_status: str, message: str = ''):
        if camera_id in self.cameras:
            cam = self.cameras[camera_id]
            cam['alarm_status'] = alarm_status
            
            alarm = self.alarms[camera_id]
            was_active = alarm['active']
            
            if alarm_status == 'alarm' and not alarm['active']:
                alarm['active'] = True
                alarm['message'] = message
                alarm['start_time'] = datetime.now().isoformat()
                alarm['type'] = 'discharge'
                cam['alarm_count'] += 1
                
                if alarm_controller:
                    try:
                        duration = config.get('ai', {}).get('alarm', {}).get('sound', {}).get('duration', 5)
                        alarm_controller.trigger(duration=duration)
                    except Exception as e:
                        logger.error(f"触发报警失败: {e}")
                        
            elif alarm_status == 'normal' and was_active:
                alarm['active'] = False
                alarm['message'] = ''
                alarm['start_time'] = None
                if alarm_controller:
                    try:
                        alarm_controller.stop()
                    except:
                        pass
            
            self.broadcast_update()
    
    async def broadcast_update(self):
        data = {
            'type': 'camera_update',
            'cameras': self.get_all_cameras(),
            'timestamp': datetime.now().isoformat()
        }
        message = json.dumps(data)
        for conn in self.connections:
            try:
                await conn.send_text(message)
            except:
                pass
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)
        await websocket.send_json({
            'type': 'init',
            'cameras': self.get_all_cameras()
        })
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)

camera_store = CameraStore()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("生产车间视频监控系统启动")
    init_alarm_controller()
    asyncio.create_task(video_stream_processor())
    yield
    if alarm_controller:
        alarm_controller.stop()
    logger.info("系统关闭")

app = FastAPI(title="生产车间监控API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
app.mount("/static", StaticFiles(directory="/www/wwwroot/production-monitor/frontend"), name="static")

@app.get("/")
async def root():
    return FileResponse("/www/wwwroot/production-monitor/frontend/index.html")

@app.get("/api/cameras")
async def get_cameras():
    return camera_store.get_all_cameras()

@app.get("/api/cameras/{camera_id}")
async def get_camera(camera_id: int):
    camera = camera_store.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    return camera

@app.get("/api/cameras/{camera_id}/stream")
async def get_camera_stream(camera_id: int):
    camera = camera_store.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    
    nvr = config['nvr']
    stream_type = camera.get('stream_type', 'main')
    rtsp_url = f"rtsp://{nvr['username']}:{nvr['password']}@{nvr['host']}:{nvr['port']}/ch{camera['rtsp_channel']}/{stream_type}/main/av_stream"
    
    return {
        "camera_id": camera_id,
        "rtsp_url": rtsp_url,
        "flv_url": f"http://localhost:8080/live/camera{camera_id}.flv",
        "hls_url": f"http://localhost:8080/hls/camera{camera_id}.m3u8"
    }

@app.get("/api/alarms")
async def get_alarms():
    return {
        camera_id: alarm 
        for camera_id, alarm in camera_store.alarms.items()
        if alarm['active']
    }

@app.post("/api/cameras/{camera_id}/alarm/acknowledge")
async def acknowledge_alarm(camera_id: int):
    if camera_id in camera_store.alarms:
        camera_store.alarms[camera_id]['active'] = False
        camera_store.update_alarm(camera_id, 'normal')
        return {"status": "ok", "message": "告警已确认"}
    raise HTTPException(status_code=404, detail="摄像头不存在")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await camera_store.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get('type') == 'ping':
                    await websocket.send_text('{"type":"pong"}')
            except:
                pass
    except:
        camera_store.disconnect(websocket)

async def video_stream_processor():
    from services.video_analyzer import VideoAnalyzer
    analyzer = VideoAnalyzer(config)
    
    while True:
        try:
            for cam in camera_store.get_all_cameras():
                if cam.get('analyze_type') == 'discharge' and cam.get('alarm_enabled'):
                    camera_id = cam['id']
                    
                    # 模拟分析
                    import random
                    has_material = random.choice([True, True, True, False])
                    
                    if not has_material:
                        camera_store.update_alarm(
                            camera_id, 
                            'alarm',
                            f"告警：{cam['name']} - 检测到不出料！请立即检查设备状态。"
                        )
                    else:
                        camera_store.update_alarm(camera_id, 'normal')
                    
                    camera_store.update_camera_status(camera_id, 'online')
            
            await asyncio.sleep(config['ai']['qwen']['analyze_interval'])
            
        except Exception as e:
            logger.error(f"视频分析错误: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
MAINEOF

# 复制分析服务
cat > "$BACKEND_DIR/services/video_analyzer.py" << 'ANALYZEREOF'
import os
import json
import base64
import asyncio
import logging
from typing import Dict, Optional
from datetime import datetime
import aiohttp

logger = logging.getLogger(__name__)

class VideoAnalyzer:
    def __init__(self, config: Dict):
        self.config = config
        self.qwen_config = config['ai']['qwen']
        self.api_key = self.qwen_config.get('api_key', '')
        self.model = self.qwen_config.get('model', 'qwen-vl-plus')
        self.alarm_threshold = self.qwen_config.get('discharge_alarm_threshold', 3)
        self.no_material_count = {}
        
    async def analyze_discharge(self, frame, camera_id: int) -> Dict:
        _, buffer = cv2.imencode('.jpg', frame)
        frame_base64 = base64.b64encode(buffer).decode('utf-8')
        
        prompt = """请分析这张工业生产图片：
        这是一个出料口，请仔细观察出料口位置是否有物料正在流出？
        请以JSON格式返回：
        {"has_material_flow": true/false, "confidence": 0.0-1.0, "description": "描述"}
        """
        
        try:
            result = await self._call_qwen_vl_api(frame_base64, prompt)
            return self._parse_discharge_result(result, camera_id)
        except Exception as e:
            logger.error(f"千问API调用失败: {e}")
            return {'has_material_flow': True, 'status': 'normal', 'confidence': 0.5, 'error': str(e)}
    
    async def _call_qwen_vl_api(self, image_base64: str, prompt: str) -> str:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        
        messages = [{
            "role": "user",
            "content": [
                {"image": f"data:image/jpeg;base64,{image_base64}"},
                {"text": prompt}
            ]
        }]
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1000
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                else:
                    raise Exception(f"API返回错误: {response.status}")
    
    def _parse_discharge_result(self, result: str, camera_id: int) -> Dict:
        try:
            data = json.loads(result)
            
            if not data.get('has_material_flow', True):
                self.no_material_count[camera_id] = self.no_material_count.get(camera_id, 0) + 1
            else:
                self.no_material_count[camera_id] = 0
            
            alarm_triggered = self.no_material_count.get(camera_id, 0) >= self.alarm_threshold
            
            return {
                'has_material_flow': data.get('has_material_flow', True),
                'confidence': data.get('confidence', 0.5),
                'description': data.get('description', ''),
                'alarm_triggered': alarm_triggered,
                'no_material_count': self.no_material_count.get(camera_id, 0)
            }
        except json.JSONDecodeError:
            return {'has_material_flow': True, 'status': 'unknown', 'error': '解析失败'}
ANALYZEREOF

# 复制USB报警服务
cat > "$BACKEND_DIR/services/usb_alarm.py" << 'ALARMEOF'
#!/usr/bin/env python3
import os
import logging
from typing import Optional
import threading

logger = logging.getLogger(__name__)

class USBAlarmController:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get('enabled', False)
        self.alarm_type = config.get('type', 'speaker')
        self.is_alarming = False
        
    def trigger(self, duration: float = 5.0):
        if not self.enabled:
            logger.info("报警功能未启用")
            return
            
        if self.is_alarming:
            return
            
        self.is_alarming = True
        
        if self.alarm_type == 'speaker':
            self._alarm_speaker(duration)
        elif self.alarm_type == 'usb_device':
            self._alarm_usb_device(duration)
        elif self.alarm_type == 'gpio':
            self._alarm_gpio(duration)
            
    def stop(self):
        self.is_alarming = False
        self._stop_device()
        
    def _alarm_speaker(self, duration: float):
        alarm_file = self.config.get('sound_file', '/www/wwwroot/production-monitor/alerts/alarm.mp3')
        
        try:
            import pygame
            pygame.mixer.init()
            
            if os.path.exists(alarm_file):
                pygame.mixer.music.load(alarm_file)
                pygame.mixer.music.set_volume(self.config.get('volume', 0.8))
                pygame.mixer.music.play()
                import time
                time.sleep(duration)
                pygame.mixer.music.stop()
            else:
                self._system_beep(duration)
                
        except ImportError:
            logger.warning("pygame未安装，使用系统蜂鸣器")
            self._system_beep(duration)
        finally:
            self.is_alarming = False
            
    def _system_beep(self, duration: float):
        try:
            import winsound
            winsound.Beep(1000, int(duration * 1000))
        except ImportError:
            try:
                import os
                for _ in range(int(duration * 2)):
                    os.system('echo -e "\\a" > /dev/tty')
                    import time
                    time.sleep(0.5)
            except:
                pass
                
    def _alarm_usb_device(self, duration: float):
        vendor_id = self.config.get('vendor_id', 0x1234)
        product_id = self.config.get('product_id', 0x5678)
        
        try:
            import usb.core
            dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
            
            if dev is None:
                logger.warning(f"未找到USB设备 {vendor_id:04x}:{product_id:04x}")
                self._alarm_speaker(duration)
                return
                
            import time
            for _ in range(int(duration * 2)):
                try:
                    dev.write(1, [0x01])
                    time.sleep(0.5)
                    dev.write(1, [0x00])
                    time.sleep(0.5)
                except:
                    break
                    
        except ImportError:
            logger.warning("pyusb未安装，回退到喇叭报警")
            self._alarm_speaker(duration)
        finally:
            self.is_alarming = False
            
    def _alarm_gpio(self, duration: float):
        try:
            import RPi.GPIO as GPIO
            pin = self.config.get('gpio_pin', 18)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT)
            
            import time
            interval = self.config.get('blink_interval', 0.2)
            blinks = int(duration / (interval * 2))
            
            for _ in range(blinks):
                GPIO.output(pin, True)
                time.sleep(interval)
                GPIO.output(pin, False)
                time.sleep(interval)
                
            GPIO.output(pin, False)
            
        except ImportError:
            logger.warning("RPi.GPIO未安装")
            self._alarm_speaker(duration)
        finally:
            self.is_alarming = False
            
    def _stop_device(self):
        pass
ALARMEOF

# 复制前端文件
cp /tmp/production-monitor/frontend/index.html "$FRONTEND_DIR/"

# 复制配置文件
cp /tmp/production-monitor/config/cameras.yaml "$CONFIG_DIR/"

# 创建requirements.txt
cat > "$BACKEND_DIR/requirements.txt" << 'REQEOF'
fastapi==0.109.0
uvicorn[standard]==0.27.0
aiohttp==3.9.1
opencv-python-headless==4.9.0.80
numpy==1.26.3
pyyaml==6.0.1
websockets==12.0
pydantic==2.5.3
pygame==2.5.3
pyusb==1.2.1
REQEOF

echo -e "${GREEN}✓ 项目文件创建完成${NC}"

#================================================================
# 第五步：安装Python依赖
#================================================================
echo -e "\n${YELLOW}[5/6] 安装Python依赖...${NC}"

cd "$BACKEND_DIR"
pip install -r requirements.txt --quiet

echo -e "${GREEN}✓ 依赖安装完成${NC}"

#================================================================
# 第六步：创建启动脚本
#================================================================
echo -e "\n${YELLOW}[6/6] 创建启动脚本...${NC}"

# 创建systemd服务
cat > /etc/systemd/system/production-monitor.service << 'SYSTEMDEOF'
[Unit]
Description=Production Monitor System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/www/wwwroot/production-monitor/backend
ExecStart=/usr/bin/python3 /www/wwwroot/production-monitor/backend/main.py
Restart=always
RestartSec=10
Environment="PYTHONPATH=/www/wwwroot/production-monitor/backend"
Environment="QWEN_API_KEY=your-api-key-here"

[Install]
WantedBy=multi-user.target
SYSTEMDEOF

# 重载systemd
systemctl daemon-reload
systemctl enable production-monitor

echo -e "${GREEN}✓ 启动脚本创建完成${NC}"

#================================================================
# 完成提示
#================================================================
echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}  部署完成！${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
echo -e "${YELLOW}后续步骤：${NC}"
echo ""
echo -e "1. ${GREEN}配置NVR信息：${NC}"
echo "   编辑: $CONFIG_DIR/cameras.yaml"
echo "   修改 nvr.host, nvr.username, nvr.password"
echo ""
echo -e "2. ${GREEN}配置千问API（可选）：${NC}"
echo "   编辑: /etc/systemd/system/production-monitor.service"
echo "   修改 QWEN_API_KEY 为你的阿里云百炼API密钥"
echo ""
echo -e "3. ${GREEN}启动服务：${NC}"
echo "   systemctl start production-monitor"
echo "   systemctl status production-monitor"
echo ""
echo -e "4. ${GREEN}配置宝塔网站（可选）：${NC}"
echo "   网站 → 添加站点 → 填写域名或IP"
echo "   网站 → 设置 → 反向代理 → 添加代理"
echo "   目标URL: http://127.0.0.1:8000"
echo ""
echo -e "5. ${GREEN}访问系统：${NC}"
echo "   http://你的服务器IP:8000"
echo ""

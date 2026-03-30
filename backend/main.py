#!/usr/bin/env python3
"""
生产车间视频监控系统 - 后端服务
基于FastAPI + 千问大模型
"""
import os
import sys
import json
import asyncio
import yaml
try:
    import cv2
except ImportError:
    cv2 = None
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

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 加载配置
CONFIG_PATH = Path("/tmp/production-monitor/config/cameras.yaml")

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    # 处理环境变量
    config['ai']['qwen']['api_key'] = os.getenv('QWEN_API_KEY', config['ai']['qwen'].get('api_key', ''))
    return config

config = load_config()

# 初始化USB报警控制器
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

# 数据存储
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
                'alarm_status': 'normal',  # normal, warning, alarm
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
                
                # 触发USB声光报警
                if alarm_controller:
                    try:
                        if hasattr(alarm_controller, 'speak'):
                            alarm_controller.alarm_with_voice(cam['name'], message)
                        else:
                            duration = config.get('ai', {}).get('alarm', {}).get('sound', {}).get('duration', 5)
                            alarm_controller.trigger(duration=duration)
                    except Exception as e:
                        logger.error(f"触发报警失败: {e}")
                        
            elif alarm_status == 'normal' and was_active:
                alarm['active'] = False
                alarm['message'] = ''
                alarm['start_time'] = None
                
                # 停止报警
                if alarm_controller:
                    try:
                        alarm_controller.stop()
                    except:
                        pass
            
            # 广播更新
            self.broadcast_update()
    
    async def broadcast_update(self):
        """广播更新到所有WebSocket连接"""
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
        # 发送初始数据
        await websocket.send_json({
            'type': 'init',
            'cameras': self.get_all_cameras()
        })
    
    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)

camera_store = CameraStore()

# FastAPI应用
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("生产车间视频监控系统启动")
    # 初始化报警控制器
    init_alarm_controller()
    # 启动视频流处理
    asyncio.create_task(video_stream_processor())
    yield
    # 停止报警
    if alarm_controller:
        alarm_controller.stop()
    logger.info("系统关闭")

app = FastAPI(title="生产车间监控API", lifespan=lifespan)

# 首页
FRONTEND_PATH = Path("/tmp/production-monitor/frontend")

@app.get("/")
async def root():
    index_file = FRONTEND_PATH / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "生产车间视频监控系统"}

# 静态文件
app.mount("/static", StaticFiles(directory=str(FRONTEND_PATH)), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Models
class CameraResponse(BaseModel):
    id: int
    name: str
    location: str
    status: str
    alarm_status: str
    analyze_type: str
    alarm_enabled: bool

class AlarmResponse(BaseModel):
    camera_id: int
    active: bool
    message: str
    start_time: Optional[str]
    type: str

# API Routes
@app.get("/api/cameras", response_model=List[CameraResponse])
async def get_cameras():
    """获取所有摄像头列表"""
    return camera_store.get_all_cameras()

@app.get("/api/cameras/{camera_id}", response_model=CameraResponse)
async def get_camera(camera_id: int):
    """获取单个摄像头信息"""
    camera = camera_store.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    return camera

@app.get("/api/cameras/{camera_id}/stream")
async def get_camera_stream(camera_id: int):
    """获取摄像头视频流地址"""
    camera = camera_store.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    
    # 生成RTSP地址 (实际使用时替换为真实的流媒体服务器)
    nvr = config['nvr']
    stream_type = camera.get('stream_type', 'main')
    
    # 海康威视NVR RTSP URL格式
    rtsp_url = f"rtsp://{nvr['username']}:{nvr['password']}@{nvr['host']}:{nvr['port']}/ch{camera['rtsp_channel']}/{stream_type}/main/av_stream"
    
    return {
        "camera_id": camera_id,
        "rtsp_url": rtsp_url,
        "flv_url": f"http://localhost:8080/live/camera{camera_id}.flv",
        "hls_url": f"http://localhost:8080/hls/camera{camera_id}.m3u8"
    }

@app.get("/api/alarms")
async def get_alarms():
    """获取所有告警状态"""
    return {
        camera_id: alarm 
        for camera_id, alarm in camera_store.alarms.items()
        if alarm['active']
    }

@app.post("/api/cameras/{camera_id}/alarm/acknowledge")
async def acknowledge_alarm(camera_id: int):
    """确认告警"""
    if camera_id in camera_store.alarms:
        camera_store.alarms[camera_id]['active'] = False
        camera_store.update_alarm(camera_id, 'normal')
        return {"status": "ok", "message": "告警已确认"}
    raise HTTPException(status_code=404, detail="摄像头不存在")

# 对话请求模型
class ChatRequest(BaseModel):
    message: str
    context: str = ""

@app.post("/api/chat")
async def chat(request: ChatRequest):
    """智能对话"""
    import aiohttp
    
    qwen_config = config.get('ai', {}).get('qwen', {})
    api_key = qwen_config.get('api_key', '')
    model = qwen_config.get('model', 'qwen-plus')
    
    # 获取默认模型
    default_model = config.get('ai', {}).get('default_model', 'qwen')
    qwen_config = config.get('ai', {}).get('qwen', {})
    deepseek_config = config.get('ai', {}).get('deepseek', {})
    
    # 根据选择的模型调用不同的API
    if default_model == 'deepseek':
        api_key = deepseek_config.get('api_key', os.getenv('DEEPSEEK_API_KEY', ''))
        model = deepseek_config.get('model', 'deepseek-chat')
        
        if not api_key:
            return {"reply": "请先配置DeepSeek API Key才能使用对话功能。"}
        
        url = "https://api.deepseek.com/chat/completions"
    else:
        api_key = qwen_config.get('api_key', os.getenv('QWEN_API_KEY', ''))
        model = qwen_config.get('model', 'qwen-plus')
        
        if not api_key:
            return {"reply": "请先配置千问API Key才能使用对话功能。"}
        
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    
    # 构建提示词
    system_prompt = """你是一个生产车间视频监控系统的智能助手。请根据系统状态信息回答用户问题。
    - 如果用户问报警情况，请统计并说明
    - 如果用户问生产情况，请根据摄像头状态分析
    - 如果不知道的信息，请如实说明
    - 回答要简洁明了"""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"{request.context}\n\n用户问题：{request.message}"}
    ]
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 500
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    result = await response.json()
                    reply = result['choices'][0]['message']['content']
                    return {"reply": reply, "model": default_model}
                else:
                    return {"reply": f"API调用失败，请检查{default_model.upper()} API Key配置。"}
    except Exception as e:
        logger.error(f"对话API错误: {e}")
        return {"reply": f"抱歉，处理您的请求时出错：{str(e)}"}

@app.get("/api/models")
async def get_models():
    """获取可用的AI模型"""
    return {
        "current": config.get('ai', {}).get('default_model', 'qwen'),
        "available": ["qwen", "deepseek"]
    }

@app.post("/api/models")
async def set_model(model: str):
    """切换AI模型"""
    if model not in ['qwen', 'deepseek']:
        return {"error": "无效的模型"}
    
    # 修改配置
    if 'ai' not in config:
        config['ai'] = {}
    config['ai']['default_model'] = model
    
    # 保存到文件
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        yaml_data = yaml.safe_load(f)
    
    if 'ai' not in yaml_data:
        yaml_data['ai'] = {}
    yaml_data['ai']['default_model'] = model
    
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(yaml_data, f, allow_unicode=True)
    
    return {"status": "ok", "current": model}

@app.get("/api/alarms/history")
async def get_alarm_history():
    """获取告警历史"""
    # 返回带有告警次数的摄像头
    return {
        cam['id']: {
            'alarm_count': cam.get('alarm_count', 0),
            'last_alarm': camera_store.alarms[cam['id']]['start_time']
        }
        for cam in camera_store.get_all_cameras()
        if cam.get('alarm_count', 0) > 0
    }

# WebSocket连接
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket实时推送"""
    await camera_store.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # 处理客户端消息
            try:
                msg = json.loads(data)
                if msg.get('type') == 'ping':
                    await websocket.send_text('{"type":"pong"}')
            except:
                pass
    except:
        camera_store.disconnect(websocket)

# 视频流处理和AI分析
async def video_stream_processor():
    """视频流处理器 - 定期分析视频帧"""
    from services.video_analyzer import VideoAnalyzer
    
    analyzer = VideoAnalyzer(config)
    
    while True:
        try:
            # 获取需要分析的摄像头 (出料口)
            for cam in camera_store.get_all_cameras():
                if cam.get('analyze_type') == 'discharge' and cam.get('alarm_enabled'):
                    camera_id = cam['id']
                    
                    # 模拟获取视频帧 (实际需要从RTSP流获取)
                    # frame = await get_frame_from_rtsp(camera_id)
                    
                    # 分析结果 (实际调用千问API)
                    # result = await analyzer.analyze_discharge(frame)
                    
                    # 模拟分析结果
                    import random
                    has_material = random.choice([True, True, True, False])  # 75%概率有物料
                    
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

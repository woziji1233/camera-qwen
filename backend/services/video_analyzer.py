#!/usr/bin/env python3
"""
千问大模型视频分析服务
使用阿里云百炼API进行视频理解
"""
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
    """视频分析器 - 使用千问大模型"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.qwen_config = config['ai']['qwen']
        self.api_key = self.qwen_config.get('api_key', '')
        self.model = self.qwen_config.get('model', 'qwen-vl-plus')
        self.alarm_threshold = self.qwen_config.get('discharge_alarm_threshold', 3)
        self.no_material_count = {}  # 记录连续无物料次数
        
    async def analyze_frame(self, frame, camera_id: int, analyze_type: str) -> Dict:
        """
        分析视频帧
        frame: cv2读取的帧
        camera_id: 摄像头ID
        analyze_type: 分析类型 (discharge/general)
        """
        if analyze_type == 'discharge':
            return await self.analyze_discharge(frame, camera_id)
        else:
            return await self.analyze_general(frame)
    
    async def analyze_discharge(self, frame, camera_id: int) -> Dict:
        """
        分析出料口是否有物料流出
        使用千问VL模型进行视觉理解
        """
        # 将帧转为base64
        _, buffer = cv2.imencode('.jpg', frame)
        frame_base64 = base64.b64encode(buffer).decode('utf-8')
        
        # 构建提示词
        prompt = """请分析这张工业生产图片：
        这是一个出料口，请仔细观察出料口位置：
        1. 是否有物料正在从出料口流出？
        2. 出料口是否有物料积压或堵塞？
        3. 传送带是否正常运转？
        
        请以JSON格式返回分析结果：
        {
            "has_material_flow": true/false,
            "material_status": "normal/blocked/absent",
            "conveyor_status": "running/stopped",
            "confidence": 0.0-1.0,
            "description": "简要描述"
        }
        """
        
        # 调用千问API (需要替换为实际API调用)
        try:
            result = await self._call_qwen_vl_api(frame_base64, prompt)
            return self._parse_discharge_result(result, camera_id)
        except Exception as e:
            logger.error(f"千问API调用失败: {e}")
            # 返回默认结果
            return {
                'has_material_flow': True,
                'status': 'normal',
                'confidence': 0.5,
                'error': str(e)
            }
    
    async def analyze_general(self, frame) -> Dict:
        """
        通用视频分析 - 检测异常行为、安全隐患等
        """
        _, buffer = cv2.imencode('.jpg', frame)
        frame_base64 = base64.b64encode(buffer).decode('utf-8')
        
        prompt = """请分析这张工厂监控图片：
        1. 是否有人员？
        2. 是否有异常情况（如烟雾、火焰、物品洒落）？
        3. 是否有安全隐患？
        
        请以JSON格式返回：
        {
            "has_person": true/false,
            "has_anomaly": true/false,
            "anomaly_type": "none/smoke/fire/spillage/other",
            "safety_status": "safe/warning/danger",
            "description": "描述"
        }
        """
        
        try:
            result = await self._call_qwen_vl_api(frame_base64, prompt)
            return self._parse_general_result(result)
        except Exception as e:
            logger.error(f"通用分析失败: {e}")
            return {'safety_status': 'safe', 'error': str(e)}
    
    async def _call_qwen_vl_api(self, image_base64: str, prompt: str) -> str:
        """
        调用千问VL API
        需要在阿里云百炼平台获取API Key
        """
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        
        # 构建消息
        messages = [
            {
                "role": "user",
                "content": [
                    {"image": f"data:image/jpeg;base64,{image_base64}"},
                    {"text": prompt}
                ]
            }
        ]
        
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
        """解析出料口分析结果"""
        try:
            # 尝试解析JSON
            data = json.loads(result)
            
            # 统计连续无物料次数
            if not data.get('has_material_flow', True):
                self.no_material_count[camera_id] = self.no_material_count.get(camera_id, 0) + 1
            else:
                self.no_material_count[camera_id] = 0
            
            # 判断是否触发告警
            alarm_triggered = self.no_material_count.get(camera_id, 0) >= self.alarm_threshold
            
            return {
                'has_material_flow': data.get('has_material_flow', True),
                'material_status': data.get('material_status', 'normal'),
                'conveyor_status': data.get('conveyor_status', 'running'),
                'confidence': data.get('confidence', 0.5),
                'description': data.get('description', ''),
                'alarm_triggered': alarm_triggered,
                'no_material_count': self.no_material_count.get(camera_id, 0)
            }
        except json.JSONDecodeError:
            logger.error(f"解析结果失败: {result}")
            return {'has_material_flow': True, 'status': 'unknown', 'error': '解析失败'}
    
    def _parse_general_result(self, result: str) -> Dict:
        """解析通用分析结果"""
        try:
            data = json.loads(result)
            return {
                'has_person': data.get('has_person', False),
                'has_anomaly': data.get('has_anomaly', False),
                'anomaly_type': data.get('anomaly_type', 'none'),
                'safety_status': data.get('safety_status', 'safe'),
                'description': data.get('description', '')
            }
        except json.JSONDecodeError:
            return {'safety_status': 'safe', 'error': '解析失败'}


class AlarmManager:
    """告警管理器"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.alarm_config = config['ai']['alarm']
        self.active_alarms = {}
    
    async def trigger_alarm(self, camera_id: int, message: str):
        """触发告警"""
        if camera_id in self.active_alarms:
            return  # 告警已存在
        
        self.active_alarms[camera_id] = {
            'message': message,
            'start_time': datetime.now()
        }
        
        # 声音告警
        if self.alarm_config['sound']['enabled']:
            await self._play_alarm_sound()
        
        # 灯光告警
        if self.alarm_config['light']['enabled']:
            await self._trigger_light_alarm()
        
        logger.warning(f"告警触发 - 摄像头{camera_id}: {message}")
    
    async def clear_alarm(self, camera_id: int):
        """清除告警"""
        if camera_id in self.active_alarms:
            del self.active_alarms[camera_id]
            logger.info(f"告警清除 - 摄像头{camera_id}")
    
    async def _play_alarm_sound(self):
        """播放告警声音"""
        try:
            alarm_file = self.alarm_config['sound']['alarm_file']
            if os.path.exists(alarm_file):
                # 使用pygame播放声音
                import pygame
                pygame.mixer.init()
                pygame.mixer.music.load(alarm_file)
                pygame.mixer.music.set_volume(self.alarm_config['sound']['volume'])
                pygame.mixer.music.play()
        except Exception as e:
            logger.error(f"播放告警声音失败: {e}")
    
    async def _trigger_light_alarm(self):
        """触发灯光告警 (需要GPIO硬件支持)"""
        try:
            import RPi.GPIO as GPIO
            gpio_pin = self.alarm_config['light']['gpio_pin']
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(gpio_pin, GPIO.OUT)
            
            # 闪烁灯光
            for _ in range(10):
                GPIO.output(gpio_pin, True)
                await asyncio.sleep(0.2)
                GPIO.output(gpio_pin, False)
                await asyncio.sleep(0.2)
        except ImportError:
            logger.warning("RPi.GPIO未安装，灯光告警不可用")
        except Exception as e:
            logger.error(f"灯光告警失败: {e}")

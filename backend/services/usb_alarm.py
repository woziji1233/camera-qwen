#!/usr/bin/env python3
"""
USB声光报警器控制模块
支持多种USB设备
"""
import os
import logging
from typing import Optional
import threading

logger = logging.getLogger(__name__)


class USBAlarmController:
    """USB声光报警控制器"""
    
    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get('enabled', False)
        self.alarm_type = config.get('type', 'speaker')  # speaker, usb_device, gpio
        self.is_alarming = False
        self.alarm_thread: Optional[threading.Thread] = None
        
    def trigger(self, duration: float = 5.0):
        """触发报警"""
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
        """停止报警"""
        self.is_alarming = False
        
        if self.alarm_thread and self.alarm_thread.is_alive():
            self.alarm_thread.join(timeout=1)
            
        self._stop_device()
        
    def _alarm_speaker(self, duration: float):
        """通过USB小喇叭/主机喇叭报警"""
        alarm_file = self.config.get('sound_file', '/tmp/production-monitor/alerts/alarm.mp3')
        
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
                # 系统蜂鸣器
                self._system_beep(duration)
                
        except ImportError:
            logger.warning("pygame未安装，使用系统蜂鸣器")
            self._system_beep(duration)
        finally:
            self.is_alarming = False
            
    def _system_beep(self, duration: float):
        """系统蜂鸣器"""
        try:
            import winsound  # Windows
            winsound.Beep(1000, int(duration * 1000))
        except ImportError:
            try:
                # Linux/macOS
                import os
                for _ in range(int(duration * 2)):
                    os.system('echo -e "\\a" > /dev/tty')
                    import time
                    time.sleep(0.5)
            except:
                pass
                
    def _alarm_usb_device(self, duration: float):
        """USB设备报警"""
        vendor_id = self.config.get('vendor_id', 0x1234)
        product_id = self.config.get('product_id', 0x5678)
        
        try:
            import usb.core
            import usb.util
            
            dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
            
            if dev is None:
                logger.warning(f"未找到USB设备 {vendor_id:04x}:{product_id:04x}")
                self._alarm_speaker(duration)  # 回退到喇叭
                return
                
            # 尝试发送命令
            for _ in range(int(duration * 2)):
                try:
                    dev.write(1, [0x01])  # 开启报警
                    import time
                    time.sleep(0.5)
                    dev.write(1, [0x00])  # 关闭
                    time.sleep(0.5)
                except:
                    break
                    
            try:
                dev.write(1, [0x00])  # 确保关闭
            except:
                pass
                
        except ImportError:
            logger.warning("pyusb未安装，回退到喇叭报警")
            self._alarm_speaker(duration)
        finally:
            self.is_alarming = False
            
    def _alarm_gpio(self, duration: float):
        """GPIO报警 (树莓派)"""
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
            logger.warning("RPi.GPIO未安装，回退到喇叭报警")
            self._alarm_speaker(duration)
        finally:
            self.is_alarming = False
            
    def _stop_device(self):
        """停止设备"""
        try:
            if self.alarm_type == 'gpio':
                import RPi.GPIO as GPIO
                pin = self.config.get('gpio_pin', 18)
                GPIO.output(pin, False)
        except:
            pass


class USBPTTalker(USBAlarmController):
    """USB语音播报器 (带TTS语音)"""
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.voice_enabled = config.get('voice_enabled', True)
        
    def speak(self, message: str):
        """语音播报消息"""
        if not self.enabled or not self.voice_enabled:
            return
            
        try:
            # 方式1: pyttsx3 (离线TTS)
            import pyttsx3
            engine = pyttsx3.init()
            engine.say(message)
            engine.runAndWait()
            
        except ImportError:
            try:
                # 方式2: 使用系统命令 (macOS)
                os.system(f'say "{message}"')
            except:
                try:
                    # 方式3: Windows SAPI
                    import win32com.client
                    speaker = win32com.client.Dispatch("SAPI.SpVoice")
                    speaker.Speak(message)
                except:
                    logger.warning("无可用TTS引擎")
                    
    def alarm_with_voice(self, camera_name: str, message: str):
        """语音+声音报警"""
        full_message = f"警告 {camera_name} {message}"
        
        if self.voice_enabled:
            self.speak(full_message)
            
        self.trigger(duration=5.0)


def create_alarm_controller(config: dict, alarm_config: dict):
    """创建报警控制器工厂"""
    alarm_type = alarm_config.get('type', 'speaker')
    
    if alarm_type == 'ptt':
        return USBPTTalker(alarm_config)
    else:
        return USBAlarmController(alarm_config)

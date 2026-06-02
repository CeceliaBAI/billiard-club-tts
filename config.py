"""
配置加载器 - 从 config.json 读取配置，缺失时使用默认值
"""
import json
import os
import sys


DEFAULT_CONFIG = {
    "title": "六六台球俱乐部播报系统",
    "always_on_top": False,
    "audio_dir": "audio",
    "volume": 1.0,
    "output_device": "",
    "window_width": 550,
    "window_height": 500,
    "buttons": [
        {"label": "欢迎光临", "file": "欢迎.mp3"},
        {"label": "禁止烟头", "file": "烟头.mp3"},
        {"label": "加时提醒", "file": "关灯.mp3"},
        {"label": "离店提醒", "file": "离店.mp3"},
    ],
}


def get_base_dir():
    """获取程序运行目录（支持 PyInstaller 打包后的路径）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config(config_path=None):
    """加载配置文件，失败时回退到默认配置。"""
    if config_path is None:
        config_path = os.path.join(get_base_dir(), "config.json")

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value
        # 解析 audio_dir 为绝对路径
        if not os.path.isabs(config.get("audio_dir", "")):
            config["audio_dir"] = os.path.join(get_base_dir(), config["audio_dir"])
        return config
    except FileNotFoundError:
        print(f"[配置] 未找到 {config_path}，使用默认配置")
        cfg = dict(DEFAULT_CONFIG)
        cfg["audio_dir"] = os.path.join(get_base_dir(), cfg["audio_dir"])
        return cfg
    except json.JSONDecodeError as e:
        print(f"[配置] JSON 解析错误: {e}，使用默认配置")
        cfg = dict(DEFAULT_CONFIG)
        cfg["audio_dir"] = os.path.join(get_base_dir(), cfg["audio_dir"])
        return cfg


def save_config(config, config_path=None):
    """保存配置到磁盘（保留未知键）。"""
    if config_path is None:
        config_path = os.path.join(get_base_dir(), "config.json")
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[配置] 保存失败: {e}")

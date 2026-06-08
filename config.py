"""
配置加载器 - 从 config.json 读取配置，缺失时使用默认值
支持 macOS 和 Windows
"""
import json
import os
import sys
import time


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


def get_config_dir():
    """获取配置文件可写目录。

    - Windows 打包后：%APPDATA%/BilliardClubTTS（Program Files 不可写）
    - macOS 打包后：~/Library/Application Support/BilliardClubTTS
      （.app bundle 内部只读，SIP 拒绝写入）
    - Linux 打包后：~/.config/BilliardClubTTS
    - 开发模式：项目根目录
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
            config_dir = os.path.join(appdata, "BilliardClubTTS")
        elif sys.platform == "darwin":
            config_dir = os.path.join(
                os.path.expanduser("~"),
                "Library", "Application Support", "BilliardClubTTS",
            )
        else:
            # Linux: 使用 XDG 约定
            config_dir = os.path.join(
                os.path.expanduser("~"), ".config", "BilliardClubTTS"
            )
    else:
        config_dir = os.path.dirname(os.path.abspath(__file__))
    return config_dir


def get_resource_dir():
    """获取打包资源目录（只读）。

    - 开发模式：项目根目录
    - 打包后：PyInstaller 临时解压目录 sys._MEIPASS
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def load_config(config_path=None):
    """加载配置文件，失败时回退到默认配置。

    config.json 查找策略（打包后）：
    1. 先在可写目录查找
    2. 若不存在，从资源目录 sys._MEIPASS 复制首份
    """
    if config_path is None:
        config_path = os.path.join(get_config_dir(), "config.json")

    # 打包后：如果可写目录没有 config.json，从资源目录复制
    if getattr(sys, "frozen", False) and not os.path.exists(config_path):
        bundled_config = os.path.join(get_resource_dir(), "config.json")
        if os.path.exists(bundled_config):
            try:
                import shutil
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                shutil.copy2(bundled_config, config_path)
                print(f"[配置] 已从资源目录复制配置到 {config_path}")
            except Exception as e:
                print(f"[配置] 复制默认配置失败: {e}，使用内存默认配置")
                cfg = dict(DEFAULT_CONFIG)
                cfg["audio_dir"] = os.path.join(get_resource_dir(), cfg["audio_dir"])
                return cfg

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value
        # 解析 audio_dir 为绝对路径（资源目录，打包后在 sys._MEIPASS 下）
        if not os.path.isabs(config.get("audio_dir", "")):
            resolved = os.path.join(get_resource_dir(), config["audio_dir"])
            config["audio_dir"] = os.path.normpath(resolved)
        return config
    except FileNotFoundError:
        print(f"[配置] 未找到 {config_path}，使用默认配置")
        cfg = dict(DEFAULT_CONFIG)
        cfg["audio_dir"] = os.path.normpath(
            os.path.join(get_resource_dir(), cfg["audio_dir"])
        )
        return cfg
    except json.JSONDecodeError as e:
        print(f"[配置] JSON 解析错误: {e}，使用默认配置")
        cfg = dict(DEFAULT_CONFIG)
        cfg["audio_dir"] = os.path.normpath(
            os.path.join(get_resource_dir(), cfg["audio_dir"])
        )
        return cfg


def save_config(config, config_path=None):
    """保存配置到可写目录。原子写入（临时文件 + 重命名），返回成功/失败。"""
    if config_path is None:
        config_path = os.path.join(get_config_dir(), "config.json")

    tmp_path = config_path + ".tmp"
    for attempt in range(3):
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, config_path)  # 原子替换
            return True
        except PermissionError:
            if attempt < 2:
                time.sleep(0.1)
            else:
                print(f"[配置] 保存失败（权限不足或文件被锁定）: {config_path}")
                return False
        except Exception as e:
            print(f"[配置] 保存失败: {e}")
            return False
    return False

"""
应用协调器 - PyWebView + Python 桥接层
支持 macOS 和 Windows
"""
import os
import sys
import json
import subprocess
import threading

import webview

from config import load_config, save_config
from audio_player import AudioPlayer


class Api:
    """暴露给前端 JS 的 Python API"""

    def __init__(self, app):
        self.app = app

    def play_audio(self, filename):
        """前端调用：播放音频文件"""
        self.app.player.play(filename)

    def stop_audio(self):
        """前端调用：停止当前播放"""
        self.app.player.stop_current()

    def set_volume(self, volume: float):
        """前端调用：设置音量 (0.0 ~ 1.0)"""
        self.app.player.set_volume(volume)
        # 持久化音量设置
        self.app.config["volume"] = volume
        save_config(self.app.config)

    def get_volume(self) -> float:
        """前端调用：获取当前音量"""
        return self.app.player.get_volume()

    def get_devices(self):
        """前端调用：获取音频输出设备列表，返回 JSON 字符串"""
        devices = self.app.get_audio_devices()
        return json.dumps(devices, ensure_ascii=False)

    def set_device(self, device_id: str):
        """前端调用：切换音频输出设备

        macOS: device_id 为设备 UID
        Windows: device_id 为设备名称
        """
        success = self.app.set_audio_device(device_id)
        if success:
            self.app.config["output_device"] = device_id
            save_config(self.app.config)
        return success

    def get_config(self):
        """前端调用：获取完整配置"""
        return json.dumps({
            "title": self.app.config.get("title", ""),
            "buttons": self.app.config.get("buttons", []),
            "volume": self.app.player.get_volume(),
            "always_on_top": self.app.config.get("always_on_top", False),
        })


class App:
    def __init__(self, config_path=None):
        self.config = load_config(config_path)

        # 初始化音频播放引擎（主线程初始化 pygame.mixer）
        audio_dir = self.config.get("audio_dir", "audio")
        self.player = AudioPlayer(audio_dir=audio_dir)

        # 恢复保存的音量
        saved_volume = self.config.get("volume", 1.0)
        self.player.set_volume(saved_volume)

        # 创建 API 桥接
        self.api = Api(self)

        # 托盘图标引用
        self._tray_icon = None

    def _get_html_path(self):
        """获取 index.html 的绝对路径"""
        if getattr(sys, "frozen", False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "web", "index.html")

    def _on_loaded(self):
        """页面加载完成后的回调：注入配置，启动托盘"""
        # 注入按钮配置
        config_json = json.dumps({
            "title": self.config.get("title", ""),
            "buttons": self.config.get("buttons", []),
        })
        self.window.evaluate_js(
            f"initButtons({config_json}.buttons, {config_json}.title)"
        )

        # 注入音量
        self.window.evaluate_js(
            f"setVolume({self.player.get_volume()})"
        )

        # 设置状态回调
        self.player.set_status_callback(self._update_status)
        self._update_status("就绪")

        # 注入设备列表
        try:
            devices = self.get_audio_devices()
            if devices:
                devices_json = json.dumps(devices, ensure_ascii=False)
                escaped_devices = devices_json.replace("\\", "\\\\").replace("'", "\\'")
                self.window.evaluate_js(f"populateDevices('{escaped_devices}')")
        except Exception as e:
            print(f"[设备] 注入设备列表失败: {e}")

        # 在 webview 创建 NSApplication 之后再启动托盘
        self._start_tray()

    def _update_status(self, text):
        """更新前端状态栏（线程安全）"""
        if hasattr(self, "window") and self.window:
            escaped = text.replace("\\", "\\\\").replace("'", "\\'")
            self.window.evaluate_js(f"updateStatus('{escaped}')")
            # 根据状态通知 JS 显示/隐藏停止按钮
            if "正在播放" in text:
                self.window.evaluate_js("setPlaybackState(true)")
            elif text in ("就绪", "已停止"):
                self.window.evaluate_js("setPlaybackState(false)")

    # ---- 音频设备枚举 ----

    def get_audio_devices(self):
        """获取音频输出设备列表（跨平台）。

        返回 list[dict]: [{"name": "...", "id": "...", "is_default": bool}]
        """
        devices = []
        try:
            import sounddevice as sd
            all_devices = sd.query_devices()
            default_output = sd.default.device[1]  # 默认输出设备索引

            for idx, dev in enumerate(all_devices):
                if dev.get("max_output_channels", 0) > 0:
                    devices.append({
                        "name": dev.get("name", f"设备 {idx}"),
                        "id": dev.get("name", f"设备 {idx}"),  # 使用设备名称（SwitchAudioSource 需要名称）
                        "is_default": idx == default_output,
                    })
        except Exception as e:
            print(f"[设备] 枚举音频设备失败: {e}")
        return devices

    # ---- 音频设备切换 ----

    def set_audio_device(self, device_id: str) -> bool:
        """切换系统默认音频输出设备（平台特定实现）。

        返回 True 表示切换成功。
        """
        if sys.platform == "darwin":
            return self._set_device_macos(device_id)
        elif sys.platform == "win32":
            return self._set_device_windows(device_id)
        else:
            self._update_status("当前系统不支持切换音频设备")
            return False

    def _find_switch_audio_source(self):
        """查找 SwitchAudioSource 二进制文件路径。"""
        # 尝试已知的 Homebrew 安装路径
        candidates = [
            "/opt/homebrew/bin/SwitchAudioSource",
            "/usr/local/bin/SwitchAudioSource",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        # 尝试在 PATH 中查找
        result = subprocess.run(
            ["which", "SwitchAudioSource"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    def _set_device_macos(self, device_uid: str) -> bool:
        """macOS: 使用 SwitchAudioSource 切换设备。"""
        try:
            sas_path = self._find_switch_audio_source()
            if not sas_path:
                self._update_status("切换失败：请安装 brew install switchaudio-osx")
                return False

            subprocess.run(
                [sas_path, "-s", device_uid],
                capture_output=True, timeout=5,
            )
            return True
        except Exception as e:
            print(f"[设备] macOS 切换设备失败: {e}")
            self._update_status(f"切换设备失败: {e}")
            return False

    def _set_device_windows(self, device_name: str) -> bool:
        """Windows: 切换默认音频输出设备。"""
        try:
            # 方案 1：使用 pycaw（如果已安装）
            try:
                from pycaw.pycaw import AudioUtilities
                # pycaw v20251023+ 支持切换默认设备
                devices = AudioUtilities.GetAllDevices()
                for dev in devices:
                    if device_name in dev.FriendlyName or device_name in str(dev.id):
                        AudioUtilities.SetDefaultAudioPlaybackDevice(dev)
                        return True
                self._update_status(f"未找到设备: {device_name}")
                return False
            except ImportError:
                pass
            except Exception as e:
                print(f"[设备] pycaw 切换失败: {e}")

            # 方案 2：使用 PowerShell（Windows 10+）
            ps_script = f'''
            Add-Type @"
            using System;
            using System.Runtime.InteropServices;
            public class AudioDevice {{
                [DllImport("winmm.dll", SetLastError=true)]
                public static extern int waveOutMessage(IntPtr uDeviceID, uint uMsg, ref int dw1, ref int dw2);
            }}
"@
            '''
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, timeout=5,
            )
            self._update_status("Windows 设备切换请使用系统声音设置")
            return False
        except Exception as e:
            print(f"[设备] Windows 切换设备失败: {e}")
            self._update_status(f"切换设备失败: {e}")
            return False

    # ---- 窗口与托盘 ----

    def _on_closing(self):
        """窗口关闭事件：有托盘时隐藏到托盘，否则退出。"""
        # 保存窗口尺寸
        try:
            if hasattr(self, "window") and self.window:
                self.config["window_width"] = self.window.width
                self.config["window_height"] = self.window.height
                save_config(self.config)
        except Exception:
            pass

        if self._tray_icon:
            # 有托盘：隐藏窗口而非退出
            try:
                self.window.hide()
            except Exception:
                pass
            return False  # 阻止默认关闭行为
        return True  # 无托盘：允许关闭

    def show_window(self):
        """显示并聚焦窗口（托盘菜单回调）。"""
        if hasattr(self, "window") and self.window:
            try:
                self.window.show()
            except Exception:
                pass

    def hide_window(self):
        """隐藏窗口到托盘（托盘菜单回调）。"""
        if hasattr(self, "window") and self.window:
            try:
                self.window.hide()
            except Exception:
                pass

    def quit_app(self):
        """完全退出应用：停止音频、销毁窗口、移除托盘。"""
        self.player.stop()
        # 移除托盘图标
        if self._tray_icon:
            try:
                if sys.platform == "darwin":
                    # NSStatusBar 图标：从状态栏移除
                    NSStatusBar.systemStatusBar().removeStatusItem_(self._tray_icon)
                else:
                    self._tray_icon.stop()
            except Exception:
                pass
        if hasattr(self, "window") and self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
        os._exit(0)

    # ---- 系统托盘 ----

    def _start_tray(self):
        """启动系统托盘图标。

        macOS: 使用 pyobjc 在主线程直接创建 NSStatusBar 图标。
        Windows: 使用 pystray 在 daemon 线程中运行。
        """
        if sys.platform == "darwin":
            self._start_tray_macos()
        else:
            self._start_tray_other()

    def _start_tray_macos(self):
        """macOS: 使用 GCD 将 NSStatusBar 创建调度到主线程。

        _on_loaded 运行在 WebKit 回调线程，而 Cocoa UI 必须在主线程操作。
        使用 dispatch_async 将实际创建逻辑派发到主队列。
        """
        app_ref = self

        def _setup():
            try:
                from AppKit import NSStatusBar, NSVariableStatusItemLength, NSMenu, NSMenuItem
                from Foundation import NSObject
                import objc

                class _TrayTarget(NSObject):
                    @objc.selector
                    def showWindow_(self, sender):
                        app_ref.show_window()

                    @objc.selector
                    def hideWindow_(self, sender):
                        app_ref.hide_window()

                    @objc.selector
                    def quitApp_(self, sender):
                        app_ref.quit_app()

                app_ref._tray_target = _TrayTarget.alloc().init()

                bar = NSStatusBar.systemStatusBar()
                status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)

                button = status_item.button()
                if button:
                    button.setTitle_("🎱")

                menu = NSMenu.alloc().init()
                menu.setAutoenablesItems_(False)

                item_show = menu.addItemWithTitle_action_keyEquivalent_(
                    "显示窗口", "showWindow:", ""
                )
                item_show.setTarget_(app_ref._tray_target)

                item_hide = menu.addItemWithTitle_action_keyEquivalent_(
                    "隐藏窗口", "hideWindow:", ""
                )
                item_hide.setTarget_(app_ref._tray_target)

                menu.addItem_(NSMenuItem.separatorItem())

                item_quit = menu.addItemWithTitle_action_keyEquivalent_(
                    "退出", "quitApp:", ""
                )
                item_quit.setTarget_(app_ref._tray_target)

                status_item.setMenu_(menu)
                app_ref._tray_icon = status_item

            except Exception as e:
                print(f"[托盘] macOS 初始化失败: {e}")
                app_ref._tray_icon = None

        try:
            from libdispatch import dispatch_get_main_queue, dispatch_async
            dispatch_async(dispatch_get_main_queue(), _setup)
        except ImportError:
            # 回退：使用 performSelectorOnMainThread
            try:
                from Foundation import NSObject
                helper = NSObject.alloc().init()
                # 将 _setup 包装为可调用的 selector
                import objc
                objc.registerMetaDataForSelector(
                    b"NSObject", b"performSelectorOnMainThread:withObject:waitUntilDone:",
                    {"arguments": {2: {"type": b"^v"}}}
                )
                helper.performSelectorOnMainThread_withObject_waitUntilDone_(
                    objc.selector(_setup, signature=b"v@:"), None, False
                )
            except Exception as e:
                print(f"[托盘] 无法调度到主线程: {e}")
                self._tray_icon = None

    def _start_tray_other(self):
        """Windows/Linux: 使用 pystray 在 daemon 线程中运行。"""
        try:
            import pystray
            from PIL import Image, ImageDraw

            base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base_dir, "assets", "logo.png")

            if os.path.exists(icon_path):
                img = Image.open(icon_path)
                img = img.resize((64, 64), Image.LANCZOS)
            else:
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse((8, 8, 56, 56), fill="#667eea")

            app_title = self.config.get("title", "播报系统")

            menu = pystray.Menu(
                pystray.MenuItem(
                    "显示窗口",
                    lambda: self.show_window(),
                    default=True,
                ),
                pystray.MenuItem(
                    "隐藏窗口",
                    lambda: self.hide_window(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出",
                    lambda: self.quit_app(),
                ),
            )

            self._tray_icon = pystray.Icon(
                "billiard_club",
                img,
                app_title,
                menu,
            )

            tray_thread = threading.Thread(
                target=self._tray_icon.run,
                daemon=True,
            )
            tray_thread.start()

        except ImportError as e:
            print(f"[托盘] pystray 未安装: {e}")
            self._tray_icon = None
        except Exception as e:
            print(f"[托盘] 初始化失败: {e}")
            self._tray_icon = None

    # ---- 启动 ----

    def run(self):
        """启动应用"""
        html_path = self._get_html_path()

        if not os.path.exists(html_path):
            print(f"错误: 找不到 {html_path}")
            sys.exit(1)

        title = self.config.get("title", "播报系统")

        # 恢复窗口尺寸
        win_width = self.config.get("window_width", 550)
        win_height = self.config.get("window_height", 500)

        # 创建窗口
        self.window = webview.create_window(
            title,
            url=f"file://{html_path}",
            js_api=self.api,
            width=win_width,
            height=win_height,
            resizable=True,
            min_size=(400, 400),
            text_select=False,
        )

        # 页面加载完成后注入配置
        self.window.events.loaded += self._on_loaded
        self.window.events.closing += self._on_closing

        # 应用置顶设置
        if self.config.get("always_on_top", False):
            try:
                self.window.on_top = True
            except Exception:
                pass

        webview.start(debug=False)

        # 退出时清理
        self.player.stop()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass

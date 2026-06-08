"""
应用协调器 - PyWebView + Python 桥接层
支持 macOS 和 Windows
"""
import os
import sys
import json
import subprocess
import threading
import time
from pathlib import Path

import webview

from config import load_config, save_config, get_config_dir
from audio_player import AudioPlayer


# ---- 单实例锁 ----

def _acquire_single_instance():
    """跨平台单实例锁（pid 文件）。返回 True 表示获得锁。"""
    try:
        lock_path = os.path.join(get_config_dir(), ".instance.pid")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        if os.path.exists(lock_path):
            try:
                with open(lock_path, "r") as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)  # 检查进程是否存在
                return False  # 已有实例在运行
            except (OSError, ValueError):
                pass  # 僵尸 pid 或无效内容
        with open(lock_path, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True  # 失败时允许启动


def _release_single_instance():
    try:
        lock_path = os.path.join(get_config_dir(), ".instance.pid")
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


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
        self.app.config["volume"] = volume
        save_config(self.app.config)

    def get_volume(self) -> float:
        """前端调用：获取当前音量"""
        return self.app.player.get_volume()

    def get_devices(self):
        """前端调用：获取音频输出设备列表，返回 Python 列表（pywebview 自动序列化）"""
        return self.app.get_audio_devices()

    def set_device(self, device_id: str):
        """前端调用：切换音频输出设备"""
        success = self.app.set_audio_device(device_id)
        if success:
            self.app.config["output_device"] = device_id
            save_config(self.app.config)
        return success

    def get_config(self):
        """前端调用：获取完整配置，返回 Python dict（pywebview 自动序列化）"""
        return {
            "title": self.app.config.get("title", ""),
            "buttons": self.app.config.get("buttons", []),
            "volume": self.app.player.get_volume(),
            "always_on_top": self.app.config.get("always_on_top", False),
        }

    def _poll_tray(self):
        """JS 定期轮询（运行在 UI 线程），安全操作窗口。

        统一处理三类待办：
        1. 托盘菜单操作（show/hide/quit）
        2. 音频状态更新（播放中/就绪/错误）
        3. 音频设备列表注入（启动时一次性）
        """
        self.app._process_tray_action()
        self.app._process_pending_status()
        self.app._process_pending_devices()


class App:
    def __init__(self, config_path=None):
        self.config = load_config(config_path)

        audio_dir = self.config.get("audio_dir", "audio")
        self.player = AudioPlayer(audio_dir=audio_dir)

        saved_volume = self.config.get("volume", 1.0)
        self.player.set_volume(saved_volume)

        self.api = Api(self)

        self._tray_icon = None
        self._tray_action = None
        self._tray_action_lock = threading.Lock()

        # 跨线程安全的状态/设备注入缓冲区
        # 工作线程写入 → JS 轮询在 UI 线程取走并调用 evaluate_js
        self._status_message = None
        self._status_lock = threading.Lock()
        self._devices_data = None
        self._devices_lock = threading.Lock()

    def _get_html_path(self):
        """获取 index.html 的绝对路径。打包后从 sys._MEIPASS 读取。"""
        if getattr(sys, "frozen", False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "web", "index.html")

    def _on_loaded(self):
        """页面加载完成后的回调：注入配置，启动托盘。

        每个初始化步骤独立 try/except，单步失败不影响其余。
        """
        # Step 1: 注入按钮配置（关键——失败则应用无功能）
        try:
            buttons = self.config.get("buttons", [])
            self.window.evaluate_js(
                f"initButtons({json.dumps(buttons)}, {json.dumps(self.config.get('title', ''))})"
            )
        except Exception as e:
            print(f"[启动] 注入按钮失败: {e}")

        # Step 2: 注入音量
        try:
            self.window.evaluate_js(
                f"setVolume({self.player.get_volume()})"
            )
        except Exception as e:
            print(f"[启动] 注入音量失败: {e}")

        # Step 3: 设置状态回调 + 初始状态
        self.player.set_status_callback(self._update_status)
        try:
            self._update_status("就绪")
        except Exception:
            pass

        # Step 4: 启动托盘（在设备枚举之前，用户可立即使用托盘）
        self._start_tray()

        # Step 5: 异步枚举音频设备（不阻塞 UI）
        # 注：托盘轮询由 web/index.html 中的 setInterval 负责，
        # 无需 Python 端额外注入。
        self._inject_devices_async()

    def _update_status(self, text):
        """记录状态文本，由 JS 轮询在 UI 线程安全更新前端。

        可被音频工作线程调用——不在此处调用 evaluate_js，
        而是将消息存入锁保护缓冲区，等待 JS 侧 _poll_tray()
        在 UI 线程取走并执行 DOM 更新。
        """
        if not hasattr(self, "window") or not self.window:
            return
        with self._status_lock:
            self._status_message = text

    # ---- 跨线程安全：JS 轮询在 UI 线程执行 DOM 更新 ----

    def _process_pending_status(self):
        """由 JS 轮询调用（运行在 UI 线程），获取最新状态文本并更新前端。"""
        with self._status_lock:
            text = self._status_message
            self._status_message = None
        if text is None:
            return
        try:
            safe_text = json.dumps(text, ensure_ascii=False)
            self.window.evaluate_js(f"updateStatus({safe_text})")
            if "正在播放" in text:
                self.window.evaluate_js("setPlaybackState(true)")
            elif text in ("就绪", "已停止"):
                self.window.evaluate_js("setPlaybackState(false)")
        except Exception as e:
            print(f"[状态] 注入失败: {e}")

    def _process_pending_devices(self):
        """由 JS 轮询调用（运行在 UI 线程），注入设备列表并恢复已保存设备。

        仅在注入成功后清空缓冲区，失败则在下个轮询重试。
        """
        with self._devices_lock:
            data = self._devices_data
        if data is None:
            return
        devices_json, saved_device = data
        try:
            self.window.evaluate_js(
                f"populateDevices({devices_json})"
            )
            if saved_device:
                self.window.evaluate_js(
                    f"selectDevice({json.dumps(saved_device, ensure_ascii=False)})"
                )
            # 注入成功，清空缓冲区
            with self._devices_lock:
                self._devices_data = None
        except Exception as e:
            print(f"[设备] JS 注入失败，下个轮询重试: {e}")

    # ---- 音频设备枚举（异步 + COM 初始化） ----

    def _inject_devices_async(self):
        """在后台线程枚举音频设备，结果存入锁缓冲区。

        由 JS 轮询 _poll_tray → _process_pending_devices 在 UI 线程
        安全注入前端（Windows 上 evaluate_js 必须由 UI 线程调用）。
        Windows 上需要 CoInitializeEx 才能正常使用 PortAudio/WASAPI。
        """

        def _query_and_inject():
            devices = []
            try:
                # Windows COM 初始化
                if sys.platform == "win32":
                    try:
                        import pythoncom
                        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
                    except ImportError:
                        pass

                try:
                    devices = self.get_audio_devices()
                except Exception as e:
                    print(f"[设备] 枚举失败: {e}")

            finally:
                # 存入锁保护缓冲区，由 JS 轮询在 UI 线程注入前端
                if devices and hasattr(self, "window") and self.window:
                    with self._devices_lock:
                        self._devices_data = (
                            json.dumps(devices, ensure_ascii=False),
                            self.config.get("output_device", ""),
                        )

                if sys.platform == "win32":
                    try:
                        import pythoncom
                        pythoncom.CoUninitialize()
                    except (ImportError, Exception):
                        pass

        t = threading.Thread(target=_query_and_inject, daemon=True)
        t.start()

    # ---- 音频设备枚举 ----

    def get_audio_devices(self):
        """获取音频输出设备列表（跨平台）。

        返回 list[dict]: [{"name": "...", "id": "...", "is_default": bool}]
        """
        devices = []
        try:
            import sounddevice as sd
            all_devices = sd.query_devices()
            default_output = sd.default.device[1] if sd.default.device else None

            for idx, dev in enumerate(all_devices):
                if dev.get("max_output_channels", 0) > 0:
                    dev_name = dev.get("name", f"设备 {idx}")
                    devices.append({
                        "name": dev_name,
                        "id": dev_name,
                        "is_default": (idx == default_output),
                    })
        except Exception as e:
            print(f"[设备] 枚举音频设备失败: {e}")
        return devices

    # ---- 音频设备切换 ----

    def set_audio_device(self, device_id: str) -> bool:
        """切换系统默认音频输出设备（平台特定实现）。"""
        if sys.platform == "darwin":
            return self._set_device_macos(device_id)
        elif sys.platform == "win32":
            return self._set_device_windows(device_id)
        else:
            self._update_status("当前系统不支持切换音频设备")
            return False

    def _find_switch_audio_source(self):
        """查找 SwitchAudioSource 二进制文件路径。"""
        candidates = [
            "/opt/homebrew/bin/SwitchAudioSource",
            "/usr/local/bin/SwitchAudioSource",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        result = subprocess.run(
            ["which", "SwitchAudioSource"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    def _set_device_macos(self, device_name: str) -> bool:
        """macOS: 使用 SwitchAudioSource 切换设备。"""
        try:
            sas_path = self._find_switch_audio_source()
            if not sas_path:
                self._update_status("切换失败：请安装 brew install switchaudio-osx")
                return False
            subprocess.run(
                [sas_path, "-s", device_name],
                capture_output=True, timeout=5,
            )
            return True
        except Exception as e:
            print(f"[设备] macOS 切换设备失败: {e}")
            self._update_status(f"切换设备失败: {e}")
            return False

    def _set_device_windows(self, device_name: str) -> bool:
        """Windows: 切换默认音频输出设备。先尝试 pycaw，失败则提示用户手动切换。"""
        try:
            from pycaw.pycaw import AudioUtilities
            devices = AudioUtilities.GetAllDevices()
            for dev in devices:
                # 精确匹配设备名称
                if device_name == dev.FriendlyName or device_name == str(dev.id):
                    AudioUtilities.SetDefaultAudioPlaybackDevice(dev)
                    return True
            self._update_status(f"未找到设备: {device_name}")
            return False
        except ImportError:
            self._update_status("请安装 pycaw 以支持切换音频设备: pip install pycaw")
            return False
        except Exception as e:
            print(f"[设备] Windows 切换设备失败: {e}")
            self._update_status(f"切换设备失败: {e}")
            return False

    # ---- 窗口与托盘 ----

    def _on_closing(self):
        """窗口关闭事件：有托盘时隐藏到托盘，否则退出。"""
        try:
            if hasattr(self, "window") and self.window:
                self.config["window_width"] = self.window.width
                self.config["window_height"] = self.window.height
                save_config(self.config)
        except Exception:
            pass

        if self._tray_icon:
            try:
                self.window.hide()
            except Exception:
                pass
            return False  # 阻止默认关闭行为
        return True  # 无托盘：允许关闭

    # ---- 托盘操作（线程安全） ----

    def _request_tray_action(self, action: str):
        """由托盘菜单回调（在 pystray daemon 线程）调用，设置待处理操作。
        实际窗口操作由 JS 轮询 _poll_tray → _process_tray_action 在 UI 线程执行。
        """
        with self._tray_action_lock:
            self._tray_action = action

    def _process_tray_action(self):
        """由 JS 轮询 _poll_tray() 调用（运行在 UI 线程），安全操作窗口。"""
        with self._tray_action_lock:
            action = self._tray_action
            self._tray_action = None

        if action == "show":
            self._do_show_window()
        elif action == "hide":
            self._do_hide_window()
        elif action == "quit":
            self._do_quit_app()

    def _do_show_window(self):
        if hasattr(self, "window") and self.window:
            try:
                self.window.show()
            except Exception:
                pass

    def _do_hide_window(self):
        if hasattr(self, "window") and self.window:
            try:
                self.window.hide()
            except Exception:
                pass

    def _do_quit_app(self):
        """完全退出应用：停止音频、销毁窗口、清理托盘。"""
        self.player.stop()
        if self._tray_icon:
            try:
                if sys.platform == "darwin":
                    from AppKit import NSStatusBar
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
        _release_single_instance()
        os._exit(0)

    # ---- 系统托盘 ----

    def _start_tray(self):
        """启动系统托盘图标。"""
        if sys.platform == "darwin":
            self._start_tray_macos()
        else:
            self._start_tray_other()

    def _start_tray_macos(self):
        """macOS: 使用 GCD 将 NSStatusBar 创建调度到主线程。"""
        app_ref = self

        def _setup():
            try:
                from AppKit import NSStatusBar, NSVariableStatusItemLength, NSMenu, NSMenuItem
                from Foundation import NSObject
                import objc

                class _TrayTarget(NSObject):
                    @objc.selector
                    def showWindow_(self, sender):
                        app_ref._do_show_window()

                    @objc.selector
                    def hideWindow_(self, sender):
                        app_ref._do_hide_window()

                    @objc.selector
                    def quitApp_(self, sender):
                        app_ref._do_quit_app()

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
            try:
                from Foundation import NSObject
                helper = NSObject.alloc().init()
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
        """Windows/Linux: 使用 pystray 在 daemon 线程中运行。

        托盘菜单回调通过 _request_tray_action 设置标志，
        JS 轮询 _poll_tray 在 UI 线程安全执行窗口操作。
        """
        try:
            import pystray
            from PIL import Image, ImageDraw

            if getattr(sys, "frozen", False):
                base_dir = sys._MEIPASS
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base_dir, "assets", "logo.png")

            if os.path.exists(icon_path):
                img = Image.open(icon_path)
                img = img.resize((32, 32), Image.LANCZOS)
            else:
                # Windows 托盘图标 32x32 即可（较小的回退图标更清晰）
                img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse((4, 4, 28, 28), fill="#667eea")

            app_title = self.config.get("title", "播报系统")

            menu = pystray.Menu(
                pystray.MenuItem(
                    "显示窗口",
                    lambda: self._request_tray_action("show"),
                    default=True,
                ),
                pystray.MenuItem(
                    "隐藏窗口",
                    lambda: self._request_tray_action("hide"),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出",
                    lambda: self._request_tray_action("quit"),
                ),
            )

            self._tray_icon = pystray.Icon(
                "billiard_club",
                img,
                app_title,
                menu,
            )

            def _run_tray_safe():
                try:
                    self._tray_icon.run()
                except Exception as e:
                    print(f"[托盘] pystray 运行异常: {e}")
                    self._tray_icon = None  # 标记失败，允许窗口正常关闭

            tray_thread = threading.Thread(
                target=_run_tray_safe,
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
        # 单实例检查
        if not _acquire_single_instance():
            print("应用已在运行中")
            sys.exit(0)

        html_path = self._get_html_path()

        if not os.path.exists(html_path):
            print(f"错误: 找不到 {html_path}")
            sys.exit(1)

        title = self.config.get("title", "播报系统")

        # 恢复窗口尺寸
        win_width = self.config.get("window_width", 550)
        win_height = self.config.get("window_height", 500)

        # 使用 Path.as_uri() 生成正确的 file:/// URL（Windows 兼容）
        html_url = Path(html_path).as_uri()

        self.window = webview.create_window(
            title,
            url=html_url,
            js_api=self.api,
            width=win_width,
            height=win_height,
            resizable=True,
            min_size=(400, 400),
            text_select=False,
        )

        self.window.events.loaded += self._on_loaded
        self.window.events.closing += self._on_closing

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
        _release_single_instance()

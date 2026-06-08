"""
台球俱乐部播报系统 - 入口文件 (PyWebView 版本)
支持 macOS 和 Windows
"""
import sys
import os

# 必须在导入 pygame 前设置：禁止 SDL2 使用视频子系统，
# 否则会与 pywebview 的 WebView 冲突导致崩溃。
os.environ["SDL_VIDEODRIVER"] = "dummy"
# 显式指定音频驱动，避免 SDL 自动检测到不稳定的驱动
if sys.platform == "darwin":
    os.environ["SDL_AUDIODRIVER"] = "coreaudio"
elif sys.platform == "win32":
    os.environ["SDL_AUDIODRIVER"] = "directsound"


def main():
    config_path = None
    if len(sys.argv) > 1 and sys.argv[1] == "--config":
        if len(sys.argv) > 2:
            config_path = sys.argv[2]
        else:
            print("用法: python main.py [--config <config.json路径>]")
            sys.exit(1)

    from app import App
    app = App(config_path=config_path)
    app.run()


if __name__ == "__main__":
    main()

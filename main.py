"""
台球俱乐部播报系统 - 入口文件 (PyWebView 版本)
支持 macOS 和 Windows
"""
import sys
import os

# 必须在导入 pygame 前设置：禁止 SDL2 使用 Cocoa 视频子系统，
# 否则会与 pywebview 的 WKWebView 冲突导致 SIGTRAP 崩溃。
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "coreaudio"


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

"""
音频播放引擎 - 基于 pygame.mixer 的跨平台线程安全音频队列
支持 macOS 和 Windows
"""
import os
import queue
import threading
import time

import pygame


class AudioPlayer:
    """基于 pygame.mixer 的线程安全音频播放引擎。

    支持 MP3 等常见格式，提供音量控制和停止功能。
    """

    MAX_QUEUE_SIZE = 3  # 最大队列长，防止连按堆积

    def __init__(self, audio_dir="audio"):
        self._queue = queue.Queue()
        self._audio_dir = audio_dir
        self._on_status_change = None
        self._volume = 1.0
        self._stop_requested = False
        self._initialized = False

        # 延迟初始化 pygame.mixer，让窗口先显示
        # init() 在 _worker 线程首次消费队列项时调用

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _ensure_init(self):
        """延迟初始化 pygame mixer（避免阻塞窗口显示）。"""
        if self._initialized:
            return True
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            self._initialized = True
            return True
        except Exception as e:
            print(f"[AudioPlayer] pygame.mixer 初始化失败: {e}")
            return False

    def set_status_callback(self, callback):
        """设置状态变化回调，用于更新 GUI 状态栏。"""
        self._on_status_change = callback

    def _notify_status(self, status):
        if self._on_status_change:
            self._on_status_change(status)

    def set_volume(self, volume: float):
        """设置播放音量。volume: 0.0（静音）到 1.0（最大）。"""
        self._volume = max(0.0, min(1.0, volume))
        if self._ensure_init():
            pygame.mixer.music.set_volume(self._volume)

    def get_volume(self) -> float:
        """获取当前音量。"""
        return self._volume

    def stop_current(self):
        """停止当前播放并清空队列，工作线程保持运行。"""
        self._stop_requested = True
        if self._ensure_init():
            pygame.mixer.music.stop()
        # 清空队列中等待的任务
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def is_playing(self) -> bool:
        """是否正在播放。"""
        if self._ensure_init():
            return pygame.mixer.music.get_busy()
        return False

    def _worker(self):
        """工作线程：处理播放队列。延迟初始化 pygame mixer。"""
        # 等待第一个播放任务到达时才初始化 mixer
        self._notify_status("就绪")

        while True:
            item = self._queue.get()
            if item is None:  # 关闭哨兵
                break

            # 延迟初始化：第一个播放任务到达时才初始化 pygame.mixer
            if not self._ensure_init():
                self._notify_status("音频初始化失败")
                self._queue.task_done()
                continue

            filepath = os.path.join(self._audio_dir, item)
            if not os.path.exists(filepath):
                self._notify_status(f"文件不存在: {item}")
                self._queue.task_done()
                continue

            self._stop_requested = False

            try:
                label = os.path.splitext(item)[0]
                self._notify_status(f"正在播放: {label}")

                pygame.mixer.music.load(filepath)
                pygame.mixer.music.set_volume(self._volume)
                pygame.mixer.music.play()

                # 阻塞直到播放完毕或收到停止请求
                while pygame.mixer.music.get_busy():
                    if self._stop_requested:
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.1)

            except Exception as e:
                self._notify_status(f"播放出错: {e}")
            finally:
                if self._stop_requested:
                    self._notify_status("已停止")
                else:
                    self._notify_status("就绪")
                self._queue.task_done()

    def play(self, filename: str):
        """非阻塞提交播放请求。队列满时丢弃最旧的。"""
        if self._queue.qsize() >= self.MAX_QUEUE_SIZE:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
        self._queue.put(filename)

    def stop(self):
        """停止当前播放并关闭工作线程。"""
        self._stop_requested = True
        if self._ensure_init():
            pygame.mixer.music.stop()
        # 清空队列中的等待任务
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        # 发送关闭哨兵
        self._queue.put(None)
        if self._ensure_init():
            pygame.mixer.quit()

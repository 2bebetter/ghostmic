#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GhostMic —— 让文字"说"进麦克风

macOS 上的本地小工具，用系统自带的 say 把文字合成语音，
再播放到虚拟音频设备（BlackHole），让浏览器 / AI 工具
把这段语音当成真实麦克风输入。

功能：
  1. 输入文字 -> say 合成 -> afconvert 转 16k 单声道 wav
  2. 播放音频到指定输出设备（默认自动选虚拟音频设备）
  3. 列出所有已生成的音频，支持试听 / 删除 / 一键退出

依赖：macOS 自带 say、afconvert；Python 的 sounddevice、numpy。
不依赖任何外部服务。
"""

import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import sounddevice as sd
import numpy as np
import wave

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

HOST = "127.0.0.1"
PORT = 8787

# 默认输出到虚拟音频设备，让浏览器 / AI 工具把它当麦克风输入
# 常见虚拟声卡名字关键字，用于自动识别
VIRTUAL_HINTS = ("blackhole", "virtual", "soundflower", "loopback", "existential")

# 路径安全：只允许目录内的普通文件名，禁止路径分隔与上跳
def safe_path(name):
    if not name or not name.lower().endswith(".wav"):
        raise ValueError("非法文件名")
    if name.startswith(".") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("非法文件名")
    if ".." in name:
        raise ValueError("非法文件名")
    path = os.path.join(AUDIO_DIR, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(AUDIO_DIR):
        raise ValueError("非法文件名")
    return path


# ---------------------------------------------------------------- 音频设备

def list_output_devices():
    """返回所有支持播放的设备。"""
    devices = []
    for idx, d in enumerate(sd.query_devices()):
        if d.get("max_output_channels", 0) > 0:
            lname = d["name"].lower()
            devices.append({
                "index": idx,
                "name": d["name"],
                "channels": d["max_output_channels"],
                "is_virtual": any(h in lname for h in VIRTUAL_HINTS),
            })
    return devices


def pick_default_device():
    """优先选虚拟音频设备（BlackHole 等），否则用系统默认输出。"""
    for d in list_output_devices():
        if d["is_virtual"]:
            return d["index"]
    try:
        return sd.default.device[1]
    except Exception:
        return None


# ---------------------------------------------------------------- 播放

_play_lock = threading.Lock()


def play_wav(path, device_index):
    """把 wav 播放到指定设备。阻塞直到播完。"""
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    data = np.frombuffer(frames, dtype=np.int16)

    if channels > 1:
        data = data.reshape(-1, channels)

    # 目标设备声道数，做映射
    info = sd.query_devices(device_index)
    out_ch = info["max_output_channels"]
    if out_ch == 1 and channels > 1:
        data = data.mean(axis=1).astype(np.int16)
    elif out_ch >= 2 and channels == 1:
        data = np.repeat(data[:, None], out_ch, axis=1)

    with _play_lock:
        sd.play(data, samplerate=rate, device=device_index, blocking=True)


# ---------------------------------------------------------------- 合成

def sanitize_stem(text):
    """从文本生成安全的文件名主干。"""
    stem = text.strip().replace("\n", " ")
    stem = re.sub(r"[\s/\\:*?\"<>|]+", "_", stem)
    stem = stem[:40].strip("._")
    if not stem:
        stem = "voice"
    return stem


def synthesize(text, voice, rate):
    """调用 say + afconvert，生成 16k 单声道 wav，返回文件名。"""
    stem = sanitize_stem(text)
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{stem}.wav"
    final_path = os.path.join(AUDIO_DIR, name)
    tmp_aiff = os.path.join(AUDIO_DIR, f".{ts}_{stem}.aiff")

    # 1) say 合成 aiff（格式转换交给 afconvert）
    say_cmd = ["say", "-v", voice, "-o", tmp_aiff]
    if rate:
        say_cmd += ["-r", str(rate)]
    say_cmd.append(text)
    subprocess.run(say_cmd, check=True)

    # 2) afconvert 转 16k 单声道 wav
    try:
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             tmp_aiff, final_path],
            check=True,
        )
    finally:
        if os.path.exists(tmp_aiff):
            os.remove(tmp_aiff)

    return name


def list_audio():
    """列出音频目录里的文件，最新在前。"""
    if not os.path.isdir(AUDIO_DIR):
        return []
    items = []
    for fn in os.listdir(AUDIO_DIR):
        if not fn.lower().endswith(".wav") or fn.startswith("."):
            continue
        path = os.path.join(AUDIO_DIR, fn)
        if not os.path.isfile(path):
            continue
        st = os.stat(path)
        # 尝试读时长
        duration = None
        try:
            with wave.open(path, "rb") as wf:
                duration = round(wf.getnframes() / float(wf.getframerate()), 2)
        except Exception:
            pass
        items.append({
            "name": fn,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "duration": duration,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def delete_audio(name):
    path = safe_path(name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静音

    # -- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _serve_index(self):
        path = os.path.join(BASE_DIR, "index.html")
        if not os.path.exists(path):
            self.send_error(404, "index.html not found")
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_wav(self, name):
        try:
            path = safe_path(name)
        except ValueError:
            self.send_error(400, "bad name")
            return
        if not os.path.isfile(path):
            self.send_error(404, "not found")
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- routes
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/state":
            return self._json({
                "devices": list_output_devices(),
                "defaultDevice": pick_default_device(),
                "audioDir": AUDIO_DIR,
                "items": list_audio(),
            })
        if path == "/api/list":
            return self._json({"items": list_audio()})
        if path.startswith("/audio/"):
            return self._serve_wav(unquote(path[len("/audio/"):]))
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/api/generate":
            text = (body.get("text") or "").strip()
            voice = body.get("voice") or "Tingting"
            rate = body.get("rate") or 200
            if not text:
                return self._json({"ok": False, "error": "文本不能为空"}, 400)
            try:
                name = synthesize(text, voice, rate)
                return self._json({"ok": True, "name": name, "items": list_audio()})
            except subprocess.CalledProcessError as e:
                return self._json({"ok": False, "error": f"合成失败：{e}"}, 500)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/play":
            name = body.get("name") or ""
            device = body.get("device")
            try:
                wav_path = safe_path(name)
            except ValueError:
                return self._json({"ok": False, "error": "非法文件名"}, 400)
            if not os.path.isfile(wav_path):
                return self._json({"ok": False, "error": "文件不存在"}, 404)
            if device is None:
                device = pick_default_device()
            try:
                # 在后台线程播放，立刻返回，方便连点
                t = threading.Thread(
                    target=play_wav, args=(wav_path, int(device)), daemon=True
                )
                t.start()
                return self._json({"ok": True, "device": int(device)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/delete":
            name = body.get("name") or ""
            try:
                ok = delete_audio(name)
                return self._json({"ok": ok, "items": list_audio()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        if path == "/api/shutdown":
            # 先回响应，再在后台线程里关服务，避免把自己阻塞死
            self._json({"ok": True, "message": "服务已关闭"})

            def _stop():
                time.sleep(0.3)
                try:
                    self.server.shutdown()
                except Exception:
                    pass

            threading.Thread(target=_stop, daemon=True).start()
            return

        if path == "/api/preview":
            # 生成后不落盘，直接给前端试听？—— 简化为直接读取已有文件
            return self._json({"ok": False, "error": "unsupported"}, 400)

        self.send_error(404)


def cleanup_tmp():
    """清掉上次异常中断留下的临时 aiff，避免残留。"""
    if not os.path.isdir(AUDIO_DIR):
        return
    for fn in os.listdir(AUDIO_DIR):
        if fn.startswith(".") and fn.lower().endswith(".aiff"):
            try:
                os.remove(os.path.join(AUDIO_DIR, fn))
            except OSError:
                pass


def main():
    cleanup_tmp()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"虚拟音频语音输入工具已启动：{url}")
    print(f"音频目录：{AUDIO_DIR}")
    print("按 Ctrl+C 退出")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\n已退出，端口已释放。本窗口可以关闭了。")


if __name__ == "__main__":
    main()
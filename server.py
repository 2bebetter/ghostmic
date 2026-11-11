#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GhostMic: 本机文字转语音，并播放到用户选定的输出设备。"""

import json
import math
import os
import re
import select
import subprocess
import sys
import threading
import time
import uuid
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import numpy as np
import sounddevice as sd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.abspath(os.environ.get("GHOSTMIC_AUDIO_DIR") or os.path.join(BASE_DIR, "audio"))
os.makedirs(AUDIO_DIR, exist_ok=True)
HOST = "127.0.0.1"
PORT = int(os.environ.get("GHOSTMIC_PORT", "8787"))
PLAYBACK_START_TIMEOUT = 10
VIRTUAL_HINTS = ("blackhole", "virtual", "soundflower", "loopback", "existential")


def safe_path(name):
    if not isinstance(name, str) or not name or not name.lower().endswith(".wav"):
        raise ValueError("非法文件名")
    if name.startswith(".") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("非法文件名")
    path = os.path.join(AUDIO_DIR, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(AUDIO_DIR):
        raise ValueError("非法文件名")
    return path


def list_output_devices():
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_output_channels", 0) > 0:
            name = device["name"]
            devices.append({
                "index": index,
                "name": name,
                "channels": device["max_output_channels"],
                "is_virtual": any(hint in name.lower() for hint in VIRTUAL_HINTS),
            })
    return devices


def pick_default_device(devices=None):
    """自动选择虚拟设备；没有时由用户明确选择输出。"""
    if devices is None:
        devices = list_output_devices()
    return next((d["index"] for d in devices if d["is_virtual"]), None)


def list_voices():
    """只展示系统 say 命令列出的声音。"""
    try:
        result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True, timeout=10)
        voices = []
        for line in result.stdout.splitlines():
            match = re.match(r"^(.+?)\s{2,}([a-z]{2}[_-][A-Za-z]{2})\s+#", line)
            if match:
                voices.append({"name": match.group(1).strip(), "language": match.group(2)})
        voices.sort(key=lambda item: (
            0 if item["name"] == "Tingting" else
            1 if item["language"] == "zh_CN" else
            2 if item["language"].startswith("zh_") else
            3 if item["name"] == "Samantha" else 4,
            item["name"].casefold(),
        ))
        return voices
    except (OSError, subprocess.SubprocessError):
        return []


class PlaybackCancelled(Exception):
    pass


def run_process(command, cancel):
    """运行合成命令，并允许在合成过程中停止。"""
    if cancel.is_set():
        raise PlaybackCancelled()
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while True:
        try:
            _, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise PlaybackCancelled()
    if cancel.is_set():
        raise PlaybackCancelled()
    if process.returncode:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message[:500] or "语音合成失败，请检查所选声音是否可用")


def sanitize_stem(text):
    stem = re.sub(r'[\s/\\:*?"<>|]+', "_", text.strip().replace("\n", " "))
    return stem[:40].strip("._") or "voice"


def synthesize(text, voice, rate, cancel=None):
    cancel = cancel or threading.Event()
    stem = sanitize_stem(text)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:8]
    name = f"{timestamp}_{stem}_{token}.wav"
    final_path = os.path.join(AUDIO_DIR, name)
    tmp_aiff = os.path.join(AUDIO_DIR, f".{token}.aiff")
    tmp_wav = os.path.join(AUDIO_DIR, f".{token}.wav")
    try:
        run_process(["say", "-v", voice, "-r", str(rate), "-o", tmp_aiff, "--", text], cancel)
        run_process(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", tmp_aiff, tmp_wav], cancel)
        if cancel.is_set():
            raise PlaybackCancelled()
        os.replace(tmp_wav, final_path)
        return name
    finally:
        for path in (tmp_aiff, tmp_wav):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def play_wav(path, device_index, cancel, on_started, on_progress=None):
    """分块播放，停止时中断输出流。"""
    if cancel.is_set():
        raise PlaybackCancelled()
    with wave.open(path, "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        if wav.getsampwidth() != 2:
            raise ValueError("只支持 16 位 WAV 音频")
        frames = wav.readframes(wav.getnframes())
    data = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
    if not len(data):
        raise ValueError("音频文件为空")
    info = sd.query_devices(device_index)
    available = info["max_output_channels"]
    if available < 1:
        raise ValueError("所选设备不能播放音频")
    output_channels = min(2, available)
    if channels != output_channels:
        mono = data.mean(axis=1).astype(np.int16) if channels > 1 else data[:, 0]
        data = mono.reshape(-1, 1) if output_channels == 1 else np.repeat(mono[:, None], output_channels, axis=1)
    stream = sd.OutputStream(samplerate=rate, channels=output_channels, dtype="int16", device=device_index)
    try:
        if cancel.is_set():
            raise PlaybackCancelled()
        stream.start()
        on_started()
        duration = len(data) / rate
        latency = max(0.0, float(getattr(stream, "latency", 0.0)))
        last_progress = time.monotonic()
        if on_progress:
            on_progress(0.0, duration)
        for start in range(0, len(data), 512):
            if cancel.is_set():
                stream.abort()
                raise PlaybackCancelled()
            chunk = data[start:start + 512]
            stream.write(chunk)
            now = time.monotonic()
            if on_progress and now - last_progress >= 0.2:
                # Written frames may still be buffered by the output device.
                elapsed = max(0.0, min(duration, (start + len(chunk)) / rate - latency))
                on_progress(elapsed, duration)
                last_progress = now
        if cancel.is_set():
            stream.abort()
            raise PlaybackCancelled()
        stream.stop()
        if cancel.is_set():
            raise PlaybackCancelled()
    finally:
        stream.close()


def stop_process(process):
    """不要让卡住的系统音频设备阻塞停止操作。"""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def play_in_process(path, device_index, cancel, on_started, on_progress=None):
    """隔离系统音频流；设备无响应时仍可停止或返回错误。"""
    command = [sys.executable, os.path.abspath(__file__), "--play-wav", path, str(device_index)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    started = False
    pending = b""
    deadline = time.monotonic() + PLAYBACK_START_TIMEOUT

    def consume(chunk, final=False):
        nonlocal started, pending
        pending += chunk
        lines = pending.split(b"\n")
        pending = b"" if final else lines.pop()
        for line in lines:
            fields = line.strip().split()
            if fields == [b"started"] and not started:
                started = True
                on_started()
            elif started and on_progress and len(fields) == 3 and fields[0] == b"progress":
                try:
                    elapsed, duration = (float(value) for value in fields[1:])
                except ValueError:
                    continue
                if math.isfinite(elapsed) and math.isfinite(duration) and duration > 0:
                    on_progress(max(0.0, min(elapsed, duration)), duration)

    try:
        while True:
            if cancel.is_set():
                stop_process(process)
                raise PlaybackCancelled()
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                consume(os.read(process.stdout.fileno(), 4096))
            if process.poll() is not None:
                remaining, stderr = process.communicate()
                consume(remaining, final=True)
                if process.returncode:
                    message = stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(message[-500:] or "播放失败，请检查输出设备")
                if not started:
                    raise RuntimeError("播放未能启动，请检查输出设备")
                return
            if not started and time.monotonic() >= deadline:
                stop_process(process)
                raise RuntimeError("输出设备响应超时，请重新选择或检查设备")
    finally:
        stop_process(process)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def list_audio():
    items = []
    if not os.path.isdir(AUDIO_DIR):
        return items
    for name in os.listdir(AUDIO_DIR):
        if not name.lower().endswith(".wav") or name.startswith("."):
            continue
        path = os.path.join(AUDIO_DIR, name)
        try:
            stat = os.stat(path)
            if not os.path.isfile(path):
                continue
            duration = None
            try:
                with wave.open(path, "rb") as wav:
                    duration = round(wav.getnframes() / float(wav.getframerate()), 2)
            except (OSError, EOFError, wave.Error, ZeroDivisionError):
                pass
            items.append({"name": name, "size": stat.st_size, "mtime": int(stat.st_mtime), "duration": duration})
        except FileNotFoundError:
            continue
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def delete_audio(name):
    path = safe_path(name)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


_job_lock = threading.Lock()
_current_job = None
_ACTIVE_STATES = {"generating", "starting", "playing"}


def job_snapshot(job=None):
    with _job_lock:
        job = _current_job if job is None else job
        if job is None:
            return None
        return {key: job[key] for key in ("id", "state", "name", "device", "error", "stopping", "elapsed", "duration")}


def update_job(job, **fields):
    with _job_lock:
        job.update(fields)


def run_job(job, text=None, voice=None, rate=None):
    try:
        if text is not None:
            name = synthesize(text, voice, rate, job["cancel"])
            update_job(job, name=name, state="starting")
        if job["cancel"].is_set():
            raise PlaybackCancelled()
        play_in_process(
            safe_path(job["name"]), job["device"], job["cancel"],
            lambda: update_job(job, state="playing"),
            lambda elapsed, duration: update_job(job, elapsed=elapsed, duration=duration),
        )
        update_job(job, state="done", stopping=False)
    except PlaybackCancelled:
        update_job(job, state="stopped", stopping=False)
    except Exception as exc:
        if job["cancel"].is_set():
            update_job(job, state="stopped", stopping=False)
        else:
            update_job(job, state="error", error=str(exc) or "播放失败", stopping=False)


def start_job(device, name=None, text=None, voice=None, rate=None):
    global _current_job
    with _job_lock:
        if _current_job and _current_job["state"] in _ACTIVE_STATES:
            raise RuntimeError("正在播放或生成语音，请先停止")
        job = {
            "id": uuid.uuid4().hex,
            "state": "generating" if text is not None else "starting",
            "name": name,
            "device": device,
            "error": None,
            "stopping": False,
            "elapsed": 0.0,
            "duration": None,
            "cancel": threading.Event(),
        }
        worker = threading.Thread(target=run_job, args=(job, text, voice, rate), daemon=True)
        job["worker"] = worker
        _current_job = job
        worker.start()
    return job_snapshot(job)


def stop_job(job_id=None):
    with _job_lock:
        job = _current_job
        if not job or job["state"] not in _ACTIVE_STATES:
            return False
        if job_id and job_id != job["id"]:
            raise ValueError("播放状态已改变，请刷新后再试")
        job["stopping"] = True
        job["cancel"].set()
        return True


def stop_and_wait(timeout=3):
    stop_job()
    with _job_lock:
        worker = _current_job.get("worker") if _current_job else None
    if worker and worker is not threading.current_thread():
        worker.join(timeout)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length < 0 or length > 100_000:
                raise ValueError("请求内容过长")
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if not isinstance(data, dict):
                raise ValueError("请求格式有误")
            return data
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("请求格式有误") from exc

    def _serve_index(self):
        with open(os.path.join(BASE_DIR, "index.html"), "rb") as source:
            data = source.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_wav(self, name):
        try:
            path = safe_path(name)
            source = open(path, "rb")
        except ValueError:
            self.send_error(400, "bad name")
            return
        except FileNotFoundError:
            self.send_error(404, "not found")
            return
        with source:
            size = os.fstat(source.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/api/state":
                devices = list_output_devices()
                return self._json({"devices": devices, "defaultDevice": pick_default_device(devices), "voices": list_voices(), "items": list_audio(), "job": job_snapshot()})
            if path == "/api/list":
                return self._json({"items": list_audio()})
            if path == "/api/playback":
                return self._json({"job": job_snapshot()})
            if path.startswith("/audio/"):
                return self._serve_wav(unquote(path[len("/audio/"):]))
            self.send_error(404)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc) or "无法读取设备状态"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_body()
            if path in ("/api/speak", "/api/play"):
                device = body.get("device")
                if isinstance(device, bool) or not isinstance(device, int):
                    raise ValueError("请先选择输出设备")
                current_device = next((item for item in list_output_devices() if item["index"] == device), None)
                if not current_device or (body.get("deviceName") is not None and body["deviceName"] != current_device["name"]):
                    raise ValueError("输出设备已变化，请重新查找并选择")
                if path == "/api/speak":
                    text = body.get("text")
                    voice = body.get("voice")
                    rate = body.get("rate", 200)
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("先写一句想说的话")
                    if not isinstance(voice, str) or not voice.strip():
                        raise ValueError("请先选择声音")
                    if isinstance(rate, bool) or not isinstance(rate, int) or not 80 <= rate <= 400:
                        raise ValueError("语速需在 80 到 400 之间")
                    job = start_job(device, text=text.strip(), voice=voice, rate=rate)
                else:
                    name = body.get("name")
                    if not os.path.isfile(safe_path(name)):
                        raise ValueError("录音不存在")
                    job = start_job(device, name=name)
                return self._json({"ok": True, "job": job})
            if path == "/api/stop":
                stop_job(body.get("id"))
                return self._json({"ok": True, "job": job_snapshot()})
            if path == "/api/generate":
                text = body.get("text")
                voice = body.get("voice") or "Tingting"
                rate = body.get("rate", 200)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("先写一句想说的话")
                if not isinstance(voice, str) or isinstance(rate, bool) or not isinstance(rate, int) or not 80 <= rate <= 400:
                    raise ValueError("声音或语速无效")
                name = synthesize(text.strip(), voice, rate)
                return self._json({"ok": True, "name": name, "items": list_audio()})
            if path == "/api/delete":
                ok = delete_audio(body.get("name"))
                return self._json({"ok": ok, "items": list_audio()})
            if path == "/api/shutdown":
                stop_job()
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.send_error(404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc) or "操作失败"}, 500)


def cleanup_tmp():
    for name in os.listdir(AUDIO_DIR):
        if (name.startswith(".") and name.lower().endswith(".aiff")) or re.fullmatch(r"\.[0-9a-f]{8}\.wav", name):
            try:
                os.remove(os.path.join(AUDIO_DIR, name))
            except OSError:
                pass


def main():
    cleanup_tmp()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{server.server_port}/"
    if os.environ.get("GHOSTMIC_READY_MESSAGE") == "1":
        print(f"GHOSTMIC_READY {server.server_port}", flush=True)
    print(f"GhostMic 已启动：{url}", flush=True)
    print(f"音频目录：{AUDIO_DIR}")
    print("按 Ctrl+C 退出")
    if os.environ.get("GHOSTMIC_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        parent_pid = int(os.environ.get("GHOSTMIC_PARENT_PID", "0"))
    except ValueError:
        parent_pid = 0
    if parent_pid:
        def watch_parent():
            while os.getppid() == parent_pid:
                time.sleep(1)
            server.shutdown()
        threading.Thread(target=watch_parent, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_and_wait()
        server.server_close()
        print("\nGhostMic 已退出。")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--play-wav":
        try:
            play_wav(
                sys.argv[2], int(sys.argv[3]), threading.Event(),
                lambda: print("started", flush=True),
                lambda elapsed, duration: print(f"progress {elapsed:.3f} {duration:.3f}", flush=True),
            )
        except Exception as exc:
            print(str(exc) or "播放失败", file=sys.stderr, flush=True)
            sys.exit(1)
    else:
        main()

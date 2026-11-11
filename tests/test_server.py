import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

import numpy as np

import server


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("操作未在预期时间内完成")


class GhostMicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ghostmic-tests-")
        self.old_audio_dir = server.AUDIO_DIR
        server.AUDIO_DIR = self.temp.name
        with server._job_lock:
            server._current_job = None

    def tearDown(self):
        server.stop_job()
        wait_for(lambda: not server.job_snapshot() or server.job_snapshot()["state"] not in server._ACTIVE_STATES)
        server.AUDIO_DIR = self.old_audio_dir
        self.temp.cleanup()

    def wav(self, name="test.wav", frames=16000):
        path = os.path.join(self.temp.name, name)
        with wave.open(path, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(np.zeros(frames, dtype=np.int16).tobytes())
        return path

    def test_synthesis_keeps_names_unique_and_cleans_temporary_files(self):
        commands = []

        def fake_run(command, cancel):
            commands.append(command)
            output = command[command.index("-o") + 1] if command[0] == "say" else command[-1]
            with open(output, "wb") as file:
                file.write(b"test")

        with mock.patch.object(server, "run_process", fake_run), mock.patch.object(server.time, "strftime", return_value="20260101_120000"):
            first = server.synthesize("-你好", "Tingting", 200)
            second = server.synthesize("-你好", "Tingting", 200)
        self.assertNotEqual(first, second)
        self.assertEqual(["--", "-你好"], commands[0][-2:])
        self.assertEqual({first, second}, set(os.listdir(self.temp.name)))

    def test_stopping_generation_and_rejecting_concurrent_jobs(self):
        entered = threading.Event()

        def fake_synthesize(text, voice, rate, cancel):
            entered.set()
            cancel.wait(2)
            raise server.PlaybackCancelled()

        with mock.patch.object(server, "synthesize", fake_synthesize):
            job = server.start_job(1, text="你好", voice="Tingting", rate=200)
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(RuntimeError, "正在播放或生成"):
                server.start_job(1, name="other.wav")
            with self.assertRaises(ValueError):
                server.stop_job("outdated-id")
            self.assertTrue(server.stop_job(job["id"]))
            wait_for(lambda: server.job_snapshot()["state"] == "stopped")

    def test_output_stream_is_stopped_and_aborted(self):
        path = self.wav(frames=2000)
        cancel = threading.Event()
        started = []

        streams = []

        class FakeStream:
            def __init__(self, **kwargs):
                streams.append(self)
                self.kwargs = kwargs
                self.aborted = False
                self.closed = False
                self.chunks = []
            def start(self): pass
            def write(self, data):
                self.chunks.append(data.copy())
                cancel.set()
            def abort(self): self.aborted = True
            def close(self): self.closed = True

        with mock.patch.object(server.sd, "query_devices", return_value={"max_output_channels": 2}), mock.patch.object(server.sd, "OutputStream", side_effect=FakeStream) as factory:
            with self.assertRaises(server.PlaybackCancelled):
                server.play_wav(path, 1, cancel, lambda: started.append(True))
        self.assertEqual(factory.call_args.kwargs["channels"], 2)
        self.assertEqual(started, [True])
        self.assertTrue(streams[0].aborted)
        self.assertTrue(streams[0].closed)
        self.assertEqual(streams[0].chunks[0].shape, (512, 2))

    def test_playback_progress_tracks_frames_and_output_latency(self):
        path = self.wav(frames=2000)
        progress = []
        events = []

        class FakeStream:
            latency = 0.01
            def __init__(self, **kwargs): pass
            def start(self): events.append("started")
            def write(self, data): events.append("write")
            def stop(self): events.append("stopped")
            def close(self): pass

        with mock.patch.object(server.sd, "query_devices", return_value={"max_output_channels": 1}), \
                mock.patch.object(server.sd, "OutputStream", FakeStream), \
                mock.patch.object(server.time, "monotonic", side_effect=[0, .21, .42, .63, .84]):
            server.play_wav(path, 1, threading.Event(), lambda: events.append("callback"), lambda elapsed, duration: progress.append((elapsed, duration)))
        self.assertEqual(events[:3], ["started", "callback", "write"])
        self.assertEqual(progress[0], (0.0, 0.125))
        self.assertAlmostEqual(progress[1][0], 512 / 16000 - 0.01)
        self.assertAlmostEqual(progress[-1][0], 0.125 - 0.01)
        self.assertTrue(all(duration == 0.125 for _, duration in progress))
        self.assertEqual(events[-1], "stopped")

    def test_playback_process_receives_split_and_batched_progress(self):
        original_popen = subprocess.Popen
        script = (
            "import os,time; "
            "os.write(1,b'start'); time.sleep(.03); "
            "os.write(1,b'ed\\nprogress 0 4\\nprogress 1.2 4\\nprogress nan 4\\n'); "
            "time.sleep(.03); os.write(1,b'progress 3 4\\n')"
        )
        def fake_process(*args, **kwargs):
            return original_popen([sys.executable, "-u", "-c", script], **kwargs)

        events = []
        with mock.patch.object(server.subprocess, "Popen", side_effect=fake_process):
            server.play_in_process("test.wav", 1, threading.Event(), lambda: events.append("started"), lambda elapsed, duration: events.append((elapsed, duration)))
        self.assertEqual(events, ["started", (0, 4), (1.2, 4), (3, 4)])

    def test_job_exposes_progress_while_playing(self):
        entered = threading.Event()
        finish = threading.Event()
        def fake_play(path, device, cancel, on_started, on_progress):
            on_started()
            on_progress(2.5, 8.0)
            entered.set()
            finish.wait(2)
        with mock.patch.object(server, "play_in_process", fake_play):
            server.start_job(1, name="test.wav")
            self.assertTrue(entered.wait(1))
            snapshot = server.job_snapshot()
            self.assertEqual((snapshot["state"], snapshot["elapsed"], snapshot["duration"]), ("playing", 2.5, 8.0))
            finish.set()
            wait_for(lambda: server.job_snapshot()["state"] == "done")

    def test_hung_device_process_can_be_cancelled(self):
        original_popen = subprocess.Popen
        spawned = []

        def hung_process(*args, **kwargs):
            process = original_popen([sys.executable, "-u", "-c", "import time; time.sleep(30)"], **kwargs)
            spawned.append(process)
            return process

        cancel = threading.Event()
        result = []
        def play():
            try:
                server.play_in_process("test.wav", 1, cancel, lambda: None)
            except server.PlaybackCancelled:
                result.append("stopped")

        with mock.patch.object(server.subprocess, "Popen", side_effect=hung_process):
            thread = threading.Thread(target=play)
            thread.start()
            wait_for(lambda: bool(spawned))
            cancel.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["stopped"])
        self.assertIsNotNone(spawned[0].poll())

    def test_hung_device_reports_timeout(self):
        original_popen = subprocess.Popen

        def hung_process(*args, **kwargs):
            return original_popen([sys.executable, "-u", "-c", "import time; time.sleep(30)"], **kwargs)

        with mock.patch.object(server.subprocess, "Popen", side_effect=hung_process), mock.patch.object(server, "PLAYBACK_START_TIMEOUT", 0.1):
            with self.assertRaisesRegex(RuntimeError, "输出设备响应超时"):
                server.play_in_process("test.wav", 1, threading.Event(), lambda: None)

    def test_device_index_reused_by_another_device_is_rejected(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"text": "你好", "voice": "Tingting", "rate": 200, "device": 1, "deviceName": "BlackHole 2ch"}).encode()
            request = Request(f"http://127.0.0.1:{httpd.server_port}/api/speak", data=body, headers={"Content-Type": "application/json"})
            with mock.patch.object(server, "list_output_devices", return_value=[{"index": 1, "name": "Speakers"}]), mock.patch.object(server, "start_job") as start:
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=3)
                self.assertEqual(error.exception.code, 400)
                self.assertIn("设备已变化", error.exception.read().decode())
                start.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(3)

    def test_desktop_server_selects_port_and_stops_cleanly(self):
        environment = {**os.environ, "GHOSTMIC_PORT": "0", "GHOSTMIC_AUDIO_DIR": self.temp.name, "GHOSTMIC_READY_MESSAGE": "1", "GHOSTMIC_NO_BROWSER": "1", "GHOSTMIC_PARENT_PID": str(os.getpid())}
        process = subprocess.Popen([sys.executable, "-u", server.__file__], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "server did not announce readiness")
            line = process.stdout.readline().strip()
            self.assertTrue(line.startswith("GHOSTMIC_READY "), line)
            port = int(line.split()[-1])
            with urlopen(f"http://127.0.0.1:{port}/api/list", timeout=3) as response:
                self.assertEqual(json.load(response), {"items": []})
            request = Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}", headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                self.assertTrue(json.load(response)["ok"])
            process.communicate(timeout=6)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_desktop_server_stops_if_parent_disappears(self):
        environment = {**os.environ, "GHOSTMIC_PORT": "0", "GHOSTMIC_AUDIO_DIR": self.temp.name, "GHOSTMIC_NO_BROWSER": "1", "GHOSTMIC_PARENT_PID": "1"}
        process = subprocess.Popen([sys.executable, "-u", server.__file__], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.communicate(timeout=6)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_safe_names_allow_ellipsis_but_not_paths(self):
        self.assertEqual(server.safe_path("20260101_120000_你好...世界.wav"), os.path.join(self.temp.name, "20260101_120000_你好...世界.wav"))
        with self.assertRaises(ValueError):
            server.safe_path("../outside.wav")

    def test_default_does_not_silently_select_speakers(self):
        devices = [{"index": 0, "name": "Speakers", "is_virtual": False}]
        self.assertIsNone(server.pick_default_device(devices))
        devices.append({"index": 1, "name": "BlackHole 2ch", "is_virtual": True})
        self.assertEqual(server.pick_default_device(devices), 1)

    def test_cleanup_preserves_unrelated_hidden_wav_files(self):
        for name in (".notes.wav", ".abcdef12.wav", ".old.aiff"):
            with open(os.path.join(self.temp.name, name), "wb") as file:
                file.write(b"x")
        server.cleanup_tmp()
        self.assertEqual(os.listdir(self.temp.name), [".notes.wav"])


if __name__ == "__main__":
    unittest.main()

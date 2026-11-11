#!/usr/bin/env python3
"""Run an isolated macOS window, menu, persistence and shutdown smoke test."""
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "GhostMic.app"


def main():
    if sys.platform != "darwin" or not APP.exists():
        raise SystemExit("Build GhostMic.app on macOS before running this check.")
    suite = "com.ghostmic.test." + uuid.uuid4().hex
    try:
        with tempfile.TemporaryDirectory(prefix="ghostmic-native-check-") as directory:
            temporary = Path(directory)
            app = temporary / "Some Apps" / "GhostMic.app"
            app.parent.mkdir()
            shutil.copytree(APP, app, symlinks=True)
            binary = app / "Contents/MacOS/GhostMic"
            architecture = subprocess.check_output(["uname", "-m"], text=True).strip()
            subprocess.run(["swiftc", "-O", "-D", "GHOSTMIC_TESTS", "-target", architecture + "-apple-macosx14.5", str(ROOT / "macos/GhostMic.swift"), "-o", str(binary)], check=True)
            subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            original = temporary / "legacy"
            original.mkdir()
            with wave.open(str(original / "earlier.wav"), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000); audio.writeframes(bytes(3200))
            with wave.open(str(original / "same-name.wav"), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000); audio.writeframes(bytes(3200))
            data = temporary / "data"
            (data / "audio").mkdir(parents=True)
            with wave.open(str(data / "audio/same-name.wav"), "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000); audio.writeframes(bytes(1600))
            original_existing = (data / "audio/same-name.wav").read_bytes()
            result = temporary / "result.json"
            screenshot = temporary / "window.png"
            environment = {**os.environ, "GHOSTMIC_TEST_PREF_SUITE": suite, "GHOSTMIC_TEST_DATA_DIR": str(data), "GHOSTMIC_TEST_LEGACY_DIR": str(original), "GHOSTMIC_TEST_RESULT": str(result), "GHOSTMIC_TEST_SCREENSHOT": str(screenshot)}
            for run in range(2):
                if result.exists():
                    result.unlink()
                environment["GHOSTMIC_TEST_EXIT_METHOD"] = "page" if run == 0 else "menu"
                process = subprocess.Popen([str(binary)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    _, error = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, error = process.communicate()
                    details = result.read_text() if result.exists() else "no page result"
                    raise AssertionError("Native app did not finish startup and shutdown: " + details + " " + error.decode(errors="replace")[-2000:])
                if process.returncode:
                    raise AssertionError(f"Native app exited with {process.returncode}: {error.decode(errors='replace')[-1000:]}")
                report = json.loads(result.read_text())
                assert screenshot.exists(), "WebKit window snapshot is missing"
                if run == 0 and os.environ.get("GHOSTMIC_DEBUG_SCREENSHOT"):
                    shutil.copyfile(screenshot, os.environ["GHOSTMIC_DEBUG_SCREENSHOT"])
                assert report.get("desktop") and report.get("layout") and report.get("ready") and report.get("progress"), report
                assert report.get("hidden") and report.get("reopened"), report
                assert "打开 GhostMic" in report["menu"] and "退出 GhostMic" in report["menu"], report
                assert report.get("storedRate") == "240", report
                if run == 1:
                    assert report.get("savedRate") == "240", report
                with socket.socket() as connection:
                    assert connection.connect_ex(("127.0.0.1", report["port"])) != 0, "server did not stop"
                print(f"Native run {run + 1}: window, preferences and {environment['GHOSTMIC_TEST_EXIT_METHOD']} exit passed")
            assert (original / "earlier.wav").exists()
            assert (data / "audio/earlier.wav").exists()
            assert (data / "audio/same-name.wav").read_bytes() == original_existing
            assert len(list((data / "audio").glob("same-name_imported_*.wav"))) == 1
            assert len(list((data / "audio").glob("*.wav"))) == 3
            print("Legacy recording migration: copied once, originals and existing recordings preserved")
    finally:
        subprocess.run(["defaults", "delete", suite], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()

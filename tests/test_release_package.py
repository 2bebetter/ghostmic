#!/usr/bin/env python3
"""Validate a preview ZIP in an isolated directory; --audio tests silence on a local speaker."""
import argparse
import hashlib
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def api(port, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def test_audio(app, directory):
    resources = app / "Contents/Resources"
    python = resources / "python/bin/python3"
    recordings = directory / "recordings"
    recordings.mkdir()
    with wave.open(str(recordings / "silence.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(16000 * 2 * 4))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "PIP_"))}
    env.update(GHOSTMIC_AUDIO_DIR=str(recordings), GHOSTMIC_PORT="0", GHOSTMIC_NO_BROWSER="1", GHOSTMIC_READY_MESSAGE="1", GHOSTMIC_PARENT_PID=str(os.getpid()), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    process = subprocess.Popen([str(python), "-I", "-B", "-u", str(resources / "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([process.stdout], [], [], 15)
        if not ready:
            raise AssertionError("packaged server did not start")
        line = process.stdout.readline().strip()
        assert line.startswith("GHOSTMIC_READY "), line
        port = int(line.split()[-1])
        state = api(port, "/api/state")
        assert state["voices"], "no system speech voices"
        generated = api(port, "/api/generate", {"text": "你好 GhostMic", "voice": state["voices"][0]["name"], "rate": 200})
        with wave.open(str(recordings / generated["name"]), "rb") as audio:
            assert audio.getnframes() > 0, "speech generation was empty"
        speaker = next((item for item in state["devices"] if "MacBook Pro" in item["name"] and not item["is_virtual"]), None)
        if not speaker:
            print("No MacBook speaker found; generation passed, physical playback skipped.")
        else:
            job = api(port, "/api/play", {"name": "silence.wav", "device": speaker["index"], "deviceName": speaker["name"]})["job"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                current = api(port, "/api/playback")["job"]
                if current["state"] == "error":
                    raise AssertionError(current["error"])
                if current["state"] == "playing" and current["elapsed"] > .2:
                    break
                time.sleep(.1)
            else:
                raise AssertionError("packaged audio did not start or advance")
            api(port, "/api/stop", {"id": job["id"]})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = api(port, "/api/playback")["job"]
                if current["state"] == "stopped":
                    break
                time.sleep(.1)
            else:
                raise AssertionError("packaged audio did not stop")
            print("System speech, silent physical output, progress and stop passed")
        api(port, "/api/shutdown", {})
        process.communicate(timeout=6)
        assert process.returncode == 0, "packaged server failed to shut down"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--audio", action="store_true")
    args = parser.parse_args()
    archive = args.archive.resolve()
    checksum = archive.with_suffix(".sha256")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_text().split()[0] == digest, "archive checksum mismatch"
    with tempfile.TemporaryDirectory(prefix="ghostmic-release-check-") as temporary:
        root = Path(temporary)
        unpacked = root / "Unpacked App With Spaces"
        unpacked.mkdir()
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(unpacked)], check=True)
        app = unpacked / "GhostMic.app"
        resources = app / "Contents/Resources"
        assert (resources / "python-licenses/LICENSE.cpython.txt").is_file()
        assert (resources / "portaudio-license.txt").is_file()
        assert (resources / "python-build-metadata.json").is_file()
        assert not (resources / "legacy-project-dir.txt").exists()
        assert not (resources / "legacy-audio-dir.txt").exists()
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "PIP_"))}
        env.update(PYTHONDONTWRITEBYTECODE="1", GHOSTMIC_TEST_APP=str(app), GHOSTMIC_TEST_ARCH=args.arch, GHOSTMIC_TEST_DISTRIBUTION="1")
        python = resources / "python/bin/python3"
        subprocess.run([str(python), "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=env, check=True)
        subprocess.run([sys.executable, "tests/test_macos_app.py"], cwd=ROOT, env=env, check=True)
        if args.audio:
            test_audio(app, root)
    print(f"{args.arch} package verification passed")


if __name__ == "__main__":
    main()

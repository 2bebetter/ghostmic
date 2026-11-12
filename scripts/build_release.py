#!/usr/bin/env python3
"""Build a self-contained, ad-hoc-signed macOS preview ZIP for one architecture."""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from build_macos_app import ROOT, build_bundle

RUNTIME_RELEASE = "20261110"
PYTHON_VERSION = "3.11.18"
RUNTIMES = {
    "arm64": ("aarch64", "3afb0567b2a2b30b3a7341bd143cb4e8a69f3d8e95d4641218eaa45f0fabbd07", "5ee82bbad0ca0c7cb355da0b19fbd6d37b384b5849b4d2839336cdc6f6983824"),
    "x86_64": ("x86_64", "c2e4b51e535ead2e526b8974909a1b1e00fbc2ebe2281087b9b8b14dad0cad5d", "864bc4c1386ff1c929f03d35c61a717fb61f36d9dee5badbdc086856305c8303"),
}


def run(*command, **kwargs):
    subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "PIP_"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def fetch_runtime(architecture, destination, *, full=False):
    platform_name, runtime_hash, full_hash = RUNTIMES[architecture]
    suffix = "pgo%2Blto-full.tar.zst" if full else "install_only_stripped.tar.gz"
    filename = f"cpython-{PYTHON_VERSION}%2B{RUNTIME_RELEASE}-{platform_name}-apple-darwin-{suffix}"
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{RUNTIME_RELEASE}/{filename}"
    label = "运行环境的许可信息" if full else "Python 运行环境"
    print(f"正在下载并校验 {architecture} 的{label}…", flush=True)
    with urllib.request.urlopen(url, timeout=90) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)
    if file_hash(destination) != (full_hash if full else runtime_hash):
        raise RuntimeError(f"{label}校验失败，请检查网络后重试。")


def build(architecture, output):
    if sys.platform != "darwin":
        raise SystemExit("发布包需要在 macOS 上构建。")
    if sys.version_info < (3, 11):
        raise SystemExit("构建发布包需要 Python 3.11 或更新版本。")
    host = platform.machine()
    if architecture != host and not (host == "arm64" and architecture == "x86_64"):
        raise SystemExit("请在对应架构的 Mac 上构建；Apple 芯片 Mac 可通过 Rosetta 构建 Intel 版本。")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"GhostMic-{architecture}.zip"
    checksum = output / f"GhostMic-{architecture}.sha256"
    if archive.exists() or checksum.exists():
        raise SystemExit(f"目标文件已存在：{archive}；请先选择其他输出目录。")
    with tempfile.TemporaryDirectory(prefix="ghostmic-release-") as temporary:
        stage = Path(temporary)
        download = stage / "runtime.tar.gz"
        fetch_runtime(architecture, download)
        unpacked = stage / "unpacked"
        unpacked.mkdir()
        with tarfile.open(download, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(unpacked, filter="data")
            else:
                # Older Python 3.11 builds lack extraction filters. The archive is hash-pinned above.
                run("/usr/bin/tar", "-xzf", str(download), "-C", str(unpacked))
        source_runtime = unpacked / "python"
        if not (source_runtime / "bin/python3").is_file():
            raise RuntimeError("下载的运行环境不完整。")
        full_archive = stage / "runtime-full.tar.zst"
        fetch_runtime(architecture, full_archive, full=True)
        licensing = stage / "licensing"
        licensing.mkdir()
        # The official full archive is hash-pinned above; extract only its notices and metadata.
        run("/usr/bin/tar", "-xf", str(full_archive), "-C", str(licensing), "python/licenses", "python/PYTHON.json")
        bundle = stage / "GhostMic.app"
        build_bundle(bundle, stage, architectures=(architecture,), include_legacy=False)
        resources = bundle / "Contents/Resources"
        runtime = resources / "python"
        shutil.move(str(source_runtime), str(runtime))
        shutil.copytree(licensing / "python/licenses", resources / "python-licenses")
        shutil.copy2(licensing / "python/PYTHON.json", resources / "python-build-metadata.json")
        shutil.copy2(ROOT / "third-party-licenses/portaudio.txt", resources / "portaudio-license.txt")
        shutil.copy2(ROOT / "requirements-release.txt", resources / "requirements-release.txt")
        python = runtime / "bin/python3"
        env = clean_environment()
        print("正在安装并校验播放组件…", flush=True)
        run(str(python), "-I", "-B", "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--only-binary=:all:", "--require-hashes", "-r", str(ROOT / "requirements-release.txt"), env=env)
        run(str(python), "-I", "-B", "-c", "import numpy, sounddevice; print('播放组件可用')", env=env)
        (resources / "distribution.txt").write_text("standalone\n", encoding="utf-8")
        print("正在生成本地预览包…", flush=True)
        run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(bundle))
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle))
        temporary_zip = stage / "GhostMic.zip"
        run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(temporary_zip))
        digest = file_hash(temporary_zip)
        # Exclusive creation avoids overwriting an unrelated existing download.
        created = False
        try:
            with archive.open("xb") as target:
                created = True
                with temporary_zip.open("rb") as source:
                    shutil.copyfileobj(source, target)
            with checksum.open("x", encoding="utf-8") as target:
                target.write(f"{digest}  {archive.name}\n")
        except BaseException:
            if created:
                archive.unlink(missing_ok=True)
            raise
    print(f"已创建 {archive} ({archive.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"SHA-256: {digest}")
    print("这是临时签名的预览包，尚未经过 Apple 开发者签名与公证。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=sorted(RUNTIMES), default=platform.machine())
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.arch not in RUNTIMES:
        parser.error("请用 --arch 指定 arm64 或 x86_64")
    try:
        build(args.arch, args.output)
    except (OSError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"打包未完成：{exc}") from exc

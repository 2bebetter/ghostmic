#!/usr/bin/env python3
"""Build a movable macOS app from the local GhostMic sources."""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / "GhostMic.app"
BUNDLE_ID = "com.ghostmic.desktop"


def run(*args):
    subprocess.run(args, check=True, cwd=ROOT)


def build_bundle(bundle, stage, architectures=("arm64", "x86_64"), include_legacy=True):
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    info = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": "GhostMic",
        "CFBundleExecutable": "GhostMic",
        "CFBundleIconFile": "GhostMic",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "GhostMic",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "14.5",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    for name in ("server.py", "index.html"):
        shutil.copy2(ROOT / name, resources / name)
    shutil.copy2(ROOT / "LICENSE", resources / "LICENSE.txt")
    shutil.copy2(ROOT / "third-party-licenses/cmdk.txt", resources / "cmdk-license.txt")
    if include_legacy:
        (resources / "legacy-audio-dir.txt").write_text(str(ROOT / "audio") + "\n", encoding="utf-8")
        (resources / "legacy-project-dir.txt").write_text(str(ROOT) + "\n", encoding="utf-8")
    binaries = []
    for architecture in architectures:
        binary = stage / ("GhostMic-" + architecture)
        print("正在构建 " + architecture + "…", flush=True)
        run("/usr/bin/swiftc", "-O", "-target", architecture + "-apple-macosx14.5", str(ROOT / "macos/GhostMic.swift"), "-o", str(binary))
        binaries.append(str(binary))
    if len(binaries) == 1:
        shutil.copy2(binaries[0], macos / "GhostMic")
    else:
        run("/usr/bin/lipo", "-create", *binaries, "-output", str(macos / "GhostMic"))
    iconset = stage / "GhostMic.iconset"
    run("/usr/bin/swift", str(ROOT / "scripts/make_icon.swift"), str(iconset))
    run("/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(resources / "GhostMic.icns"))


def build():
    if sys.platform != "darwin":
        raise SystemExit("GhostMic.app 需要在 macOS 上构建。")
    if DESTINATION.exists():
        try:
            with (DESTINATION / "Contents/Info.plist").open("rb") as handle:
                identity = plistlib.load(handle).get("CFBundleIdentifier")
        except (OSError, ValueError):
            identity = None
        if identity != BUNDLE_ID:
            raise SystemExit("GhostMic.app 已存在且不是本构建脚本创建的应用；没有覆盖它。")
    with tempfile.TemporaryDirectory(prefix=".ghostmic-build-", dir=ROOT) as temporary:
        stage = Path(temporary)
        bundle = stage / "GhostMic.app"
        build_bundle(bundle, stage)
        run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(bundle))
        backup = stage / "previous.app"
        if DESTINATION.exists():
            os.replace(DESTINATION, backup)
        try:
            os.replace(bundle, DESTINATION)
        except Exception:
            if backup.exists():
                os.replace(backup, DESTINATION)
            raise
    print("已创建 " + str(DESTINATION))


if __name__ == "__main__":
    build()

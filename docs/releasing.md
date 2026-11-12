# macOS 发布准备

项目可以从源码构建应用，也可以构建包含 Python 和播放组件的独立预览包。独立预览包仅使用临时签名，尚未经过 Apple 签名和公证，可以免费发布，但首次打开时可能被 macOS 阻止。

## 构建独立预览包

需要 macOS、Python 3.11 或更新版本、Xcode 命令行工具和网络。以下命令分别生成 Apple 芯片和 Intel 版本：

```bash
python3 scripts/build_release.py --arch arm64
python3 scripts/build_release.py --arch x86_64
```

在 Apple 芯片 Mac 上构建 Intel 版本需要安装 Rosetta；Intel Mac 应构建 Intel 版本。产物和 SHA-256 校验文件位于 `dist/`。脚本不会覆盖已存在的同名文件；如需再次构建，请指定一个新的 `--output` 目录。

应用运行时无需系统 Python 或联网安装组件。每个版本只包含一种芯片架构的运行环境；2026 年 11 月的本机构建 ZIP 约为 35–36 MiB。构建脚本校验固定版本的 Python 下载和依赖文件，并随包附带许可文本。预览包没有写入构建机器的项目路径，不自动扫描旧源码目录；从旧网页版迁移录音的方法见使用说明。

在有图形界面的 macOS 会话中，可运行下面的检查。`--audio` 会用本机扬声器播放一段静音文件，并在临时目录生成一段语音：

```bash
python3 tests/test_release_package.py dist/GhostMic-arm64.zip --arch arm64 --audio
python3 tests/test_release_package.py dist/GhostMic-x86_64.zip --arch x86_64 --audio
```

## 发布预览版后的后续工作

- 准备 Developer ID 证书，针对内嵌程序和库完成正式签名、Apple 公证和装订；参见[申请签名说明](signing.md)。保护证书私钥和公证凭据，不放入源码仓库。
- 在干净的 Apple 芯片与 Intel Mac 上分别验证安装、首次启动、退出、更新和旧录音保留；覆盖声明支持的最低 macOS 版本。Rosetta 测试不能代替 Intel 实机测试。
- 在无 Python、无开发工具和离线条件下验证启动；检查普通扬声器、本机试听以及 BlackHole 输出，在目标应用里确认麦克风实际收到声音。
- 验证从下载状态解压后的图标、版本信息、签名、公证和卸载体验，复核第三方许可与构建依赖。
- 持续提供带明确版本号的 ZIP 或 DMG、校验值和更新说明。源码仓库继续保留源码安装方式；稳定下载入口发布后再考虑移除旧版 `启动工具.command`。

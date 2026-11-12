import AppKit
import Foundation
import WebKit

final class GhostMicApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView?
    private var loadingText: NSTextField!
    private var statusItem: NSStatusItem!
    private var serverProcess: Process?
    private var outputPipe: Pipe?
    private var outputBuffer = ""
    private var startupTimeout: DispatchWorkItem?
    private var serverURL: URL?
    private var shuttingDown = false
    private var setupProcess: Process?
    private let setupLock = NSLock()
    private let preferences: UserDefaults = {
        #if GHOSTMIC_TESTS
        if let name = ProcessInfo.processInfo.environment["GHOSTMIC_TEST_PREF_SUITE"], let defaults = UserDefaults(suiteName: name) { return defaults }
        #endif
        return .standard
    }()
    private let fileManager = FileManager.default
    private var appData: URL {
        #if GHOSTMIC_TESTS
        if let directory = ProcessInfo.processInfo.environment["GHOSTMIC_TEST_DATA_DIR"] { return URL(fileURLWithPath: directory, isDirectory: true) }
        #endif
        return fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("GhostMic", isDirectory: true)
    }
    private var audioDirectory: URL { appData.appendingPathComponent("audio", isDirectory: true) }
    private var pythonDirectory: URL { appData.appendingPathComponent("python", isDirectory: true) }
    private var logURL: URL { appData.appendingPathComponent("GhostMic.log") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        createWindow()
        createStatusItem()
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            if event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
               event.charactersIgnoringModifiers?.lowercased() == "q" {
                NSApp.terminate(nil)
                return nil
            }
            return event
        }
        showWindow(nil)
        do {
            try fileManager.createDirectory(at: audioDirectory, withIntermediateDirectories: true)
            try migrateHistoryIfNeeded()
        } catch {
            showFailure("无法准备录音目录：\(error.localizedDescription)")
            return
        }
        findPythonAndStart()
    }

    private func createWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 480),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "GhostMic"
        window.minSize = NSSize(width: 380, height: 430)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        showLoading("正在准备 GhostMic…")
    }

    private func createStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "GhostMic") {
            image.isTemplate = true
            statusItem.button?.image = image
        } else {
            statusItem.button?.title = "GM"
        }
        let menu = NSMenu()
        let open = NSMenuItem(title: "打开 GhostMic", action: #selector(showWindow(_:)), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出 GhostMic", action: #selector(quitFromMenu(_:)), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
    }

    @objc private func showWindow(_ sender: Any?) {
        if window.isMiniaturized { window.deminiaturize(nil) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func quitFromMenu(_ sender: Any?) { NSApp.terminate(nil) }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow(nil)
        return true
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    private func showLoading(_ message: String) {
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 680, height: 480))
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.startAnimation(nil)
        loadingText = NSTextField(labelWithString: message)
        loadingText.textColor = .secondaryLabelColor
        loadingText.alignment = .center
        loadingText.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(spinner)
        view.addSubview(loadingText)
        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: view.centerYAnchor, constant: -15),
            loadingText.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            loadingText.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 14),
            loadingText.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 20),
            loadingText.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -20),
        ])
        window.contentView = view
    }

    private func migrateHistoryIfNeeded() throws {
        let marker = appData.appendingPathComponent(".history-migrated")
        if fileManager.fileExists(atPath: marker.path) { return }
        var legacyDirectory: String?
        #if GHOSTMIC_TESTS
        legacyDirectory = ProcessInfo.processInfo.environment["GHOSTMIC_TEST_LEGACY_DIR"]
        #endif
        if legacyDirectory == nil, let path = Bundle.main.path(forResource: "legacy-audio-dir", ofType: "txt") {
            legacyDirectory = try? String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let legacy = legacyDirectory, !legacy.isEmpty, fileManager.fileExists(atPath: legacy) {
            for source in try fileManager.contentsOfDirectory(at: URL(fileURLWithPath: legacy), includingPropertiesForKeys: [.isRegularFileKey], options: [.skipsHiddenFiles]) {
                guard source.pathExtension.lowercased() == "wav",
                      (try? source.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true else { continue }
                var destination = audioDirectory.appendingPathComponent(source.lastPathComponent)
                if fileManager.fileExists(atPath: destination.path) {
                    if fileManager.contentsEqual(atPath: source.path, andPath: destination.path) { continue }
                    destination = audioDirectory.appendingPathComponent(source.deletingPathExtension().lastPathComponent + "_imported_" + UUID().uuidString.prefix(8) + ".wav")
                }
                try fileManager.copyItem(at: source, to: destination)
            }
        }
        try Data("done\n".utf8).write(to: marker, options: .atomic)
    }

    private var isDistribution: Bool { Bundle.main.path(forResource: "distribution", ofType: "txt") != nil }

    private func candidatePythons() -> [String] {
        if isDistribution {
            guard let resources = Bundle.main.resourceURL else { return [] }
            let python = resources.appendingPathComponent("python/bin/python3").path
            return fileManager.isExecutableFile(atPath: python) ? [python] : []
        }
        let home = fileManager.homeDirectoryForCurrentUser.path
        let legacyRoot = Bundle.main.path(forResource: "legacy-project-dir", ofType: "txt")
            .flatMap { try? String(contentsOfFile: $0, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) }
        var paths = [pythonDirectory.appendingPathComponent("bin/python3").path]
        if let legacyRoot, !legacyRoot.isEmpty { paths.append(legacyRoot + "/.venv/bin/python3") }
        paths += [home + "/.workbuddy/binaries/python/envs/default/bin/python", "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        if let inherited = ProcessInfo.processInfo.environment["PATH"] {
            paths += inherited.split(separator: ":").map { String($0) + "/python3" }
        }
        var seen = Set<String>()
        return paths.filter { seen.insert($0).inserted && fileManager.isExecutableFile(atPath: $0) }
    }

    private func findPythonAndStart() {
        loadingText.stringValue = "正在准备 GhostMic…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let candidates = self.candidatePythons()
            let arguments = (self.isDistribution ? ["-I", "-B"] : []) + ["-c", "import numpy, sounddevice"]
            let python = candidates.first { self.runSilently($0, arguments) }
            DispatchQueue.main.async {
                guard !self.shuttingDown else { return }
                if let python = python { self.startServer(python) }
                else if self.isDistribution {
                    self.showFailure("应用文件不完整或已损坏，请重新下载安装包。")
                } else {
                    let bootstrap = candidates.first { $0 != self.pythonDirectory.appendingPathComponent("bin/python3").path } ?? candidates.first
                    self.offerDependencySetup(bootstrap)
                }
            }
        }
    }

    private func runSilently(_ executable: String, _ arguments: [String]) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); process.waitUntilExit(); return process.terminationStatus == 0 }
        catch { return false }
    }

    private func offerDependencySetup(_ python: String?) {
        guard let python = python else {
            showFailure("没有找到 Python 3。请先安装 Python 3，然后重新打开 GhostMic。")
            return
        }
        let alert = NSAlert()
        alert.messageText = "完成一次准备"
        alert.informativeText = "GhostMic 需要安装语音播放组件。安装只需一次，需要网络；录音仍保存在本机。"
        alert.addButton(withTitle: "安装并继续")
        alert.addButton(withTitle: "退出")
        if alert.runModal() == .alertFirstButtonReturn { installDependencies(python) }
        else { NSApp.terminate(nil) }
    }

    private func installDependencies(_ python: String) {
        loadingText.stringValue = "正在安装组件，完成后会自动打开…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let environmentPython = self.pythonDirectory.appendingPathComponent("bin/python3").path
            let created = self.runLogged(python, ["-m", "venv", self.pythonDirectory.path])
            let installed = created && self.runLogged(environmentPython, ["-m", "pip", "install", "--disable-pip-version-check", "sounddevice", "numpy"])
            DispatchQueue.main.async {
                guard !self.shuttingDown else { return }
                if installed { self.startServer(environmentPython) }
                else { self.showFailure("组件安装没有完成。请检查网络和 Python 环境后重试。", showLog: true) }
            }
        }
    }

    private func logHandle() -> FileHandle? {
        if !fileManager.fileExists(atPath: logURL.path) { fileManager.createFile(atPath: logURL.path, contents: nil) }
        guard let handle = try? FileHandle(forWritingTo: logURL) else { return nil }
        _ = try? handle.seekToEnd()
        return handle
    }

    private func runLogged(_ executable: String, _ arguments: [String]) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        let log = logHandle()
        process.standardOutput = log ?? FileHandle.nullDevice
        process.standardError = log ?? FileHandle.nullDevice
        setupLock.lock()
        if shuttingDown { setupLock.unlock(); try? log?.close(); return false }
        setupProcess = process
        do {
            try process.run()
            setupLock.unlock()
            process.waitUntilExit()
            setupLock.lock(); setupProcess = nil; setupLock.unlock()
            try? log?.close()
            return process.terminationStatus == 0
        } catch {
            setupProcess = nil
            setupLock.unlock()
            try? log?.close()
            return false
        }
    }

    private func startServer(_ python: String) {
        guard let script = Bundle.main.path(forResource: "server", ofType: "py") else { showFailure("应用文件不完整，请重新构建 GhostMic。"); return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = (isDistribution ? ["-I", "-B"] : []) + ["-u", script]
        var environment = ProcessInfo.processInfo.environment
        if isDistribution {
            environment.removeValue(forKey: "PYTHONHOME")
            environment.removeValue(forKey: "PYTHONPATH")
            environment["PYTHONNOUSERSITE"] = "1"
        }
        environment["GHOSTMIC_PORT"] = "0"
        environment["GHOSTMIC_AUDIO_DIR"] = audioDirectory.path
        environment["GHOSTMIC_NO_BROWSER"] = "1"
        environment["GHOSTMIC_READY_MESSAGE"] = "1"
        environment["GHOSTMIC_PARENT_PID"] = String(getpid())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        let log = logHandle()
        process.standardError = log ?? FileHandle.nullDevice
        outputPipe = pipe
        serverProcess = process
        outputBuffer = ""
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self?.consumeOutput(text) }
        }
        process.terminationHandler = { [weak self] process in
            try? log?.close()
            DispatchQueue.main.async {
                guard let self = self, !self.shuttingDown, self.serverProcess === process else { return }
                self.startupTimeout?.cancel()
                self.serverProcess = nil
                self.showFailure("GhostMic 已停止运行，请重新打开。", showLog: true)
            }
        }
        do { try process.run() }
        catch { pipe.fileHandleForReading.readabilityHandler = nil; serverProcess = nil; try? log?.close(); showFailure("启动失败：\(error.localizedDescription)"); return }
        let timeout = DispatchWorkItem { [weak self] in
            guard let self = self, self.serverURL == nil, !self.shuttingDown else { return }
            self.serverProcess?.terminate()
            self.serverProcess = nil
            self.showFailure("启动时间过长，请重新打开 GhostMic。", showLog: true)
        }
        startupTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 25, execute: timeout)
    }

    private func consumeOutput(_ text: String) {
        outputBuffer += text
        while let end = outputBuffer.firstIndex(of: "\n") {
            let line = String(outputBuffer[..<end])
            outputBuffer.removeSubrange(...end)
            let prefix = "GHOSTMIC_READY "
            if line.hasPrefix(prefix), let port = Int(line.dropFirst(prefix.count)), (1...65535).contains(port), serverURL == nil {
                startupTimeout?.cancel()
                serverURL = URL(string: "http://127.0.0.1:\(port)/")!
                loadWebView()
            }
        }
    }

    private func loadWebView() {
        guard let serverURL = serverURL else { return }
        let controller = WKUserContentController()
        controller.add(self, name: "ghostmic")
        let settingKeys = ["device", "voice", "rate"]
        var values: [String: String] = [:]
        for key in settingKeys { if let value = preferences.string(forKey: "ghostmic." + key) { values[key] = value } }
        let encoded = (try? JSONSerialization.data(withJSONObject: values)).flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        let source = """
        (() => {
          const values = \(encoded);
          window.ghostMicDesktop = {
            getSetting: key => values[key] ?? null,
            saveSetting: (key, value) => { values[key] = value; window.webkit.messageHandlers.ghostmic.postMessage({action: 'setting', key, value}); },
            quit: () => window.webkit.messageHandlers.ghostmic.postMessage({action: 'quit'})
          };
        })();
        """
        controller.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let configuration = WKWebViewConfiguration()
        configuration.userContentController = controller
        let view = WKWebView(frame: window.contentView?.bounds ?? .zero, configuration: configuration)
        view.navigationDelegate = self
        view.uiDelegate = self
        webView = view
        window.contentView = view
        view.load(URLRequest(url: serverURL))
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        if action == "quit" { NSApp.terminate(nil) }
        if action == "setting", let key = body["key"] as? String, ["device", "voice", "rate"].contains(key), let value = body["value"] as? String, value.count < 500 {
            preferences.set(value, forKey: "ghostmic." + key)
        }
    }

    #if GHOSTMIC_TESTS
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard ProcessInfo.processInfo.environment["GHOSTMIC_TEST_RESULT"] != nil else { return }
        testPage(attempt: 0)
    }

    private func testPage(attempt: Int) {
        let script = """
        (() => {
          updateProgress({state: 'generating', elapsed: 0, duration: 12});
          const hiddenDuringPreparation = document.querySelector('#playback-progress').hidden;
          updateProgress({state: 'playing', elapsed: 4, duration: 12});
          const progress = document.querySelector('#playback-progress');
          const track = document.querySelector('#progress-track').getBoundingClientRect();
          const visibleDuringPlayback = !progress.hidden && track.width > 0 && track.right <= window.innerWidth &&
              document.querySelector('#progress-time').textContent === '00:04 / 00:12';
          updateProgress({state: 'done', elapsed: 12, duration: 12});
          return { desktop: !!window.ghostMicDesktop, layout: document.body.classList.contains('desktop'),
                   ready: !document.querySelector('#speak').disabled,
                   progress: hiddenDuringPreparation && visibleDuringPlayback && progress.hidden,
                   savedRate: window.ghostMicDesktop?.getSetting('rate') ?? null,
                   title: document.title };
        })()
        """
        webView?.evaluateJavaScript(script) { [weak self] result, error in
            guard let self = self else { return }
            guard let page = result as? [String: Any], page["ready"] as? Bool == true else {
                if attempt < 40 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.testPage(attempt: attempt + 1) } }
                else { self.writeTestResult(["error": error?.localizedDescription ?? "page not ready"]); NSApp.terminate(nil) }
                return
            }
            self.webView?.evaluateJavaScript("window.ghostMicDesktop.saveSetting('rate', '240')") { _, _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                    self.webView?.takeSnapshot(with: nil) { image, _ in
                        if let path = ProcessInfo.processInfo.environment["GHOSTMIC_TEST_SCREENSHOT"],
                           let tiff = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff),
                           let png = bitmap.representation(using: .png, properties: [:]) {
                            try? png.write(to: URL(fileURLWithPath: path), options: .atomic)
                        }
                        _ = self.windowShouldClose(self.window)
                        let hidden = !self.window.isVisible
                        self.showWindow(nil)
                        let reopened = self.window.isVisible
                        var report = page
                        report["hidden"] = hidden
                        report["reopened"] = reopened
                        report["menu"] = self.statusItem.menu?.items.map { $0.title } ?? []
                        report["storedRate"] = self.preferences.string(forKey: "ghostmic.rate") ?? ""
                        report["port"] = self.serverURL?.port ?? 0
                        report["python"] = self.serverProcess?.executableURL?.path ?? ""
                        self.writeTestResult(report)
                        if ProcessInfo.processInfo.environment["GHOSTMIC_TEST_EXIT_METHOD"] == "page" {
                            self.webView?.evaluateJavaScript("document.querySelector('#exit').click()")
                        } else {
                            self.quitFromMenu(nil)
                        }
                    }
                }
            }
        }
    }

    private func writeTestResult(_ result: [String: Any]) {
        guard let path = ProcessInfo.processInfo.environment["GHOSTMIC_TEST_RESULT"], let data = try? JSONSerialization.data(withJSONObject: result) else { return }
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
    }
    #endif

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if action.targetFrame != nil, url.scheme == "http", url.host == "127.0.0.1", url.port == serverURL?.port {
            decisionHandler(.allow)
        } else {
            if action.navigationType == .linkActivated, ["http", "https"].contains(url.scheme?.lowercased() ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if !shuttingDown && (error as NSError).code != NSURLErrorCancelled {
            showFailure("界面没有加载成功：\(error.localizedDescription)", showLog: true)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn) }
        #if GHOSTMIC_TESTS
        if ProcessInfo.processInfo.environment["GHOSTMIC_TEST_EXIT_METHOD"] == "page" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { alert.buttons.first?.performClick(nil) }
        }
        #endif
    }

    private func showFailure(_ message: String, showLog: Bool = false) {
        showWindow(nil)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "无法打开 GhostMic"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        if showLog && fileManager.fileExists(atPath: logURL.path) { alert.addButton(withTitle: "查看日志") }
        let result = alert.runModal()
        if result == .alertSecondButtonReturn { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }
        NSApp.terminate(nil)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if shuttingDown { return .terminateNow }
        setupLock.lock()
        shuttingDown = true
        if let process = setupProcess, process.isRunning { process.terminate() }
        setupLock.unlock()
        startupTimeout?.cancel()
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        guard let process = serverProcess, process.isRunning else { return .terminateNow }
        if let url = serverURL?.appendingPathComponent("api/shutdown") {
            var request = URLRequest(url: url, timeoutInterval: 2)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = Data("{}".utf8)
            let done = DispatchSemaphore(value: 0)
            URLSession.shared.dataTask(with: request) { _, _, _ in done.signal() }.resume()
            _ = done.wait(timeout: .now() + 2)
        }
        let until = Date().addingTimeInterval(4)
        while process.isRunning && Date() < until { Thread.sleep(forTimeInterval: 0.05) }
        if process.isRunning { process.terminate() }
        return .terminateNow
    }

}

let app = NSApplication.shared
let delegate = GhostMicApp()
app.delegate = delegate
app.run()

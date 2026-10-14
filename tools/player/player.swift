// CoreS3 demo 的 Mac 播放与执行端。
//
// 分工（见 docs/product-alignment.md）：S3 决策，Mac 执行。这个进程就是"执行"那半边：
//   * 播**原版**本地曲目，不做任何预渲染；
//   * 每收到一帧 S3 决策就把 24 段增益**实时**应用到正在播放的音频上；
//   * 把"实际应用了什么"如实回报（应用后的 24 段、引擎状态、输出设备、实测峰值）。
//
// 为什么是独立进程而不是嵌进 Python：Python 这边没有 PyObjC，而 macOS 上唯一能做实时
// 多段 EQ 的是 AVAudioEngine + AVAudioUnitEQ。所以这里用 swiftc 编译一个纯命令行程序，
// 用 stdin/stdout 传 JSON 行——上位机（tools/host/）负责驱动它，面板负责显示。
//
// 协议（每行一个 JSON）：
//   入（stdin）: {"cmd":"play"} {"cmd":"pause"} {"cmd":"toggle"} {"cmd":"next"} {"cmd":"prev"}
//                {"cmd":"select","index":1} {"cmd":"seek","seconds":30} {"cmd":"volume","percent":80}
//                {"cmd":"eq","bands":[24 个数],"depth":2.5,"media_percent":100}
//                {"cmd":"bypass","on":true} {"cmd":"status"} {"cmd":"quit"}
//   出（stdout）: {"event":"ready",...} / {"event":"state",...} / {"event":"error",...}
//
// 口径（不许含糊）：
//   * `source_bands` 是 S3 给的建议，`applied_bands` 是 Mac 真正设进 EQ 的值，两者都报，
//     面板才能显示"设备建议 vs 实际执行"，而不是把二者混成一个数。
//   * EQ 只在 S3 报了非零建议时才动；S3 说 flat 就回到平直——"什么时候不动作"同样是决策。

import AVFoundation
import CoreAudio
import Foundation

/// 与固件 main/controller.cpp:8 的 kBandCenter 逐段一致。不一致就等于 Mac 在按另一套频段
/// 执行 S3 的决策，面板上两条曲线也没法对齐。
let bandCenters: [Float] = [
    70.7, 141.4, 244.9, 346.4, 451.7, 566.8, 696.5, 841.7,
    996.8, 1171.3, 1370.0, 1595.0, 1854.7, 2154.1, 2503.3,
    2916.3, 3414.7, 4036.1, 4829.1, 5824.1, 7020.0, 8552.8,
    10677.1, 13638.2,
]

/// 固件的硬上限是 kMaxSpeechGainDb = 6.5、demo 上限 kDemoSpeechGainDb = 8.0，用户既定规则是
/// "任何测试不得超过 +10 dB"。Mac 这一侧再放大也不许越过 10。
let maxAppliedGainDb: Float = 10.0
/// 允许的衰减范围。S3 目前只发 0…+4（song_eq）与 0…+8（人声意图），负值留给以后。
let minAppliedGainDb: Float = -12.0
/// 每个 tick 向目标推进的比例。100 ms 决策节拍下，约 5 个 tick（≈100 ms）跟上，
/// 与固件 300 ms attack 的手感一致，同时消掉逐帧跳变带来的 zipper 噪声。
let rampFraction: Double = 0.25
let tickSeconds: Double = 0.02
let stateEveryTicks = 10  // 20 ms × 10 = 每 200 ms 报一次状态

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
          let line = String(data: data, encoding: .utf8) else { return }
    print(line)
    fflush(stdout)
}

func emitError(_ message: String) {
    emit(["event": "error", "message": message])
}

/// 默认输出设备名。S3 第二页显示的路由由上位机同步，这里报的是"播放器实际写进了哪个设备"，
/// 两者必须能对得上，否则"AirPods 已播放"就只是嘴上说说。
func defaultOutputName() -> String {
    var deviceID = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID) == noErr,
        deviceID != 0 else { return "未知" }

    var nameAddr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var name: Unmanaged<CFString>?
    var nameSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(deviceID, &nameAddr, 0, nil, &nameSize, &name) == noErr,
          let value = name?.takeRetainedValue() else { return "未知" }
    return value as String
}

struct Track {
    let title: String
    let path: String
}

func loadTracks(path: String) -> [Track] {
    guard let data = FileManager.default.contents(atPath: path),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let raw = root["tracks"] as? [[String: Any]] else { return [] }
    return raw.compactMap { entry in
        guard let file = entry["path"] as? String else { return nil }
        let title = (entry["title"] as? String) ?? URL(fileURLWithPath: file).lastPathComponent
        return Track(title: title, path: file)
    }
}

/// 播放器状态。音频线程（tap）、stdin 线程、主线程都会碰，所以统一用一把锁。
/// 没有用 actor/async：AVAudioEngine 的参数更新必须发生在固定线程上，混进 Swift 并发只会
/// 让"谁在什么时候改了 EQ"更难查。
final class Player {
    private let lock = NSLock()
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let eq = AVAudioUnitEQ(numberOfBands: bandCenters.count)

    private var tracks: [Track] = []
    private var commands: [[String: Any]] = []

    private var file: AVAudioFile?
    private var fileRate: Double = 44100
    private var trackIndex = 0
    private var playing = false
    private var bypass = false

    private var depth: Double = 2.5
    private var mediaPercent: Double = 100
    private var sourceBands = [Double](repeating: 0, count: bandCenters.count)
    private var targetBands = [Double](repeating: 0, count: bandCenters.count)
    private var currentBands = [Double](repeating: 0, count: bandCenters.count)

    /// 当前片段从第几帧开始播（seek 后不为 0），那一刻的 playerTime 采样点，以及本段的总帧数。
    private var segmentStartFrame: AVAudioFramePosition = 0
    private var segmentStartPlayerSample: AVAudioFramePosition = 0
    private var segmentFrames: AVAudioFrameCount = 0
    /// 本段是否真的在走。**位置计算必须知道这件事**：节点的时间轴是累计的，片段切换后
    /// 它仍停在上一段的读数上，拿它去减锚点会算出一个远超文件末尾的位置——2026-10-14 实测
    /// 的"换曲后卡死 + 自动下一首自激"就是这个（每 0.5 秒判一次播完）。
    private var segmentActive = false
    /// 队列里是否已经排好了一段音频（scheduleCurrent 排过、还没被作废）。
    /// play() 靠它区分"接着放（暂停恢复 / seek 之后）"与"从头排一段"——不能一律重排：
    /// 重排会丢掉 seek 的目标，也会排出两段音频。
    private var segmentPrepared = false
    /// 已经建立的 player → eq 连接格式。**只有曲目格式与它不同才去重连**：重连要停引擎，
    /// 而"停引擎 → 重连 → 重启"这条路在本机实测会让引擎不再渲染（换曲后峰值冻住、
    /// 位置恒 0）。两首曲子都是 44.1 kHz，走不到那条路。
    private var connectionFormat: AVAudioFormat?
    /// 片段代次。`player.stop()` 也会触发上段的完成回调——不认代次就会收到一条假的
    /// "播完了"，于是自动下一首自激（2026-10-14 实测：曲目在 0/1 之间反复跳）。
    private var segmentGeneration = 0

    private var peakDb: Double = -120
    private var outputDevice = "未知"
    private var engineRunning = false
    private var lastError = ""

    init() {
        engine.attach(player)
        engine.attach(eq)
        for (index, band) in eq.bands.enumerated() {
            band.filterType = .parametric
            band.frequency = bandCenters[index]
            // 固件那 24 段是分析滤波器组，中心频率间隔约 1/4 倍频程；Mac 这里是参数均衡，
            // 用 0.30 倍频程让相邻段轻微交叠，避免在面板上画出一条梳状曲线。
            band.bandwidth = 0.30
            band.gain = 0
            band.bypass = false
        }
        // eq → mainMixer 固定；player → eq 一开始就用一个**固定格式**（44.1 kHz 立体声
        // Float32）连好。顺序很关键：**先连好再 start()**——引擎运行中才接节点，之后
        // player.play() 会抛 "player started when in a disconnected state"（本机实测第一次
        // play 就崩）。固定格式的好处见 connectionFormat 的注释：不再走"停引擎重连"那条路。
        if let format = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 2) {
            engine.connect(player, to: eq, format: format)
            connectionFormat = format
        }
        engine.connect(eq, to: engine.mainMixerNode, format: nil)
        // 只装 tap 不碰 mainMixerNode 的话，engine.start() 会返回成功但输入不被拉动
        // （姊妹工程记过这个坑）。顺带用它量峰值，作为"音真的在响"的证据。
        let mixer = engine.mainMixerNode
        mixer.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            guard let self else { return }
            var peak: Float = 0
            if let channels = buffer.floatChannelData {
                for channel in 0..<Int(buffer.format.channelCount) {
                    let samples = channels[channel]
                    for index in 0..<Int(buffer.frameLength) {
                        peak = max(peak, abs(samples[index]))
                    }
                }
            }
            self.lock.lock()
            self.peakDb = peak > 0 ? 20.0 * log10(Double(peak)) : -120
            self.lock.unlock()
        }
    }

    func setTracks(_ list: [Track]) {
        lock.lock(); tracks = list; lock.unlock()
    }

    func enqueue(_ command: [String: Any]) {
        lock.lock(); commands.append(command); lock.unlock()
    }

    // ---------------------------------------------------------------- 主循环

    func start() {
        // 先把第一首挂到图上，再启动引擎。engine 运行中才去接一个未连接的节点，之后
        // player.play() 会抛 "player started when in a disconnected state"——本机实测第一次
        // play 就崩，而且抛在 Objective-C 异常里，上位机只能看到进程没了。
        if !tracks.isEmpty { _ = loadFile(index: 0) }
        player.volume = Float(max(0, min(100, defaultVolumePercent)) / 100.0)
        do {
            try engine.start()
            engineRunning = true
        } catch {
            emitError("音频引擎启动失败：\(error.localizedDescription)")
            return
        }
        outputDevice = defaultOutputName()
        let timer = Timer(timeInterval: tickSeconds, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(timer, forMode: .common)
        emit([
            "event": "ready",
            "tracks": tracks.map { ["title": $0.title, "path": $0.path] },
            "band_centers": bandCenters.map { Double($0) },
            "output_device": outputDevice,
            "max_applied_gain_db": Double(maxAppliedGainDb),
        ])
    }

    private var tickCount = 0
    private var deviceCheckCount = 0

    private func tick() {
        lock.lock()
        let pending = commands
        commands.removeAll()
        lock.unlock()

        for command in pending { handle(command) }

        // 斜坡：把 current 推向 target。S3 已经做过 slew，这里只是为了不产生阶跃噪声。
        lock.lock()
        for index in 0..<bandCenters.count {
            currentBands[index] += (targetBands[index] - currentBands[index]) * rampFraction
        }
        let gains = currentBands
        let shouldBypass = bypass
        let media = mediaPercent
        lock.unlock()

        if engineRunning {
            for (index, band) in eq.bands.enumerated() {
                band.gain = Float(gains[index])
            }
            eq.bypass = shouldBypass
            // 媒体音量是 S3 的另一项决定（当前固定 100）。放在 EQ 之后，顺序与设备语义一致。
            engine.mainMixerNode.outputVolume = Float(max(0, min(100, media)) / 100.0)
        }

        tickCount += 1
        deviceCheckCount += 1
        if deviceCheckCount >= 100 {  // 每 2 秒复检一次输出设备，AirPods 断了要立刻看出来
            deviceCheckCount = 0
            let name = defaultOutputName()
            lock.lock(); outputDevice = name; lock.unlock()
        }
        if tickCount >= stateEveryTicks {
            tickCount = 0
            emitState()
        }
    }

    private func emitState() {
        lock.lock()
        let snapshot: [String: Any] = [
            "event": "state",
            "engine": engineRunning,
            "playing": playing,
            "bypass": bypass,
            "track_index": trackIndex,
            "track_title": tracks.indices.contains(trackIndex) ? tracks[trackIndex].title : "",
            "track_path": tracks.indices.contains(trackIndex) ? tracks[trackIndex].path : "",
            "position_s": positionSecondsLocked(),
            "duration_s": durationSecondsLocked(),
            "depth": depth,
            "media_percent": mediaPercent,
            "source_bands": sourceBands,
            "applied_bands": currentBands,
            "target_bands": targetBands,
            "peak_db": peakDb,
            "output_device": outputDevice,
            "sample_rate": fileRate,
            "last_error": lastError,
            "track_count": tracks.count,
        ]
        lock.unlock()
        emit(snapshot)
    }

    // ---------------------------------------------------------------- 命令

    private func handle(_ command: [String: Any]) {
        guard let name = command["cmd"] as? String else { return }
        switch name {
        case "play": play()
        case "pause": pause()
        case "toggle": isPlaying() ? pause() : play()
        case "stop": stop()
        case "next": select(index: currentIndex() + 1, autoplay: isPlaying())
        case "prev": select(index: currentIndex() - 1, autoplay: isPlaying())
        case "select":
            let index = (command["index"] as? Int) ?? 0
            select(index: index, autoplay: (command["autoplay"] as? Bool) ?? true)
        case "seek": seek(to: (command["seconds"] as? Double) ?? 0)
        case "volume":
            let percent = (command["percent"] as? Double) ?? 80
            player.volume = Float(max(0, min(100, percent)) / 100.0)
        case "eq": applyEq(command)
        case "bypass":
            lock.lock(); bypass = (command["on"] as? Bool) ?? false; lock.unlock()
        case "status":
            emitState()
        case "quit":
            emit(["event": "quitting"])
            exit(0)
        default:
            emitError("未知命令：\(name)")
        }
    }

    private func applyEq(_ command: [String: Any]) {
        let bands = (command["bands"] as? [Double]) ?? []
        let newDepth = (command["depth"] as? Double) ?? 2.5
        let media = (command["media_percent"] as? Double) ?? 100
        lock.lock()
        depth = max(0, min(4, newDepth))
        mediaPercent = media
        for index in 0..<bandCenters.count where index < bands.count {
            sourceBands[index] = bands[index]
            // Mac 的处方 = S3 的逐段形状 × 面板上的深度倍率，再夹到 +10 dB。
            // 形状来自 S3（这是"谁在做决策"的唯一变量），深度是本机为了"听得出来"设的
            // 演示参数，面板上必须写明，不能把放大量说成设备的建议。
            let scaled = bands[index] * depth
            targetBands[index] = Double(max(minAppliedGainDb, min(maxAppliedGainDb, Float(scaled))))
        }
        lock.unlock()
    }

    // ---------------------------------------------------------------- 播放控制

    private func currentIndex() -> Int {
        lock.lock(); defer { lock.unlock() }
        return trackIndex
    }

    private func isPlaying() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return playing
    }

    private func loadFile(index: Int) -> Bool {
        guard tracks.indices.contains(index) else { return false }
        let url = URL(fileURLWithPath: tracks[index].path)
        do {
            let opened = try AVAudioFile(forReading: url)
            let format = opened.processingFormat
            lock.lock()
            file = opened
            fileRate = format.sampleRate
            trackIndex = index
            // 新曲目一律从头开始：上一段的时间轴属于上一首，留着它会让 positionSeconds()
            // 算出错误的起点（换曲卡死的根因之一）。
            segmentStartFrame = 0
            segmentStartPlayerSample = 0
            segmentFrames = 0
            segmentActive = false
            let current = connectionFormat
            let needsReconnect = current == nil
                || current!.sampleRate != format.sampleRate
                || current!.channelCount != format.channelCount
            if needsReconnect { connectionFormat = format }
            lock.unlock()

            // 只有格式真的不同才重连。**换曲时尽量别走这条路**：它要停引擎，而本机实测
            // "停引擎 → 重连 → 重启"之后引擎不再渲染（换曲后峰值冻住、位置恒 0、
            // 面板显示在播却没声）。两首曲子都是 44.1 kHz，正常换曲走不到这里。
            if needsReconnect {
                let wasRunning = engine.isRunning
                if wasRunning { engine.stop() }
                engine.disconnectNodeOutput(player)
                engine.connect(player, to: eq, format: format)
                if wasRunning {
                    do {
                        try engine.start()
                    } catch {
                        lock.lock(); lastError = "换曲后引擎重启失败：\(error.localizedDescription)"; lock.unlock()
                        emitError(lastError)
                        return false
                    }
                }
            }
            return true
        } catch {
            lock.lock(); lastError = "打不开曲目：\(tracks[index].path)（\(error.localizedDescription)）"; lock.unlock()
            emitError(lastError)
            return false
        }
    }

    private func scheduleCurrent(fromSeconds: Double) {
        lock.lock()
        guard let opened = file else { lock.unlock(); return }
        let rate = opened.processingFormat.sampleRate
        let totalFrames = opened.length
        let duration = rate > 0 ? Double(totalFrames) / rate : 0
        // 位置必须夹在 [0, 时长) 内。**换曲后这一条是必须的**：`playerTime(forNodeTime:)`
        // 可能还停在上一段的读数上，于是 positionSeconds() 会算出"已到文件末尾"，
        // remaining = 0 → 什么都没排进队列 → 面板显示"播放中"却永远 0 秒、没有声音。
        // 2026-10-14 实测撞到（第二首卡在 pos=0 / 峰值 -120 dB）。
        var start = fromSeconds.isFinite ? fromSeconds : 0
        start = max(0, min(start, max(0, duration - 0.5)))
        var startFrame = AVAudioFramePosition(start * rate)
        var remaining = AVAudioFrameCount(max(0, totalFrames - startFrame))
        if remaining == 0 {  // 兜底：任何情况下都不允许"排一段空音频"
            startFrame = 0
            remaining = AVAudioFrameCount(totalFrames)
        }
        segmentStartFrame = startFrame
        segmentStartPlayerSample = 0
        segmentFrames = remaining
        segmentActive = false   // 真正开始走是在 play() 里
        segmentPrepared = true  // 队列里已经有这一段了
        segmentGeneration += 1
        let generation = segmentGeneration
        lock.unlock()
        guard remaining > 0 else { return }
        // 播完自动下一首：演示时音乐不该在曲末停住，而"下一首"本身就是一次状态变化。
        // 回调必须核对代次：`player.stop()` 也会触发上段的完成回调，不认代次就会收到一条
        // 假的"播完了"，于是自动下一首自激（实测曲目在 0/1 之间反复跳、且没有声音）。
        player.scheduleSegment(opened, startingFrame: startFrame, frameCount: remaining,
                               at: nil, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.segmentFinished(generation)
        }
    }

    private func segmentFinished(_ generation: Int) {
        lock.lock()
        let isCurrent = generation == segmentGeneration
        let stillPlaying = playing
        lock.unlock()
        guard isCurrent, stillPlaying else { return }
        enqueue(["cmd": "next", "autoplay": true])
    }

    private func play() {
        let index = currentIndex()
        lock.lock()
        let hasFile = file != nil
        let prepared = segmentPrepared
        lock.unlock()
        if !hasFile && !loadFile(index: index) { return }
        if !player.isPlaying {
            if prepared {
                // 暂停后继续，或 seek 之后接着放：节点会从暂停处继续走，所以把"片段起点"
                // 挪到当前位置，否则位置读数会退回片段开头。**不能在这里再 schedule 一次**：
                // 那会把 seek 的目标丢掉（实测 seek 到 60 s 后从 0 开始），也会排出两段音频。
                let resumeAt = positionSeconds()
                lock.lock()
                segmentStartFrame = AVAudioFramePosition(resumeAt * (fileRate > 0 ? fileRate : 44100))
                lock.unlock()
            } else {
                scheduleCurrent(fromSeconds: 0)
            }
            player.play()
            lock.lock()
            if let renderTime = player.lastRenderTime,
               let nodeTime = player.playerTime(forNodeTime: renderTime) {
                segmentStartPlayerSample = nodeTime.sampleTime
            }
            segmentActive = true
            playing = true
            lock.unlock()
        }
        emitState()
    }

    private func pause() {
        // 先把当前位置取下来再暂停：暂停后 `playerTime(forNodeTime:)` 会返回 nil，
        // 位置只剩"片段起点"可读，暂停期间的读数就会退回 0（实测：暂停在 2.5 s，
        // 面板显示 0:00，继续播放也从 0 记起）。
        let frozen = positionSeconds()
        player.pause()
        lock.lock()
        let rate = fileRate > 0 ? fileRate : 44100
        segmentStartFrame = AVAudioFramePosition(frozen * rate)
        segmentStartPlayerSample = 0
        segmentActive = false   // 暂停期间位置就冻在这个 base 上
        playing = false
        lock.unlock()
        emitState()
    }

    /// 停止播放。**顺序是最要紧的部分**：`player.stop()` 会（同步）触发上一段的完成回调，
    /// 所以必须先把 `playing=false` 与代次作废做掉。反过来的话，回调进来时看到的还是
    /// "这一段有效且在播" → 塞一条 `next` → 换曲又 stop → 又一条 next，自激换曲、
    /// 全程没有声音。2026-10-14 实测：状态事件刷到 800+ 条、曲目在 0/1 之间反复跳。
    private func invalidateSegmentLocked() {
        playing = false
        segmentStartFrame = 0
        segmentStartPlayerSample = 0
        segmentFrames = 0
        segmentActive = false
        segmentPrepared = false
        segmentGeneration += 1
    }

    private func stop() {
        lock.lock(); invalidateSegmentLocked(); lock.unlock()
        player.stop()
        emitState()
    }

    private func select(index: Int, autoplay: Bool) {
        lock.lock(); let count = tracks.count; lock.unlock()
        guard count > 0 else { return }
        let wasPlaying = isPlaying()
        let wrapped = ((index % count) + count) % count
        // 先作废再 stop，顺序不能换（理由见 invalidateSegmentLocked）。
        // 另外先落 playing=false 再换曲：loadFile 失败时会提前 return，旧写法把 playing
        // 留成 true，面板于是显示"在播但没出声"（silent），而真实原因是这一首根本打不开。
        lock.lock(); invalidateSegmentLocked(); lock.unlock()
        player.stop()
        guard loadFile(index: wrapped) else { emitState(); return }
        if autoplay || wasPlaying { play() } else { emitState() }
    }

    private func seek(to seconds: Double) {
        let wasPlaying = isPlaying()
        lock.lock(); invalidateSegmentLocked(); lock.unlock()
        player.stop()
        scheduleCurrent(fromSeconds: max(0, seconds))
        if wasPlaying { play() }
        emitState()
    }

    // ---------------------------------------------------------------- 读数

    private func positionSecondsLocked() -> Double {
        let rate = fileRate > 0 ? fileRate : 44100
        let duration = durationSecondsLocked()
        let base = Double(segmentStartFrame) / rate
        // 本段没在走（刚换曲、刚 seek、刚停止）时位置就是片段起点。**不能拿累计的
        // playerTime 去减锚点**：它可能还停在上一段的读数上，差值会把位置推到文件末尾，
        // 于是"每 0.5 秒播完一次" → 自动下一首自激。2026-10-14 实测撞到过。
        guard segmentActive,
              let nodeTime = player.lastRenderTime,
              let playerTime = player.playerTime(forNodeTime: nodeTime) else {
            return max(0, min(base, duration))
        }
        let played = max(0, playerTime.sampleTime - segmentStartPlayerSample)
        // 差值同样夹在本段长度内：任何残留累计都不允许越过本段。
        let limit = segmentFrames > 0 ? Double(segmentFrames) / rate : duration
        return max(0, min(base + min(Double(played) / rate, limit), duration))
    }

    private func positionSeconds() -> Double {
        lock.lock(); defer { lock.unlock() }
        return positionSecondsLocked()
    }

    private func durationSecondsLocked() -> Double {
        guard let opened = file else { return 0 }
        let rate = opened.processingFormat.sampleRate
        return rate > 0 ? Double(opened.length) / rate : 0
    }
}

// ---------------------------------------------------------------- 入口

/// 默认音量。**刻意调到 30%**：这是演示桌上第一次出声，100% 会把人吓一跳（用户明确要求
/// "默认不要放 100%"）。要更响在面板上调，或 `--volume` 覆盖。
var defaultVolumePercent: Double = 30

let arguments = CommandLine.arguments
var tracksPath = URL(fileURLWithPath: arguments[0]).deletingLastPathComponent()
    .appendingPathComponent("tracks.json").path
var index = 1
while index < arguments.count {
    switch arguments[index] {
    case "--tracks": index += 1; if index < arguments.count { tracksPath = arguments[index] }
    case "--volume":
        index += 1
        if index < arguments.count { defaultVolumePercent = Double(arguments[index]) ?? defaultVolumePercent }
    default: break
    }
    index += 1
}

let loadedTracks = loadTracks(path: tracksPath)
let app = Player()
app.setTracks(loadedTracks)

// stdin 读取放在后台线程：主线程要跑 RunLoop 驱动音频与斜坡，不能阻塞在 readLine 上。
let reader = Thread {
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { continue }
        guard let data = trimmed.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            emitError("不是合法 JSON：\(trimmed.prefix(80))")
            continue
        }
        app.enqueue(object)
    }
    // stdin 关闭（上位机退出）就跟着退出——否则会留下一个占着 AirPods 输出的孤儿进程。
    app.enqueue(["cmd": "quit"])
}
reader.stackSize = 1 << 20
reader.start()

app.start()
RunLoop.main.run()

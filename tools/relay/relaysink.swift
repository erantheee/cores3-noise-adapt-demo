// 实时中继的播放端：把 S3 麦克风的 16 kHz 单声道 PCM 送到**指定**的输出设备，
// 并且把它真正排了多少队**报出来**。
//
// 为什么不用 ffplay（这个程序存在的全部理由）
// ------------------------------------------------
// 1. ffplay 不给反压，也不暴露播放位置。实测（tools/probe_player_buffer.py）：按实时速率喂它
//    5 秒音频，0.94 秒就全吞下去了（吸收比 5.3×）。它内部囤了多少、声音晚了多久，从外面
//    看不出来。上位机只能拿"我们写进去多少字节"当播放进度，于是任何一次卡顿造成的积压都变成
//    **永久且不可见**的延迟——没有任何东西能把它拉回来。这正是"改了参数，延迟只动一点点"、
//    "差到 1 秒"那类症状的机制。
// 2. 它播到"当前默认输出"。默认输出在 AirPods 与内置扬声器之间跳一次，蓝牙流就被拆掉重建
//    一次（实测冷启动 ≈1 秒），而它一声不吭。用户戴着耳机听不到声音，会去查延迟和缓冲，
//    真正的原因却是"声音压根没进耳机"。
// 3. SDL 的回调缓冲约 sample_rate/30（16 kHz 时 ≈64 ms），命令行改不动。
//
// 本程序把三件事同时做对：
//   * **钉住设备**：用 AUHAL 的 kAudioOutputUnitProperty_CurrentDevice 把引擎输出钉在名字
//     匹配的设备上。它**不改系统默认输出**（那是用户的设置），只是让这条流去哪由我们说了算。
//   * **有界延迟**：交给播放器的帧数（scheduled）减去播放头真正播到的帧数（playhead）就是
//     排队深度。深度超过目标就丢掉来不及时播的那一块：宁可断一下，也不要越听越晚。
//     这两个数都是真实计数，不是估算。
//   * **报出来**：每 0.5 秒一行 JSON（stdout），含排队深度、丢弃量、驱动侧输出延迟。
//     面板因此能显示"现在到底晚了多少"，而不是靠猜。
//
// 协议（stdout，每行一个 JSON）：
//   {"type":"relay","state":"running","device":"AirPods Pro","pinned":true,"target_ms":60.0,
//    "queued_ms":21.3,"dropped_ms":0.0,"received_s":12.3,"starved":0,"clipped":0,
//    "buffer_frames":512,"driver_ms":170.7,"est_out_ms":192.0,"sample_rate":16000}
//   {"type":"relay","state":"stopped"|"device_changed"|"configuration_change"|"error", ...}
//
// 用法：
//   sh tools/relay/build.sh
//   tools/relay/relaysink --device "AirPods Pro" --target-ms 60 < pcm.s16le
//
// 边界（不许含糊）：`driver_ms` 是**驱动自己报的**输出延迟（回调缓冲 + 安全偏移 + 设备延迟 +
// 流延迟），它是这条路径的物理下限，不是我们优化出来的。`est_out_ms` = 排队深度 + driver_ms，
// 不含设备侧的打包延迟与 USB 传输——那两段由上位机侧另算。

import AVFoundation
import CoreAudio
import Darwin
import Foundation

// MARK: - 命令行

struct Options {
    var rate: Double = 16000
    var channels: Int = 1
    /// 输出设备名（子串匹配，不区分大小写）。空表示"跟随当前默认输出"。
    var deviceName: String = ""
    /// 允许的最大排队深度。这是整条链路的延迟上限，不是优化参数。
    var targetMs: Double = 60.0
    /// 麦克风电平刻意保守（板上约 −40 dB RMS），这里做补偿增益。
    var gainDb: Double = 18.0
    /// 增益之后的峰值上限。0.95 与原来 ffplay 那条 alimiter=limit=0.95 对齐。
    var limit: Double = 0.95
    var statsHz: Double = 2.0
}

func parseOptions(_ arguments: [String]) -> Options {
    var options = Options()
    var index = 0
    while index < arguments.count {
        let key = arguments[index]
        func value() -> String? {
            index += 1
            return index < arguments.count ? arguments[index] : nil
        }
        switch key {
        case "--rate": if let text = value(), let parsed = Double(text) { options.rate = parsed }
        case "--channels": if let text = value(), let parsed = Int(text) { options.channels = parsed }
        case "--device": if let text = value() { options.deviceName = text }
        case "--target-ms": if let text = value(), let parsed = Double(text) { options.targetMs = parsed }
        case "--gain-db": if let text = value(), let parsed = Double(text) { options.gainDb = parsed }
        case "--limit": if let text = value(), let parsed = Double(text) { options.limit = parsed }
        case "--stats-hz": if let text = value(), let parsed = Double(text) { options.statsHz = parsed }
        case "--help", "-h":
            print("""
            relaysink --rate 16000 [--device 名字] [--target-ms 60] [--gain-db 18] [--limit 0.95]
            从 stdin 读 s16le PCM，播到指定输出设备；每行 JSON 报一次真实排队深度。
            """)
            exit(0)
        default:
            FileHandle.standardError.write("未知参数：\(key)\n".data(using: .utf8)!)
            exit(2)
        }
        index += 1
    }
    return options
}

// MARK: - CoreAudio 只读工具

func uint32Property(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector,
                    _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func float64Property(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector,
                     _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> Float64? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var value: Float64 = 0
    var size = UInt32(MemoryLayout<Float64>.size)
    let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func deviceName(_ device: AudioObjectID) -> String {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let status = withUnsafeMutablePointer(to: &name) {
        AudioObjectGetPropertyData(device, &address, 0, nil, &size, $0)
    }
    return status == noErr ? (name as String) : "(未知设备)"
}

func allDevices() -> [AudioDeviceID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &address, 0, nil, &size) == noErr else { return [] }
    let count = Int(size) / MemoryLayout<AudioDeviceID>.size
    var devices = [AudioDeviceID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                     0, nil, &size, &devices) == noErr else { return [] }
    return devices
}

func hasOutput(_ device: AudioDeviceID) -> Bool {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr else {
        return false
    }
    return size > 0
}

func currentDefaultOutput() -> AudioDeviceID {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var device = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                     0, nil, &size, &device) == noErr else { return 0 }
    return device
}

func findOutput(named needle: String) -> AudioDeviceID? {
    guard !needle.isEmpty else { return nil }
    return allDevices().first {
        hasOutput($0) && deviceName($0).localizedCaseInsensitiveContains(needle)
    }
}

/// 驱动侧输出延迟的分解。这是"Mac 把一帧交给驱动"到"耳机振膜动"的部分，
/// 也就是这条路径**不可压缩**的下限。
struct DriverLatency {
    var device = "?"
    var sampleRate: Double = 0
    var bufferFrames: UInt32 = 0
    var safetyFrames: UInt32 = 0
    var deviceFrames: UInt32 = 0
    var streamFrames: UInt32 = 0

    var totalMs: Double {
        guard sampleRate > 0 else { return 0 }
        let frames = Double(bufferFrames + safetyFrames + deviceFrames + streamFrames)
        return frames / sampleRate * 1000.0
    }
}

func driverLatency(_ device: AudioDeviceID) -> DriverLatency {
    var info = DriverLatency()
    guard device != 0 else { return info }
    info.device = deviceName(device)
    info.sampleRate = float64Property(device, kAudioDevicePropertyNominalSampleRate) ?? 0
    info.bufferFrames = uint32Property(device, kAudioDevicePropertyBufferFrameSize,
                                       kAudioObjectPropertyScopeOutput) ?? 0
    info.safetyFrames = uint32Property(device, kAudioDevicePropertySafetyOffset,
                                       kAudioObjectPropertyScopeOutput) ?? 0
    info.deviceFrames = uint32Property(device, kAudioDevicePropertyLatency,
                                       kAudioObjectPropertyScopeOutput) ?? 0
    var streamAddress = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeOutput,
        mElement: kAudioObjectPropertyElementMain)
    var streamBytes: UInt32 = 0
    if AudioObjectGetPropertyDataSize(device, &streamAddress, 0, nil, &streamBytes) == noErr,
       streamBytes >= UInt32(MemoryLayout<AudioStreamID>.size) {
        var one = AudioStreamID(0)
        var size = UInt32(MemoryLayout<AudioStreamID>.size)
        if AudioObjectGetPropertyData(device, &streamAddress, 0, nil, &size, &one) == noErr {
            info.streamFrames = uint32Property(one, kAudioStreamPropertyLatency) ?? 0
        }
    }
    return info
}

// MARK: - 输出

func emit(_ fields: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(fields),
          let data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

// MARK: - 中继播放

final class RelaySink {
    private let options: Options
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let pcmFormat: AVAudioFormat
    private let targetFrames: Int64
    private let gain: Double
    private let clipLimit: Double

    /// 计数在读取线程、统计线程与播放器的完成回调之间共享；渲染回调本身不碰它们，
    /// 所以一把普通锁就够——关键路径上没有持锁等待。
    private let lock = NSLock()
    private var scheduledFrames: Int64 = 0
    /// 播放器回报"已经播完"的帧数。这是排队深度的另一半，也是旧实现缺的那个回执。
    private var playedFrames: Int64 = 0
    private var droppedFrames: Int64 = 0
    /// 启动时排空的旧音频。**与 droppedFrames 分开报**：那一个是"来不及播、被上限挡掉的"，
    /// 这一个是"开始播放前就已经过期的"。混成一个数会让人以为中继一直在丢音。
    private var drainedFrames: Int64 = 0
    private var receivedFrames: Int64 = 0
    private var clippedSamples: Int64 = 0
    private var starvedTicks = 0
    private var lastReceivedAtTick: Int64 = 0

    private(set) var deviceID: AudioDeviceID = 0
    private(set) var pinned = false
    private var running = true
    private var restartRequested = false

    init(options: Options) {
        self.options = options
        self.gain = pow(10.0, options.gainDb / 20.0)
        self.clipLimit = min(max(options.limit, 0.05), 1.0)
        self.targetFrames = Int64(options.rate * options.targetMs / 1000.0)
        guard let format = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: options.rate,
                                         channels: AVAudioChannelCount(options.channels),
                                         interleaved: true) else {
            emit(["type": "relay", "state": "error", "message": "无法建立 16 kHz 单声道格式"])
            exit(2)
        }
        self.pcmFormat = format
    }

    // ---------------------------------------------------------------- 设备

    /// 选设备：优先名字匹配，匹配不到就退回当前默认输出，并**如实报出没钉住**。
    /// 不替用户改系统默认输出：那是用户的设置，擅自改会让"没声音"变成更难查的问题。
    private func resolveDevice() {
        if let matched = findOutput(named: options.deviceName) {
            deviceID = matched
            pinned = true
            return
        }
        deviceID = currentDefaultOutput()
        pinned = false
        if !options.deviceName.isEmpty, deviceID != 0 {
            emit(["type": "relay", "state": "device_missing",
                  "requested": options.deviceName, "device": deviceName(deviceID)])
        }
    }

    /// 把引擎的输出单元钉在 deviceID 上。这是 AUHAL 的私有属性用法，不改变系统默认输出。
    private func pinDevice() -> Bool {
        guard deviceID != 0, let unit = engine.outputNode.audioUnit else { return false }
        var device = deviceID
        let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
                                          kAudioUnitScope_Global, 0, &device,
                                          UInt32(MemoryLayout<AudioDeviceID>.size))
        return status == noErr
    }

    // ---------------------------------------------------------------- 启动

    func start() -> Bool {
        resolveDevice()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: pcmFormat)
        engine.mainMixerNode.outputVolume = 1.0

        let pinnedOK = pinDevice()
        do {
            engine.prepare()
            try engine.start()
        } catch {
            emit(["type": "relay", "state": "error",
                  "message": "音频引擎启动失败：\(error.localizedDescription)"])
            return false
        }
        player.play()

        NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            // 只置位，不在这里动引擎：改动统一在统计线程上做，避免和读取线程抢同一把锁。
            self?.lock.lock()
            self?.restartRequested = true
            self?.lock.unlock()
        }

        let info = driverLatency(deviceID)
        emit(["type": "relay", "state": "running", "event": "started",
              "device": info.device, "pinned": pinnedOK && pinned,
              "requested_device": options.deviceName,
              "driver_ms": info.totalMs, "buffer_frames": info.bufferFrames,
              "sample_rate": options.rate, "target_ms": options.targetMs])
        return true
    }

    /// 引擎被系统拆掉之后重建。蓝牙掉线/默认输出跳变都会走这里。
    /// 重建后播放头从 0 重新开始，所以**必须**重新对齐计数，否则深度会算出负数。
    private func restart() {
        engine.stop()
        resolveDevice()
        _ = pinDevice()
        do {
            engine.prepare()
            try engine.start()
            player.play()
            lock.lock()
            scheduledFrames = 0
            playedFrames = 0  // 新引擎的播放位置从 0 起，不对齐就算出负深度
            lastReceivedAtTick = receivedFrames
            restartRequested = false
            lock.unlock()
            let info = driverLatency(deviceID)
            emit(["type": "relay", "state": "restarted", "device": info.device,
                  "pinned": pinned, "driver_ms": info.totalMs])
        } catch {
            emit(["type": "relay", "state": "error",
                  "message": "重建音频引擎失败：\(error.localizedDescription)"])
        }
    }

    // ---------------------------------------------------------------- 计数

    /// 播放头位置，**只用于诊断**。
    ///
    /// 它不参与丢包判断，原因是量测过它不可靠：`playerTime(forNodeTime:)` 的 sampleTime
    /// 时间基在转换器插入后并不等于我们连接的 16 kHz，而渲染是按块推进的，两次采样之间会
    /// 跳一大步。第一版用它算深度，结果是对着实时喂进来的音频也丢掉了 22%（见 commit 记录）。
    /// **深度必须用"已交付 − 已完成回调"，那是逐块的确定计数，不是采样。**
    private func playheadSeconds() -> Double? {
        guard player.isPlaying, let render = player.lastRenderTime,
              let time = player.playerTime(forNodeTime: render) else { return nil }
        let rate = time.sampleRate > 0 ? time.sampleRate : options.rate
        return Double(time.sampleTime) / rate
    }

    /// 排队深度 = 已交付播放器的帧 − 播放器回报"已经播完"的帧。
    ///
    /// 这是整个修复的核心：深度来自**播放器自己的回执**，不是我们"写进去多少"的假设。
    /// 旧实现（ffplay）没有这个回执，只能假设写入即按实时播放，于是任何一次卡顿造成的积压
    /// 都永久留在里面且看不见。
    private func queuedFrames() -> Int64 {
        lock.lock()
        let scheduled = scheduledFrames
        let played = playedFrames
        lock.unlock()
        return max(0, scheduled - played)
    }

    // ---------------------------------------------------------------- 读取

    /// 等到引擎真的开始渲染为止。
    ///
    /// 启动这段时间管道里会积下音频——那是"我们还没来得及播"的旧声音，不是现在的声音。
    /// 旧实现（ffplay）会把它全部播出来，于是**从第一声起就晚了这几百毫秒**，而且再也追不回来。
    private func waitForRender(timeout: Double = 1.5) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if player.isPlaying, player.lastRenderTime != nil { return }
            Thread.sleep(forTimeInterval: 0.01)
        }
    }

    /// 排空开始播放之前积在管道里的旧音频，让**第一声就是最新的声音**。
    /// 返回丢掉的字节数（计入 dropped_ms，面板上看得到）。
    private func drainStaleInput() -> Int {
        let flags = fcntl(STDIN_FILENO, F_GETFL, 0)
        guard flags >= 0 else { return 0 }
        _ = fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK)
        var raw = [UInt8](repeating: 0, count: 8192)
        var drained = 0
        while true {
            let count = read(STDIN_FILENO, &raw, raw.count)
            if count <= 0 { break }
            drained += count
        }
        _ = fcntl(STDIN_FILENO, F_SETFL, flags)
        // 丢掉的必须是偶数个字节：丢掉奇数个会让之后每个 16 位样本的两个字节错位，
        // 听上去就是噪声。多读一个字节把它对齐。
        if drained % 2 == 1 {
            var one = [UInt8](repeating: 0, count: 1)
            if read(STDIN_FILENO, &one, 1) == 1 { drained += 1 }
        }
        if drained > 0 {
            lock.lock()
            drainedFrames += Int64(drained / 2)
            lock.unlock()
            emit(["type": "relay", "state": "drained_stale", "bytes": drained])
        }
        return drained
    }

    func run() -> Int32 {
        waitForRender()
        _ = drainStaleInput()
        let statsInterval = 1.0 / max(options.statsHz, 0.2)
        let statsQueue = DispatchQueue(label: "relay.stats")
        statsQueue.async { [weak self] in
            while let self, self.running {
                Thread.sleep(forTimeInterval: statsInterval)
                self.publishStats()
            }
        }

        var raw = [UInt8](repeating: 0, count: 8192)
        var pending = [UInt8]()
        while running {
            let count = read(STDIN_FILENO, &raw, raw.count)
            if count <= 0 { break }  // EOF（上位机退出）或错误
            pending.append(contentsOf: raw[0..<count])
            let usable = pending.count - (pending.count % 2)
            if usable == 0 { continue }
            deliver(pending[0..<usable])
            pending.removeFirst(usable)
        }
        shutdown()
        return 0
    }

    private func deliver(_ bytes: ArraySlice<UInt8>) {
        let frames = bytes.count / 2
        guard frames > 0 else { return }
        lock.lock()
        receivedFrames += Int64(frames)
        lock.unlock()

        // 丢的是**这一块**，不是排队里最老的那块：已经交给播放器的音频收不回来，
        // 能做的只是"不再往前排"。直播场景下这就是正确的取舍——宁可漏掉刚刚这 20 ms，
        // 也不要让之后每一句都晚一点。阈值刻意留出一整块余量，避免把渲染块的抖动
        // 误判成积压（第一版就是那样，对着实时输入白丢了 22%）。
        let depth = queuedFrames()
        if depth > targetFrames {
            lock.lock()
            droppedFrames += Int64(frames)
            lock.unlock()
            return
        }

        guard let buffer = AVAudioPCMBuffer(pcmFormat: pcmFormat,
                                            frameCapacity: AVAudioFrameCount(frames)),
              let channel = buffer.int16ChannelData?[0] else { return }
        buffer.frameLength = AVAudioFrameCount(frames)

        let peak = clipLimit * 32767.0
        var clipped: Int64 = 0
        // 逐字节拼 s16le，不 bindMemory 到 Int16：切片起点只有 1 字节对齐保证，按 2 字节
        // 解释是未定义行为。设备与 Mac 都是小端，这里按小端还原。
        bytes.withUnsafeBufferPointer { source in
            for index in 0..<frames {
                let low = UInt16(source[index * 2])
                let high = UInt16(source[index * 2 + 1])
                let value = Int16(bitPattern: low | (high << 8))
                let scaled = Double(value) * gain
                if scaled > peak || scaled < -peak {
                    clipped += 1
                    channel[index] = scaled > 0 ? Int16(peak) : Int16(-peak)
                } else {
                    channel[index] = Int16(scaled)
                }
            }
        }
        if clipped > 0 {
            lock.lock()
            clippedSamples += clipped
            lock.unlock()
        }

        // `.dataPlayedBack` 是播放器给出的**逐块回执**：这一块真的播完了才计数。
        // 它就是排队深度的另一半。回调线程上只加一个计数器，不做别的。
        player.scheduleBuffer(buffer, at: nil, options: []) { [weak self] in
            guard let self else { return }
            self.lock.lock()
            self.playedFrames += Int64(frames)
            self.lock.unlock()
        }
        lock.lock()
        scheduledFrames += Int64(frames)
        lock.unlock()
    }

    // ---------------------------------------------------------------- 统计

    private func publishStats() {
        lock.lock()
        let needsRestart = restartRequested
        lock.unlock()
        if needsRestart { restart() }

        let depth = queuedFrames()
        let info = driverLatency(deviceID)
        lock.lock()
        let scheduled = scheduledFrames
        let played = playedFrames
        let dropped = droppedFrames
        let drained = drainedFrames
        let received = receivedFrames
        let clipped = clippedSamples
        let starved = received == lastReceivedAtTick
        lastReceivedAtTick = received
        if starved { starvedTicks += 1 }
        let starvedTotal = starvedTicks
        lock.unlock()

        let queuedMs = Double(depth) / options.rate * 1000.0
        var fields: [String: Any] = [
            "type": "relay", "state": "running",
            "device": info.device, "pinned": pinned,
            "queued_ms": queuedMs,
            "queued_frames": depth,
            "dropped_ms": Double(dropped) / options.rate * 1000.0,
            "drained_ms": Double(drained) / options.rate * 1000.0,
            "delivered_ms": Double(scheduled) / options.rate * 1000.0,
            "played_ms": Double(played) / options.rate * 1000.0,
            "received_s": Double(received) / options.rate,
            "starved_ticks": starvedTotal,
            "clipped_samples": clipped,
            "buffer_frames": info.bufferFrames,
            "driver_ms": info.totalMs,
            "est_out_ms": queuedMs + info.totalMs,
            "target_ms": options.targetMs,
            "sample_rate": options.rate,
        ]
        // 播放头只作诊断：它与"已播完"对不上时说明引擎的报告不可信，这一条必须看得见。
        if let playhead = playheadSeconds() {
            fields["playhead_s"] = playhead
        }
        emit(fields)
    }

    private func shutdown() {
        running = false
        player.stop()
        engine.stop()
        lock.lock()
        let dropped = droppedFrames
        let drained = drainedFrames
        let received = receivedFrames
        let clipped = clippedSamples
        let starvedTotal = starvedTicks
        lock.unlock()
        emit(["type": "relay", "state": "stopped",
              "received_s": Double(received) / options.rate,
              "dropped_ms": Double(dropped) / options.rate * 1000.0,
              "drained_ms": Double(drained) / options.rate * 1000.0,
              "clipped_samples": clipped,
              "starved_ticks": starvedTotal])
    }
}

// MARK: - 入口

let options = parseOptions(Array(CommandLine.arguments.dropFirst()))
signal(SIGPIPE, SIG_IGN)

let sink = RelaySink(options: options)
guard sink.start() else { exit(3) }
exit(sink.run())

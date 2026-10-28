// CoreS3 demo 的第三方播放器执行端（原型）。
//
// 目的：QQ 音乐（或其它 bundle）播放时，S3 仍然做决策；但音频不再由
// tools/player 自己解码，而是用 Core Audio process tap 截获目标 App 的输出，
// 过 24 段 AVAudioUnitEQ，再送到当前默认输出（耳机）。
//
// 协议（stdin 每行一个 JSON，和 tools/player 的 eq 命令同形）：
//   {"cmd":"eq","bands":[24],"depth":2.5}
//   {"cmd":"flat"}
//   {"cmd":"bypass","on":true}
//   {"cmd":"status"}
//   {"cmd":"quit"}
// stdout：{"event":"ready",...} / {"event":"state",...} / {"event":"error",...}
//
// 边界：
//   * 只 tap 目标 App（默认 com.tencent.QQMusicMac），不 tap 整个系统，避免把自己的输出再抓回来；
//   * muteBehavior = mutedWhenTapped：原声在读取期间静音，EQ 副本从耳机输出；
//   * tap/engine 启动失败、退出、stdin EOF 都要销毁 tap，让原声恢复（fail-open）；
//   * 这是原型：默认输出设备切换、蓝牙 HFP、流媒体 DRM、延迟还没有完整验收。

import Accelerate
import AppKit
import AVFoundation
import CoreAudio
import Darwin
import Foundation
import os

let bandCenters: [Float] = [
    70.7, 141.4, 244.9, 346.4, 451.7, 566.8, 696.5, 841.7,
    996.8, 1171.3, 1370.0, 1595.0, 1854.7, 2154.1, 2503.3,
    2916.3, 3414.7, 4036.1, 4829.1, 5824.1, 7020.0, 8552.8,
    10677.1, 13638.2,
]

/// demo 基线两档（dB，直接加在最终 EQ 上，不乘深度）：
///   * basic（基础）＝**Harman 形态轻量版 + 内容自适应高频**（2026-10-22 晚五次校准）：
///     形态参考 Harman target（中低频轻降、2–3 kHz 轻峰），幅度收敛到"轻量耳机校正"
///     级别（±1.3 dB 内）。**低频 70/141 Hz 为 0**：OWS 低频泄漏已由 S3 端侧内容先验
///     补偿（×深度），Mac 侧只做形态、不叠低频。
///     **高频段由内容自适应接管**（方案 A，用户拍板）：4 kHz+ 不写死负值，
///     而是按 ContentMeter.harshness 动态收放——人声清唱(≈0)保留清透，
///     密集器乐/电子(≈1)收高频压底噪（4–7 kHz −1.5、8.5 kHz+ −2.5 × harshness）。
///     原则：**端侧为主**（N/D/模式路由全在 S3），Mac 基线只做轻量形态与内容特性校正。
///   * aggressive（激进）＝2026-10-22 晚用户拍板"demo 要明显"的 ×2 版。
/// 切换走 stdin：{"cmd":"mode","value":"basic"|"aggressive"}。
/// **掩蔽（D41）保留**：背景人声时 S3 仍切 EXTERNAL_VOICE_INTENT（全段 +1、
/// 1–4 kHz 到 +4），基础档实测叠加后约 +6.7 dB/段，不顶爆。
/// S3 的 24 段自适应建议仍按 depth 叠加，场景过渡由 slew 平滑；
/// 用户口径：允许削波、不装限幅器、不允许加延迟。
let basicBaseDb: [Float] = [
    0.0, 0.0, 0.0, -0.6, -0.6, -0.4, -0.2, -0.1,
    0.0, 0.4, 0.7, 0.9, 1.1, 1.2, 1.3, 1.3,
    1.1, 0.9, 0.7, 0.5, 0.4, 0.4, 0.2, 0.0,
]
let aggressiveBaseDb: [Float] = [
    0.0, 0.0, 0.0, -3.0, -3.0, -2.0, -1.0, -1.0,
    2.0, 3.0, 4.0, 5.0, 6.0, 6.0, 6.0, 6.0,
    5.0, 4.0, 4.0, 4.0, 5.0, 6.0, 6.0, 5.0,
]
/// 最终每段增益的上下界。用户明确不关心削波，所以放宽到 ±24 dB；
/// 仍保留一个界是为了避免极端输入把 EQ 参数推到非法值。
let maxAppliedGainDb: Float = 24.0
//: 正增益**总**上限（2026-10-28 现场"人声毛刺"）：base 曲线 + 内容曲线 + D48 掩蔽
//: 叠加后，presence 段实测 applied 峰值到 5.7–6.4 dB，而且是 0.3 oct 窄带。
//: 掩蔽要靠"事件期间的时间门控"，不该靠无上限的静态抬升——静态抬到 6 dB 的代价
//: 就是人声毛刺。这里给正增益一个总预算；负增益（去糊/削减）不受限。
let maxBoostDb: Float = 5.0

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
          var line = String(data: data, encoding: .utf8) else { return }
    line += "\n"
    FileHandle.standardOutput.write(Data(line.utf8))
}

func emitError(_ message: String) {
    emit(["event": "error", "message": message])
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func clamp(_ value: Float, _ low: Float, _ high: Float) -> Float {
    return max(low, min(high, value))
}

func defaultOutputUID() -> CFString? {
    var deviceAddress = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var deviceID = AudioDeviceID(kAudioObjectUnknown)
    var deviceSize = UInt32(MemoryLayout<AudioDeviceID>.size)
    let deviceStatus = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &deviceAddress, 0, nil,
        &deviceSize, &deviceID)
    guard deviceStatus == noErr, deviceID != kAudioObjectUnknown else { return nil }

    var uidAddress = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var deviceUID: Unmanaged<CFString>?
    var uidSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let uidStatus = AudioObjectGetPropertyData(
        deviceID, &uidAddress, 0, nil, &uidSize, &deviceUID)
    guard uidStatus == noErr, let deviceUID else { return nil }
    return deviceUID.takeRetainedValue()
}

// MARK: - SPSC ring buffer（producer = tap IOProc，consumer = AVAudioSourceNode）

final class AudioRingBuffer: @unchecked Sendable {
    let channelCount: Int
    let capacity: Int
    private var channels: [UnsafeMutableBufferPointer<Float>]

    private struct State {
        var head = 0
        var tail = 0
        var fill = 0
    }
    private let lock = OSAllocatedUnfairLock(initialState: State())

    init(channelCount: Int, capacity: Int) {
        precondition(channelCount > 0)
        precondition(capacity > 0)
        self.channelCount = channelCount
        self.capacity = capacity
        self.channels = (0..<channelCount).map { _ in
            let buffer = UnsafeMutableBufferPointer<Float>.allocate(capacity: capacity)
            buffer.initialize(repeating: 0)
            return buffer
        }
    }

    deinit {
        for buffer in channels { buffer.deallocate() }
    }

    @discardableResult
    func writeInterleaved(_ source: UnsafePointer<Float>, frames: Int) -> Int {
        guard frames > 0 else { return 0 }
        return lock.withLockUnchecked { state in
            let available = capacity - state.fill
            let toWrite = min(frames, available)
            guard toWrite > 0 else { return 0 }
            let first = min(toWrite, capacity - state.head)
            let second = toWrite - first
            for channel in 0..<channelCount {
                let dest = channels[channel].baseAddress!
                for index in 0..<first {
                    dest[state.head + index] = source[index * channelCount + channel]
                }
                for index in 0..<second {
                    dest[index] = source[(first + index) * channelCount + channel]
                }
            }
            state.head = (state.head + toWrite) % capacity
            state.fill += toWrite
            return toWrite
        }
    }

    @discardableResult
    func writePlanar(_ sources: UnsafePointer<UnsafePointer<Float>>,
                     sourceChannels: Int, frames: Int) -> Int {
        guard frames > 0 else { return 0 }
        return lock.withLockUnchecked { state in
            let available = capacity - state.fill
            let toWrite = min(frames, available)
            guard toWrite > 0 else { return 0 }
            let first = min(toWrite, capacity - state.head)
            let second = toWrite - first
            let writable = min(channelCount, sourceChannels)
            for channel in 0..<writable {
                let src = sources[channel]
                let dest = channels[channel].baseAddress!
                dest.advanced(by: state.head).update(from: src, count: first)
                if second > 0 {
                    dest.update(from: src.advanced(by: first), count: second)
                }
            }
            state.head = (state.head + toWrite) % capacity
            state.fill += toWrite
            return toWrite
        }
    }

    @discardableResult
    func read(into destinations: UnsafePointer<UnsafeMutablePointer<Float>>,
              channelCount destinationChannels: Int, frames: Int) -> Int {
        guard frames > 0 else { return 0 }
        return lock.withLockUnchecked { state in
            let toRead = min(frames, state.fill)
            guard toRead > 0 else { return 0 }
            let first = min(toRead, capacity - state.tail)
            let second = toRead - first
            let readable = min(channelCount, destinationChannels)
            for channel in 0..<readable {
                let dest = destinations[channel]
                let source = channels[channel].baseAddress!
                dest.update(from: source.advanced(by: state.tail), count: first)
                if second > 0 {
                    dest.advanced(by: first).update(from: source, count: second)
                }
            }
            state.tail = (state.tail + toRead) % capacity
            state.fill -= toRead
            return toRead
        }
    }

    var fillCount: Int {
        lock.withLockUnchecked { $0.fill }
    }
}

// MARK: - Meter

final class Meter: @unchecked Sendable {
    private struct State {
        var callbacks = 0
        var frames = 0
        var sumSquares = 0.0
        var peak = 0.0
    }
    private let lock = OSAllocatedUnfairLock(initialState: State())

    func add(sumSquares: Double, frames: Int, peak: Double) {
        lock.withLockUnchecked { state in
            state.callbacks += 1
            state.frames += frames
            state.sumSquares += sumSquares
            state.peak = max(state.peak, peak)
        }
    }

    func snapshot() -> (callbacks: Int, frames: Int, rms: Double, peak: Double) {
        lock.withLockUnchecked { state in
            let rms = state.frames > 0 ? sqrt(state.sumSquares / Double(state.frames)) : 0
            return (state.callbacks, state.frames, rms, state.peak)
        }
    }
}

/// EQ **之后**的输出计量（2026-10-28）：确认"毛刺"是不是削波时，必须量 EQ 之后的
/// 信号；tap 的 `Meter` 在 EQ 之前，EQ 抬了多少它看不到。`take()` 读走并清零窗口，
/// 供 50 ms 控制拍做 headroom 预算；累计量留给状态上报。
final class OutMeter: @unchecked Sendable {
    private struct State {
        var peak = 0.0
        var clipFrames = 0
        var frames = 0
        var totalPeak = 0.0
        var totalClipFrames = 0
        var totalFrames = 0
    }
    private let lock = OSAllocatedUnfairLock(initialState: State())

    func add(_ buffer: AVAudioPCMBuffer) {
        guard let channels = buffer.floatChannelData else { return }
        let frames = Int(buffer.frameLength)
        let channelCount = Int(buffer.format.channelCount)
        guard frames > 0, channelCount > 0 else { return }
        var peak = 0.0
        var clip = 0
        for channel in 0..<channelCount {
            let samples = channels[channel]
            for index in 0..<frames {
                let magnitude = abs(Double(samples[index]))
                if magnitude > peak { peak = magnitude }
                if magnitude >= 1.0 { clip += 1 }
            }
        }
        let samples = frames * channelCount
        lock.withLockUnchecked { state in
            state.peak = max(state.peak, peak)
            state.clipFrames += clip
            state.frames += samples
            state.totalPeak = max(state.totalPeak, peak)
            state.totalClipFrames += clip
            state.totalFrames += samples
        }
    }

    /// 读走当前窗口（峰值、削波样本数）并清零。
    func take() -> (peak: Double, clipFrames: Int) {
        lock.withLockUnchecked { state in
            let result = (state.peak, state.clipFrames)
            state.peak = 0
            state.clipFrames = 0
            state.frames = 0
            return result
        }
    }

    func totals() -> (peak: Double, clipFrames: Int, frames: Int) {
        lock.withLockUnchecked { ($0.totalPeak, $0.totalClipFrames, $0.totalFrames) }
    }
}

final class ConsumedMeter: @unchecked Sendable {
    private struct State { var frames = 0; var callbacks = 0 }
    private let lock = OSAllocatedUnfairLock(initialState: State())
    func add(frames: Int) { lock.withLockUnchecked { s in s.frames += frames; s.callbacks += 1 } }
    func snapshot() -> (frames: Int, callbacks: Int) { lock.withLockUnchecked { ($0.frames, $0.callbacks) } }
}

/// 音乐内容特征（Mac 侧，tap 到的音乐流）：区分"稀疏人声"与"密集器乐/电子"。
///   * harshness = 0 → 人声清唱/稀疏内容（保留高频，清透）；
///   * harshness = 1 → 密集器乐/电子（收高频，压底噪/嘶声）。
/// 特征：高频(4 kHz+)能量占比 highRatio 与 1–4 kHz 人声带占比 midRatio（FFT 1024 点）。
/// 只在 tap 回调线程喂样本；快照供 applyEq / 状态上报读。
/// 背景（2026-10-22 晚，用户"底噪 vs 人声清透"两难后拍板方案 A）：
/// 底噪/嘶声与齿音/空气同在 4 kHz+，固定 EQ 两全不了；按内容动态收放高频是折中。
final class ContentMeter: @unchecked Sendable {
    static let fftSize = 1024

    private struct State {
        var harshness: Float = 0
        var highRatio: Float = 0
        var midRatio: Float = 0
        var fedFrames = 0
        var analyses = 0
        var windowRms: Float = 0
        var fill = 0
    }
    private let lock = OSAllocatedUnfairLock(initialState: State())

    var sampleRate: Double = 48000

    private var window: [Float]
    private var windowFill = 0
    private let setup: vDSP_DFT_Setup
    private let hann: [Float]
    private var windowed: [Float]
    private var imagIn: [Float]
    private var realOut: [Float]
    private var imagOut: [Float]
    private var mags: [Float]
    private var smoothed: Float = 0

    init() {
        let size = ContentMeter.fftSize
        window = [Float](repeating: 0, count: size)
        windowed = [Float](repeating: 0, count: size)
        imagIn = [Float](repeating: 0, count: size)
        realOut = [Float](repeating: 0, count: size)
        imagOut = [Float](repeating: 0, count: size)
        mags = [Float](repeating: 0, count: size / 2)
        hann = (0..<size).map {
            Float(0.5 - 0.5 * cos(2.0 * Double.pi * Double($0) / Double(size - 1)))
        }
        setup = vDSP_DFT_zop_CreateSetup(nil, vDSP_Length(size), vDSP_DFT_Direction.FORWARD)!
    }

    /// 喂单声道样本。interleaved 时 stride = 声道数（取 ch0）；planar 时 stride = 1。
    func add(samples: UnsafePointer<Float>, count: Int, stride: Int) {
        lock.withLockUnchecked { $0.fedFrames += count }
        var index = 0
        while index < count {
            window[windowFill] = samples[index * stride]
            windowFill += 1
            index += 1
            if windowFill == Self.fftSize {
                analyze()
                windowFill = 0
            }
        }
    }

    private func analyze() {
        // 计数提前到 FFT 之前：分析跑没跑与"有没有信号"是两回事，静音窗口也要计数。
        lock.withLockUnchecked { $0.analyses += 1 }
        // 用显式 buffer 作用域包住整个 FFT：DSPSplitComplex 存的是裸指针，
        // 隐式临时指针在 init 返回后就失效，后面 Execute 会踩悬垂指针。
        windowed.withUnsafeMutableBufferPointer { winBuf in
            imagIn.withUnsafeMutableBufferPointer { imgInBuf in
                realOut.withUnsafeMutableBufferPointer { realBuf in
                    imagOut.withUnsafeMutableBufferPointer { imgBuf in
                        vDSP_vmul(window, 1, hann, 1, winBuf.baseAddress!, 1,
                                  vDSP_Length(ContentMeter.fftSize))
                        imgInBuf.initialize(repeating: 0)
                        let inSplit = DSPSplitComplex(realp: winBuf.baseAddress!,
                                                      imagp: imgInBuf.baseAddress!)
                        var outSplit = DSPSplitComplex(realp: realBuf.baseAddress!,
                                                       imagp: imgBuf.baseAddress!)
                        vDSP_DFT_Execute(setup, inSplit.realp, inSplit.imagp,
                                         outSplit.realp, outSplit.imagp)
                        mags.withUnsafeMutableBufferPointer { magBuf in
                            vDSP_zvmags(&outSplit, 1, magBuf.baseAddress!, 1,
                                        vDSP_Length(ContentMeter.fftSize / 2))
                        }
                    }
                }
            }
        }

        var windowEnergy: Float = 0
        for sample in window { windowEnergy += sample * sample }
        let windowRms = sqrt(windowEnergy / Float(ContentMeter.fftSize))

        let bin1k = max(1, Int(Double(ContentMeter.fftSize) * 1000 / sampleRate))
        let bin4k = max(bin1k + 1, Int(Double(ContentMeter.fftSize) * 4000 / sampleRate))
        var total: Float = 0
        var high: Float = 0
        var mid: Float = 0
        let bins = ContentMeter.fftSize / 2
        if bin4k < bins {
            for bin in 1..<bins {
                let magnitude = mags[bin]
                total += magnitude
                if bin >= bin4k { high += magnitude }
                else if bin >= bin1k { mid += magnitude }
            }
        }
        guard total > 1e-9 else { return }
        let highRatio = high / total
        let midRatio = mid / total

        // 亮度项：高频能量占比越高越该收；人声项：1–4 kHz 占比高（稀疏人声）时少收。
        let bright = min(max((highRatio - 0.10) / 0.30, 0), 1)
        let voiced = min(max(midRatio / 0.45, 0), 1)
        let target = bright * (1 - 0.65 * voiced)

        // 平滑：上升快（亮起来赶紧收）、回落慢（句间不抖），约 0.1 s / 0.3 s。
        let alpha: Float = target > smoothed ? 0.30 : 0.10
        smoothed += (target - smoothed) * alpha

        lock.withLockUnchecked { state in
            state.harshness = smoothed
            state.highRatio = highRatio
            state.midRatio = midRatio
            state.windowRms = windowRms
            state.fill = windowFill
        }
    }

    func snapshot() -> (harshness: Float, highRatio: Float, midRatio: Float, fedFrames: Int, analyses: Int, windowRms: Float, fill: Int) {
        lock.withLockUnchecked { ($0.harshness, $0.highRatio, $0.midRatio, $0.fedFrames, $0.analyses, $0.windowRms, $0.fill) }
    }
}

// MARK: - Process tap

final class TapCapture {
    let sampleRate: Double
    let channels: Int
    let format: AVAudioFormat
    let ring: AudioRingBuffer
    let meter = Meter()
    let content = ContentMeter()
    private let processID: AudioObjectID
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioDeviceID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var clientData: UnsafeMutableRawPointer?
    private var running = false

    init(bundleID: String) throws {
        guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
            throw NSError(domain: "SystemEq", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "App 未运行：\(bundleID)"])
        }
        let pid = app.processIdentifier

        var pidValue = pid
        var processAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var audioProcessID = AudioObjectID(kAudioObjectUnknown)
        var processSize = UInt32(MemoryLayout<AudioObjectID>.size)
        let processStatus = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &processAddress,
            UInt32(MemoryLayout<pid_t>.size), &pidValue, &processSize, &audioProcessID)
        guard processStatus == noErr, audioProcessID != kAudioObjectUnknown else {
            throw NSError(domain: "SystemEq", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "找不到 QQ 音乐的 process object"])
        }
        self.processID = audioProcessID

        let description = CATapDescription(stereoMixdownOfProcesses: [audioProcessID])
        description.name = "cores3-systemeq"
        description.isPrivate = true
        let muteRaw = Int(ProcessInfo.processInfo.environment["SYSTEMEQ_MUTE"] ?? "2") ?? 2
        FileHandle.standardError.write(Data("systemeq mute_behavior=\(muteRaw)\n".utf8))
        description.muteBehavior = CATapMuteBehavior(rawValue: muteRaw)!  // 2 = mutedWhenTapped
        var createdTap = AudioObjectID(kAudioObjectUnknown)
        let tapStatus = AudioHardwareCreateProcessTap(description, &createdTap)
        guard tapStatus == noErr, createdTap != kAudioObjectUnknown else {
            throw NSError(domain: "SystemEq", code: 4,
                          userInfo: [NSLocalizedDescriptionKey: "创建 process tap 失败：\(tapStatus)"])
        }
        self.tapID = createdTap

        var formatAddress = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var asbd = AudioStreamBasicDescription()
        var asbdSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let formatStatus = AudioObjectGetPropertyData(createdTap, &formatAddress, 0, nil,
                                                      &asbdSize, &asbd)
        guard formatStatus == noErr, asbd.mSampleRate > 0, asbd.mChannelsPerFrame > 0 else {
            AudioHardwareDestroyProcessTap(createdTap)
            throw NSError(domain: "SystemEq", code: 5,
                          userInfo: [NSLocalizedDescriptionKey: "读 tap 格式失败：\(formatStatus)"])
        }
        self.sampleRate = asbd.mSampleRate
        self.channels = Int(asbd.mChannelsPerFrame)
        guard let audioFormat = AVAudioFormat(
            standardFormatWithSampleRate: asbd.mSampleRate,
            channels: AVAudioChannelCount(asbd.mChannelsPerFrame)) else {
            AudioHardwareDestroyProcessTap(createdTap)
            throw NSError(domain: "SystemEq", code: 6,
                          userInfo: [NSLocalizedDescriptionKey: "无法构造 AVAudioFormat"])
        }
        self.format = audioFormat
        self.ring = AudioRingBuffer(channelCount: self.channels,
                                    capacity: max(4096, Int(asbd.mSampleRate * 2)))
        content.sampleRate = asbd.mSampleRate
    }

    deinit {
        stop()
    }

    func start() throws {
        guard !running else { return }
        guard let tapUID = readTapUID() else {
            throw NSError(domain: "SystemEq", code: 7,
                          userInfo: [NSLocalizedDescriptionKey: "读 tap UID 失败"])
        }

        // 默认空 subdevices + master=0：实测这样 IOProc 才按 tap 的 48 kHz 走，
        // 把默认输出 UID 塞进 master 会让聚合多跑一路时钟（producer 到 ~1.5×，ring 溢出）。
        let useOutputMaster = ProcessInfo.processInfo.environment["SYSTEMEQ_MASTER"] == "output"
        var outputUID: CFString?
        if useOutputMaster {
            guard let uid = defaultOutputUID() else {
                throw NSError(domain: "SystemEq", code: 8,
                              userInfo: [NSLocalizedDescriptionKey: "找不到默认输出设备"])
            }
            outputUID = uid
        }

        let aggregateUID = "cores3-systemeq.\(UUID().uuidString)"
        var aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceNameKey: "cores3-systemeq",
            kAudioAggregateDeviceUIDKey: aggregateUID,
            kAudioAggregateDeviceSubDeviceListKey: [] as CFArray,
            kAudioAggregateDeviceMasterSubDeviceKey: 0,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
        ]
        if useOutputMaster, let outputUID {
            aggregateDescription[kAudioAggregateDeviceSubDeviceListKey] = [outputUID] as CFArray
            aggregateDescription[kAudioAggregateDeviceMasterSubDeviceKey] = outputUID
        }

        var createdAggregate = AudioDeviceID(kAudioObjectUnknown)
        let aggregateStatus = AudioHardwareCreateAggregateDevice(
            aggregateDescription as CFDictionary, &createdAggregate)
        guard aggregateStatus == noErr, createdAggregate != kAudioObjectUnknown else {
            throw NSError(domain: "SystemEq", code: 9,
                          userInfo: [NSLocalizedDescriptionKey: "创建聚合设备失败：\(aggregateStatus)"])
        }
        self.aggregateID = createdAggregate

        var tapList = [tapUID] as CFArray
        var tapListAddress = AudioObjectPropertyAddress(
            mSelector: kAudioAggregateDevicePropertyTapList,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        let tapListStatus = withUnsafePointer(to: &tapList) { pointer in
            AudioObjectSetPropertyData(createdAggregate, &tapListAddress, 0, nil,
                                       UInt32(MemoryLayout<CFArray>.stride), pointer)
        }
        guard tapListStatus == noErr else {
            stop()
            throw NSError(domain: "SystemEq", code: 10,
                          userInfo: [NSLocalizedDescriptionKey: "设置 tap list 失败：\(tapListStatus)"])
        }

        // 与只读探针保持同一条已跑通的调用序：建 IOProc 前先读一次 composition，
        // 否则 AudioDeviceStart 可能返回 1852797029 ('nope')。
        var compositionAddress = AudioObjectPropertyAddress(
            mSelector: kAudioAggregateDevicePropertyComposition,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var composition: Unmanaged<CFDictionary>?
        var compositionSize = UInt32(MemoryLayout<Unmanaged<CFDictionary>?>.size)
        _ = AudioObjectGetPropertyData(
            createdAggregate, &compositionAddress, 0, nil, &compositionSize, &composition)

        let client = Unmanaged.passUnretained(self).toOpaque()
        var createdIOProc: AudioDeviceIOProcID?
        let ioStatus = AudioDeviceCreateIOProcID(
            createdAggregate, systemEqIOProc, client, &createdIOProc)
        guard ioStatus == noErr, let createdIOProc else {
            stop()
            throw NSError(domain: "SystemEq", code: 11,
                          userInfo: [NSLocalizedDescriptionKey: "创建 IOProc 失败：\(ioStatus)"])
        }
        self.clientData = client
        self.ioProcID = createdIOProc

        let startStatus = AudioDeviceStart(createdAggregate, createdIOProc)
        guard startStatus == noErr else {
            stop()
            throw NSError(domain: "SystemEq", code: 12,
                          userInfo: [NSLocalizedDescriptionKey: "AudioDeviceStart 失败：\(startStatus)"])
        }
        running = true
    }

    func stop() {
        running = false
        if aggregateID != kAudioObjectUnknown, let ioProcID {
            _ = AudioDeviceStop(aggregateID, ioProcID)
            _ = AudioDeviceDestroyIOProcID(aggregateID, ioProcID)
        }
        ioProcID = nil
        clientData = nil
        if aggregateID != kAudioObjectUnknown {
            _ = AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioDeviceID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            _ = AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    private func readTapUID() -> CFString? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var uid: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(tapID, &address, 0, nil, &size, &uid)
        guard status == noErr, let uid else { return nil }
        return uid.takeRetainedValue()
    }

    fileprivate func handleIOProc(_ inputData: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: inputData))
        guard buffers.count > 0 else { return }

        if channels > 1 && buffers.count == 1, let data = buffers[0].mData {
            let totalFloats = Int(buffers[0].mDataByteSize) / MemoryLayout<Float>.size
            let frames = totalFloats / channels
            guard frames > 0 else { return }
            let samples = data.assumingMemoryBound(to: Float.self)
            _ = ring.writeInterleaved(samples, frames: frames)

            var sum = 0.0
            var peak = 0.0
            for index in 0..<totalFloats {
                let value = Double(samples[index])
                sum += value * value
                peak = max(peak, abs(value))
            }
            meter.add(sumSquares: sum, frames: frames, peak: peak)
            content.add(samples: samples, count: frames, stride: channels)
        } else {
            let frames = Int(buffers[0].mDataByteSize) / MemoryLayout<Float>.size
            guard frames > 0 else { return }
            var sum = 0.0
            var peak = 0.0
            withUnsafeTemporaryAllocation(
                of: UnsafePointer<Float>.self, capacity: buffers.count
            ) { pointers in
                var count = 0
                for buffer in buffers {
                    guard let data = buffer.mData else { break }
                    pointers[count] = UnsafePointer(data.assumingMemoryBound(to: Float.self))
                    count += 1
                    let samples = data.assumingMemoryBound(to: Float.self)
                    for index in 0..<frames {
                        let value = Double(samples[index])
                        sum += value * value
                        peak = max(peak, abs(value))
                    }
                }
                guard count > 0, let base = pointers.baseAddress else { return }
                _ = ring.writePlanar(base, sourceChannels: count, frames: frames)
                content.add(samples: base[0], count: frames, stride: 1)
            }
            meter.add(sumSquares: sum, frames: frames, peak: peak)
        }
    }
}

private let systemEqIOProc: AudioDeviceIOProc = {
    _, _, inInputData, _, _, _, inClientData -> OSStatus in
    guard let inClientData else { return noErr }
    let capture = Unmanaged<TapCapture>.fromOpaque(inClientData).takeUnretainedValue()
    capture.handleIOProc(inInputData)
    return noErr
}

// MARK: - State / control

final class EqState: @unchecked Sendable {
    private struct State {
        var sourceBands = [Float](repeating: 0, count: 24)
        var depth: Float = 2.5
        var bypass = false
        var mediaPercent: Float = 100
        var mode = "aggressive"
        //: 基线缩放系数（0 = 完全平直）。S3 报 FLAT（安静/对话）时 host 下发 0，
        //: 保证"安静时完全没有 EQ 动作"（2026-10-23 用户口径）。
        var baseGain: Float = 1.0
    }
    private let lock = OSAllocatedUnfairLock(initialState: State())

    func update(bands: [Float], depth: Float, mediaPercent: Float, baseGain: Float = 1.0) {
        lock.withLockUnchecked { state in
            for index in 0..<24 where index < bands.count {
                state.sourceBands[index] = bands[index]
            }
            state.depth = clamp(depth, 0, 4)
            state.mediaPercent = clamp(mediaPercent, 0, 100)
            state.baseGain = clamp(baseGain, 0, 1)
        }
    }

    func setMode(_ mode: String) {
        lock.withLockUnchecked { $0.mode = mode }
    }

    func flat() {
        lock.withLockUnchecked { state in
            for index in 0..<24 { state.sourceBands[index] = 0 }
        }
    }

    func setBypass(_ bypass: Bool) {
        lock.withLockUnchecked { $0.bypass = bypass }
    }

    func toggleBypass() -> Bool {
        lock.withLockUnchecked { state in
            state.bypass.toggle()
            return state.bypass
        }
    }

    func snapshot() -> (bands: [Float], depth: Float, bypass: Bool, mediaPercent: Float, mode: String, baseGain: Float) {
        lock.withLockUnchecked { state in
            return (state.sourceBands, state.depth, state.bypass, state.mediaPercent,
                    state.mode, state.baseGain)
        }
    }
}

// MARK: - Main

struct Options {
    var app = "com.tencent.QQMusicMac"
    var bands = [Float](repeating: 0, count: 24)
    var depth: Float = 2.5
    var seconds: Double = 0
    var abSeconds: Double = 0
    var reportSeconds: Double = 1
    var demo = false
}

func parseOptions() -> Options {
    var options = Options()
    var index = 1
    let arguments = CommandLine.arguments
    while index < arguments.count {
        let argument = arguments[index]
        switch argument {
        case "--app":
            index += 1
            if index < arguments.count { options.app = arguments[index] }
        case "--demo":
            options.demo = true
        case "--bands":
            index += 1
            if index < arguments.count {
                let values = arguments[index].split(separator: ",").compactMap { Float($0) }
                for band in 0..<24 where band < values.count {
                    options.bands[band] = values[band]
                }
            }
        case "--depth":
            index += 1
            if index < arguments.count { options.depth = Float(arguments[index]) ?? options.depth }
        case "--seconds":
            index += 1
            if index < arguments.count { options.seconds = Double(arguments[index]) ?? 0 }
        case "--ab":
            index += 1
            if index < arguments.count { options.abSeconds = Double(arguments[index]) ?? 0 }
        case "--report":
            index += 1
            if index < arguments.count { options.reportSeconds = Double(arguments[index]) ?? 1 }
        default:
            break
        }
        index += 1
    }
    return options
}

let options = parseOptions()
let state = EqState()
let consumed = ConsumedMeter()
state.update(bands: options.bands, depth: options.depth, mediaPercent: 100)

let capture: TapCapture
do {
    capture = try TapCapture(bundleID: options.app)
} catch {
    emitError("初始化失败：\(error.localizedDescription)")
    exit(2)
}

// 先启动 tap，再创建 AVAudioEngine。实测：如果先建/准备 engine，
// 聚合设备的 AudioDeviceStart 会返回 1852797029 ('nope')、coreaudiod 报
// "no master engine info"；先起 tap 再起 engine 则正常。
do {
    try capture.start()
} catch {
    emitError("tap 启动失败：\(error.localizedDescription)")
    exit(3)
}

let sourceFormat = capture.format
let engine = AVAudioEngine()
let outputFormat = engine.outputNode.outputFormat(forBus: 0)
// 采样率不匹配（tap 48 kHz vs 蓝牙设备 44.1 kHz 等）：消费端重采样。
// 2026-10-23 实测：不加转换时消费按输出设备时钟拉 48 kHz 样本，慢 8.8%，ring 几分钟积满
// 丢帧 = 抖动。转换器 + 预分配缓冲都在 render 线程外建好（实时回调内不分配内存）。
let needResample = fabs(sourceFormat.sampleRate - outputFormat.sampleRate) > 1.0
// sourceNode/engine 一律用 non-interleaved：render 回调每声道一块，出来直接进/出
// planar ring（AudioRingBuffer 就是 planar 的），省掉交错重排。
let nodeFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32,
    sampleRate: needResample ? outputFormat.sampleRate : sourceFormat.sampleRate,
    channels: sourceFormat.channelCount,
    interleaved: false)!

var resampler: AVAudioConverter? = nil
var resampleIn: AVAudioPCMBuffer? = nil
var resampleOut: AVAudioPCMBuffer? = nil
// 转换器输出格式与设备采样率一致但用 non-interleaved（写 outRing 用 planar）。
let converterOutFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32, sampleRate: nodeFormat.sampleRate,
    channels: sourceFormat.channelCount, interleaved: false)!
// 输出侧 ring：吸收 AVAudioConverter 每次输出帧数 ±1 的抖动（少了填静音/多了丢弃
// 都会造成跳帧毛刺，音量放大更明显——2026-10-23 实测）。render 从 outRing 读，
// 不足时先转换补足，水位稳定后长期平衡。
let resampleOutRing = AudioRingBuffer(channelCount: Int(sourceFormat.channelCount), capacity: 8192)
// 输入读取的滑动累加器：每次转换需要 chunkOut×(48/44.1) 帧输入，直接取整会累积丢帧。
var resampleInputAccum: Double = 0
if needResample {
    let inFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: sourceFormat.sampleRate,
        channels: sourceFormat.channelCount, interleaved: false)!
    resampler = AVAudioConverter(from: inFormat, to: converterOutFormat)
    resampleIn = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: 8192)
    resampleOut = AVAudioPCMBuffer(pcmFormat: converterOutFormat, frameCapacity: 4096)
}

let sourceNode = AVAudioSourceNode(format: nodeFormat) { _, _, frameCount, audioBufferList -> OSStatus in
    let buffers = UnsafeMutableAudioBufferListPointer(audioBufferList)
    guard buffers.count > 0 else { return noErr }
    let frames = Int(frameCount)

    if let resampler, let inBuffer = resampleIn, let outBuffer = resampleOut {
        let ratio = sourceFormat.sampleRate / converterOutFormat.sampleRate
        let inChannels = Int(sourceFormat.channelCount)
        // 保证 outRing 至少够本次请求：不足就转换补足（最多 4 轮防卡死）。
        var rounds = 0
        while resampleOutRing.fillCount < frames && rounds < 4 {
            rounds += 1
            // 每次转换产出约 frames 帧；输入按比例累加取整（滑动累加器，长期零损耗）。
            resampleInputAccum += Double(frames) * ratio
            let needIn = min(Int(inBuffer.frameCapacity), Int(resampleInputAccum.rounded(.down)))
            let read = withUnsafeTemporaryAllocation(
                of: UnsafeMutablePointer<Float>.self, capacity: inChannels
            ) { pointers -> Int in
                for channel in 0..<inChannels {
                    pointers[channel] = inBuffer.floatChannelData![channel]
                }
                return capture.ring.read(into: pointers.baseAddress!,
                                         channelCount: inChannels, frames: needIn)
            }
            resampleInputAccum -= Double(read)
            if read == 0 { break }
            inBuffer.frameLength = AVAudioFrameCount(read)
            if read < needIn {
                for channel in 0..<inChannels {
                    memset(inBuffer.floatChannelData![channel].advanced(by: read), 0,
                           (needIn - read) * MemoryLayout<Float>.size)
                }
            }
            var convertError: NSError? = nil
            var supplied = false
            outBuffer.frameLength = 0
            // 关键：input block 只能一次给数据（重复 .haveData 会让转换器重复消费同一段
            // 输入 → 输出错乱）。第一次 .haveData，之后 .noDataNow。
            resampler.convert(to: outBuffer, error: &convertError) { _, status in
                if supplied {
                    status.pointee = .noDataNow
                    return nil
                }
                supplied = true
                status.pointee = .haveData
                return inBuffer
            }
            let got = Int(outBuffer.frameLength)
            if got == 0 { break }
            if let planes = outBuffer.floatChannelData {
                withUnsafeTemporaryAllocation(of: UnsafePointer<Float>.self,
                                              capacity: inChannels) { pointers in
                    for channel in 0..<inChannels {
                        pointers[channel] = UnsafePointer(planes[channel])
                    }
                    _ = resampleOutRing.writePlanar(pointers.baseAddress!,
                                                    sourceChannels: inChannels, frames: got)
                }
            }
        }
        let readOut = withUnsafeTemporaryAllocation(
            of: UnsafeMutablePointer<Float>.self, capacity: buffers.count
        ) { pointers -> Int in
            var count = 0
            for buffer in buffers {
                guard let data = buffer.mData else { break }
                pointers[count] = data.assumingMemoryBound(to: Float.self)
                count += 1
            }
            guard count > 0, let base = pointers.baseAddress else { return 0 }
            return resampleOutRing.read(into: base, channelCount: count, frames: frames)
        }
        consumed.add(frames: readOut)
        if readOut < frames {
            for buffer in buffers {
                guard let data = buffer.mData else { continue }
                let pointer = data.assumingMemoryBound(to: Float.self)
                memset(pointer.advanced(by: readOut), 0,
                       (frames - readOut) * MemoryLayout<Float>.size)
            }
        }
        return noErr
    }

    let read = withUnsafeTemporaryAllocation(
        of: UnsafeMutablePointer<Float>.self, capacity: buffers.count
    ) { pointers -> Int in
        var count = 0
        for buffer in buffers {
            guard let data = buffer.mData else { break }
            pointers[count] = data.assumingMemoryBound(to: Float.self)
            count += 1
        }
        guard count > 0, let base = pointers.baseAddress else { return 0 }
        return capture.ring.read(into: base, channelCount: count, frames: frames)
    }
    consumed.add(frames: read)
    if read < frames {
        let missing = frames - read
        for buffer in buffers {
            guard let data = buffer.mData else { continue }
            let pointer = data.assumingMemoryBound(to: Float.self)
            memset(pointer.advanced(by: read), 0, missing * MemoryLayout<Float>.size)
        }
    }
    return noErr
}

let eq = AVAudioUnitEQ(numberOfBands: 24)
for (index, center) in bandCenters.enumerated() {
    let band = eq.bands[index]
    band.filterType = .parametric
    band.frequency = center
    // 0.30 oct：24 段是 1/3 oct 间隔，0.30 oct 的带宽让相邻段几乎不重叠（每个频点约
    // 1 个滤波器生效）。**不要随手加宽**（2026-10-28 实测）：AVAudioUnitEQ 的段是并联
    // 叠加的，加到 0.55 oct 后相邻段重叠，等效增益翻倍——输出峰值从 1.8 冲到 4.0、
    // 4.6% 采样削波，"毛刺"更重。要平滑音色应该动增益预算/滤波类型，不是加宽带宽。
    band.bandwidth = 0.30
    band.gain = 0
    band.bypass = false
}

engine.attach(sourceNode)
engine.attach(eq)
engine.connect(sourceNode, to: eq, format: nodeFormat)
engine.connect(eq, to: engine.mainMixerNode, format: nodeFormat)
engine.mainMixerNode.outputVolume = 1.0
engine.prepare()
// 输出（EQ 之后）计量：削波判据 + headroom 预算（2026-10-28）。
let outMeter = OutMeter()
engine.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, _ in
    outMeter.add(buffer)
}

var currentBands = [Float](repeating: 0, count: 24)
var appliedBands = [Float](repeating: 0, count: 24)
var cleanedUp = false
var trimmedHarshness: Float = 0
//: 输出削波时对**正增益动态部分**的收缩量（0…2 dB，50 ms 一拍调整）。
var outHeadroomTrimDb: Float = 0
var smoothedBaseGain: Float = 1.0
var lastEnvExposure: Float = 0

func applyEq(_ state: EqState) {
    let snapshot = state.snapshot()
    let content = capture.content.snapshot()
    // 高频收放（仅基础档）由两个维度取 max：
    //   * 内容 harshness（方案 A）：0(稀疏人声)=保留高频清透，1(密集器乐/电子)=收高频压底噪；
    //   * 环境曝光 envExposure（2026-10-23 用户口径）：环境音进入时也要压 EQ——S3 端侧
    //     处方幅度就是"环境吵不吵"的信号（安静≈0、越吵越大），处方活动度 max|band|/2.5
    //     超过阈值就把高频压下去（环境高频嘶声与音乐高频叠加只会更吵）。
    // 4–7 kHz 轻收、8.5 kHz+ 重收；两维各自平滑后取 max，再走一步 50 ms 节拍平滑。
    var maxAbsBand: Float = 0
    for value in snapshot.bands { maxAbsBand = max(maxAbsBand, abs(value)) }
    let envExposure = clamp(maxAbsBand / 2.5, 0, 1)
    lastEnvExposure = envExposure
    let trimTarget = max(content.harshness, envExposure)
    trimmedHarshness += (trimTarget - trimmedHarshness) * 0.2
    // 基线平滑：S3 切 FLAT（安静/对话）时 baseGain 0↔1 平滑过渡，避免硬切。
    smoothedBaseGain += (snapshot.baseGain - smoothedBaseGain) * 0.15
    let baseGain = smoothedBaseGain
    for index in 0..<24 {
        currentBands[index] += (snapshot.bands[index] - currentBands[index]) * 0.25
        let base: Float
        if !options.demo {
            base = 0
        } else if snapshot.mode == "basic" {
            base = basicBaseDb[index] * baseGain
        } else {
            base = aggressiveBaseDb[index] * baseGain
        }
        var extra: Float = 0
        if options.demo && snapshot.mode == "basic" {
            if index >= 17 && index <= 20 {
                extra = -1.5 * trimmedHarshness * baseGain
            } else if index >= 21 {
                extra = -2.5 * trimmedHarshness * baseGain
            }
        }
        // 正增益的"动态部分"在输出削波时收缩（负增益不动：削减永远安全）。
        var dynamic = currentBands[index] * snapshot.depth
        if dynamic > 0 && outHeadroomTrimDb > 0 {
            dynamic = max(0, dynamic - outHeadroomTrimDb)
        }
        // 正增益总预算：base + extra + 动态 三者相加后封顶 maxBoostDb（负方向仍到
        // -maxAppliedGainDb）。这是"听清歌曲"的硬约束：掩蔽不许把静态抬升堆到 6 dB。
        let target = min(clamp(base + extra + dynamic,
                               -maxAppliedGainDb, maxAppliedGainDb), maxBoostDb)
        eq.bands[index].gain = target
        appliedBands[index] = target
    }
    eq.bypass = snapshot.bypass
    engine.mainMixerNode.outputVolume = clamp(snapshot.mediaPercent / 100.0, 0, 1)
}

func cleanup() {
    if cleanedUp { return }
    cleanedUp = true
    engine.stop()
    capture.stop()
}

func quit(_ reason: String) {
    let snapshot = state.snapshot()
    emit(["event": "quitting", "reason": reason, "bypass": snapshot.bypass])
    cleanup()
    exit(0)
}

// 预缓冲（2026-10-23）：tap 已经先启动、ring 在积累。若不预缓冲就起 engine，
// 首次 render 回调只能拿到几百帧 -> AVAudioConverter 输出不足 -> 静音填充造成
// 周期性"刷"声。等 ring 积累 ≥ 0.5 s（24000 帧 @48k）再起引擎；上限 3 s 防挂死。
var prerollWaitedMs = 0
while capture.ring.fillCount < 24000 && prerollWaitedMs < 3000 {
    usleep(10_000)
    prerollWaitedMs += 10
}

// 输出设备配置变化（A2DP/HFP 切换、蓝牙重连、默认输出改变）会让 AVAudioEngine
// 静默停止拉数据 -> ring 积压 -> EQ 链失效。监听配置变化立即退出，由 host 重建
// （systemeq.py 对这类退出允许有限次自动重启）。2026-10-23 实测的"又没了"根因。
NotificationCenter.default.addObserver(
    forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
) { _ in
    quit("输出设备配置变化（A2DP/HFP 切换或设备切换），重建 EQ 链")
}

do {
    try engine.start()
} catch {
    emitError("音频引擎启动失败：\(error.localizedDescription)")
    cleanup()
    exit(4)
}

emit([
    "event": "ready",
    "app": options.app,
    "sample_rate": capture.sampleRate,
    "channels": capture.channels,
    "band_centers": bandCenters.map { Double($0) },
    "depth": Double(options.depth),
    "seconds": options.seconds,
    "ab_seconds": options.abSeconds,
    "demo": options.demo,
    "mode": state.snapshot().mode,
    "resampling": needResample,
    "engine_rate": Double(nodeFormat.sampleRate),
])

// stdin：和 player 一样按行 JSON。读不到 stdin（EOF）不退出，方便用 --seconds 做试听。
Thread {
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty,
              let data = trimmed.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let command = object["cmd"] as? String else { continue }
        switch command {
        case "eq":
            let rawBands = (object["bands"] as? [Double]) ?? []
            let bands = (0..<24).map { $0 < rawBands.count ? Float(rawBands[$0]) : 0 }
            let depth = Float((object["depth"] as? Double) ?? 2.5)
            let media = Float((object["media_percent"] as? Double) ?? 100)
            let baseGain = Float((object["base_gain"] as? Double) ?? 1.0)
            state.update(bands: bands, depth: depth, mediaPercent: media, baseGain: baseGain)
        case "flat":
            state.flat()
        case "bypass":
            state.setBypass((object["on"] as? Bool) ?? false)
        case "mode":
            if let value = object["value"] as? String, value == "basic" || value == "aggressive" {
                state.setMode(value)
            } else {
                emitError("mode 只接受 basic 或 aggressive")
            }
        case "status":
            let snapshot = state.snapshot()
            emit(["event": "status", "bands": snapshot.bands.map { Double($0) },
                  "depth": Double(snapshot.depth), "bypass": snapshot.bypass,
                  "media_percent": Double(snapshot.mediaPercent), "mode": snapshot.mode])
        case "quit":
            DispatchQueue.main.async { quit("stdin") }
        default:
            emitError("未知命令：\(command)")
        }
    }
}.start()

let startTime = Date()
var stallStreak = 0

let controlTimer = Timer(timeInterval: 0.05, repeats: true) { _ in
    // 先看上一拍 EQ 之后有没有削波：有就收缩正增益预算（最快 1.4 dB/s），输出干净则慢回。
    let out = outMeter.take()
    // D54（2026-10-28 现场"歌曲声音忽大忽小"）：headroom 的回落要**很慢**。原来 0.02/50ms
    // （≈0.4 dB/s）会在响/静交替的段落里来回收缩又放开，听成音量抽动；改成 0.004/50ms
    // （≈0.08 dB/s，20 s 才回 1 dB），收紧仍然快（秒级），避免削波。
    if out.peak >= 1.0 || out.clipFrames > 0 {
        outHeadroomTrimDb = min(2.0, outHeadroomTrimDb + 0.10)
    } else if out.peak < 0.90 {
        outHeadroomTrimDb = max(0, outHeadroomTrimDb - 0.004)
    }
    applyEq(state)
    // 链路自检（2026-10-23）：tap 采样率与输出设备不一致时（如 QQ 音乐 48 kHz、蓝牙
    // 设备 44.1 kHz），消费端永远比生产端慢，ring 持续积满 → 丢帧 = 用户听到的抖动。
    // 连续 3 秒 fill > 80% 就判定不匹配：退出并销毁 tap，让 QQ 音乐原声恢复（fail-open）。
    // 治本（消费端重采样）单独做；这里保证 44.1 kHz 设备上至少能正常听歌、不抖。
    if capture.ring.fillCount > capture.ring.capacity * 8 / 10 {
        stallStreak += 1
        if stallStreak >= 60 {
            quit("ring 持续积压（多为输出设备配置变化），退出重建")
        }
    } else {
        stallStreak = 0
    }
}
RunLoop.main.add(controlTimer, forMode: .common)

var reportTimer: Timer?
if options.reportSeconds > 0 {
    reportTimer = Timer(timeInterval: options.reportSeconds, repeats: true) { _ in
        let meter = capture.meter.snapshot()
        let snapshot = state.snapshot()
        let content = capture.content.snapshot()
        emit([
            "event": "state",
            "rms": meter.rms,
            "peak": meter.peak,
            "callbacks": meter.callbacks,
            "frames": meter.frames,
            "ring_fill": capture.ring.fillCount,
            "consumed_frames": consumed.snapshot().frames,
            "consumed_callbacks": consumed.snapshot().callbacks,
            "depth": Double(snapshot.depth),
            "media_percent": Double(snapshot.mediaPercent),
            "bypass": snapshot.bypass,
            "mode": snapshot.mode,
            "bands": snapshot.bands.map { Double($0) },
            "applied_bands": appliedBands.map { Double($0) },
            "content_harshness": Double(content.harshness),
            "content_high_ratio": Double(content.highRatio),
            "content_mid_ratio": Double(content.midRatio),
            "content_fed_frames": content.fedFrames,
            "content_analyses": content.analyses,
            "content_window_rms": Double(content.windowRms),
            "content_fill": content.fill,
            "out_ring_fill": resampleOutRing.fillCount,
            "content_env_exposure": Double(lastEnvExposure),
            "content_trim": Double(trimmedHarshness),
            "out_peak": outMeter.totals().peak,
            "out_clip_frames": outMeter.totals().clipFrames,
            "out_frames": outMeter.totals().frames,
            "headroom_trim_db": Double(outHeadroomTrimDb),
        ])
    }
    RunLoop.main.add(reportTimer!, forMode: .common)
}

var abTimer: Timer?
if options.abSeconds > 0 {
    abTimer = Timer(timeInterval: options.abSeconds, repeats: true) { _ in
        let bypass = state.toggleBypass()
        emit(["event": "ab", "bypass": bypass])
    }
    RunLoop.main.add(abTimer!, forMode: .common)
}

if options.seconds > 0 {
    DispatchQueue.main.asyncAfter(deadline: .now() + options.seconds) {
        quit("seconds")
    }
}

signal(SIGINT) { _ in
    // 复杂清理放在主线程；这里只保证进程能退出，tap 由进程死亡回收。
    exit(0)
}

RunLoop.main.run()

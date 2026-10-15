// 读当前默认输出设备的驱动层延迟。
//
// 为什么需要它：AirPods 的蓝牙输出延迟决定了「S3 → Mac → AirPods」这条路径的物理下限。
// 声学测法（放一个脉冲再录回来）会被房间噪声和蓝牙唤醒干扰——实测四次里三次是 1.0–1.2 秒、
// 一次 205 ms，分不清哪个是稳态。驱动自己报的这几个数字是确定的，也不受房间影响。
//
// 总输出延迟 ≈ 回调缓冲 + 安全偏移 + 设备延迟 + 流延迟：
//   kAudioDevicePropertyBufferFrameSize  ffplay/SDL 那类回调一次吃多少帧
//   kAudioDevicePropertySafetyOffset      驱动为对抗抖动留的余量，蓝牙设备上通常很大
//   kAudioDevicePropertyLatency           设备自身（含蓝牙编解码与无线段）的延迟
//   kAudioStreamPropertyLatency           流级附加延迟
//
// 用法：
//   outlat              报当前默认输出的延迟分解
//   outlat --list       列出所有输出设备
//   outlat --set NAME   把默认输出切到名字包含 NAME 的设备（例如 "AirPods"）
//   outlat --watch N    每秒采样一次默认输出，N 秒内只要变了就打印
//
// --watch 存在的理由：真机日志里默认输出在「AirPods Pro」和「MacBook Air扬声器」之间
// 反复跳（1372 s 切到 AirPods、1383 s 又切回扬声器）。每跳一次，蓝牙音频流就被拆掉重建，
// 重建一次就是一秒左右的冷启动——这才是"延迟 1 秒"的来源，而不是缓冲参数。既然我们
// 自己的代码只读不写，就得先看清是谁在抢。
//
// 编译：swiftc -O -o /tmp/outlat tools/host/output_latency.swift
import CoreAudio
import Foundation

func uint32Property(_ device: AudioDeviceID, _ selector: AudioObjectPropertySelector,
                    _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func float64Property(_ device: AudioDeviceID, _ selector: AudioObjectPropertySelector,
                     _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> Float64? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var value: Float64 = 0
    var size = UInt32(MemoryLayout<Float64>.size)
    let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func deviceName(_ device: AudioDeviceID) -> String {
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

func setDefaultOutput(_ device: AudioDeviceID) -> Bool {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value = device
    let size = UInt32(MemoryLayout<AudioDeviceID>.size)
    return AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
                                      0, nil, size, &value) == noErr
}

let arguments = Array(CommandLine.arguments.dropFirst())

if let index = arguments.firstIndex(of: "--list") {
    _ = index
    for device in allDevices() where hasOutput(device) {
        let marker = device == currentDefaultOutput() ? "*" : " "
        print("\(marker) \(deviceName(device))")
    }
    exit(0)
}

if let index = arguments.firstIndex(of: "--set"), index + 1 < arguments.count {
    let needle = arguments[index + 1]
    guard let target = allDevices().first(where: {
        hasOutput($0) && deviceName($0).localizedCaseInsensitiveContains(needle)
    }) else {
        print("没有名字包含 \(needle) 的输出设备")
        exit(1)
    }
    if setDefaultOutput(target) {
        print("默认输出已切换到：\(deviceName(target))")
        exit(0)
    }
    print("切换失败：\(deviceName(target))")
    exit(1)
}

if let index = arguments.firstIndex(of: "--watch"), index + 1 < arguments.count {
    let seconds = Double(arguments[index + 1]) ?? 30
    var last = AudioDeviceID(0)
    let deadline = Date().addingTimeInterval(seconds)
    print(String(format: "监视默认输出 %.0f 秒…", seconds))
    while Date() < deadline {
        let now = currentDefaultOutput()
        if now != last {
            print(String(format: "  %6.1f s  %@",
                         seconds - deadline.timeIntervalSinceNow, deviceName(now)))
            last = now
        }
        Thread.sleep(forTimeInterval: 0.5)
    }
    exit(0)
}

let device = currentDefaultOutput()
guard device != 0 else {
    print("读不到默认输出设备")
    exit(1)
}

let rate = float64Property(device, kAudioDevicePropertyNominalSampleRate) ?? 0
let buffer = uint32Property(device, kAudioDevicePropertyBufferFrameSize, kAudioObjectPropertyScopeOutput) ?? 0
let safety = uint32Property(device, kAudioDevicePropertySafetyOffset, kAudioObjectPropertyScopeOutput) ?? 0
let deviceLatency = uint32Property(device, kAudioDevicePropertyLatency, kAudioObjectPropertyScopeOutput) ?? 0

// 流级延迟：设备上通常是第一个输出流。取不到就按 0 计，并在输出里标注。
var streamLatency: UInt32 = 0
var streamFound = false
var streamAddress = AudioObjectPropertyAddress(
    mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeOutput,
    mElement: kAudioObjectPropertyElementMain)
var streamBytes: UInt32 = 0
if AudioObjectGetPropertyDataSize(device, &streamAddress, 0, nil, &streamBytes) == noErr,
   streamBytes >= UInt32(MemoryLayout<AudioStreamID>.size) {
    var one = AudioStreamID(0)
    var size = UInt32(MemoryLayout<AudioStreamID>.size)
    if AudioObjectGetPropertyData(device, &streamAddress, 0, nil, &size, &one) == noErr {
        streamLatency = uint32Property(one, kAudioStreamPropertyLatency) ?? 0
        streamFound = true
    }
}

func ms(_ frames: UInt32) -> Double {
    rate > 0 ? Double(frames) / rate * 1000.0 : 0
}

print("默认输出：\(deviceName(device))")
print(String(format: "采样率      %.0f Hz", rate))
print(String(format: "回调缓冲    %6u 帧  %7.1f ms", buffer, ms(buffer)))
print(String(format: "安全偏移    %6u 帧  %7.1f ms", safety, ms(safety)))
print(String(format: "设备延迟    %6u 帧  %7.1f ms", deviceLatency, ms(deviceLatency)))
if streamFound {
    print(String(format: "流延迟      %6u 帧  %7.1f ms", streamLatency, ms(streamLatency)))
} else {
    print("流延迟          取不到（按 0 计）")
}

let total = ms(buffer) + ms(safety) + ms(deviceLatency) + ms(streamLatency)
print(String(format: "合计        %20.1f ms", total))
print("这是「Mac 把一帧交给驱动」到「耳机振膜动」的驱动侧估算，不含播放器自己的排队。")

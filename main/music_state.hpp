#pragma once
/// 「歌是 Mac 放的」状态（阶段二/B1，2026-10-30）。
///
/// 为什么需要：S3 听到的 24 段环境谱里混着自己正在放的歌。没有这个信号，设备无法
/// 区分"环境里有音乐"和"我自己放出来的音乐"——肩上/近耳位置现场实测有 25–33% 的帧
/// 因此被判成情境 music，漏音也会偶尔触发 1 s 事件层的 speech。host 现在在歌真的在播
/// （执行端实测 `applied`/`playing`）时每 1 s 下发一次 `music on <ttl_ms>`。
///
/// **v1 只做遥测**（命令帧 `music_playing`）：host 侧漏音残差（B2）与内容角色侧链（B3）
/// 才是真正改调音动作的地方；设备侧的对话/背景人声判定**这一版不动**——没有真机数据
/// 就改阈值，最可能的结果是把真实对话也一起压掉。等 B2/B3 的现场数据回来再加门控。
///
/// fail-open：TTL（默认 3 s）内没有续期就回到"没在放歌"，host 崩了/串口断了也不会让
/// 设备永久以为歌在放。纯逻辑、无 IDF 依赖，主机单测见 `test/music_state_test.cpp`。
#include <cstdint>

namespace music {

/// host 每 1 s 续期一次，TTL 给到 3 s：允许连续丢两次续期才失效。
constexpr int64_t kDefaultTtlMs = 3000;
/// 上限：防御坏行（或上层的 bug）把状态锁死太久。
constexpr int64_t kMaxTtlMs = 10000;

/// 只记录"到什么时候为止算在放歌"，不持有时间源：调用方传 now_us，
/// 这样主机单测可以随意拨钟。
class State {
public:
    /// `music on [ttl_ms]`：ttl 缺省/非法时用 kDefaultTtlMs，超上限夹到 kMaxTtlMs。
    void on(int64_t now_us, int64_t ttl_ms = 0) {
        if (ttl_ms <= 0) ttl_ms = kDefaultTtlMs;
        if (ttl_ms > kMaxTtlMs) ttl_ms = kMaxTtlMs;
        until_us_ = now_us + ttl_ms * 1000;
    }

    /// `music off`：立刻失效。
    void off() { until_us_ = 0; }

    /// 这一拍算不算"歌在放"。边界取严格小于：到期那一拍就是没在放。
    bool playing(int64_t now_us) const {
        return until_us_ > 0 && now_us < until_us_;
    }

    int64_t until_us() const { return until_us_; }

private:
    int64_t until_us_ = 0;
};

}  // namespace music

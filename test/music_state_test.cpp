// Tests for main/music_state.hpp：「歌是 Mac 放的」状态（阶段二/B1，2026-10-30）。
//
// Why this suite exists: 这个状态决定设备能不能区分"环境里有音乐"和"我自己放的歌"。
// 它的失败模式不是崩溃，而是**悄悄永远为真**——host 崩了、串口断了、控制行发坏，
// 设备还一直以为歌在放，后面所有基于这个旗标的门控都会错。所以这里钉死三件事：
//   * TTL 到点必须失效（fail-open），不是"等下一帧"；
//   * 缺省/非法 ttl 用默认值，超大 ttl 夹到上限；
//   * `music off` 立刻失效，`music on` 可以续期延长。
// 纯逻辑、不碰时间和串口，所以整个时序可以在主机上毫秒级跑完。
#include <iostream>
#include <string>

#include "../main/music_state.hpp"

namespace {
int passed = 0;
int failed = 0;

void check(bool condition, const std::string& name) {
    if (condition) {
        ++passed;
        std::cout << "PASS " << name << '\n';
    } else {
        ++failed;
        std::cout << "FAIL " << name << '\n';
    }
}

constexpr int64_t kMs = 1000;  // 1 ms 的微秒数
}  // namespace

int main() {
    // ---- 1) 默认没在放歌 ----
    {
        music::State state;
        check(!state.playing(0), "默认：没在放歌");
        check(state.until_us() == 0, "默认：没有到期时刻");
    }

    // ---- 2) on 之后在 TTL 内为真，到期当拍为假 ----
    {
        music::State state;
        const int64_t now = 10'000'000;
        state.on(now, 3000);
        check(state.playing(now), "on 当拍：在放歌");
        check(state.playing(now + 2999 * kMs), "到期前 1 ms：仍在放歌");
        check(!state.playing(now + 3000 * kMs), "到期当拍：失效（fail-open）");
        check(!state.playing(now + 6000 * kMs), "过期之后：仍然失效");
    }

    // ---- 3) off 立刻失效 ----
    {
        music::State state;
        state.on(1'000'000, 3000);
        check(state.playing(1'100'000), "off 之前：在放歌");
        state.off();
        check(!state.playing(1'100'000), "off 当拍：立刻失效");
        check(state.until_us() == 0, "off：清掉到期时刻");
    }

    // ---- 4) 续期会延长，而不是从旧到期时间往后接 ----
    {
        music::State state;
        state.on(0, 3000);
        state.on(2000 * kMs, 3000);
        check(state.playing(4500 * kMs), "续期后：旧到期点之后仍在放歌");
        check(!state.playing(5000 * kMs), "续期后：新的到期点之外失效");
    }

    // ---- 5) ttl 缺省/非法用默认值，超大夹到上限 ----
    {
        music::State state;
        state.on(0, 0);
        check(state.until_us() == music::kDefaultTtlMs * kMs, "ttl=0：用默认 TTL");
        state.on(0, -5);
        check(state.until_us() == music::kDefaultTtlMs * kMs, "ttl<0：用默认 TTL");
        state.on(0, 999999);
        check(state.until_us() == music::kMaxTtlMs * kMs, "超大 ttl：夹到上限");
        state.on(0, music::kMaxTtlMs);
        check(state.until_us() == music::kMaxTtlMs * kMs, "正好上限：不改变");
    }

    std::cout << "\n" << passed << " 条通过、" << failed << " 条失败\n";
    return failed == 0 ? 0 : 1;
}

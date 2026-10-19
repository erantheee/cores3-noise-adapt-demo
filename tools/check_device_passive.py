"""被动设备检查：从面板 /api/snapshot 轮询，不独占串口、不需要人做动作。

这份检查存在的理由：真机五相验证需要独占串口 + 人在场配合（说话、走动），
但有一半的项**只要设备在跑就能看**——固件版本、IMU 是否就绪、重力基线是否落在
±4g 的合理范围、丢采样、采样率、证据来源与策略是否一致。先跑这个，再去排五相。

用法：
    python3 tools/check_device_passive.py                 # 默认 90 秒
    python3 tools/check_device_passive.py --seconds 30 --url http://127.0.0.1:8787/api/snapshot

2026-10-19 第一次跑就抓到一条真问题：C4c IMU 采样率只有 30.1 Hz（设计是 100 Hz，
ear-context 实测 100.1 Hz），且 imu_drops 增量为 0 —— 也就是**读得慢，不是丢队列**。
只覆盖"只要设备在跑就能看到"的项：C1 固件版本、C2 IMU 就绪、C3 重力基线、
C4b 丢采样、C4c 采样率、证据来源与策略一致性。C5–C9 需要脚本化的五相，不在本检查内。"""
import json, statistics, time, urllib.request

URL = "http://127.0.0.1:8787/api/snapshot"
SECONDS = 90.0
rows = []
start = time.monotonic()
while time.monotonic() - start < SECONDS:
    try:
        with urllib.request.urlopen(URL, timeout=2) as r:
            s = json.load(r)
        f = s.get("frame") or {}
        rows.append((time.monotonic() - start, f, s.get("problems") or []))
    except Exception as exc:
        rows.append((time.monotonic() - start, {}, [f"抓取失败: {exc}"]))
    time.sleep(0.1)

frames = [r for r in rows if r[1]]
print(f"轮询 {len(rows)} 次，拿到帧 {len(frames)} 个（{len(frames)/SECONDS*1.0:.1f} 帧/秒）")
if not frames:
    print("❌ 一帧都没拿到"); raise SystemExit(2)

def vals(key):
    return [r[1][key] for r in frames if r[1].get(key) is not None]

schemas = sorted({r[1].get("schema") for r in frames if r[1].get("schema") is not None})
gravity = vals("imu_gravity")
samples = vals("imu_samples")
drops = vals("imu_drops")
imu_ok = [r[1].get("imu_ok") for r in frames if r[1].get("imu_ok") is not None]
sources = sorted({r[1].get("own_voice_source") for r in frames if r[1].get("own_voice_source")})
policies = sorted({r[1].get("evidence_policy") for r in frames if r[1].get("evidence_policy")})
media = vals("media_percent")
problems = [p for r in rows for p in r[2]]

checks = []
def check(name, ok, detail):
    checks.append((name, ok, detail)); print(f"{'✅' if ok else '❌'} {name}：{detail}")

check("C1 固件是 v3 或更新", bool(schemas) and min(schemas) >= 3, f"schema={schemas}")
check("C2 IMU 就绪", imu_ok and sum(imu_ok)/len(imu_ok) >= 0.9,
      f"{sum(imu_ok)}/{len(imu_ok)} 帧 imu_ok=true")
check("C3 重力基线（±4g，m/s²）", gravity and 9.0 <= statistics.median(gravity) <= 10.5,
      f"p50={statistics.median(gravity):.2f}" if gravity else "没有 imu_gravity")
if samples and len(samples) >= 2:
    hz = (samples[-1] - samples[0]) / SECONDS
    check("C4c IMU 采样率", 90 <= hz <= 110, f"{hz:.1f} Hz")
else:
    check("C4c IMU 采样率", False, "帧里没有 imu_samples")
if drops and len(drops) >= 2:
    check("C4b IMU 丢采样", drops[-1] - drops[0] == 0, f"增量 {drops[-1]-drops[0]}")
else:
    check("C4b IMU 丢采样", False, "帧里没有 imu_drops")
expected = {"bone_only": {"bone"}, "near_field_demo": {"near_field"}, "disabled": {"none"}}
ok = bool(policies) and all(s in expected.get(p, set()) for p in policies for s in sources)
check("C5b 证据来源与策略一致", ok, f"policy={policies} source={sources}")
ambient = [r[1].get("ambient_open") for r in frames if "ambient_open" in r[1]]
if schemas and min(schemas) >= 4:
    check("C6 环境声意图可读", bool(ambient) and all(isinstance(v, bool) for v in ambient),
          f"{sum(1 for v in ambient if v)}/{len(ambient)} 帧请求开放"
          if ambient else "帧里没有 ambient_open")
else:
    check("C6 环境声意图可读", True, "v3 固件没有 ambient_open（升级后可见）")
check("遥测无解析异常", not problems, f"{len(problems)} 条 anomalies")
if media:
    check("媒体下压确有动作（演示模式观察项）", min(media) < 100,
          f"最低 media={min(media)}%，下压帧 {sum(1 for m in media if m < 99)}/{len(media)}")
failed = [c for c in checks if not c[1]]
print(f"\n被动检查：{len(checks)-len(failed)}/{len(checks)} 通过")
raise SystemExit(0 if not failed else 1)

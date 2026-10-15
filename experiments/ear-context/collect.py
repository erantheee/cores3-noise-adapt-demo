#!/usr/bin/env python3
"""Save device JSON without reinterpreting activity labels or model predictions."""
import argparse
import datetime
import json
from pathlib import Path
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', required=True)
    p.add_argument('--seconds', type=float, default=60, help='0 = until Ctrl-C')
    p.add_argument('--label', required=True, help='Human activity label, not a model prediction')
    p.add_argument('--position', choices=['left-ear', 'right-ear', 'headband', 'desk'], required=True)
    p.add_argument('--subject', required=True, help='Anonymous participant ID')
    p.add_argument('--out', type=Path)
    args = p.parse_args()
    if args.seconds < 0:
        p.error('--seconds must be nonnegative')
    import serial
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = args.out or Path(__file__).resolve().parent.parent / 'listening-model/data/real' / stamp
    out.mkdir(parents=True, exist_ok=False)
    (out / 'session.json').write_text(json.dumps({
        'schema': 1, 'started_utc': stamp, 'subject': args.subject,
        'position': args.position, 'human_activity_label': args.label,
        'firmware': 'ear-context', 'model': 'v0.2-s3',
        'axis_frame': 'BMI270 sensor axes; mounting orientation not calibrated',
        'accel_units': 'm/s^2 including gravity', 'gyro_units': 'degrees/s',
        'device_time': 'monotonic read-completion time, not exact sensor acquisition time',
        'clock': 'device wall clock unset; host receipt time is not acquisition time',
        'raw_audio': False,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    start = last_valid = time.monotonic()
    counts = {'imu': 0, 'context': 0}
    previous = {}
    resets = 0
    try:
        with serial.Serial(args.port, 115200, timeout=1) as port, \
                (out / 'samples.jsonl').open('x', buffering=1) as sink:
            while not args.seconds or time.monotonic() - start < args.seconds:
                line = port.readline()
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    record = None
                if isinstance(record, dict) and record.get('type') in counts and isinstance(record.get('t_us'), int):
                    kind = record['type']
                    if kind in previous and record['t_us'] < previous[kind]:
                        resets += 1
                        previous.clear()
                    previous[kind] = record['t_us']
                    record['host_received_unix_ns'] = time.time_ns()
                    record['boot_segment'] = resets
                    sink.write(json.dumps(record, ensure_ascii=False) + '\n')
                    counts[kind] += 1
                    last_valid = time.monotonic()
                if time.monotonic() - last_valid > 10:
                    raise RuntimeError('10 seconds without valid device data; check firmware/connection')
    except KeyboardInterrupt:
        pass
    finally:
        (out / 'summary.json').write_text(json.dumps({'counts': counts, 'resets': resets,
            'elapsed_seconds': time.monotonic() - start}, indent=2))
        print(out, counts)


if __name__ == '__main__':
    main()

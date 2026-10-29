#!/usr/bin/env python3
"""Read-only PM:INFO window statistics; no automatic diagnosis or time mapping."""
import argparse
import collections
import json
import math
import re
from pathlib import Path

PM = re.compile(r'\bPM:INFO\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*$')
EVENT = re.compile(r'MAIN\s+reset_reason\s*=\s*(\d+)|(?:nst11x_ic_init\s+fail|thermo_init\s+(?:success|fail))')


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('value must be finite and positive')
    return number


def summarize(path, start=1, end=None, capacity=None, period=None):
    first = last = peak = None
    pm_count = malformed = 0
    invalid_soc = 0
    bad_f6 = collections.Counter()
    f7 = collections.Counter()
    reset = collections.Counter()
    init = collections.Counter()
    soc_rises = 0
    invalid_f6_samples = []
    event_samples = []
    saw_high_band = False
    lines_read = 0
    with Path(path).open(encoding='utf-8-sig', errors='replace') as stream:
        for line_no, raw in enumerate(stream, 1):
            if end is not None and line_no > end:
                break
            lines_read = line_no
            if line_no < start:
                continue
            line = raw.strip()
            event = EVENT.search(line)
            if event:
                if event.group(1) is not None:
                    reset[event.group(1)] += 1
                else:
                    init[event.group(0)] += 1
                if len(event_samples) < 12:
                    event_samples.append({'line': line_no, 'raw': line})
            if 'PM:INFO' not in line:
                continue
            match = PM.search(line)
            if not match:
                malformed += 1
                continue
            values = list(map(int, match.groups()))
            row = {'line': line_no, 'fields': values, 'raw': line,
                   'battery_percent': values[1], 'temperature_c': values[4],
                   'vbus_mv': values[5], 'vbus_v': values[5] / 1000,
                   'charging_stage': values[6]}
            pm_count += 1
            if not 0 <= values[1] <= 100:
                invalid_soc += 1
            if last is not None and values[1] > last['fields'][1]:
                soc_rises += 1
            if first is None:
                first = row
            last = row
            if peak is None or values[4] > peak['fields'][4]:
                peak = row
            f6 = values[5]
            high_band = 4500 <= f6 <= 5500
            saw_high_band |= high_band
            if not (0 <= f6 <= 100 or high_band):
                bad_f6[f6] += 1
                if len(invalid_f6_samples) < 8:
                    invalid_f6_samples.append(row)
            f7[values[6]] += 1
    out = {
        'file': str(path), 'requested_lines': [start, end],
        'last_line_read': lines_read,
        'pm_records': pm_count, 'malformed_pm_records': malformed,
        'invalid_soc_records': invalid_soc,
        'first': first, 'last': last, 'temperature_peak_record': peak,
        'f6_out_of_band_count': sum(bad_f6.values()),
        'f6_out_of_band_values': dict(bad_f6),
        'f6_out_of_band_samples': invalid_f6_samples,
        'f7_counts': dict(f7), 'charging_stage_counts': dict(f7),
        'adjacent_soc_rises': soc_rises,
        'reset_reasons_in_window': dict(reset),
        'init_events_in_window': dict(init), 'event_samples': event_samples,
        'limitations': [
            'Only records within requested source line bounds are counted.',
            'No wall-clock mapping, boot/gap segmentation, wear or charging-state verification.',
            'Missing events do not prove that the device never had those events.',
            'Temperature sensor validity is not verified.',
            'VBUS is charging input voltage in mV, not battery voltage or current.',
            'Legacy f6_out_of_band statistics use historical 0-100/4500-5500 mV bands; they are not a fault diagnosis.',
            'Charging stage 3 is tentatively constant-current per user; other stage meanings are not established.',
        ],
    }
    if first and last:
        out['soc_drop_percentage_points'] = first['fields'][1] - last['fields'][1]
        out['temperature_delta_raw'] = last['fields'][4] - first['fields'][4]
    if period is not None:
        reasons = []
        if pm_count < 2:
            reasons.append('Fewer than two PM records.')
        if malformed or invalid_soc:
            reasons.append('Malformed PM records or invalid SOC values.')
        if reset:
            reasons.append('Window contains reset records; segment by boot first.')
        if soc_rises:
            reasons.append('SOC rises in window; inspect charging/rebound and segment first.')
        if saw_high_band or any(state != 0 for state in f7):
            reasons.append('Possible charging or unknown f7 state; inspect and segment first.')
        out['sampling_estimate'] = {'assumed_period_minutes': period,
            'capacity_mah': capacity,
            'assumptions': 'Unverified: continuous samples, no duplicates/gaps/boots, valid discharge and model capacity.'}
        if reasons:
            out['sampling_estimate']['withheld_reasons'] = reasons
        else:
            hours = (pm_count - 1) * period / 60
            rate = out['soc_drop_percentage_points'] / hours
            out['sampling_estimate'].update({
                'estimated_hours': hours, 'estimated_discharge_percent_per_hour': rate,
                'estimated_average_current_ma': capacity * rate / 100 if capacity else None,
            })
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    parser.add_argument('--start-line', type=int, default=1)
    parser.add_argument('--end-line', type=int)
    parser.add_argument('--capacity-mah', type=positive_float)
    parser.add_argument('--assume-period-minutes', type=positive_float)
    args = parser.parse_args()
    if args.start_line < 1 or (args.end_line is not None and args.end_line < args.start_line):
        parser.error('line bounds must be positive and end must be >= start')
    if args.capacity_mah is not None and args.assume_period_minutes is None:
        parser.error('--capacity-mah requires --assume-period-minutes; no implicit time basis')
    try:
        report = summarize(args.log, args.start_line, args.end_line,
                           args.capacity_mah, args.assume_period_minutes)
    except OSError as exc:
        parser.exit(2, f'Cannot read log: {exc}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report['pm_records'] == 0:
        parser.exit(1, 'No valid PM:INFO records found in requested window.\n')


if __name__ == '__main__':
    main()

import argparse
import csv
import json
import math
import struct
import time
from pathlib import Path

import libusb_package
import usb.core
import usb.util


VID = 0x2A0E
PID = 0x002D

CMD_CTL_WR = 0xB0
CMD_CTL_RD_PRE = 0xB1
CMD_CTL_RD = 0xB2

DSL_CTL_FW_VERSION = 0
DSL_CTL_HW_STATUS = 2
DSL_CTL_INTRDY = 6
DSL_CTL_WORDWIDE = 7
DSL_CTL_START = 8
DSL_CTL_STOP = 9
DSL_CTL_BULK_WR = 10
DSL_CTL_I2C_REG = 14
DSL_CTL_I2C_STATUS = 15

BM_WR_WORDWIDE = 1
BM_WR_INTRDY = 0x80
BM_SYS_CLR = 1 << 3
BM_FORCE_RDY = 1 << 1

CTR0_ADDR = 0x70
HDL_VERSION_ADDR = 0x04
DSLOGIC_ATOMIC_SAMPLES = 64
DSLOGIC_ATOMIC_SIZE = 8
SAMPLES_ALIGN = 1023
STREAM_MODE_BIT = 12
CHANNEL_COUNT = 16


def parse_channel_list(spec):
    channels = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid channel range: {part}")
            channels.extend(range(start, end + 1))
        else:
            channels.append(int(part))
    channels = sorted(set(channels))
    for channel in channels:
        if channel < 0 or channel >= CHANNEL_COUNT:
            raise ValueError(f"channel out of range 0..15: {channel}")
    return channels


def open_device():
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        raise RuntimeError("DSLogic-compatible device 2A0E:002D not found")
    try:
        dev.set_configuration()
    except Exception:
        pass
    try:
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as exc:
        if getattr(exc, "errno", None) == 13:
            raise RuntimeError(
                "DSLogic USB access denied. Close DSView and stop any other "
                "capture process; the analyzer interface is exclusive."
            ) from exc
        raise
    return dev


def ctl_rd(dev, dest, size, offset=0):
    header = struct.pack("<BHB", dest, offset, size)
    dev.ctrl_transfer(0x40, CMD_CTL_RD_PRE, 0, 0, header, timeout=3000)
    time.sleep(0.010)
    return bytes(dev.ctrl_transfer(0xC0, CMD_CTL_RD, 0, 0, size, timeout=3000))


def ctl_wr(dev, dest, payload=b"", offset=0):
    header = struct.pack("<BHB", dest, offset, len(payload))
    return dev.ctrl_transfer(0x40, CMD_CTL_WR, 0, 0, header + payload, timeout=3000)


def wr_reg(dev, addr, val):
    return ctl_wr(dev, DSL_CTL_I2C_REG, bytes([val & 0xFF]), addr)


def build_setting(limit_samples, samplerate, ch_num=CHANNEL_COUNT):
    actual_samples = (limit_samples + SAMPLES_ALIGN) & ~SAMPLES_ALIGN
    divider = math.ceil(500_000_000 / samplerate)
    div_h = (((5 - 1) if divider >= 5 else (divider - 1)) << 8)
    divider = math.ceil(divider / 5)
    div_l = divider & 0xFFFF
    div_h = (div_h + (divider >> 16)) & 0xFFFF
    cnt = actual_samples >> 4
    tpos = DSLOGIC_ATOMIC_SAMPLES & (0xFFFF << 6)
    mode = 1 << STREAM_MODE_BIT
    trig_glb = (ch_num & 0x1F) << 8

    parts = []

    def p16(value):
        parts.append(struct.pack("<H", value & 0xFFFF))

    def p32(value):
        parts.append(struct.pack("<I", value & 0xFFFFFFFF))

    p32(0xF5A5F5A5)
    for value in [
        0x0001, mode,
        0x0102, div_l, div_h,
        0x0302, cnt & 0xFFFF, cnt >> 16,
        0x0502, tpos & 0xFFFF, tpos >> 16,
        0x0701, trig_glb,
        0x0802, actual_samples & 0xFFFF, actual_samples >> 16,
        0x0A02, 0xFFFF, 0,
        0x0C01, 0,
        0x40A0,
    ]:
        p16(value)

    for _ in range(16):
        p16(0xFFFF)
    for _ in range(16):
        p16(0xFFFF)
    for _ in range(16 * 4):
        p16(0)
    for _ in range(16):
        p16(2)
    for _ in range(16):
        p16(2)
    for _ in range(16):
        p32(0)
    p32(0xFA5AFA5A)

    setting = b"".join(parts)
    if len(setting) != 372:
        raise AssertionError(len(setting))
    actual_bytes = actual_samples // DSLOGIC_ATOMIC_SAMPLES * ch_num * DSLOGIC_ATOMIC_SIZE
    return setting, actual_samples, actual_bytes


def capture_usb(samplerate, limit_samples):
    dev = open_device()
    try:
        fw_version = ctl_rd(dev, DSL_CTL_FW_VERSION, 2)
        hdl = ctl_rd(dev, DSL_CTL_I2C_STATUS, HDL_VERSION_ADDR + 1)[HDL_VERSION_ADDR]
        hw_before = ctl_rd(dev, DSL_CTL_HW_STATUS, 1)[0]

        setting, actual_samples, actual_bytes = build_setting(limit_samples, samplerate)
        ctl_wr(dev, DSL_CTL_STOP)
        ctl_wr(dev, DSL_CTL_WORDWIDE, bytes([BM_WR_WORDWIDE]))
        arm_size = len(setting) // 2
        ctl_wr(dev, DSL_CTL_BULK_WR, bytes([arm_size & 0xFF, arm_size >> 8, arm_size >> 16]))

        deadline = time.time() + 2
        while True:
            status = ctl_rd(dev, DSL_CTL_HW_STATUS, 1)[0]
            if status & BM_SYS_CLR:
                break
            if time.time() > deadline:
                raise TimeoutError(f"SYS_CLR timeout: {status:#x}")

        dev.write(0x02, setting, timeout=3000)
        ctl_wr(dev, DSL_CTL_INTRDY, bytes([BM_WR_INTRDY]))
        hw_after_arm = ctl_rd(dev, DSL_CTL_HW_STATUS, 1)[0]
        ctl_wr(dev, DSL_CTL_START)

        header = bytes(dev.read(0x86, 512, timeout=3000))
        chunks = []
        got = 0
        while got < actual_bytes:
            chunk = bytes(dev.read(0x86, min(40448, actual_bytes - got), timeout=3000))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)

        meta = {
            "vid": f"0x{VID:04X}",
            "pid": f"0x{PID:04X}",
            "fw_version_hex": fw_version.hex(" "),
            "hdl_version": hdl,
            "hw_status_before": hw_before,
            "hw_status_after_arm": hw_after_arm,
            "samplerate": samplerate,
            "requested_samples": limit_samples,
            "actual_samples": actual_samples,
            "actual_bytes": actual_bytes,
        }
        return header, b"".join(chunks)[:actual_bytes], meta
    finally:
        try:
            wr_reg(dev, CTR0_ADDR, BM_FORCE_RDY)
        except Exception:
            pass
        try:
            ctl_wr(dev, DSL_CTL_STOP)
        except Exception:
            pass
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass


def decode_channels(data, channels, samples=None, ch_num=CHANNEL_COUNT):
    decoded = {channel: [] for channel in channels}
    group = ch_num * DSLOGIC_ATOMIC_SIZE
    for group_index in range(len(data) // group):
        base = group_index * group
        for channel in channels:
            block = data[
                base + channel * DSLOGIC_ATOMIC_SIZE:
                base + channel * DSLOGIC_ATOMIC_SIZE + DSLOGIC_ATOMIC_SIZE
            ]
            values = decoded[channel]
            for byte in block:
                for bit in range(8):
                    values.append((byte >> bit) & 1)
    if samples is not None:
        for channel in channels:
            decoded[channel] = decoded[channel][:samples]
    return decoded


def summarize_channel(values, samplerate, max_edges=20):
    if not values:
        return {"samples": 0, "edges": 0, "first_edges": []}
    first_edges = []
    edge_count = 0
    for index in range(1, len(values)):
        if values[index] != values[index - 1]:
            edge_count += 1
            if len(first_edges) < max_edges:
                first_edges.append({
                    "sample": index,
                    "time_s": index / samplerate,
                    "level": values[index],
                })
    return {
        "samples": len(values),
        "initial": values[0],
        "final": values[-1],
        "high_ratio": sum(values) / len(values),
        "edges": edge_count,
        "first_edges": first_edges,
    }


def parse_i2c(scl, sda):
    events = []
    for index in range(1, len(scl)):
        if scl[index] and sda[index] != sda[index - 1]:
            events.append((index, "START" if sda[index] == 0 else "STOP"))

    transactions = []
    idx = 0
    while idx < len(events):
        if events[idx][1] != "START":
            idx += 1
            continue
        start = events[idx][0]
        stop = len(scl) - 1
        next_idx = idx + 1
        while next_idx < len(events):
            if events[next_idx][1] == "STOP":
                stop = events[next_idx][0]
                break
            if events[next_idx][1] == "START":
                stop = events[next_idx][0]
                next_idx -= 1
                break
            next_idx += 1

        rises = [i for i in range(start + 1, stop) if scl[i - 1] == 0 and scl[i] == 1]
        bits = [sda[i] for i in rises]
        groups = []
        for group_index in range(len(bits) // 9):
            byte = 0
            for bit in bits[group_index * 9:group_index * 9 + 8]:
                byte = (byte << 1) | bit
            groups.append((byte, bits[group_index * 9 + 8]))
        transactions.append((start, stop, rises, groups))
        idx = next_idx + 1
    return transactions


def decode_i2c(channel_data, samplerate, scl_ch, sda_ch, max_transactions):
    scl = channel_data[scl_ch]
    sda = channel_data[sda_ch]
    transactions = parse_i2c(scl, sda)
    decoded_transactions = []
    for start, stop, rises, groups in transactions:
        decoded_groups = [{"byte": byte, "ack_bit": ack} for byte, ack in groups]
        item = {
            "start_sample": start,
            "stop_sample": stop,
            "groups": decoded_groups,
        }
        if groups:
            item["address"] = groups[0][0] >> 1
            item["rw"] = groups[0][0] & 1
            item["ack_bit"] = groups[0][1]
            item["ack"] = groups[0][1] == 0
        decoded_transactions.append(item)

    scl_hz = None
    for _, _, rises, _ in transactions:
        if len(rises) > 3:
            periods = [b - a for a, b in zip(rises, rises[1:])][:16]
            avg = sum(periods) / len(periods)
            scl_hz = samplerate / avg
            break

    print(f"protocol i2c CH{scl_ch}=SCL CH{sda_ch}=SDA")
    print("transactions", len(transactions))
    for index, item in enumerate(decoded_transactions[:max_transactions]):
        groups = [(hex(group["byte"]), group["ack_bit"]) for group in item["groups"]]
        print(f"TX{index} start={item['start_sample']} stop={item['stop_sample']} groups={groups}")
        if "address" in item:
            ack_text = "ACK" if item["ack"] else "NACK"
            print(
                f"  addr=0x{item['address']:02X} rw={item['rw']} "
                f"ack_bit={item['ack_bit']} {ack_text}"
            )
    if scl_hz is not None:
        print(f"scl_hz~{scl_hz:.1f}")

    return {
        "protocol": "i2c",
        "scl_ch": scl_ch,
        "sda_ch": sda_ch,
        "scl_hz": scl_hz,
        "transactions": decoded_transactions,
    }


def bits_to_bytes(bits, lsb_first=False):
    values = []
    for offset in range(0, len(bits) - (len(bits) % 8), 8):
        byte = 0
        chunk = bits[offset:offset + 8]
        if lsb_first:
            for bit_index, bit in enumerate(chunk):
                byte |= bit << bit_index
        else:
            for bit in chunk:
                byte = (byte << 1) | bit
        values.append(byte)
    return values


def find_active_spans(signal, active_level):
    spans = []
    start = 0 if signal and signal[0] == active_level else None
    for index in range(1, len(signal)):
        if signal[index] == active_level and signal[index - 1] != active_level:
            start = index
        elif signal[index] != active_level and signal[index - 1] == active_level:
            if start is not None:
                spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(signal) - 1))
    return spans


def decode_spi(
    channel_data,
    samplerate,
    cs_ch,
    sclk_ch,
    mosi_ch,
    miso_ch,
    cs_active_level,
    cpol,
    cpha,
    lsb_first,
    max_transactions,
):
    cs = channel_data[cs_ch]
    sclk = channel_data[sclk_ch]
    mosi = channel_data.get(mosi_ch) if mosi_ch >= 0 else None
    miso = channel_data.get(miso_ch) if miso_ch >= 0 else None
    sample_level = (1 - cpol) if cpha == 0 else cpol
    spans = find_active_spans(cs, cs_active_level)
    frames = []
    edge_periods = []

    for start, stop in spans:
        sample_edges = []
        for index in range(max(start + 1, 1), stop):
            if sclk[index] != sclk[index - 1] and sclk[index] == sample_level:
                sample_edges.append(index)
        edge_periods.extend([b - a for a, b in zip(sample_edges, sample_edges[1:])])
        mosi_bits = [mosi[index] for index in sample_edges] if mosi is not None else []
        miso_bits = [miso[index] for index in sample_edges] if miso is not None else []
        frames.append({
            "start_sample": start,
            "stop_sample": stop,
            "bit_count": len(sample_edges),
            "mosi_bytes": bits_to_bytes(mosi_bits, lsb_first) if mosi is not None else None,
            "miso_bytes": bits_to_bytes(miso_bits, lsb_first) if miso is not None else None,
        })

    sclk_hz = None
    if edge_periods:
        avg = sum(edge_periods[:64]) / min(len(edge_periods), 64)
        sclk_hz = samplerate / avg

    mode = (cpol << 1) | cpha
    print(
        f"protocol spi mode={mode} CH{cs_ch}=CS CH{sclk_ch}=SCLK "
        f"CH{mosi_ch}=MOSI CH{miso_ch}=MISO"
    )
    print("frames", len(frames))
    for index, frame in enumerate(frames[:max_transactions]):
        mosi_text = "" if frame["mosi_bytes"] is None else " ".join(
            f"{byte:02X}" for byte in frame["mosi_bytes"]
        )
        miso_text = "" if frame["miso_bytes"] is None else " ".join(
            f"{byte:02X}" for byte in frame["miso_bytes"]
        )
        print(
            f"SPI{index} start={frame['start_sample']} stop={frame['stop_sample']} "
            f"bits={frame['bit_count']} MOSI=[{mosi_text}] MISO=[{miso_text}]"
        )
    if sclk_hz is not None:
        print(f"sclk_hz~{sclk_hz:.1f}")

    return {
        "protocol": "spi",
        "cs_ch": cs_ch,
        "sclk_ch": sclk_ch,
        "mosi_ch": mosi_ch,
        "miso_ch": miso_ch,
        "cs_active_level": cs_active_level,
        "cpol": cpol,
        "cpha": cpha,
        "lsb_first": lsb_first,
        "sclk_hz": sclk_hz,
        "frames": frames,
    }


def write_csv_samples(path, channel_data, channels, limit):
    if not channels:
        return
    sample_count = min(limit, min(len(channel_data[channel]) for channel in channels))
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample", *[f"ch{channel}" for channel in channels]])
        for sample in range(sample_count):
            writer.writerow([sample, *[channel_data[channel][sample] for channel in channels]])


def run(args):
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    channels = parse_channel_list(args.channels)
    protocol_channels = set(channels)
    if args.protocol == "i2c":
        protocol_channels.update([args.scl_ch, args.sda_ch])
    if args.protocol == "spi":
        protocol_channels.update([args.cs_ch, args.sclk_ch])
        if args.mosi_ch >= 0:
            protocol_channels.add(args.mosi_ch)
        if args.miso_ch >= 0:
            protocol_channels.add(args.miso_ch)
    channels = sorted(protocol_channels)

    header, data, meta = capture_usb(args.samplerate, args.samples)
    header_path = output_dir / f"{args.prefix}_header.bin"
    raw_path = output_dir / f"{args.prefix}_raw.bin"
    summary_path = output_dir / f"{args.prefix}_summary.json"
    header_path.write_bytes(header)
    raw_path.write_bytes(data)

    print("FW_VERSION", meta["fw_version_hex"])
    print(
        "HW_STATUS before",
        hex(meta["hw_status_before"]),
        "HDL",
        hex(meta["hdl_version"]),
    )
    print("HW_STATUS after arm", hex(meta["hw_status_after_arm"]))
    print("captured", len(data), "bytes", meta["actual_samples"], "samples @", args.samplerate)
    print("raw", raw_path)

    channel_data = decode_channels(data, channels, meta["actual_samples"])
    summaries = {}
    for channel in channels:
        summary = summarize_channel(channel_data[channel], args.samplerate)
        summaries[f"ch{channel}"] = summary
        print(
            f"CH{channel}: edges={summary['edges']} "
            f"initial={summary.get('initial')} final={summary.get('final')} "
            f"high_ratio={summary.get('high_ratio', 0):.3f}"
        )

    protocol_result = {"protocol": "raw"}
    if args.protocol == "i2c":
        protocol_result = decode_i2c(
            channel_data,
            args.samplerate,
            args.scl_ch,
            args.sda_ch,
            args.max_transactions,
        )
    elif args.protocol == "spi":
        protocol_result = decode_spi(
            channel_data,
            args.samplerate,
            args.cs_ch,
            args.sclk_ch,
            args.mosi_ch,
            args.miso_ch,
            args.cs_active_level,
            args.spi_cpol,
            args.spi_cpha,
            args.spi_lsb_first,
            args.max_transactions,
        )

    if args.export_csv:
        csv_path = output_dir / f"{args.prefix}_samples.csv"
        write_csv_samples(csv_path, channel_data, channels, args.csv_samples)
        print("csv", csv_path)

    summary = {
        "capture": meta,
        "files": {
            "header": str(header_path),
            "raw": str(raw_path),
            "summary": str(summary_path),
        },
        "channels": summaries,
        "decode": protocol_result,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("summary", summary_path)


def main():
    parser = argparse.ArgumentParser(
        description="Direct USB capture from DSLogic-compatible analyzers."
    )
    parser.add_argument("--samplerate", type=int, default=10_000_000)
    parser.add_argument("--samples", type=int, default=262144)
    parser.add_argument("--channels", default="0,1,2,3")
    parser.add_argument("--protocol", choices=["raw", "i2c", "spi"], default="raw")
    parser.add_argument("--scl-ch", type=int, default=0)
    parser.add_argument("--sda-ch", type=int, default=1)
    parser.add_argument("--cs-ch", type=int, default=0)
    parser.add_argument("--sclk-ch", type=int, default=1)
    parser.add_argument("--mosi-ch", type=int, default=2)
    parser.add_argument("--miso-ch", type=int, default=3)
    parser.add_argument("--cs-active-level", type=int, choices=[0, 1], default=0)
    parser.add_argument("--spi-cpol", type=int, choices=[0, 1], default=0)
    parser.add_argument("--spi-cpha", type=int, choices=[0, 1], default=0)
    parser.add_argument("--spi-lsb-first", action="store_true")
    parser.add_argument("--max-transactions", type=int, default=40)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / ".dslogic-captures")
    parser.add_argument("--prefix", default="dslogic_capture")
    parser.add_argument("--export-csv", action="store_true")
    parser.add_argument("--csv-samples", type=int, default=200000)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()

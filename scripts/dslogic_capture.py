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
DSL_CTL_PROG_B = 3
DSL_CTL_LED = 5
DSL_CTL_INTRDY = 6
DSL_CTL_WORDWIDE = 7
DSL_CTL_START = 8
DSL_CTL_STOP = 9
DSL_CTL_BULK_WR = 10
DSL_CTL_NVM = 12
DSL_CTL_I2C_REG = 14
DSL_CTL_I2C_STATUS = 15

BM_WR_WORDWIDE = 1
BM_WR_INTRDY = 0x80
BM_NONE = 0
BM_SYS_CLR = 1 << 3
BM_FORCE_RDY = 1 << 1
BM_GPIF_DONE = 1 << 7
BM_FPGA_DONE = 1 << 6
BM_FPGA_INIT_B = 1 << 5
BM_LED_RED = 1 << 1
BM_LED_GREEN = 1 << 0
BM_WR_PROG_B = 1 << 2
BM_SECU_READY = 1 << 3
BM_SECU_PASS = 1 << 4

CTR0_ADDR = 0x70
VTH_ADDR = 0x78
ADCC_ADDR = 0x48
SEC_DATA_ADDR = 0x75
SEC_CTRL_ADDR = 0x73
SECU_START = 0x0513
SECU_CHECK = 0x0219
SECU_EEP_ADDR = 0x3C00
SECU_STEPS = 8
SECU_TRY_CNT = 8
HDL_VERSION_ADDR = 0x04
DSLOGIC_ATOMIC_SAMPLES = 64
DSLOGIC_ATOMIC_SIZE = 8
SAMPLES_ALIGN = 1023
STREAM_MODE_BIT = 12
CHANNEL_COUNT = 16
DEFAULT_FPGA_BITSTREAM = "DSLogicU2Pro16.bin"
DEFAULT_VTH_VOLTS = 1.0
ADC_CLK_INIT_500M = [
    (ADCC_ADDR + 2, 0, [0x01]),
    (ADCC_ADDR, 0, [0x01, 0x61, 0x00, 0x30]),
    (ADCC_ADDR, 0, [0x01, 0x40, 0xF1, 0x46]),
    (ADCC_ADDR, 10, [0x01, 0x62, 0x3D, 0x40]),
]


def as_u8(value):
    return value & 0xFF


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


def rd_reg(dev, addr):
    return ctl_rd(dev, DSL_CTL_I2C_STATUS, 1, addr)[0]


def rd_nvm(dev, addr, size):
    return ctl_rd(dev, DSL_CTL_NVM, size, addr)


def security_reset(dev):
    wr_reg(dev, SEC_CTRL_ADDR, 0)
    wr_reg(dev, SEC_CTRL_ADDR + 1, 0)
    time.sleep(0.010)
    wr_reg(dev, SEC_CTRL_ADDR, 1)
    wr_reg(dev, SEC_CTRL_ADDR + 1, 0)


def security_write(dev, cmd, value):
    wr_reg(dev, SEC_DATA_ADDR, value & 0xFF)
    wr_reg(dev, SEC_DATA_ADDR + 1, (value >> 8) & 0xFF)
    wr_reg(dev, SEC_CTRL_ADDR, cmd & 0xFF)
    wr_reg(dev, SEC_CTRL_ADDR + 1, (cmd >> 8) & 0xFF)


def security_read(dev):
    high = rd_reg(dev, SEC_DATA_ADDR + 1)
    low = rd_reg(dev, SEC_DATA_ADDR)
    return (high << 8) | low


def security_ready(dev):
    return bool(rd_reg(dev, SEC_CTRL_ADDR) & BM_SECU_READY)


def security_passed(dev):
    return bool(rd_reg(dev, SEC_CTRL_ADDR) & BM_SECU_PASS)


def run_security_check(dev):
    encryption_data = rd_nvm(dev, SECU_EEP_ADDR, SECU_STEPS * 2)
    encryption = struct.unpack("<" + "H" * SECU_STEPS, encryption_data)

    security_reset(dev)
    if security_passed(dev):
        return {"checked": True, "passed": False, "error": "pass bit set immediately after reset"}

    security_write(dev, SECU_START, 0)
    tries_left = SECU_TRY_CNT
    for step in range(SECU_STEPS - 1, -1, -1):
        if security_passed(dev):
            return {"checked": True, "passed": False, "error": f"pass bit set early at step {step}"}
        while not security_ready(dev):
            if tries_left <= 0:
                return {"checked": True, "passed": False, "error": f"ready timeout at step {step}"}
            tries_left -= 1
            time.sleep(0.001)
        if security_read(dev) != 0:
            return {"checked": True, "passed": False, "error": f"non-zero security read at step {step}"}
        security_write(dev, SECU_CHECK, encryption[step])

    return {"checked": True, "passed": True, "error": None}


def config_adc(dev, config):
    for dest, delay_ms, values in config:
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        for value in values:
            wr_reg(dev, dest, value)


def configure_frontend(dev, vth_volts=DEFAULT_VTH_VOLTS):
    vth_code = int(vth_volts / 3.3 * (1.5 / 2.5) * 255) & 0xFF
    wr_reg(dev, VTH_ADDR, vth_code)
    config_adc(dev, ADC_CLK_INIT_500M)
    return {
        "configured": True,
        "vth_volts": vth_volts,
        "vth_code": vth_code,
        "adc_clock": "500m",
    }


def read_device_status(dev):
    fw_version = ctl_rd(dev, DSL_CTL_FW_VERSION, 2)
    hw_status = ctl_rd(dev, DSL_CTL_HW_STATUS, 1)[0]
    i2c_status = ctl_rd(dev, DSL_CTL_I2C_STATUS, HDL_VERSION_ADDR + 1)
    return {
        "fw_version": fw_version,
        "hw_status": hw_status,
        "hdl_version": i2c_status[HDL_VERSION_ADDR],
    }


def candidate_bitstream_paths(explicit_path=None):
    if explicit_path is not None:
        yield Path(explicit_path)
        return

    env_path = None
    try:
        import os
        env_path = os.environ.get("DSLOGIC_FPGA_BITSTREAM")
    except Exception:
        env_path = None
    if env_path:
        yield Path(env_path)

    script_dir = Path(__file__).resolve().parent
    for base in [script_dir, script_dir.parent, script_dir.parent.parent]:
        yield base / "res" / DEFAULT_FPGA_BITSTREAM
        yield base / DEFAULT_FPGA_BITSTREAM

    cwd = Path.cwd().resolve()
    for base in [cwd, *cwd.parents]:
        yield base / ".tools" / "DSView-local" / "res" / DEFAULT_FPGA_BITSTREAM
        yield base / ".tools" / "dsview-src" / "DSView" / "res" / DEFAULT_FPGA_BITSTREAM
        yield base / "DSView" / "res" / DEFAULT_FPGA_BITSTREAM
        yield base / "res" / DEFAULT_FPGA_BITSTREAM

    for base in [
        Path("C:/Program Files/DreamSourceLab/DSView/res"),
        Path("C:/Program Files (x86)/DreamSourceLab/DSView/res"),
        Path("C:/Program Files/DSView/res"),
        Path("C:/Program Files (x86)/DSView/res"),
    ]:
        yield base / DEFAULT_FPGA_BITSTREAM


def find_fpga_bitstream(explicit_path=None):
    checked = []
    for path in candidate_bitstream_paths(explicit_path):
        path = path.expanduser()
        checked.append(str(path))
        if path.is_file():
            return path
    if explicit_path is not None:
        raise FileNotFoundError(f"FPGA bitstream not found: {explicit_path}")
    return None


def wait_hw_bit(dev, mask, timeout_s, label):
    deadline = time.time() + timeout_s
    last_status = 0
    while time.time() < deadline:
        last_status = ctl_rd(dev, DSL_CTL_HW_STATUS, 1)[0]
        if last_status & mask:
            return last_status
        time.sleep(0.010)
    raise TimeoutError(f"{label} timeout, last HW_STATUS={last_status:#x}")


def configure_fpga(dev, bitstream_path, timeout_s=2.0):
    data = Path(bitstream_path).read_bytes()

    # Recover from a previous aborted arm/capture before switching PROG_B.
    try:
        ctl_wr(dev, DSL_CTL_STOP)
        wr_reg(dev, CTR0_ADDR, BM_FORCE_RDY)
        wr_reg(dev, CTR0_ADDR, BM_NONE)
        ctl_wr(dev, DSL_CTL_INTRDY, bytes([as_u8(~BM_WR_INTRDY)]))
    except Exception:
        pass

    ctl_wr(dev, DSL_CTL_PROG_B, bytes([as_u8(~BM_WR_PROG_B)]))
    ctl_wr(dev, DSL_CTL_LED, bytes([as_u8(~BM_LED_GREEN & ~BM_LED_RED)]))
    ctl_wr(dev, DSL_CTL_PROG_B, bytes([BM_WR_PROG_B]))
    wait_hw_bit(dev, BM_FPGA_INIT_B, timeout_s, "FPGA INIT_B")

    ctl_wr(dev, DSL_CTL_INTRDY, bytes([as_u8(~BM_WR_INTRDY)]))
    size = len(data)
    ctl_wr(dev, DSL_CTL_BULK_WR, bytes([
        size & 0xFF,
        (size >> 8) & 0xFF,
        (size >> 16) & 0xFF,
    ]))
    transferred = dev.write(0x02, data, timeout=5000)
    if transferred != size:
        raise RuntimeError(f"FPGA bitstream short write: {transferred} != {size}")

    ctl_wr(dev, DSL_CTL_INTRDY, bytes([BM_WR_INTRDY]))
    wait_hw_bit(dev, BM_GPIF_DONE, timeout_s, "FPGA GPIF_DONE")
    ctl_wr(dev, DSL_CTL_INTRDY, bytes([as_u8(~BM_WR_INTRDY)]))
    wait_hw_bit(dev, BM_FPGA_DONE, timeout_s, "FPGA DONE")
    ctl_wr(dev, DSL_CTL_LED, bytes([BM_LED_GREEN]))
    ctl_wr(dev, DSL_CTL_WORDWIDE, bytes([BM_WR_WORDWIDE]))
    wr_reg(dev, CTR0_ADDR, BM_NONE)


def ensure_fpga_ready(
    dev,
    explicit_bitstream=None,
    skip_init=False,
    timeout_s=2.0,
    force_init=False,
    skip_security_check=False,
):
    initial = read_device_status(dev)
    if (
        not force_init and
        (initial["hw_status"] & BM_FPGA_DONE) and
        initial["hdl_version"] != 0
    ):
        wr_reg(dev, CTR0_ADDR, BM_NONE)
        ready = read_device_status(dev)
        security = (
            {"checked": False, "passed": None, "error": "skipped"}
            if skip_security_check else
            run_security_check(dev)
        )
        frontend = configure_frontend(dev)
        return initial, {
            "configured": False,
            "bitstream": None,
            "status": ready,
            "security": security,
            "frontend": frontend,
        }

    if skip_init:
        raise RuntimeError(
            "DSLogic capture core is not loaded "
            f"(HW_STATUS={initial['hw_status']:#x}, HDL={initial['hdl_version']:#x}). "
            "Run without --skip-fpga-init or provide --fpga-bitstream."
        )

    bitstream = find_fpga_bitstream(explicit_bitstream)
    if bitstream is None:
        raise RuntimeError(
            "DSLogic capture core is not loaded and no FPGA bitstream was found. "
            "Install DSView, place DSLogicU2Pro16.bin under a res directory, "
            "set DSLOGIC_FPGA_BITSTREAM, or pass --fpga-bitstream."
        )

    configure_fpga(dev, bitstream, timeout_s)
    final = read_device_status(dev)
    if not (final["hw_status"] & BM_FPGA_DONE) or final["hdl_version"] == 0:
        raise RuntimeError(
            "FPGA initialization completed but capture core still looks unavailable: "
            f"HW_STATUS={final['hw_status']:#x}, HDL={final['hdl_version']:#x}"
        )
    security = (
        {"checked": False, "passed": None, "error": "skipped"}
        if skip_security_check else
        run_security_check(dev)
    )
    frontend = configure_frontend(dev)
    return initial, {
        "configured": True,
        "bitstream": str(bitstream),
        "status": final,
        "security": security,
        "frontend": frontend,
    }


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


def initialize_usb(
    fpga_bitstream=None,
    skip_fpga_init=False,
    fpga_init_timeout=2.0,
    force_fpga_init=False,
    skip_security_check=False,
):
    dev = open_device()
    try:
        initial, fpga = ensure_fpga_ready(
            dev,
            explicit_bitstream=fpga_bitstream,
            skip_init=skip_fpga_init,
            timeout_s=fpga_init_timeout,
            force_init=force_fpga_init,
            skip_security_check=skip_security_check,
        )
        return {
            "initial": initial,
            "fpga": fpga,
        }
    finally:
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass


def capture_usb(
    samplerate,
    limit_samples,
    fpga_bitstream=None,
    skip_fpga_init=False,
    fpga_init_timeout=2.0,
    force_fpga_init=False,
    skip_security_check=False,
):
    dev = open_device()
    try:
        initial_status, fpga = ensure_fpga_ready(
            dev,
            explicit_bitstream=fpga_bitstream,
            skip_init=skip_fpga_init,
            timeout_s=fpga_init_timeout,
            force_init=force_fpga_init,
            skip_security_check=skip_security_check,
        )
        ready_status = fpga["status"]
        fw_version = ready_status["fw_version"]
        hdl = ready_status["hdl_version"]
        hw_before = ready_status["hw_status"]

        setting, actual_samples, actual_bytes = build_setting(limit_samples, samplerate)
        ctl_wr(dev, DSL_CTL_STOP)
        wr_reg(dev, CTR0_ADDR, BM_NONE)
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
            "hdl_version_initial": initial_status["hdl_version"],
            "hw_status_before": hw_before,
            "hw_status_initial": initial_status["hw_status"],
            "hw_status_after_arm": hw_after_arm,
            "fpga_configured": fpga["configured"],
            "fpga_bitstream": fpga["bitstream"],
            "security": fpga["security"],
            "frontend": fpga["frontend"],
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


def decode_channels_sample_major(data, channels, samples=None):
    if len(data) >= 2:
        sample_count = len(data) // 2
        if samples is not None:
            sample_count = min(sample_count, samples)
        decoded = {channel: [] for channel in channels}
        for sample_index in range(sample_count):
            word = data[sample_index * 2] | (data[sample_index * 2 + 1] << 8)
            for channel in channels:
                decoded[channel].append((word >> channel) & 1)
        return decoded

    return {channel: [] for channel in channels}


def decode_channels_channel_major(data, channels, samples=None, ch_num=CHANNEL_COUNT):
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


def choose_decode_layout(meta, requested):
    if requested != "auto":
        return requested
    # HDL 0x0e, observed after DSView/DreamSourceLab initialization, returns the
    # older channel-major 64-sample block layout.  Cold devices with HDL 0x00 may
    # not have their capture core loaded yet; keep sample-major as the conservative
    # fallback so the existing error/status output stays readable.
    if meta.get("hdl_version") == 0x0E:
        return "channel-major"
    return "sample-major"


def decode_channels(data, channels, samples=None, ch_num=CHANNEL_COUNT, layout="sample-major"):
    if layout == "channel-major":
        return decode_channels_channel_major(data, channels, samples, ch_num)
    return decode_channels_sample_major(data, channels, samples)


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
    if args.init_only:
        init = initialize_usb(
            fpga_bitstream=args.fpga_bitstream,
            skip_fpga_init=args.skip_fpga_init,
            fpga_init_timeout=args.fpga_init_timeout,
            force_fpga_init=args.force_fpga_init,
            skip_security_check=args.skip_security_check,
        )
        initial = init["initial"]
        final = init["fpga"]["status"]
        print("FW_VERSION", final["fw_version"].hex(" "))
        print(
            "HW_STATUS initial",
            hex(initial["hw_status"]),
            "HDL initial",
            hex(initial["hdl_version"]),
        )
        print(
            "FPGA",
            "configured" if init["fpga"]["configured"] else "already-ready",
            init["fpga"]["bitstream"] or "",
        )
        print(
            "HW_STATUS ready",
            hex(final["hw_status"]),
            "HDL ready",
            hex(final["hdl_version"]),
        )
        print("security", init["fpga"]["security"])
        print("frontend", init["fpga"]["frontend"])
        summary_path = output_dir / f"{args.prefix}_summary.json"
        summary_path.write_text(json.dumps(init, indent=2, default=str), encoding="utf-8")
        print("summary", summary_path)
        return

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

    header, data, meta = capture_usb(
        args.samplerate,
        args.samples,
        fpga_bitstream=args.fpga_bitstream,
        skip_fpga_init=args.skip_fpga_init,
        fpga_init_timeout=args.fpga_init_timeout,
        force_fpga_init=args.force_fpga_init,
        skip_security_check=args.skip_security_check,
    )
    header_path = output_dir / f"{args.prefix}_header.bin"
    raw_path = output_dir / f"{args.prefix}_raw.bin"
    summary_path = output_dir / f"{args.prefix}_summary.json"
    header_path.write_bytes(header)
    raw_path.write_bytes(data)

    print("FW_VERSION", meta["fw_version_hex"])
    print(
        "HW_STATUS initial",
        hex(meta["hw_status_initial"]),
        "HDL initial",
        hex(meta["hdl_version_initial"]),
    )
    print(
        "FPGA",
        "configured" if meta["fpga_configured"] else "already-ready",
        meta["fpga_bitstream"] or "",
    )
    print(
        "HW_STATUS before",
        hex(meta["hw_status_before"]),
        "HDL",
        hex(meta["hdl_version"]),
    )
    print("HW_STATUS after arm", hex(meta["hw_status_after_arm"]))
    print("security", meta["security"])
    print("frontend", meta["frontend"])
    print("captured", len(data), "bytes", meta["actual_samples"], "samples @", args.samplerate)
    print("raw", raw_path)

    decode_layout = choose_decode_layout(meta, args.decode_layout)
    print("decode_layout", decode_layout)
    channel_data = decode_channels(data, channels, meta["actual_samples"], layout=decode_layout)
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
        "decode_layout": decode_layout,
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
    parser.add_argument(
        "--decode-layout",
        choices=["auto", "sample-major", "channel-major"],
        default="auto",
        help="Raw LA_CROSS_DATA layout. Auto selects channel-major for HDL 0x0e.",
    )
    parser.add_argument(
        "--fpga-bitstream",
        type=Path,
        default=None,
        help="Path to DSLogicU2Pro16.bin. Defaults to DSView/res auto-discovery.",
    )
    parser.add_argument(
        "--skip-fpga-init",
        action="store_true",
        help="Do not load the FPGA capture core if the analyzer is cold-started.",
    )
    parser.add_argument(
        "--force-fpga-init",
        action="store_true",
        help="Reload the FPGA bitstream even when the analyzer already looks ready.",
    )
    parser.add_argument(
        "--skip-security-check",
        action="store_true",
        help="Skip the DSLogic U2Pro16 security handshake normally done by DSView.",
    )
    parser.add_argument(
        "--fpga-init-timeout",
        type=float,
        default=2.0,
        help="Seconds to wait for FPGA INIT/DONE status bits during initialization.",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Initialize the analyzer and exit without capturing samples.",
    )
    parser.add_argument("--max-transactions", type=int, default=40)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / ".dslogic-captures")
    parser.add_argument("--prefix", default="dslogic_capture")
    parser.add_argument("--export-csv", action="store_true")
    parser.add_argument("--csv-samples", type=int, default=200000)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()

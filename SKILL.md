---
name: dslogic-capture
description: Direct USB capture, raw sample export, and optional protocol decoding for DreamSourceLab DSLogic-compatible logic analyzers, especially DSLogic U2Pro16 / DreamSourceLab devices exposed as USB VID:PID 2A0E:002D with WinUSB/libusb. Use when Codex needs to capture digital signals from a logic analyzer without DSView, inspect channel edges, save raw logic samples, decode I2C/IIC or other protocols, debug MCU pins, verify ACK/NACK or bus timing, or add a new protocol decoder to the same reusable DSLogic capture workflow.
---

# DSLogic Capture

## Model

Treat this as one general logic-analyzer skill, not a per-protocol skill. The USB capture path is shared; protocol decoders are options inside `scripts/dslogic_capture.py`.

Use the bundled script to:

- control the DSLogic-compatible analyzer directly over USB
- capture raw `LA_CROSS_DATA`
- summarize selected channel edges
- optionally decode a protocol from those channels
- save binary and JSON artifacts for later inspection

## Workflow

1. Close DSView; the analyzer USB interface is exclusive.
2. Confirm the analyzer appears as `2A0E:002D` with WinUSB/libusb.
3. If the analyzer LED is red after USB plug-in, initialize and enable the FPGA capture core before decoding. A normal capture command performs this automatically; use `--init-only` when the user only wants to bring the analyzer to the ready/green-LED state. The script auto-loads `DSLogicU2Pro16.bin` when it can find DSView resources, or use `--fpga-bitstream`.
4. Capture raw first when the signal is unknown.
5. Select a protocol decoder only after the channel mapping is known.
6. Extend this same script for new protocols instead of creating another skill.
7. Run only one capture process at a time; parallel captures will race for the same USB interface.

Initialize only, without capturing:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --init-only `
  --output-dir D:\Codes\HPM\.tools
```

After unplug/replug, run the `--init-only` command or any capture command before expecting protocol decode output. If a wrong or stale FPGA image may already be loaded, add `--force-fpga-init`. Prefer the bitstream from the same DSView distribution that works manually, for example `D:\Codes\HPM\.tools\DSView-local\res\DSLogicU2Pro16.bin`.

For DSLogic U2Pro16, the script also performs the DSView security handshake and front-end setup after FPGA initialization, including the 1.0 V threshold and 500 MHz ADC clock setup. Use `--skip-security-check` only for diagnosis.

Raw capture and channel edge summary:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --samplerate 1000000 `
  --samples 1048576 `
  --channels 0,1,2,3 `
  --protocol raw `
  --output-dir D:\Codes\HPM\.tools
```

I2C decode, with `CH0=SCL` and `CH1=SDA`:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --samplerate 1000000 `
  --samples 1048576 `
  --protocol i2c `
  --scl-ch 0 `
  --sda-ch 1 `
  --max-transactions 130 `
  --output-dir D:\Codes\HPM\.tools
```

SPI decode, with `CH0=CS`, `CH1=SCLK`, `CH2=MOSI`, and `CH3=MISO`:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --samplerate 10000000 `
  --samples 262144 `
  --protocol spi `
  --cs-ch 0 `
  --sclk-ch 1 `
  --mosi-ch 2 `
  --miso-ch 3 `
  --spi-cpol 0 `
  --spi-cpha 0 `
  --output-dir D:\Codes\HPM\.tools
```

Use `--samplerate 10000000 --samples 262144` for short high-resolution checks. Use lower samplerates and more samples for longer scans.

## Outputs

The script writes files named from `--prefix`:

- `<prefix>_header.bin`
- `<prefix>_raw.bin`
- `<prefix>_summary.json`
- `<prefix>_samples.csv` when `--export-csv` is set

The summary JSON stores samplerate, aligned sample count, channel edge summaries, and protocol decode results.

## Current Decoders

- `raw`: capture and summarize channel transitions only.
- `i2c`: decode START/STOP, address/RW, ACK/NACK, and estimate SCL frequency. Interpret `ack_bit=0` as ACK and `ack_bit=1` as NACK.
- `spi`: decode CS-framed SPI bytes on MOSI and MISO. Configure `--spi-cpol`, `--spi-cpha`, `--spi-lsb-first`, and `--cs-active-level` to match the bus.

Typical I2C no-device scan output:

```text
addr=0x50 rw=0 ack_bit=1 NACK
```

Expected responding slave:

```text
addr=0x50 rw=0 ack_bit=0 ACK
```

## Extending

When the user asks for SPI, UART, CAN, 1-Wire, PWM, GPIO timing, or another protocol, keep using this skill. Add a decoder function to `scripts/dslogic_capture.py`, register it in the protocol dispatch, and keep raw capture unchanged.

For new decoders, preserve these conventions:

- accept channel numbers as CLI arguments
- write decoded data into `<prefix>_summary.json`
- print a concise human-readable decode
- keep raw binary output available for re-decode

## Troubleshooting

- If the device cannot be opened, close DSView and any other process using the analyzer. Do not run two capture commands in parallel.
- If Python imports fail, install `pyusb` and `libusb-package` in the active Python environment.
- If `HDL=0x0` or the LED stays red, the USB firmware is present but the FPGA capture core is not loaded. Re-run without `--skip-fpga-init`, install DSView resources, set `DSLOGIC_FPGA_BITSTREAM`, or pass `--fpga-bitstream <path-to-DSLogicU2Pro16.bin>`.
- If no edges appear, verify probe channel, ground reference, signal voltage, and capture duration.
- If a protocol decode fails, inspect `--protocol raw` edge summaries first and correct channel mapping.
- Do not treat this as DAP USB capture. DAP flashes/runs firmware; this captures the logic analyzer USB stream containing sampled pin levels.

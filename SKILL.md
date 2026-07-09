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
3. If the analyzer LED is red after USB plug-in, initialize and enable the FPGA capture core before decoding. Opening DSView once can make the LED turn green because DSView performs this cold-start setup implicitly; the script must do the same setup before any useful capture. A normal capture command performs this automatically; use `--init-only` when the user only wants to bring the analyzer to the ready/green-LED state. The script auto-loads `DSLogicU2Pro16.bin` when it can find DSView resources, or use `--fpga-bitstream`.
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

After unplug/replug, run the `--init-only` command or any capture command before expecting protocol decode output. A red LED normally means only the USB firmware is alive; the analyzer is not ready until the FPGA image is loaded and the capture core is enabled. If a wrong or stale FPGA image may already be loaded, add `--force-fpga-init`. Prefer the bitstream from the same DSView distribution that works manually, for example `D:\Codes\HPM\.tools\DSView-local\res\DSLogicU2Pro16.bin`.

For DSLogic U2Pro16, the script also performs the DSView security handshake and front-end setup after FPGA initialization, including the 1.0 V threshold and 500 MHz ADC clock setup. Use `--skip-security-check` only for diagnosis.

Raw capture and channel edge summary:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --samplerate 1000000 `
  --samples 1048576 `
  --channels '0,1,2,3' `
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

CAN/CAN FD decode from one digital channel:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --samplerate 50000000 `
  --samples 25000000 `
  --channels '0' `
  --protocol can `
  --can-ch 0 `
  --can-bitrate 500000 `
  --can-data-bitrate 2000000 `
  --can-sample-point 0.875 `
  --can-data-sample-point 0.750 `
  --can-dominant-level 0 `
  --output-dir D:\Codes\HPM\.tools
```

The CAN decoder works from a single digital waveform. It does not require a
differential CANH/CANL pair if one analyzer channel already sees a valid
recessive/dominant digital level, for example a transceiver RXD/TXD pin or one
bus-side probe that DSView can also decode. Use `--can-dominant-level 0` for the
normal recessive-high/dominant-low waveform; flip it to `1` only when the probed
signal is inverted.

For HPM/xrobot CAN FD at 500 kbit/s nominal and 2 Mbit/s data phase, prefer
`--samplerate 50000000`. A 10 MHz capture can identify activity and often decode
IDs/DLCs, but 2 Mbit/s data phase has only 5 samples per bit and is too tight for
reliable long-payload decoding. The decoder reports standard/extended ID,
classic/FD, RTR, BRS, ESI, DLC, payload bytes, length histograms, and common IDs.
It does not validate CRC yet, so use an external CAN tool for CRC/error-frame
validation when that matters.

Use `--samplerate 10000000 --samples 262144` for short high-resolution checks. Use lower samplerates and more samples for longer scans.

## Stable Windows/PowerShell Workflow

Use these guardrails for repeatable captures from Codex on Windows:

- Quote comma-separated arguments in PowerShell. For example, use `--channels '0,1,2,3'`; unquoted `0,1,2,3` can be passed as multiple native-process arguments and fail with `unrecognized arguments`.
- After USB unplug/replug or a red analyzer LED, run `--init-only` or a normal capture command with the correct `--fpga-bitstream` before expecting decode output. A green LED means the FPGA capture core was loaded and enabled.
- Prefer a short but complete first capture for startup SPI checks, such as `--samplerate 10000000 --samples 20000000` for about 2 seconds. Increase to larger captures only after the short run produces a summary.
- If running the script in a PowerShell background job, do not kill or `Remove-Job -Force` the job before `<prefix>_summary.json` is written. If a wait times out, either wait longer or rerun with fewer samples.
- Keep the first pass raw or low-transaction-count protocol decode. Once the edge summary confirms channel activity and polarity, increase `--max-transactions`.

## HPM SPI Example

For the HPM5361EVKLite SPI CS0 + W25Q128 wiring used during validation:

- `CH3=CS`
- `CH1=SCLK`
- `CH0=MOSI`
- `CH2=MISO`
- SPI mode 0: `--spi-cpol 0 --spi-cpha 0`

Stable command:

```powershell
python C:\Users\asus\.codex\skills\dslogic-capture\scripts\dslogic_capture.py `
  --samplerate 10000000 `
  --samples 20000000 `
  --protocol spi `
  --channels '0,1,2,3' `
  --cs-ch 3 `
  --sclk-ch 1 `
  --mosi-ch 0 `
  --miso-ch 2 `
  --spi-cpol 0 `
  --spi-cpha 0 `
  --max-transactions 160 `
  --output-dir D:\Codes\HPM\.tools `
  --prefix hpm5361_spi_capture `
  --fpga-bitstream D:\Codes\HPM\.tools\DSView-local\res\DSLogicU2Pro16.bin
```

Known-good HPM SPI command frames include `MOSI=[9F 00 00 00]` plus commands `90`, `AB`, `5A`, `05`, `35`, `15`, and `03` framed by CS. If CS, SCLK, and MOSI decode cleanly but JEDEC ID reads wrong, verify the flash module, MISO wiring, pull-up/pull-down behavior, and board pin sharing before changing the decoder.

The HPM SPI test firmware may periodically switch into a GPIO logic-analyzer marker phase. In that phase the summary can show low-frequency edges, empty SPI bytes, and `sclk_hz` near GPIO marker timing rather than the SPI clock. Wait for the marker phase to finish, reset/retrigger the firmware, or use a clean SPI-only firmware loop before treating that output as an SPI decoder failure.

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
- `can`: decode single-channel CAN/CAN FD frames. Configure nominal/data bitrate,
  sample points, channel, and dominant level. CRC is not checked.

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

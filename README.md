# DSLogic Capture

Direct USB capture and protocol decoding workflow for DreamSourceLab
DSLogic-compatible logic analyzers.

This repository is a Codex skill plus a reusable Python capture script. It is
designed as one general logic-analyzer workflow: capture raw samples first, then
optionally decode protocols such as I2C or SPI from the same saved data path.

## What It Does

- Opens DSLogic-compatible USB analyzers directly, without DSView.
- Captures raw `LA_CROSS_DATA` samples from selected logic channels.
- Saves raw binary artifacts for later inspection or re-decoding.
- Summarizes channel edges to help identify signal mapping.
- Decodes supported protocols into a JSON summary and concise terminal output.
- Keeps protocol decoders inside one shared capture tool instead of splitting
  each protocol into a separate skill.

## Supported Hardware

The current USB path targets DreamSourceLab-compatible devices exposed as:

```text
USB VID:PID 2A0E:002D
```

On Windows, the analyzer interface must be bound to WinUSB/libusb. DSView must be
closed before capture because the USB interface is exclusive.

## Requirements

- Python 3
- `pyusb`
- `libusb-package`
- A DSLogic-compatible analyzer using WinUSB/libusb

Install Python dependencies:

```powershell
python -m pip install pyusb libusb-package
```

## Quick Start

Raw capture with channel edge summary:

```powershell
python scripts\dslogic_capture.py `
  --samplerate 1000000 `
  --samples 1048576 `
  --channels 0,1,2,3 `
  --protocol raw `
  --output-dir .\.dslogic-captures
```

I2C decode with `CH0=SCL` and `CH1=SDA`:

```powershell
python scripts\dslogic_capture.py `
  --samplerate 1000000 `
  --samples 1048576 `
  --protocol i2c `
  --scl-ch 0 `
  --sda-ch 1 `
  --max-transactions 130 `
  --output-dir .\.dslogic-captures
```

SPI decode with `CH0=CS`, `CH1=SCLK`, `CH2=MOSI`, and `CH3=MISO`:

```powershell
python scripts\dslogic_capture.py `
  --samplerate 10000000 `
  --samples 262144 `
  --protocol spi `
  --cs-ch 0 `
  --sclk-ch 1 `
  --mosi-ch 2 `
  --miso-ch 3 `
  --spi-cpol 0 `
  --spi-cpha 0 `
  --output-dir .\.dslogic-captures
```

## Output Files

The script writes files named from `--prefix`:

- `<prefix>_header.bin`: capture header bytes from the analyzer stream.
- `<prefix>_raw.bin`: raw packed logic sample bytes.
- `<prefix>_summary.json`: samplerate, sample count, edge summaries, and decode
  results.
- `<prefix>_samples.csv`: optional sample export when `--export-csv` is used.

## Protocol Decoders

Current decoders:

- `raw`: capture and summarize channel transitions only.
- `i2c`: decode START/STOP, address/RW, ACK/NACK, and estimate SCL frequency.
- `spi`: decode CS-framed SPI bytes on MOSI and MISO with configurable CPOL,
  CPHA, bit order, and CS polarity.

I2C ACK convention:

```text
ack_bit=0 -> ACK
ack_bit=1 -> NACK
```

## Codex Skill Usage

This repository can be installed as a Codex skill. The skill instructions live in
`SKILL.md`, and the capture implementation lives in `scripts/dslogic_capture.py`.

The key workflow is:

1. Close DSView.
2. Confirm the analyzer appears as `2A0E:002D` with WinUSB/libusb.
3. Capture `--protocol raw` first if the channel mapping is unknown.
4. Select a decoder only after SCL/SDA, CS/SCLK/MOSI/MISO, or other mappings are
   known.
5. Save raw artifacts so captures can be inspected or decoded again later.

## Extending

Add new protocol support by extending `scripts/dslogic_capture.py`:

1. Add CLI arguments for the required channel mapping and protocol options.
2. Implement a decoder that consumes the raw sample stream.
3. Register the decoder in the protocol dispatch.
4. Store decoded data in `<prefix>_summary.json`.
5. Keep the raw capture path unchanged.

This keeps UART, CAN, 1-Wire, PWM, GPIO timing, or future protocol support inside
the same reusable logic-analyzer workflow.

## Troubleshooting

- `DSLogic-compatible device 2A0E:002D not found`: check USB connection, driver
  binding, and whether DSView is still open.
- No signal edges: verify probe channel, ground reference, signal voltage, sample
  rate, and capture duration.
- I2C decode shows only NACK: verify address, pull-ups, wiring, and whether
  `CH0/CH1` match SCL/SDA.
- SPI decode looks shifted: verify CPOL/CPHA, CS polarity, and bit order.
- Do not treat this as DAP USB capture. DAP flashes or debugs the MCU; this tool
  captures sampled logic-analyzer pin levels.

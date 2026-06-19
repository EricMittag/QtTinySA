# tinySA CLI Tools

Small headless helpers for tinySA Ultra testing from a terminal.

These scripts use the tinySA USB serial port at 576000 baud and auto-detect the
device by USB VID/PID. Pass `--port /dev/ttyACM0` if more than one compatible
device is connected.

## Generator CLI

Use `src/tinysa_generator_cli.py` for CW generator control.

Quick 880 MHz test:

```bash
.venv/bin/python src/tinysa_generator_cli.py test880 --duration 30 --handoff-sweep --verbose
```

Arbitrary frequency:

```bash
.venv/bin/python src/tinysa_generator_cli.py start --freq 875e6 --level -18.5 --duration 30 --handoff-sweep --verbose
```

Frequency values accept plain Hz or scientific notation:

```text
100e3
30e6
875e6
2.4e9
2400000000
```

Useful commands:

```bash
.venv/bin/python src/tinysa_generator_cli.py probe
.venv/bin/python src/tinysa_generator_cli.py stop
.venv/bin/python src/tinysa_generator_cli.py handoff
.venv/bin/python src/tinysa_generator_cli.py idle
```

`handoff` turns generator output off and returns the tinySA to analyzer input
mode. `idle` additionally pauses the sweep, which is useful before leaving the
unit powered but quiet.

The generator CLI intentionally defaults to the UI command path for frequency
and level changes. On the tested firmware, direct shell commands such as
`sweep cw` can wedge the USB shell in generator mode.

## Sweep CLI

Use `src/tinysa_sweep_cli.py` for one-shot analyzer sweeps saved as CSV.

```bash
.venv/bin/python src/tinysa_sweep_cli.py --center 880e6 --span 2e6 --points 300 --verbose
```

This writes `frequency_hz,dbm` rows to `sweep_files/<timestamp>_sweep.csv`
unless `--out` is provided.

After generator testing, use `--handoff-sweep` on the generator command or run:

```bash
.venv/bin/python src/tinysa_generator_cli.py handoff --verbose
.venv/bin/python src/tinysa_sweep_cli.py --center 880e6 --span 2e6 --points 300 --verbose
```

## Plot Sweep CSVs

Use `src/spectrum_plotter.py` to plot sweep CSV files and print strongest peaks.

```bash
.venv/bin/python src/spectrum_plotter.py sweep_files/2026-06-19-170837_sweep.csv
```

Save plots and peak CSVs without opening a window:

```bash
.venv/bin/python src/spectrum_plotter.py sweep_files/*.csv --save --no-show
```

## Safety Notes

Check cabling, attenuation, and maximum input ratings before connecting the
tinySA to other RF equipment. For overnight or weekend parking, prefer physical
power-off when possible. If leaving the unit connected, run:

```bash
.venv/bin/python src/tinysa_generator_cli.py idle --verbose
```

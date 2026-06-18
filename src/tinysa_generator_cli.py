#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Minimal headless tinySA Ultra signal generator CLI."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Iterable

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - exercised only without pyserial
    raise SystemExit("pyserial is required. Install the project requirements first.") from exc


BAUDRATE = 576000
TINYSA_VID = 0x0483
TINYSA_PID = 0x5740
PROMPT = b"ch> "
SINE_REGION_MAX_HZ = 800_000_000
LEVEL_MIN_DBM = -115.0
LEVEL_MAX_DBM = -19.0


class GeneratorError(RuntimeError):
    """Raised when generator control cannot complete cleanly."""


def parse_frequency(value: str) -> int:
    """Parse CLI frequency values such as '2400e6' into integer Hz."""
    try:
        frequency = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid frequency value: {value!r}") from exc

    if frequency <= 0:
        raise argparse.ArgumentTypeError("frequency values must be positive")
    return int(round(frequency))


def parse_level(value: str) -> float:
    try:
        level = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid level value: {value!r}") from exc

    if level < LEVEL_MIN_DBM or level > LEVEL_MAX_DBM:
        raise argparse.ArgumentTypeError(
            f"level must be between {LEVEL_MIN_DBM:g} and {LEVEL_MAX_DBM:g} dBm"
        )
    return level


def parse_duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration value: {value!r}") from exc

    if duration <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return duration


def require_ultra(version: str) -> None:
    if not version.startswith("tinySA4"):
        raise GeneratorError(f"tinySA Ultra required; device reported {version!r}")


def output_command_for_mode(mode: str) -> str:
    if mode == "clean":
        return "output normal\r"
    if mode == "accurate":
        return "output mixer\r"
    raise ValueError(f"unknown output mode: {mode}")


def output_region_description(frequency_hz: int, mode: str) -> str:
    if frequency_hz < SINE_REGION_MAX_HZ:
        return "sine-wave region below 800 MHz"
    if mode == "clean":
        return "cleanest signal / square-wave region above 800 MHz"
    return "highest accuracy mode / reduced harmonics with possible strong spurs"


def start_commands(frequency_hz: int, level_dbm: float, mode: str) -> list[str]:
    return [
        "abort\r",
        "mode output\r",
        output_command_for_mode(mode),
        f"sweep cw {frequency_hz}\r",
        f"level {level_dbm:g}\r",
        "output on\r",
    ]


def stop_commands() -> list[str]:
    return [
        "abort\r",
        "mode output\r",
        "output off\r",
    ]


def find_tinysa_ports() -> list[object]:
    return [
        port
        for port in list_ports.comports()
        if port.vid == TINYSA_VID and port.pid == TINYSA_PID
    ]


def choose_port(port_override: str | None) -> str:
    if port_override:
        return port_override

    ports = find_tinysa_ports()
    if not ports:
        raise GeneratorError("no tinySA device found; pass --port to select a serial device")
    if len(ports) > 1:
        port_list = ", ".join(port.device for port in ports)
        raise GeneratorError(f"multiple tinySA devices found ({port_list}); pass --port")
    return ports[0].device


def clear_buffer(usb: serial.Serial) -> None:
    while usb.in_waiting:
        usb.read_all()
        time.sleep(0.01)


def serial_query(usb: serial.Serial, command: str) -> str:
    payload = command.encode()
    usb.write(payload)
    usb.read_until(payload + b"\n")
    response = usb.read_until(PROMPT)
    if not response.endswith(PROMPT):
        raise GeneratorError(f"timed out waiting for prompt after {command.strip()!r}")
    return response[: -len(PROMPT)].decode(errors="replace").strip()


def serial_write(usb: serial.Serial, command: str) -> None:
    payload = command.encode()
    usb.write(payload)
    response = usb.read_until(PROMPT)
    if not response.endswith(PROMPT):
        raise GeneratorError(f"timed out waiting for prompt after {command.strip()!r}")


def send_commands(usb: serial.Serial, commands: list[str]) -> None:
    for command in commands:
        logging.info("send: %s", command.strip())
        serial_write(usb, command)


def open_ultra(port: str) -> serial.Serial:
    logging.info("opening %s at %s baud", port, BAUDRATE)
    usb = serial.Serial(port, baudrate=BAUDRATE, timeout=2)
    try:
        clear_buffer(usb)
        version = serial_query(usb, "version\r")
        require_ultra(version)
        logging.info("device: %s", version)
        return usb
    except Exception:
        usb.close()
        raise


def run_start(args: argparse.Namespace) -> int:
    port = choose_port(args.port)
    commands = start_commands(args.freq, args.level, args.mode)
    region = output_region_description(args.freq, args.mode)

    with open_ultra(port) as usb:
        send_commands(usb, commands)
        print("Configured tinySA Ultra generator:")
        print(f"frequency: {args.freq / 1e6:.6f} MHz")
        print(f"level: {args.level:.1f} dBm")
        print(f"mode: {args.mode} ({region})")
        print("output: on")

        if args.duration is not None:
            print(f"duration: {args.duration:g} s")
            time.sleep(args.duration)
            send_commands(usb, ["output off\r"])
            print("output: off")
        else:
            print("duration: persistent; run the stop command to turn output off")
    return 0


def run_stop(args: argparse.Namespace) -> int:
    port = choose_port(args.port)
    with open_ultra(port) as usb:
        send_commands(usb, stop_commands())
    print("tinySA Ultra generator output: off")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control tinySA Ultra CW generator output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start CW RF output")
    start.add_argument("--freq", required=True, type=parse_frequency, help="output frequency in Hz")
    start.add_argument("--level", required=True, type=parse_level, help="output level in dBm")
    start.add_argument(
        "--mode",
        choices=("clean", "accurate"),
        default="clean",
        help="output path above 800 MHz: clean=normal, accurate=mixer (default: clean)",
    )
    start.add_argument("--duration", type=parse_duration, help="seconds to leave output on before stopping")
    start.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    start.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    start.set_defaults(func=run_start)

    stop = subparsers.add_parser("stop", help="turn RF output off")
    stop.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    stop.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    stop.set_defaults(func=run_stop)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(format="%(message)s", level=logging.INFO if args.verbose else logging.WARNING)

    try:
        return args.func(args)
    except (OSError, serial.SerialException, GeneratorError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

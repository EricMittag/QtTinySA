#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Minimal headless tinySA Ultra signal generator CLI."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Iterable

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - exercised only without pyserial
    raise SystemExit("pyserial is required. Install the project requirements first.") from exc


BAUDRATE = 576000
COMMAND_TIMEOUT_SECONDS = 25
WRITE_TIMEOUT_SECONDS = 2
TINYSA_VID = 0x0483
TINYSA_PID = 0x5740
PROMPT = b"ch> "
LCD_WIDTH = 480
LCD_HEIGHT = 320
CAPTURE_BYTES = LCD_WIDTH * LCD_HEIGHT * 2
SINE_REGION_MAX_HZ = 800_000_000
ULTRA_CLEAN_MAX_HZ = 4_400_000_000
ULTRA_ACCURATE_MAX_HZ = 5_400_000_000
ZS407_ACCURATE_MAX_HZ = 7_300_000_000
LEVEL_MIN_DBM = -115.0
LEVEL_MAX_DBM = -18.0
DEFAULT_TEST_LEVEL_DBM = -18.5
DEFAULT_CAPTURE_DIR = Path("captures")
DEFAULT_GROUP_DELAY_SECONDS = 1.0
SESSION_RESTORE_COMMANDS = ["abort on\r"]
HANDOFF_RESTORE_COMMANDS = ["release\r", "abort on\r"]
FREQUENCY_RE = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)?|\.\d+)\s*(?P<suffix>[kmg]?)\s*(?:hz)?\s*$",
    re.IGNORECASE,
)


class GeneratorError(RuntimeError):
    """Raised when generator control cannot complete cleanly."""


def parse_frequency(value: str) -> int:
    """Parse CLI frequency values such as '880e6' into integer Hz."""
    match = FREQUENCY_RE.match(value)
    if match:
        multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[
            match.group("suffix").lower()
        ]
        frequency = float(match.group("number")) * multiplier
    else:
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


def parse_nonnegative_duration(value: str) -> float:
    try:
        duration = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration value: {value!r}") from exc

    if duration < 0:
        raise argparse.ArgumentTypeError("duration must be non-negative")
    return duration


def require_ultra(version: str) -> None:
    if "tinySA4" not in version:
        raise GeneratorError(f"tinySA Ultra required; device reported {version!r}")


def is_zs407(version: str) -> bool:
    return "ZS407" in version.upper()


def frequency_limit_for_mode(high_mode: str, version: str | None = None) -> int:
    if high_mode == "clean":
        return ULTRA_CLEAN_MAX_HZ
    if version and is_zs407(version):
        return ZS407_ACCURATE_MAX_HZ
    return ULTRA_ACCURATE_MAX_HZ


def output_path_command(frequency_hz: int) -> str:
    if frequency_hz <= SINE_REGION_MAX_HZ:
        return "mode low output\r"
    return "mode output\r"


def high_mode_command(high_mode: str) -> str:
    if high_mode == "keep":
        return ""
    if high_mode == "clean":
        return "output normal\r"
    if high_mode == "accurate":
        return "output mixer\r"
    raise ValueError(f"unknown high output mode: {high_mode}")


def output_region_description(frequency_hz: int, high_mode: str) -> str:
    if frequency_hz <= SINE_REGION_MAX_HZ:
        return "sine-wave region below 800 MHz"
    if high_mode == "keep":
        return "above 800 MHz using the tinySA's current output-path setting"
    if high_mode == "clean":
        return "cleanest signal / square-wave region above 800 MHz"
    return "highest accuracy mode / reduced harmonics with possible strong spurs"


def validate_frequency(frequency_hz: int, high_mode: str, version: str | None = None) -> None:
    if frequency_hz <= SINE_REGION_MAX_HZ:
        return

    limit_hz = frequency_limit_for_mode(high_mode, version)
    if frequency_hz > limit_hz:
        raise GeneratorError(
            f"{frequency_hz / 1e9:.6f} GHz exceeds the {limit_hz / 1e9:g} GHz limit for {high_mode} mode"
        )


def frequency_commands(frequency_hz: int, command_style: str) -> list[str]:
    if command_style == "sweep":
        return [f"sweep cw {frequency_hz}\r"]
    if command_style == "freq":
        return [f"freq {frequency_hz}\r"]
    if command_style == "ui":
        return [
            "menu 11 2 2\r",
            f"text {frequency_hz}\r",
        ]
    raise ValueError(f"unknown frequency command style: {command_style}")


def ui_level_commands(level_dbm: float) -> list[str]:
    return [
        "menu 11 2 3\r",
        f"text {level_dbm:g}\r",
    ]


def start_commands(
    frequency_hz: int,
    level_dbm: float,
    high_mode: str,
    command_style: str = "sweep",
) -> list[str]:
    output_selector = []
    if frequency_hz > SINE_REGION_MAX_HZ and high_mode != "keep":
        output_selector.append(high_mode_command(high_mode))

    if command_style == "ui":
        return (
            frequency_commands(frequency_hz, command_style)
            + output_selector
            + ui_level_commands(level_dbm)
            + ["output on\r"]
        )

    return (
        [output_path_command(frequency_hz)]
        + output_selector
        + frequency_commands(frequency_hz, command_style)
        + [f"level {level_dbm:g}\r", "output on\r"]
    )


def stop_commands() -> list[str]:
    return [
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
    usb.reset_input_buffer()
    usb.reset_output_buffer()
    time.sleep(0.1)
    while usb.in_waiting:
        usb.read_all()
        time.sleep(0.01)


def serial_query(usb: serial.Serial, command: str) -> str:
    return serial_command(usb, command)


def serial_command(usb: serial.Serial, command: str) -> str:
    payload = command.encode()
    usb.write(payload)
    response = usb.read_until(PROMPT)
    if not response.endswith(PROMPT):
        partial = response.decode(errors="replace").strip()
        detail = f"; partial response: {partial!r}" if partial else ""
        raise GeneratorError(f"timed out waiting for prompt after {command.strip()!r}{detail}")

    text = response[: -len(PROMPT)].decode(errors="replace").strip()
    lines = text.splitlines()
    if lines and lines[0].strip() == command.strip():
        lines = lines[1:]
    return "\n".join(line for line in lines).strip()


def serial_capture(usb: serial.Serial) -> bytes:
    command = "capture\r"
    payload = command.encode()
    usb.write(payload)
    echo = usb.read_until(payload + b"\n")
    if not echo.endswith(payload + b"\n"):
        raise GeneratorError("timed out waiting for capture command echo")

    data = usb.read(CAPTURE_BYTES)
    if len(data) != CAPTURE_BYTES:
        raise GeneratorError(f"capture returned {len(data)} bytes, expected {CAPTURE_BYTES}")

    prompt = usb.read_until(PROMPT)
    if not prompt.endswith(PROMPT):
        raise GeneratorError("timed out waiting for prompt after capture")
    return data


def rgb565_to_png(raw: bytes, path: Path) -> None:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - project requirements include Pillow
        raise GeneratorError("Pillow is required to write PNG captures") from exc

    rgb = bytearray(LCD_WIDTH * LCD_HEIGHT * 3)
    for pixel_index in range(LCD_WIDTH * LCD_HEIGHT):
        value = raw[pixel_index * 2] | (raw[pixel_index * 2 + 1] << 8)
        red = (value >> 11) & 0x1F
        green = (value >> 5) & 0x3F
        blue = value & 0x1F
        out = pixel_index * 3
        rgb[out] = (red << 3) | (red >> 2)
        rgb[out + 1] = (green << 2) | (green >> 4)
        rgb[out + 2] = (blue << 3) | (blue >> 2)

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGB", (LCD_WIDTH, LCD_HEIGHT), bytes(rgb)).save(path)


def default_capture_path(output_dir: Path = DEFAULT_CAPTURE_DIR) -> Path:
    timestamp = time.strftime("%Y-%m-%d-%H%M%S")
    path = output_dir / f"{timestamp}_tinysa_capture.png"
    counter = 1
    while path.exists():
        path = output_dir / f"{timestamp}_tinysa_capture_{counter}.png"
        counter += 1
    return path


def query_version(usb: serial.Serial) -> str:
    response = serial_query(usb, "version\r")
    if "tinySA4" in response:
        return response

    # A rebooted tinySA can greet with shell banner/noise before the first
    # command response. Re-sync at a prompt and ask once more.
    usb.write(b"\r")
    usb.read_until(PROMPT)
    response = serial_query(usb, "version\r")
    return response


def serial_write(usb: serial.Serial, command: str) -> str:
    return serial_command(usb, command)


def send_commands(usb: serial.Serial, commands: list[str]) -> None:
    for command in commands:
        logging.info("send: %s", command.strip())
        response = serial_write(usb, command)
        if response:
            logging.info("recv: %s", response)


def send_best_effort(usb: serial.Serial, commands: list[str]) -> None:
    for command in commands:
        try:
            logging.info("cleanup: %s", command.strip())
            response = serial_write(usb, command)
            if response:
                logging.info("cleanup recv: %s", response)
        except (OSError, serial.SerialException, GeneratorError) as exc:
            logging.info("cleanup failed after %s: %s", command.strip(), exc)
            return


def send_commands_stepwise(usb: serial.Serial, commands: list[str]) -> None:
    for index, command in enumerate(commands, start=1):
        label = command.strip()
        input(f"[{index}/{len(commands)}] Press Enter to send: {label}")
        response = serial_write(usb, command)
        print(f"sent: {label}")
        if response:
            print(response)
        input("Inspect the tinySA screen, then press Enter to continue.")


def send_command_group(
    port: str,
    commands: list[str],
    step: bool = False,
    restore_interactive: bool = True,
    restore_commands: list[str] | None = None,
) -> None:
    with open_ultra(port) as usb:
        completed = False
        try:
            if step:
                send_commands_stepwise(usb, commands)
            else:
                send_commands(usb, commands)
            completed = True
        finally:
            if restore_interactive and completed:
                send_best_effort(usb, restore_commands or SESSION_RESTORE_COMMANDS)


def send_command_groups(
    port: str,
    groups: list[list[str]],
    step: bool = False,
    group_delay: float = DEFAULT_GROUP_DELAY_SECONDS,
) -> None:
    for index, group in enumerate(groups):
        send_command_group(port, group, step)
        if group_delay and index < len(groups) - 1:
            time.sleep(group_delay)


def print_commands(commands: list[str]) -> None:
    for command in commands:
        print(command.strip())


def open_serial(port: str) -> serial.Serial:
    logging.info("opening %s at %s baud", port, BAUDRATE)
    return serial.Serial(
        port,
        baudrate=BAUDRATE,
        timeout=COMMAND_TIMEOUT_SECONDS,
        write_timeout=WRITE_TIMEOUT_SECONDS,
    )


def open_ultra(port: str) -> serial.Serial:
    usb = open_serial(port)
    try:
        clear_buffer(usb)
        version = query_version(usb)
        require_ultra(version)
        serial_write(usb, "abort off\r")
        usb.tinysa_version = version
        logging.info("device: %s", version)
        return usb
    except Exception:
        usb.close()
        raise


def run_start(args: argparse.Namespace) -> int:
    validate_frequency(args.freq, args.high_mode)
    commands = start_commands(args.freq, args.level, args.high_mode, args.freq_command)
    region = output_region_description(args.freq, args.high_mode)

    if args.dry_run:
        print_commands(commands)
        if args.duration is not None:
            print(f"sleep {args.duration:g}")
            print("output off")
            if args.handoff_sweep:
                print("mode input")
                print_commands(HANDOFF_RESTORE_COMMANDS)
        return 0

    port = choose_port(args.port)
    if args.freq_command == "ui":
        groups = [
            frequency_commands(args.freq, "ui"),
        ]
        if args.freq > SINE_REGION_MAX_HZ and args.high_mode != "keep":
            groups.append([high_mode_command(args.high_mode)])
        groups.extend([ui_level_commands(args.level), ["output on\r"]])

        send_command_groups(port, groups, args.step, args.group_delay)

        print("Configured tinySA Ultra generator:")
        print(f"frequency: {args.freq / 1e6:.6f} MHz")
        print(f"level: {args.level:.1f} dBm")
        print(f"mode: {args.high_mode} ({region})")
        print("output: on")

        if args.duration is not None:
            print(f"duration: {args.duration:g} s")
            time.sleep(args.duration)
            send_command_group(port, ["output off\r"])
            print("output: off")
            if args.handoff_sweep:
                if args.group_delay:
                    time.sleep(args.group_delay)
                send_command_group(
                    port,
                    ["mode input\r"],
                    restore_commands=HANDOFF_RESTORE_COMMANDS,
                )
                print("mode: input/analyzer")
        else:
            print("duration: persistent; run the stop command to turn output off")
        return 0

    with open_ultra(port) as usb:
        if args.command_timeout is not None:
            usb.timeout = args.command_timeout
        version = getattr(usb, "tinysa_version", None)
        validate_frequency(args.freq, args.high_mode, version)
        if args.step:
            send_commands_stepwise(usb, commands)
        else:
            send_commands(usb, commands)
        print("Configured tinySA Ultra generator:")
        print(f"frequency: {args.freq / 1e6:.6f} MHz")
        print(f"level: {args.level:.1f} dBm")
        print(f"mode: {args.high_mode} ({region})")
        print("output: on")

        if args.duration is not None:
            print(f"duration: {args.duration:g} s")
            time.sleep(args.duration)
            send_commands(usb, ["output off\r"])
            print("output: off")
            if args.handoff_sweep:
                send_commands(usb, ["mode input\r"])
                send_best_effort(usb, HANDOFF_RESTORE_COMMANDS)
                print("mode: input/analyzer")
        else:
            print("duration: persistent; run the stop command to turn output off")
    return 0


def run_stop(args: argparse.Namespace) -> int:
    commands = stop_commands()
    if args.dry_run:
        print_commands(commands)
        return 0

    port = choose_port(args.port)
    send_command_group(port, commands)
    print("tinySA Ultra generator output: off")
    return 0


def run_on(args: argparse.Namespace) -> int:
    commands = ["output on\r"]
    if args.dry_run:
        print_commands(commands)
        return 0

    port = choose_port(args.port)
    send_command_group(port, commands)
    print("tinySA Ultra generator output: on")
    return 0


def run_setfreq(args: argparse.Namespace) -> int:
    commands = frequency_commands(args.freq, "ui")
    if args.dry_run:
        print_commands(commands)
        return 0

    port = choose_port(args.port)
    send_command_group(port, commands, args.step)
    print(f"tinySA generator frequency set to {args.freq / 1e6:.6f} MHz")
    return 0


def run_setlevel(args: argparse.Namespace) -> int:
    commands = ui_level_commands(args.level)
    if args.dry_run:
        print_commands(commands)
        return 0

    port = choose_port(args.port)
    send_command_group(port, commands, args.step)
    print(f"tinySA generator level set to {args.level:.1f} dBm")
    return 0


def run_handoff(args: argparse.Namespace) -> int:
    commands = ["output off\r", "mode input\r"]
    if args.dry_run:
        print_commands(commands + HANDOFF_RESTORE_COMMANDS)
        return 0

    port = choose_port(args.port)
    send_command_group(port, commands, restore_commands=HANDOFF_RESTORE_COMMANDS)
    print("tinySA Ultra generator output: off")
    print("mode: input/analyzer")
    return 0


def run_idle(args: argparse.Namespace) -> int:
    commands = ["output off\r", "mode input\r", "pause\r"]
    if args.dry_run:
        print_commands(commands + HANDOFF_RESTORE_COMMANDS)
        return 0

    port = choose_port(args.port)
    send_command_group(port, commands, restore_commands=HANDOFF_RESTORE_COMMANDS)
    print("tinySA Ultra generator output: off")
    print("mode: input/analyzer")
    print("sweep: paused")
    return 0


def run_apply(args: argparse.Namespace) -> int:
    groups = [
        frequency_commands(args.freq, "ui"),
        ui_level_commands(args.level),
    ]
    if args.dry_run:
        for group in groups:
            print_commands(group)
        return 0

    port = choose_port(args.port)
    send_command_groups(port, groups, args.step, args.group_delay)
    print(f"tinySA generator set to {args.freq / 1e6:.6f} MHz at {args.level:.1f} dBm")
    print("output state unchanged; enable output manually on the tinySA first")
    return 0


def run_probe(args: argparse.Namespace) -> int:
    port = choose_port(args.port)
    with open_ultra(port) as usb:
        version = getattr(usb, "tinysa_version", None)
        print(f"version: {version}")
        if args.info:
            print()
            print(serial_query(usb, "info\r"))
    return 0


def run_raw(args: argparse.Namespace) -> int:
    port = choose_port(args.port)
    opener = open_serial if args.no_probe else open_ultra
    with opener(port) as usb:
        if args.no_probe:
            clear_buffer(usb)
        for command in args.commands:
            command = command.rstrip("\r\n") + "\r"
            print(f"> {command.strip()}")
            response = serial_command(usb, command)
            if response:
                print(response)
    return 0


def run_recover(args: argparse.Namespace) -> int:
    port = choose_port(args.port)
    commands = ["\r", "abort\r", "output off\r", "mode input\r", "version\r"]

    with open_serial(port) as usb:
        clear_buffer(usb)
        for command in commands:
            print(f"> {command.strip() or '<enter>'}")
            try:
                usb.write(command.encode())
                time.sleep(0.5)
                response = usb.read_all().decode(errors="replace").strip()
                if response:
                    print(response)
            except serial.SerialTimeoutException as exc:
                raise GeneratorError(
                    "serial write timed out; unplug/replug the tinySA USB connection and try probe again"
                ) from exc
    return 0


def run_capture(args: argparse.Namespace) -> int:
    port = choose_port(args.port)
    output_path = args.out or default_capture_path()
    with open_ultra(port) as usb:
        raw = serial_capture(usb)
    rgb565_to_png(raw, output_path)
    print(f"wrote {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control tinySA Ultra CW generator output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start CW RF output")
    start.add_argument("--freq", required=True, type=parse_frequency, help="output frequency in Hz")
    start.add_argument("--level", required=True, type=parse_level, help="output level in dBm")
    start.add_argument(
        "--high-mode",
        choices=("keep", "clean", "accurate"),
        default="keep",
        help="output path above 800 MHz: keep=current tinySA setting, clean=cleanest signal, accurate=highest accuracy (default: keep)",
    )
    start.add_argument(
        "--freq-command",
        choices=("ui", "sweep", "freq"),
        default="ui",
        help="tinySA command used to set CW frequency (default: ui)",
    )
    start.add_argument("--duration", type=parse_duration, help="seconds to leave output on before stopping")
    start.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    start.add_argument("--command-timeout", type=parse_duration, help="serial read timeout per command in seconds")
    start.add_argument(
        "--group-delay",
        default=DEFAULT_GROUP_DELAY_SECONDS,
        type=parse_nonnegative_duration,
        help=f"seconds to wait between UI command groups (default: {DEFAULT_GROUP_DELAY_SECONDS:g})",
    )
    start.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    start.add_argument("--step", action="store_true", help="pause before and after each serial command")
    start.add_argument(
        "--handoff-sweep",
        action="store_true",
        help="after a timed run, switch output off and return the tinySA to analyzer input mode",
    )
    start.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    start.set_defaults(func=run_start)

    test880 = subparsers.add_parser("test880", help="start an 880 MHz CW output test")
    test880.add_argument(
        "--level",
        default=DEFAULT_TEST_LEVEL_DBM,
        type=parse_level,
        help=f"output level in dBm (default: {DEFAULT_TEST_LEVEL_DBM:g})",
    )
    test880.add_argument(
        "--high-mode",
        choices=("keep", "clean", "accurate"),
        default="keep",
        help="output path above 800 MHz: keep=current tinySA setting, clean=cleanest signal, accurate=highest accuracy (default: keep)",
    )
    test880.add_argument(
        "--freq-command",
        choices=("ui", "sweep", "freq"),
        default="ui",
        help="tinySA command used to set CW frequency (default: ui)",
    )
    test880.add_argument("--duration", type=parse_duration, help="seconds to leave output on before stopping")
    test880.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    test880.add_argument("--command-timeout", type=parse_duration, help="serial read timeout per command in seconds")
    test880.add_argument(
        "--group-delay",
        default=DEFAULT_GROUP_DELAY_SECONDS,
        type=parse_nonnegative_duration,
        help=f"seconds to wait between UI command groups (default: {DEFAULT_GROUP_DELAY_SECONDS:g})",
    )
    test880.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    test880.add_argument("--step", action="store_true", help="pause before and after each serial command")
    test880.add_argument(
        "--handoff-sweep",
        action="store_true",
        help="after a timed run, switch output off and return the tinySA to analyzer input mode",
    )
    test880.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    test880.set_defaults(func=run_start, freq=880_000_000)

    stop = subparsers.add_parser("stop", help="turn RF output off")
    stop.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    stop.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    stop.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    stop.set_defaults(func=run_stop)

    handoff = subparsers.add_parser(
        "handoff",
        help="turn generator output off and return the tinySA to analyzer input mode",
    )
    handoff.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    handoff.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    handoff.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    handoff.set_defaults(func=run_handoff)

    idle = subparsers.add_parser(
        "idle",
        help="park the tinySA with generator off, analyzer input mode, and sweep paused",
    )
    idle.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    idle.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    idle.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    idle.set_defaults(func=run_idle)

    on = subparsers.add_parser("on", help="turn RF output on")
    on.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    on.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    on.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    on.set_defaults(func=run_on)

    setfreq = subparsers.add_parser("setfreq", help="set generator frequency through the tinySA UI keypad")
    setfreq.add_argument("--freq", required=True, type=parse_frequency, help="output frequency in Hz")
    setfreq.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    setfreq.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    setfreq.add_argument("--step", action="store_true", help="pause before and after each serial command")
    setfreq.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    setfreq.set_defaults(func=run_setfreq)

    setlevel = subparsers.add_parser("setlevel", help="set generator level through the tinySA UI keypad")
    setlevel.add_argument("--level", required=True, type=parse_level, help="output level in dBm")
    setlevel.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    setlevel.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    setlevel.add_argument("--step", action="store_true", help="pause before and after each serial command")
    setlevel.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    setlevel.set_defaults(func=run_setlevel)

    apply = subparsers.add_parser(
        "apply",
        help="set generator frequency and level without changing output on/off state",
    )
    apply.add_argument("--freq", required=True, type=parse_frequency, help="output frequency in Hz")
    apply.add_argument("--level", required=True, type=parse_level, help="output level in dBm")
    apply.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    apply.add_argument(
        "--group-delay",
        default=DEFAULT_GROUP_DELAY_SECONDS,
        type=parse_nonnegative_duration,
        help=f"seconds to wait between UI command groups (default: {DEFAULT_GROUP_DELAY_SECONDS:g})",
    )
    apply.add_argument("--dry-run", action="store_true", help="print serial commands without opening the device")
    apply.add_argument("--step", action="store_true", help="pause before and after each serial command")
    apply.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    apply.set_defaults(func=run_apply)

    probe = subparsers.add_parser("probe", help="show connected tinySA Ultra identity")
    probe.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    probe.add_argument("--info", action="store_true", help="also print the device info command")
    probe.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    probe.set_defaults(func=run_probe)

    raw = subparsers.add_parser("raw", help="send raw tinySA commands and print responses")
    raw.add_argument("commands", nargs="+", help="command strings, e.g. 'sweep' 'level' 'mode high output'")
    raw.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    raw.add_argument("--no-probe", action="store_true", help="open serial port without first querying version")
    raw.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    raw.set_defaults(func=run_raw)

    recover = subparsers.add_parser("recover", help="try to return the tinySA serial console to prompt")
    recover.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    recover.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    recover.set_defaults(func=run_recover)

    capture = subparsers.add_parser("capture", help="save a tinySA screen capture PNG")
    capture.add_argument("--out", type=Path, help="PNG output path (default: timestamped PNG in captures)")
    capture.add_argument("--port", help="serial port override, e.g. /dev/ttyACM0 or COM3")
    capture.add_argument("--verbose", action="store_true", help="show serial setup and command progress")
    capture.set_defaults(func=run_capture)

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

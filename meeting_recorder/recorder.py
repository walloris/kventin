#!/usr/bin/env python3
"""Record microphone and computer audio into a mixed MP3 file."""

from __future__ import annotations

import argparse
import contextlib
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import lameenc
import numpy as np
import sounddevice as sd


DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_BITRATE_KBPS = 192
BLOCK_SECONDS = 0.1
NATIVE_SYSTEM_AUDIO_HELPER = (
    Path(__file__).resolve().parent / "native" / "system_audio_capture"
)


@dataclass(frozen=True)
class InputSource:
    name: str
    device: int
    channels: int


StatusCallback = Callable[[str], None]


def get_input_sources() -> list[InputSource]:
    sources: list[InputSource] = []
    for index, device in enumerate(sd.query_devices()):
        max_input_channels = int(device.get("max_input_channels", 0))
        if max_input_channels > 0:
            sources.append(
                InputSource(
                    name=str(device["name"]),
                    device=index,
                    channels=min(2, max_input_channels),
                )
            )
    return sources


def list_devices() -> None:
    devices = sd.query_devices()
    print("Input audio devices:")
    for index, device in enumerate(devices):
        max_input_channels = int(device.get("max_input_channels", 0))
        if max_input_channels <= 0:
            continue

        default_rate = int(float(device.get("default_samplerate", DEFAULT_SAMPLE_RATE)))
        print(
            f"{index:>3}  {device['name']}  "
            f"inputs={max_input_channels}  default_rate={default_rate}"
        )


def resolve_input_source(value: str, label: str) -> InputSource:
    devices = sd.query_devices()

    if value.isdigit():
        index = int(value)
        if index < 0 or index >= len(devices):
            raise ValueError(f"{label}: device id {index} not found")
        device = devices[index]
    else:
        matches = [
            (index, device)
            for index, device in enumerate(devices)
            if value.lower() in str(device["name"]).lower()
        ]
        if not matches:
            raise ValueError(f"{label}: input device matching {value!r} not found")
        if len(matches) > 1:
            names = ", ".join(f"{index}:{device['name']}" for index, device in matches)
            raise ValueError(f"{label}: ambiguous device {value!r}; matches: {names}")
        index, device = matches[0]

    channels = min(2, int(device.get("max_input_channels", 0)))
    if channels <= 0:
        raise ValueError(f"{label}: {device['name']} has no input channels")

    return InputSource(name=str(device["name"]), device=index, channels=channels)


def make_callback(name: str, sink: queue.Queue[np.ndarray], on_status: StatusCallback):
    def callback(indata, frames, timestamp, status):
        if status:
            on_status(f"{name}: {status}")
        try:
            sink.put_nowait(indata.copy())
        except queue.Full:
            on_status(f"{name}: input buffer is full; dropped audio block")

    return callback


def to_stereo(block: np.ndarray) -> np.ndarray:
    if block.ndim == 1:
        block = block[:, None]
    if block.shape[1] == 1:
        return np.repeat(block, 2, axis=1)
    return block[:, :2]


def pop_or_silence(source_queue: queue.Queue[np.ndarray], frames: int) -> np.ndarray:
    try:
        block = source_queue.get(timeout=1.0)
    except queue.Empty:
        return np.zeros((frames, 2), dtype=np.float32)

    block = to_stereo(block).astype(np.float32, copy=False)
    if len(block) == frames:
        return block
    if len(block) > frames:
        return block[:frames]

    padded = np.zeros((frames, 2), dtype=np.float32)
    padded[: len(block)] = block
    return padded


def read_native_system_audio(
    process: subprocess.Popen[bytes],
    source_queue: queue.Queue[np.ndarray],
    frames: int,
    stop_event: threading.Event,
    on_status: StatusCallback,
) -> None:
    bytes_per_frame = 2 * np.dtype(np.int16).itemsize
    block_bytes = frames * bytes_per_frame
    pending = b""

    while not stop_event.is_set():
        if process.stdout is None:
            return

        chunk = process.stdout.read(block_bytes - len(pending))
        if not chunk:
            if process.poll() is not None and not stop_event.is_set():
                on_status("native system audio stopped")
            return

        pending += chunk
        if len(pending) < block_bytes:
            continue

        raw_block = pending[:block_bytes]
        pending = pending[block_bytes:]
        block = np.frombuffer(raw_block, dtype=np.int16).reshape(-1, 2)
        block = block.astype(np.float32) / np.iinfo(np.int16).max
        try:
            source_queue.put_nowait(block)
        except queue.Full:
            on_status("native system audio: input buffer is full; dropped audio block")


def float_to_pcm16(block: np.ndarray) -> bytes:
    clipped = np.clip(block, -1.0, 1.0)
    pcm = (clipped * np.iinfo(np.int16).max).astype(np.int16)
    return pcm.tobytes()


class MeetingRecorder:
    def __init__(
        self,
        mic: InputSource,
        system: Optional[InputSource],
        output: Path,
        duration: Optional[float] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        bitrate_kbps: int = DEFAULT_BITRATE_KBPS,
        native_system_audio: bool = False,
        on_status: Optional[StatusCallback] = None,
    ) -> None:
        self.mic = mic
        self.system = system
        self.output = output
        self.duration = duration
        self.sample_rate = sample_rate
        self.bitrate_kbps = bitrate_kbps
        self.native_system_audio = native_system_audio
        self.on_status = on_status or (lambda message: None)

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._done_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._started_at: Optional[float] = None
        self._paused_at: Optional[float] = None
        self._paused_total = 0.0
        self._native_process: Optional[subprocess.Popen[bytes]] = None

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        pause_extra = 0.0
        if self._paused_at is not None:
            pause_extra = time.monotonic() - self._paused_at
        return max(0.0, time.monotonic() - self._started_at - self._paused_total - pause_extra)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("recording is already running")
        self._thread = threading.Thread(target=self._run_guarded, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if not self.is_running or self.is_paused:
            return
        self._paused_at = time.monotonic()
        self._pause_event.set()
        self.on_status("Paused")

    def resume(self) -> None:
        if not self.is_running or not self.is_paused:
            return
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
        self._pause_event.clear()
        self.on_status("Recording")

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()
        self._stop_native_process()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done_event.wait(timeout)

    def _run_guarded(self) -> None:
        try:
            self._run()
        except BaseException as exc:
            self._error = exc
            self.on_status(f"Error: {exc}")
        finally:
            self._done_event.set()

    def _run(self) -> None:
        blocksize = int(self.sample_rate * BLOCK_SECONDS)
        mic_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)
        system_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)

        encoder = lameenc.Encoder()
        encoder.set_bit_rate(self.bitrate_kbps)
        encoder.set_in_sample_rate(self.sample_rate)
        encoder.set_channels(2)
        encoder.set_quality(2)

        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._started_at = time.monotonic()

        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    sd.InputStream(
                        device=self.mic.device,
                        channels=self.mic.channels,
                        samplerate=self.sample_rate,
                        blocksize=blocksize,
                        dtype="float32",
                        callback=make_callback("microphone", mic_queue, self.on_status),
                    )
                )
                if self.native_system_audio:
                    self._start_native_system_audio(system_queue, blocksize)
                else:
                    if self.system is None:
                        raise RuntimeError("system audio device is not selected")
                    stack.enter_context(
                        sd.InputStream(
                            device=self.system.device,
                            channels=self.system.channels,
                            samplerate=self.sample_rate,
                            blocksize=blocksize,
                            dtype="float32",
                            callback=make_callback("system", system_queue, self.on_status),
                        )
                    )

                mp3_file = stack.enter_context(self.output.open("wb"))
                self.on_status("Recording")

                while not self._stop_event.is_set():
                    if self.duration is not None and self.elapsed_seconds >= self.duration:
                        break

                    mic_block = pop_or_silence(mic_queue, blocksize)
                    system_block = pop_or_silence(system_queue, blocksize)

                    if self.is_paused:
                        continue

                    mixed = (mic_block + system_block) * 0.5
                    encoded = encoder.encode(float_to_pcm16(mixed))
                    if encoded:
                        mp3_file.write(encoded)

                tail = encoder.flush()
                if tail:
                    mp3_file.write(tail)
        finally:
            self._stop_native_process()

        self.on_status(f"Saved {self.output} ({self.elapsed_seconds:.1f} sec)")

    def _stop_native_process(self) -> None:
        process = self._native_process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def _start_native_system_audio(
        self, system_queue: queue.Queue[np.ndarray], blocksize: int
    ) -> None:
        if not NATIVE_SYSTEM_AUDIO_HELPER.exists():
            raise RuntimeError(
                "native system audio helper is not built. Run build_native_audio.sh"
            )

        process = subprocess.Popen(
            [str(NATIVE_SYSTEM_AUDIO_HELPER)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._native_process = process
        time.sleep(0.2)

        if process.poll() is not None:
            error = ""
            if process.stderr is not None:
                error = process.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(error or "native system audio helper failed to start")

        reader = threading.Thread(
            target=read_native_system_audio,
            args=(process, system_queue, blocksize, self._stop_event, self.on_status),
            daemon=True,
        )
        reader.start()
        self.on_status("Native macOS system audio enabled")


def record(
    mic: InputSource,
    system: Optional[InputSource],
    output: Path,
    duration: Optional[float],
    sample_rate: int,
    bitrate_kbps: int,
) -> None:
    stop_requested = False

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    recorder = MeetingRecorder(
        mic=mic,
        system=system,
        output=output,
        duration=duration,
        sample_rate=sample_rate,
        bitrate_kbps=bitrate_kbps,
        on_status=lambda message: print(message, file=sys.stderr)
        if message.startswith(("Error:", "microphone:", "system:"))
        else print(message),
    )

    try:
        print(f"Recording microphone: {mic.name}")
        if system is not None:
            print(f"Recording system audio: {system.name}")
        print(f"Writing MP3: {output}")
        print("Press Ctrl+C to stop.")
        recorder.start()
        while not recorder.wait(0.2):
            if stop_requested:
                recorder.stop()
        if recorder.error is not None:
            raise recorder.error
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record microphone and computer audio into an MP3 file."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-devices", help="show available input audio devices")

    record_parser = subparsers.add_parser("record", help="start recording")
    record_parser.add_argument("--mic-device", required=True, help="microphone name or id")
    record_parser.add_argument(
        "--system-device",
        required=True,
        help="virtual input device for computer audio, for example BlackHole",
    )
    record_parser.add_argument(
        "--output",
        default="meeting.mp3",
        type=Path,
        help="path to resulting MP3 file",
    )
    record_parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="recording duration in seconds; omit to record until Ctrl+C",
    )
    record_parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help=f"sample rate in Hz, default {DEFAULT_SAMPLE_RATE}",
    )
    record_parser.add_argument(
        "--bitrate",
        type=int,
        default=DEFAULT_BITRATE_KBPS,
        help=f"MP3 bitrate in kbps, default {DEFAULT_BITRATE_KBPS}",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list-devices":
            list_devices()
            return 0

        mic = resolve_input_source(args.mic_device, "microphone")
        system = resolve_input_source(args.system_device, "system audio")
        record(
            mic=mic,
            system=system,
            output=args.output,
            duration=args.duration,
            sample_rate=args.sample_rate,
            bitrate_kbps=args.bitrate,
        )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

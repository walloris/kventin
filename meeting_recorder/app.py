#!/usr/bin/env python3
"""Tkinter UI for recording meetings into MP3."""

from __future__ import annotations

import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from recorder import (
    DEFAULT_BITRATE_KBPS,
    DEFAULT_SAMPLE_RATE,
    InputSource,
    MeetingRecorder,
    get_input_sources,
)


class RecorderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Meeting Recorder")
        self.geometry("720x420")
        self.minsize(640, 380)

        self.sources: list[InputSource] = []
        self.recorder: Optional[MeetingRecorder] = None
        self.messages: queue.Queue[str] = queue.Queue()

        self.mic_var = tk.StringVar()
        self.system_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.cwd() / "meeting.mp3"))
        self.status_var = tk.StringVar(value="Ready")
        self.timer_var = tk.StringVar(value="00:00:00")

        self._build_ui()
        self.refresh_devices()
        self.after(200, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.configure(bg="#f5f7fb")

        container = ttk.Frame(self, padding=20)
        container.pack(fill="both", expand=True)

        title = ttk.Label(container, text="Meeting Recorder", font=("Arial", 22, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w")

        subtitle = ttk.Label(
            container,
            text="Запись микрофона и системного аудио в MP3",
            font=("Arial", 12),
        )
        subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 18))

        ttk.Label(container, text="Микрофон").grid(row=2, column=0, sticky="w")
        self.mic_combo = ttk.Combobox(
            container, textvariable=self.mic_var, state="readonly"
        )
        self.mic_combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 12))

        ttk.Label(container, text="Звук компьютера").grid(row=4, column=0, sticky="w")
        self.system_combo = ttk.Combobox(
            container, textvariable=self.system_var, state="readonly"
        )
        self.system_combo.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 12))

        refresh_button = ttk.Button(
            container, text="Обновить устройства", command=self.refresh_devices
        )
        refresh_button.grid(row=3, column=2, sticky="ew", padx=(12, 0), pady=(4, 12))

        ttk.Label(container, text="Файл MP3").grid(row=6, column=0, sticky="w")
        output_entry = ttk.Entry(container, textvariable=self.output_var)
        output_entry.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 18))

        browse_button = ttk.Button(container, text="Выбрать файл", command=self.choose_output)
        browse_button.grid(row=7, column=2, sticky="ew", padx=(12, 0), pady=(4, 18))

        controls = ttk.Frame(container)
        controls.grid(row=8, column=0, columnspan=3, sticky="ew")

        self.start_button = ttk.Button(controls, text="Старт", command=self.start_recording)
        self.start_button.pack(side="left", padx=(0, 8))

        self.pause_button = ttk.Button(
            controls, text="Пауза", command=self.toggle_pause, state="disabled"
        )
        self.pause_button.pack(side="left", padx=8)

        self.stop_button = ttk.Button(
            controls, text="Стоп", command=self.stop_recording, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)

        status_panel = ttk.Frame(container, padding=(0, 22, 0, 0))
        status_panel.grid(row=9, column=0, columnspan=3, sticky="ew")

        ttk.Label(status_panel, textvariable=self.timer_var, font=("Arial", 28, "bold")).pack(
            side="left"
        )
        ttk.Label(status_panel, textvariable=self.status_var, font=("Arial", 12)).pack(
            side="left", padx=(18, 0)
        )

        hint = ttk.Label(
            container,
            text="Для системного звука на macOS выберите BlackHole/Loopback как источник звука компьютера.",
            foreground="#5b6472",
        )
        hint.grid(row=10, column=0, columnspan=3, sticky="w", pady=(20, 0))

        container.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.columnconfigure(2, weight=0)

    def refresh_devices(self) -> None:
        try:
            self.sources = get_input_sources()
        except Exception as exc:
            messagebox.showerror("Ошибка аудиоустройств", str(exc))
            return

        labels = [self._source_label(source) for source in self.sources]
        self.mic_combo["values"] = labels
        self.system_combo["values"] = labels

        if labels and not self.mic_var.get():
            self.mic_var.set(labels[0])
        if labels and not self.system_var.get():
            blackhole = next(
                (label for label in labels if "blackhole" in label.lower()), labels[0]
            )
            self.system_var.set(blackhole)

        self.status_var.set(f"Ready. Devices: {len(labels)}")

    def choose_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Сохранить запись",
            defaultextension=".mp3",
            filetypes=[("MP3 audio", "*.mp3")],
            initialfile="meeting.mp3",
        )
        if filename:
            self.output_var.set(filename)

    def start_recording(self) -> None:
        try:
            mic = self._selected_source(self.mic_var.get())
            system = self._selected_source(self.system_var.get())
            output = Path(self.output_var.get()).expanduser()
            if output.suffix.lower() != ".mp3":
                output = output.with_suffix(".mp3")
                self.output_var.set(str(output))

            self.recorder = MeetingRecorder(
                mic=mic,
                system=system,
                output=output,
                sample_rate=DEFAULT_SAMPLE_RATE,
                bitrate_kbps=DEFAULT_BITRATE_KBPS,
                on_status=self.messages.put,
            )
            self.recorder.start()
        except Exception as exc:
            messagebox.showerror("Не удалось начать запись", str(exc))
            self.recorder = None
            return

        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="Пауза")
        self.stop_button.configure(state="normal")
        self.mic_combo.configure(state="disabled")
        self.system_combo.configure(state="disabled")
        self.status_var.set("Recording")

    def toggle_pause(self) -> None:
        if self.recorder is None:
            return
        if self.recorder.is_paused:
            self.recorder.resume()
            self.pause_button.configure(text="Пауза")
        else:
            self.recorder.pause()
            self.pause_button.configure(text="Продолжить")

    def stop_recording(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.status_var.set("Stopping...")
            self.stop_button.configure(state="disabled")
            self.pause_button.configure(state="disabled")

    def _tick(self) -> None:
        self._drain_messages()

        if self.recorder is not None:
            self.timer_var.set(self._format_seconds(self.recorder.elapsed_seconds))
            if not self.recorder.is_running and self.recorder.wait(0):
                error = self.recorder.error
                self.recorder = None
                self._set_idle_controls()
                if error is not None:
                    messagebox.showerror("Ошибка записи", str(error))

        self.after(200, self._tick)

    def _drain_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                return
            self.status_var.set(message)

    def _set_idle_controls(self) -> None:
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled", text="Пауза")
        self.stop_button.configure(state="disabled")
        self.mic_combo.configure(state="readonly")
        self.system_combo.configure(state="readonly")

    def _selected_source(self, label: str) -> InputSource:
        for source in self.sources:
            if self._source_label(source) == label:
                return source
        raise ValueError("Выберите аудиоустройство")

    @staticmethod
    def _source_label(source: InputSource) -> str:
        return f"{source.device}: {source.name}"

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _on_close(self) -> None:
        if self.recorder is not None and self.recorder.is_running:
            if not messagebox.askyesno("Закрыть", "Остановить запись и закрыть приложение?"):
                return
            self.recorder.stop()
            self.recorder.wait(3)
        self.destroy()


def main() -> None:
    app = RecorderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

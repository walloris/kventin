#!/usr/bin/env python3
"""Tkinter UI for recording meetings into MP3."""

from __future__ import annotations

from datetime import datetime
import queue
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
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
        self.geometry("720x430")
        self.minsize(640, 380)

        self.sources: list[InputSource] = []
        self.recorder: Optional[MeetingRecorder] = None
        self.messages: queue.Queue[str] = queue.Queue()

        self.mic_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(self._next_output_path()))
        self.status_var = tk.StringVar(value="Ready")
        self.timer_var = tk.StringVar(value="00:00:00")
        self.hint_var = tk.StringVar(
            value="Системный звук пишется встроенным macOS-захватом. При первом запуске разрешите Screen Recording."
        )

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
        system_label = ttk.Label(
            container,
            text="Встроенный macOS system audio",
            foreground="#334155",
        )
        system_label.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 12))

        refresh_button = ttk.Button(
            container, text="Обновить устройства", command=self.refresh_devices
        )
        refresh_button.grid(row=3, column=2, sticky="ew", padx=(12, 0), pady=(4, 12))

        ttk.Label(container, text="Сохранение").grid(row=6, column=0, sticky="w")
        output_label = ttk.Label(
            container,
            textvariable=self.output_var,
            foreground="#334155",
            wraplength=660,
        )
        output_label.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 18))

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
            textvariable=self.hint_var,
            foreground="#5b6472",
            wraplength=660,
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

        if labels and not self.mic_var.get():
            self.mic_var.set(labels[0])

        self.status_var.set(f"Ready. Devices: {len(labels)}")

    def start_recording(self) -> None:
        try:
            mic = self._selected_source(self.mic_var.get())
            output = self._next_output_path()
            self.output_var.set(str(output))

            self.recorder = MeetingRecorder(
                mic=mic,
                system=None,
                output=output,
                sample_rate=DEFAULT_SAMPLE_RATE,
                bitrate_kbps=DEFAULT_BITRATE_KBPS,
                native_system_audio=True,
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
        self.status_var.set(f"Recording: {Path(self.output_var.get()).name}")

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
                else:
                    self.output_var.set(str(self._next_output_path()))

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

    def _selected_source(self, label: str) -> InputSource:
        for source in self.sources:
            if self._source_label(source) == label:
                return source
        raise ValueError("Выберите аудиоустройство")

    @staticmethod
    def _source_label(source: InputSource) -> str:
        return f"{source.device}: {source.name}"

    @staticmethod
    def _recordings_dir() -> Path:
        return Path.home() / "Downloads" / "Записи встреч"

    @classmethod
    def _next_output_path(cls) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return cls._recordings_dir() / f"meeting_{timestamp}.mp3"

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

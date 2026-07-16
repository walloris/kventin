# Meeting Recorder

Локальное Python-приложение для записи онлайн-встреч в `.mp3`.

Оно пишет два источника одновременно:

- звук с микрофона;
- звук компьютера через нативный macOS ScreenCaptureKit helper.

## Важно про системный звук на macOS

Python не может напрямую "подслушать" звук приложений macOS через обычные
аудиоустройства. Поэтому приложение использует нативный Swift helper на
ScreenCaptureKit.

При первом запуске macOS может запросить разрешение **Screen Recording**.
Это нормально: Apple отдаёт системный звук через тот же privacy-механизм,
что и захват экрана. Если разрешение не дали, включите его в:

```text
System Settings -> Privacy & Security -> Screen & System Audio Recording
```

## Установка

```bash
cd /Users/walloris/Documents/kventin/meeting_recorder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./build_native_audio.sh
```

## Запустить приложение с UI

```bash
python app.py
```

В окне выберите:

- микрофон;

Звук компьютера пишется автоматически через `Встроенный macOS system audio`.

Файл сохраняется автоматически в:

```text
~/Downloads/Записи встреч/meeting_YYYYMMDD_HHMMSS.mp3
```

Например:

```text
~/Downloads/Записи встреч/meeting_20260716_111213.mp3
```

Кнопки:

- `Старт` начинает запись;
- `Пауза` временно не пишет звук в файл;
- `Продолжить` возвращает запись;
- `Стоп` завершает запись и сохраняет MP3.

## Посмотреть аудиоустройства

```bash
python recorder.py list-devices
```

## Записать встречу

CLI-режим пока оставлен для старого варианта с виртуальным аудиоустройством.
Для записи системного звука без BlackHole/Loopback используйте UI:

```bash
python app.py
```

Старый CLI-вариант:

```bash
python recorder.py record \
  --mic-device "MacBook Pro Microphone" \
  --system-device "BlackHole 2ch" \
  --output meeting.mp3
```

Остановить запись: `Ctrl+C`.

Записать фиксированное время, например 60 минут:

```bash
python recorder.py record \
  --mic-device "MacBook Pro Microphone" \
  --system-device "BlackHole 2ch" \
  --duration 3600 \
  --output meeting.mp3
```

Если имя устройства длинное или меняется, можно указать его ID из `list-devices`:

```bash
python recorder.py record --mic-device 1 --system-device 4 --output meeting.mp3
```

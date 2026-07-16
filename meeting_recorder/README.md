# Meeting Recorder

Локальное Python-приложение для записи онлайн-встреч в `.mp3`.

Оно пишет два источника одновременно:

- звук с микрофона;
- звук компьютера через виртуальное аудиоустройство.

## Важно про системный звук на macOS

Python не может напрямую "подслушать" звук приложений macOS. Нужен виртуальный аудиодрайвер:

- BlackHole 2ch: https://existential.audio/blackhole/
- Loopback: https://rogueamoeba.com/loopback/

После установки BlackHole обычно нужно создать Multi-Output Device в `Audio MIDI Setup`, чтобы звук шёл одновременно в наушники/колонки и в BlackHole. В приложении выбирайте BlackHole как `--system-device`.

## Установка

```bash
cd /Users/walloris/Documents/kventin/meeting_recorder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запустить приложение с UI

```bash
python app.py
```

В окне выберите:

- микрофон;
- источник звука компьютера, например `BlackHole 2ch`;
- путь для итогового `.mp3`.

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

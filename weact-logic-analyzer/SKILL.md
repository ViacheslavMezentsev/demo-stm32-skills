---
name: weact-logic-analyzer
description: Логический анализатор WeAct LogicAnalyzerV1 (FX2, 8 каналов, 24 МГц) на Linux через sigrok-cli — проверка наличия, права, запись, обёртка weact_la.py и типовые измерения для STM32 (ISR, PWM, UART, I2C, SPI, старт, джиттер). Плюс работа на удалённом стенде по SSH и поиск начала обмена, когда момент события зависит от сценария под отладчиком.
license: MIT
compatibility: логический анализатор Saleae-совместимый (в том числе WeAct) и sigrok/PulseView на стенде
metadata:
  author: Viacheslav Mezentsev
  version: "1.1.0"
  workflow: исследование → навык → проект
allowed-tools: Bash Read Write Edit Glob Grep
---

# WeAct LogicAnalyzerV1 + sigrok: автоматические записи для разработки на STM32

Применяй, когда нужно: проверить, что анализатор подключён и доступен; снять короткую запись 8 каналов
из скрипта или теста; измерить тайминги или разобрать протокол в прошивке STM32; встроить анализатор
в стенд автотестов (в том числе рядом с gdb-обвязкой). Отвечай пользователю на его языке.

Порядок работы: **(1) проверка → (2) запись в CLI → (3) обёртка `weact_la.py` → (4) типовое измерение.**
Не перескакивай к коду, пока `python3 weact_la.py doctor` не зелёный.

---

## 1. Железо

| Параметр | Значение |
| --- | --- |
| Контроллер | Cypress CY7C68013A или Corebai CBM9002A (FX2), EEPROM 2 Кбит |
| Каналы / частота | 8 каналов, до **24 МГц** на всех (драйвер предлагает и 48 МГц — по USB 2.0 не держит) |
| Входы | 0–5,5 В, VIH > 2 В, VIL < 0,8 В → STM32 на 3,3 В подключается напрямую; на каждом входе 100 Ом и буфер |
| USB ID | `0925:3881` («Lakeview Research Saleae Logic») — ID не меняется и после загрузки прошивки |
| Светодиоды | жёлтый — прошивка fx2lafw загружена; зелёный — CH0, синий — CH1 |
| ПО | sigrok (fx2lafw, PulseView, sigrok-cli); прошивка грузится в ОЗУ при каждом открытии, своей не нужно |

Правила подключения:
- **Общая земля обязательна** (GND анализатора ↔ GND платы). Без неё — случайные уровни; неподключённые
  входы читаются как `1`.
- Не выше 5,5 В; RS-232/RS-485 — только после преобразователя уровня.
- Подключать в отдельный порт USB 2.0/3.0 без хаба, иначе на 12–24 МГц будут обрывы.
- Каналы стенда фиксируй в паспорте проекта, например: D0 UART_TX, D1 UART_RX, D2 SCK, D3 MOSI, D4 MISO,
  D5 NSS/SCL, D6 SDA, **D7 — маркер-GPIO для триггера**.

---

## 2. Проверка наличия и подготовка Linux (Ubuntu/Debian, x86-64 и ARM64)

Шаг за шагом; при ошибке — таблица в разделе 9.

```bash
lsusb | grep -iE '0925:3881|1d50:608c|04b4:8613'      # 1. устройство на шине
sudo apt install sigrok-cli sigrok-firmware-fx2lafw    # 2. ПО и прошивка
ls /usr/share/sigrok-firmware/fx2lafw-saleae-logic.fw
```

3. Права без root (правило пакета может давать доступ только локальной графической сессии через
   `uaccess`, а SSH и systemd-сервисам — нет):

```bash
sudo tee /etc/udev/rules.d/99-weact-la.rules >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0925", ATTRS{idProduct}=="3881", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="608c", MODE="0660", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev "$USER"     # затем перелогиниться (или newgrp plugdev в текущем shell)
# переподключить анализатор
```

4. Проверка:

```bash
id | grep -o plugdev                         # в выводе может быть дважды — это норма после newgrp
ls -l /dev/bus/usb/<bus>/<dev>               # crw-rw----+ root plugdev
sigrok-cli -d fx2lafw --scan                 # fx2lafw - Saleae Logic with 8 channels: D0 … D7
python3 weact_la.py doctor                   # всё [OK] → код 0
```

`sigrok-cli -d fx2lafw --show` — список частот (20 кГц … 48 МГц) и триггеров (`0 1 r f e`).
Для systemd-сервиса стенда: `SupplementaryGroups=plugdev` в unit-файле.

---

## 3. sigrok-cli: шпаргалка и особенности версии 0.7.x (Ubuntu 20.04/22.04)

```bash
# короткая запись: 0,5 с при 8 МГц, все 8 каналов (по умолчанию включены все)
sigrok-cli -d fx2lafw --config samplerate=8m --samples 4m -o cap.sr

# с триггером по фронту маркера D7, 10 % до триггера, не дольше 10 с
timeout -s INT 10 sigrok-cli -d fx2lafw --config samplerate=8m:captureratio=10 \
  --channels D0,D1,D2,D3,D4,D5,D6,D7 --samples 4m \
  --triggers D7=r --wait-trigger -o cap.sr          # код 124 — триггер не сработал

unzip -p cap.sr metadata                            # частота, каналы
sigrok-cli -i cap.sr -O csv -o cap.csv              # также vcd (GTKWave), bits, ascii, binary
sigrok-cli -i cap.sr -P uart:rx=D0:baudrate=115200 -A uart=rx-data
sigrok-cli -i cap.sr -P i2c:scl=D5:sda=D6 -A i2c=address-write:data-write:nack
sigrok-cli -i cap.sr -P spi:clk=D2:mosi=D3:miso=D4:cs=D5 -A spi=mosi-data
sigrok-cli -L                                       # список декодеров (uart, i2c, spi, can, pwm, timing…)
```

Подводные камни (проверено):
- **Диапазон `--channels D0-D7` не работает** → `Invalid channel 'D0'`. Перечисляй через запятую или не
  указывай вовсе.
- **`--triggers` + `--wait-trigger` ждёт вечно**, если события нет: всегда `timeout -s INT N` снаружи
  (или `collect(timeout=…)` обёртки).
- Предел записи задавай `--samples` (= частота × время); `--time` в старых версиях пересчитывается ненадёжно.
- `sigrok-cli -i file.sr --show` в 0.7.x пишет «2 devices found» — метаданные читай `unzip -p … metadata`.
- Сообщение `Device only sent N samples` безвредно (последний неполный блок).
- Каналы в файлах из `-I binary` называются `0..7`, а не `D0..D7`.
- Положение триггера в `.sr` не сохраняется: меряй относительно фронтов маркера, а не «нуля записи».

---

## 4. Частота дискретизации и длина записи (короткие записи)

Разрешение по времени = 1/частота; ошибка измерения интервала — ±1 отсчёт на каждый фронт.
Объём: 1 байт на отсчёт (8 каналов) → 8 МГц × 0,5 с = 4 МБ. Держи записи **0,01–2 с** и ≤ 32 М отсчётов:
так они надёжно проходят через USB, быстро декодируются и не забивают диск стенда.

| Сигнал STM32 | Частота | Почему |
| --- | --- | --- |
| UART 115200 | 1–2 МГц | ≥ 8 отсчётов на бит |
| UART 921600 | 8–12 МГц | |
| I²C 100 кГц / 400 кГц / 1 МГц | 2 / 4–8 / 12–24 МГц | ≥ 10× SCL |
| SPI | ≥ 4× SCK, т. е. SCK ≤ 4–6 МГц при 24 МГц | на время отладки поднять делитель SPI |
| PWM, тайминги таймеров | ≥ 10 / самый короткий импульс | |
| Длительность ISR, отладочные GPIO | 12–24 МГц | разрешение 83–42 нс |
| Старт, сон/пробуждение, медленные события | 20 кГц – 1 МГц | длинное окно при малом объёме |

Допустимые частоты fx2lafw: 20k 25k 50k 100k 200k 250k 500k 1m 2m 3m 4m 6m 8m 12m 16m 24m (48m — нестабильно).

---

## 5. Обёртка `weact_la.py`

Положи `scripts/weact_la.py` из комплекта навыка в проект (например, `tools/weact_la.py`). Зависимости — только стандартная
библиотека Python ≥ 3.8 и `sigrok-cli`. Отсчёты читаются прямо из `.sr` (это zip), поэтому измерения не
вызывают sigrok-cli; декодеры протоколов — вызывают.

CLI:

```bash
python3 weact_la.py doctor                                     # проверка цепочки ПО→USB→права→sigrok
python3 weact_la.py capture run.sr --rate 8m --time 0.2 --trigger D7=r --timeout 5
python3 weact_la.py info run.sr                                # фронты, частота, скважность по каналам
```

API:

| Вызов | Что делает |
| --- | --- |
| `doctor()` | список `(проверка, ok, подробности)` |
| `LogicAnalyzer(samplerate="8m", channels=range(8), driver="fx2lafw", conn=None)` | `driver="demo"` — без железа; `conn="7.3"` — если анализаторов несколько |
| `la.capture(path, duration=…/samples=…, trigger={7:"r"}, pretrigger=10, timeout=10)` | синхронная запись → `Capture` |
| `la.arm(...)` / `la.collect(timeout)` / `la.abort()` | взвести в фоне → сделать действие → забрать; `TriggerTimeout`, если триггера не было |
| `Capture(path)` | открыть готовый `.sr` |
| `.n`, `.samplerate`, `.duration`, `.t(sample)` | основное |
| `.edges(ch, "r"/"f"/"e")`, `.edge_times(...)`, `.first_edge(ch, kind, after)`, `.count(ch)` | фронты |
| `.pulses(ch, level=1)`, `.pulse_stats(ch)`, `.periods(ch)`, `.frequency(ch)`, `.duty(ch)` | тайминги (полные импульсы) |
| `.delay(ch_from, ch_to, edge_from, edge_to, after)` | задержка между событиями |
| `Capture.stats(values)` | `n, min, max, mean, jitter` |
| `.uart(rx, baudrate)` → `bytes` | декодер UART |
| `.spi(clk, mosi, miso, cs)` → `[(mosi, miso)]` | декодер SPI |
| `.i2c(scl, sda)` / `.i2c_transactions(scl, sda)` | события / `[{'addr','read','data','nack'}]` |
| `.decode(decoder, annotation, **opts)` | любой декодер sigrok → `[(t0, t1, текст)]` |
| `.export("csv"/"vcd")`, `.summary()` | экспорт, сводка |

Каналы — номера `0..7` или имена `"D0"`. Триггер: `{7: "r"}`, `{0: "f", 1: "1"}` (И), `"D7=r"`.

Самопроверка без железа (драйвер `demo` + синтетические сигналы): сохрани тест из раздела 10 рядом и
запусти `python3 test_weact_la.py` — должно закончиться `ВСЁ OK`.

---

## 6. Подготовка прошивки STM32: отладочные выводы

Отладочный GPIO стоит 1–2 такта, не влияет на тайминги и даёт точные метки для анализатора.

```c
/* debug_pins.h — порт и выводы под свою плату; режим Output PP, скорость Very High */
#define DBG_PORT        GPIOB
#define DBG_HI(n)       (DBG_PORT->BSRR = (1u << (n)))
#define DBG_LO(n)       (DBG_PORT->BSRR = (1u << ((n) + 16u)))
#define DBG_PULSE(n)    do { DBG_HI(n); __NOP(); DBG_LO(n); } while (0)
#define DBG_MARK_PIN    7u   /* → D7 анализатора: маркер начала тестового участка */
```

- Маркер (`DBG_HI(DBG_MARK_PIN)`) ставь в начале проверяемого сценария — он служит триггером и нулём
  времени.
- Для разных ISR — разные выводы или кодирование числом импульсов.
- Выводы отладки держи вне сборки релиза (`#ifdef DEBUG_PINS`).

---

## 7. Типовые применения

Во всех примерах: `from weact_la import LogicAnalyzer, Capture`; `la = LogicAnalyzer("<частота>")`.

### 7.1 Длительность ISR или функции (D0 = DBG-вывод HI на входе, LO на выходе)

```python
cap = la.capture("isr.sr", duration=0.2, trigger={7: "r"})
st = cap.pulse_stats(0)                     # секунды: n, min, max, mean, jitter
assert st["max"] < 5e-6, f"ISR дольше 5 мкс: {st}"
```

Частота 24m. Загрузку CPU оценивай как `sum(cap.pulses(0)) / cap.duration`.

### 7.2 Частота и скважность PWM (D1 = выход TIMx_CHy)

```python
cap = LogicAnalyzer("24m").capture("pwm.sr", duration=0.05)
assert abs(cap.frequency(1) - 20_000) < 20
assert abs(cap.duty(1) - 0.25) < 0.005
```

### 7.3 Время старта (D0 = NRST, D7 = маркер в начале `main()`)

```python
la = LogicAnalyzer("1m")
la.arm("boot.sr", duration=1.0, trigger={0: "r"}, pretrigger=5, settle=1.5)
reset_target()                              # стенд дёргает NRST (gdb `monitor reset`, GPIO, питание)
cap = la.collect(timeout=5)
t_boot = cap.delay(0, 7, "r", "r")          # от отпускания сброса до маркера
assert t_boot < 0.050
```

### 7.4 Джиттер периодической задачи (D2 = импульс в начале задачи 1 мс)

```python
cap = LogicAnalyzer("8m").capture("tick.sr", duration=0.5)
st = Capture.stats(cap.periods(2))
assert abs(st["mean"] - 1e-3) < 1e-6 and st["jitter"] < 20e-6, st
```

### 7.5 Вывод UART (D0 = USARTx_TX)

```python
cap = LogicAnalyzer("2m").capture("uart.sr", duration=0.3, trigger={7: "r"})
out = cap.uart(0, 115200)
assert b"READY\r\n" in out, out
```

### 7.6 Транзакции I²C (D5 = SCL, D6 = SDA)

```python
cap = LogicAnalyzer("8m").capture("i2c.sr", duration=0.1, trigger={6: "f", 5: "1"})   # START: SDA↓ при SCL=1
tr = cap.i2c_transactions(5, 6)
assert tr[0]["addr"] == 0x3C and not tr[0]["nack"], tr
```

### 7.7 Обмен SPI (D2 = SCK, D3 = MOSI, D4 = MISO, D5 = NSS)

```python
cap = LogicAnalyzer("24m").capture("spi.sr", duration=0.02, trigger={5: "f"})
frames = cap.spi(clk=2, mosi=3, miso=4, cs=5)
assert frames[0][0] == 0x9F                 # первый байт MOSI — команда JEDEC ID
```

Режим SPI (CPOL/CPHA), порядок бит: `cap.decode("spi", "mosi-data", clk=2, mosi=3, cpol=1, cpha=1, bitorder="lsb-first")`.

### 7.8 Латентность реакции (D3 = вход EXTI от стенда, D4 = выход реакции)

```python
cap = LogicAnalyzer("24m").capture("lat.sr", duration=0.05, trigger={3: "r"})
lat = [cap.delay(3, 4, "r", "r", after=t) for t in cap.edge_times(3, "r")]
assert max(x for x in lat if x is not None) < 2e-6
```

### 7.9 Вместе с gdb-обвязкой стенда

Анализатор взводится **до** запуска цели: первая запись после подключения грузит прошивку 1–2 с.

```python
la = LogicAnalyzer("8m")
la.arm("runs/t42.sr", duration=0.5, trigger={7: "r"}, settle=1.0)
t.resume(sync=False)
try:
    t.settle(timeout=0.2)
finally:
    t.halt(timeout=2.0)
cap = la.collect(timeout=5)                 # TriggerTimeout → прошивка не дошла до маркера
```

### 7.10 Фикстура pytest

```python
import pytest
from weact_la import LogicAnalyzer, doctor

@pytest.fixture(scope="session")
def la():
    if not all(ok for _, ok, _ in doctor(verbose=False)):
        pytest.skip("логический анализатор недоступен")
    a = LogicAnalyzer("8m")
    yield a
    a.abort()

def test_pwm(la, tmp_path):
    cap = la.capture(tmp_path / "pwm.sr", duration=0.05)
    assert abs(cap.duty(1) - 0.5) < 0.01
```

Записи сохраняй в артефакты прогона (`runs/<тест>.sr`) — их можно открыть в PulseView.

---

## 8. Как интерпретировать результаты

- Все каналы `1`, фронтов нет — нет общей земли или сигнала; проверь `summary()`.
- `pulses()` возвращает только полные импульсы: обрезанные на краях записи отбрасываются; если список
  пуст — увеличь `duration`.
- Импульсы короче 2–3 отсчётов на 24 МГц (< 100 нс) не измеряются надёжно — расширь импульс в прошивке.
- Расхождение частоты UART > 2–3 % ломает декодер — проверь тактирование STM32 (HSE/HSI, PLL).
- Шум на I²C — смотри подтяжки (обычно 2,2–4,7 кОм к 3,3 В) и длину проводов анализатора.

---

## 9. Неисправности

| Симптом | Причина | Решение |
| --- | --- | --- |
| `lsusb` не видит `0925:3881` | кабель «только зарядка», хаб, питание | другой кабель/порт, без хаба |
| `LIBUSB_ERROR_ACCESS` | нет прав на `/dev/bus/usb/…` | udev-правило раздела 2, `plugdev`, перелогин, переподключить |
| `--scan` показывает только `demo` | права или прошивка не загружена | `doctor`; есть ли `fx2lafw-saleae-logic.fw` |
| Не горит жёлтый светодиод | прошивка не загрузилась | переподключить; `sigrok-cli -d fx2lafw --scan -l 5` для лога |
| `Invalid channel 'D0'` | диапазон `D0-D7` | `D0,D1,…,D7` или без `--channels` |
| Запись «висит» | ждёт триггер | `timeout -s INT`, `collect(timeout=…)`, проверь маркер |
| `LIBUSB_ERROR_*` / обрывы на 16–24 МГц | пропускная способность USB | ниже частота, отдельный порт, меньше `--samples` |
| Все отсчёты `1` | висящие входы, нет GND | общая земля, проверить щупы |
| Декодер молчит | не те каналы/скорость/частота мала | `-A <декодер>` без фильтра, поднять частоту по разделу 4 |
| Два анализатора | неоднозначный выбор | `LogicAnalyzer(conn="<bus>.<dev>")`, адрес из `--scan` |

## 10. Работа на удалённом стенде по SSH

Анализатор подключён к стенду (Linux), а не к рабочей машине. Порядок:

1. Зайти на стенд по SSH и убедиться, что анализатор не занят: PulseView держит устройство монопольно,
   и `sigrok-cli` в это время не может его открыть — сначала закрыть PulseView.
2. Проверить окружение на стенде: `python3 weact_la.py doctor` (sigrok-cli, прошивка fx2lafw, USB-устройство
   `0925:3881` или `1d50:608c`, группа `plugdev`). Права через `uaccess` действуют только для локального
   входа, поэтому для SSH и служб нужна группа `plugdev` (раздел 2).
3. Запускать запись и разбор **на стенде**: обёртке нужен только Python 3.8+ из стандартной библиотеки,
   поэтому разные версии Python на стенде и рабочей машине не мешают.
4. Забирать готовые файлы `.sr` и отчёты через `scp`. Доступ к стенду — только из лабораторной сети
   или через SSH-туннель.

## 11. Поиск начала обмена

Когда событие запускает сценарий под отладчиком, момент старта заранее неизвестен. Приёмы:

| Приём | Как | Ограничение |
|---|---|---|
| Окно записи | заведомо длинная запись, затем поиск нужного участка в ней | много данных, зато не нужен триггер |
| Гейт по разрешающему сигналу | свободный вывод MCU поднимается в начале сценария и служит триггером | нужен свободный вывод |
| Лог прошивки как опора времени | событие помечается строкой в логе, запись привязывается к ней | точность — до задержки вывода лога |

Если сценарий останавливает ядро в точке останова, сигналы на время остановки замирают, а запись
может закончиться раньше, чем обмен начнётся. Поэтому обмен для записи запускают **без остановок**
ядра: включением платы, сбросом или командой, а не пошаговым выполнением.

---

## Файлы навыка

```
scripts/
  test_weact_la.py       тесты обёртки
  weact_la.py            doctor / capture / info: проверка окружения, запись, разбор (sigrok-cli)
```

Пути — относительно каталога навыка. Обёртке нужны только стандартная библиотека Python 3.8+
и `sigrok-cli`; `test_weact_la.py` проверяет её без железа (демо-драйвер sigrok и синтетические сигналы).

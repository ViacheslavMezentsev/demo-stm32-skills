---
name: stm32-acoustic-modem-morse
version: 1.0.0
description: Архитектура и реализация однонаправленного акустического модема (OOK Morse) для передачи диагностики с закрытых STM32-устройств на ПК/смартфон.
license: MIT
compatibility: STM32 (Arduino Core), Python 3.8+ (sounddevice, numpy)
tags: [stm32, dsp, audio, telemetry, morse, ook]
---

# Акустический модем (OOK Morse) для встраиваемых систем

Этот навык описывает, как организовать надежный однонаправленный канал связи между герметичным устройством (или упавшим БПЛА) и ноутбуком/смартфоном, используя только дешевый пассивный пьезоизлучатель (buzzer) и микрофон. Скорость передачи составляет 10-20 Бод (WPM).

## 🧠 Физические ограничения и выбор модуляции

При разработке акустического канала на пьезодинамике возникает ряд физических ограничений:
1. **Узкий резонанс:** Пьезоизлучатель громко звучит только на своей резонансной частоте (обычно ~3000 Гц). Передача данных двумя частотами (FSK, например 2500 Гц и 3000 Гц) неэффективна — одна из частот будет звучать в десятки раз тише, что разрушит алгоритмы распознавания.
2. **Акустическое эхо и инерция:** Пьезомембрана продолжает вибрировать после снятия напряжения. Комната создает эхо. В итоге "тишина" заплывает звуком, и сигналы сливаются.
3. **Джиттер ШИМ:** Использование функции `tone()` может давать нестабильную скважность.

**✅ Идеальное решение: OOK Morse (Амплитудная манипуляция Азбукой Морзе)**
- Используется только одна (самая громкая) резонансная частота через аппаратный ШИМ 50%: `analogWrite(PIN, 127)`.
- Для борьбы с эхом **удлиняются паузы**. Стандартные паузы Морзе (1T) не работают в помещении. Необходимо использовать паузы 2T между точками/тире и 4T между буквами.
- Базовая единица времени $T$ должна быть не менее **50-80 мс**.

## 🏗 Архитектура передатчика (C++ FSM)

Передатчик строится как неблокирующий конечный автомат (FSM), вызываемый в супер-цикле (например, каждую 1 мс). 
Для надежности приема скриптом, передача должна начинаться с **Преамбулы** (Pilot Tone) — непрерывного гудка длительностью 1.5-2 секунды, чтобы микрофон настроил АРУ (AGC), а скрипт синхронизировался.

### Пример реализации C++ (внутри `HmiManager` или аналогичного класса)

```cpp
// Константы таймингов
static constexpr uint32_t MORSE_T_MS = 60; // Базовая единица (T)

// Таблица Морзе (A-Z, 0-9, Точка)
// 1 = точка, 3 = тире, 0 = конец
static const uint8_t MORSE_DICT[37][7] = {
    {1, 3, 0}, {3, 1, 1, 1, 0}, {3, 1, 3, 1, 0}, {3, 1, 1, 0}, {1, 0}, {1, 1, 3, 1, 0}, {3, 3, 1, 0},
    {1, 1, 1, 1, 0}, {1, 1, 0}, {1, 3, 3, 3, 0}, {3, 1, 3, 0}, {1, 3, 1, 1, 0}, {3, 3, 0}, {3, 1, 0},
    {3, 3, 3, 0}, {1, 3, 3, 1, 0}, {3, 3, 1, 3, 0}, {1, 3, 1, 0}, {1, 1, 1, 0}, {3, 0}, {1, 1, 3, 0},
    {1, 1, 1, 3, 0}, {1, 3, 3, 0}, {3, 1, 1, 3, 0}, {3, 1, 3, 3, 0}, {3, 3, 1, 1, 0},
    {3, 3, 3, 3, 3, 0}, {1, 3, 3, 3, 3, 0}, {1, 1, 3, 3, 3, 0}, {1, 1, 1, 3, 3, 0}, {1, 1, 1, 1, 3, 0},
    {1, 1, 1, 1, 1, 0}, {3, 1, 1, 1, 1, 0}, {3, 3, 1, 1, 1, 0}, {3, 3, 3, 1, 1, 0}, {3, 3, 3, 3, 1, 0},
    {1, 3, 1, 3, 1, 3, 0}
};

// State 0=IDLE, 1=TONE, 2=GAP, 3=CHAR_GAP, 4=PREAMBLE, 5=PREAMBLE_GAP, 6=WORD_GAP
bool processMorseModem(uint32_t now) {
    if (morseState == 0 && morseBufferIdx >= morseBufferLen) return false;

    // PREAMBLE (Длинный тон)
    if (morseState == 4) {
        analogWrite(PIN_BUZZER, 127);
        if (morseTimerMs == 0) morseTimerMs = now;
        if (now - morseTimerMs >= 1500) {
            morseState = 5; morseTimerMs = now;
            analogWrite(PIN_BUZZER, 0); digitalWrite(PIN_BUZZER, LOW);
        }
        return true;
    }
    if (morseState == 5) {
        if (now - morseTimerMs >= 500) { morseState = 0; morseTimerMs = 0; }
        return true;
    }
    if (morseBufferIdx >= morseBufferLen) {
        analogWrite(PIN_BUZZER, 0); digitalWrite(PIN_BUZZER, LOW);
        return false;
    }

    // Загрузка символа
    if (morseState == 0) {
        char c = morseBuffer[morseBufferIdx];
        if (c >= 'a' && c <= 'z') c -= 32; 

        if (c == ' ') {
            morseState = 6; // Важно: отдельное состояние, избегает Integer Underflow!
            morseTimerMs = now;
            morseBufferIdx++;
            return true;
        }

        const uint8_t* pattern = nullptr;
        if (c >= 'A' && c <= 'Z') pattern = MORSE_DICT[c - 'A'];
        else if (c >= '0' && c <= '9') pattern = MORSE_DICT[26 + (c - '0')];
        else if (c == '.') pattern = MORSE_DICT[36];

        if (pattern) {
            morseSymbolLen = 0;
            while (pattern[morseSymbolLen] != 0 && morseSymbolLen < 8) {
                morseSymbol[morseSymbolLen] = pattern[morseSymbolLen];
                morseSymbolLen++;
            }
            morseSymbolIdx = 0; morseState = 1;
        } else {
            morseBufferIdx++; return true;
        }
    }

    // Генерация Писка
    if (morseState == 1) {
        analogWrite(PIN_BUZZER, 127);
        uint32_t duration = MORSE_T_MS * morseSymbol[morseSymbolIdx];
        if (morseTimerMs == 0) morseTimerMs = now;
        if (now - morseTimerMs >= duration) {
            morseState = 2; morseTimerMs = now;
            analogWrite(PIN_BUZZER, 0); digitalWrite(PIN_BUZZER, LOW);
        }
    }
    // Пауза (Эхо-подавление: 2T)
    else if (morseState == 2) {
        if (now - morseTimerMs >= (MORSE_T_MS * 2)) {
            morseSymbolIdx++;
            if (morseSymbolIdx >= morseSymbolLen) { morseState = 3; morseTimerMs = now; }
            else { morseState = 1; morseTimerMs = 0; }
        }
    }
    // Межбуквенная пауза (4T)
    else if (morseState == 3) {
        if (now - morseTimerMs >= (MORSE_T_MS * 4)) {
            morseState = 0; morseBufferIdx++; morseTimerMs = 0;
        }
    }
    // Пробел (3T)
    else if (morseState == 6) {
        if (now - morseTimerMs >= (MORSE_T_MS * 3)) {
            morseState = 0; morseTimerMs = 0;
        }
    }
    return true;
}
```

## 📡 Архитектура Приемника (Python SDR)

Приемник работает как SDR (Software-Defined Radio), анализируя аудиопоток порциями по 16 мс.
Ключевые механизмы (Pipeline):
1. **FFT Bandpass Filter:** Энергия собирается строго в окне $\pm 100$ Гц вокруг несущей частоты. Все остальные звуки (голос, шум кулеров) игнорируются.
2. **Экспоненциальное сглаживание (EMA):** Подавляет микро-провалы громкости, чтобы скрипт не "рвал" тире на точки.
3. **Триггер Шмитта (Гистерезис):** Исключает "дребезг" при пересечении порога тишины.
4. **Адаптивные пороги Морзе:** Из-за эха точка всегда физически длиннее, чем $1T$. Порог разделения Точка/Тире смещается к $2.5T$, а порог конца буквы — к $3.0T$.

### Пример реализации Python-декодера (`decoder.py`)

```python
import sounddevice as sd
import numpy as np
import time, sys

SAMPLE_RATE = 16000
CHUNK_SIZE = 256        # 16 мс

FREQ_TARGET = 3000      # Резонансная частота устройства
THRESHOLD_HIGH = 0.012  # Триггер ВКЛ
THRESHOLD_LOW  = 0.006  # Триггер ВЫКЛ
DOT_MS = 60             # Должно совпадать с прошивкой

MORSE_DECODE = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E', '..-.': 'F',
    '--.': 'G', '....': 'H', '..': 'I', '.---': 'J', '-.-': 'K', '.-..': 'L',
    '--': 'M', '-.': 'N', '---': 'O', '.--.': 'P', '--.-': 'Q', '.-.': 'R',
    '...': 'S', '-': 'T', '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X',
    '-.--': 'Y', '--..': 'Z', '-----': '0', '.----': '1', '..---': '2', 
    '...--': '3', '....-': '4', '.....': '5', '-....': '6', '--...': '7', 
    '---..': '8', '----.': '9', '.-.-.-': '.'
}

class MorseDecoder:
    def __init__(self):
        self.is_sounding = False
        self.last_change_time = time.time()
        self.current_symbol = ""
        self.decoded_word = ""
        self.is_synced = False 

    def process_level(self, level_high):
        now = time.time()
        duration_ms = (now - self.last_change_time) * 1000

        if level_high and not self.is_sounding:
            # Звук НАЧАЛСЯ. Оцениваем прошедшую тишину.
            if duration_ms > DOT_MS * 3.0: 
                if self.current_symbol in MORSE_DECODE:
                    self.decoded_word += MORSE_DECODE[self.current_symbol]
                elif len(self.current_symbol) > 0:
                    self.decoded_word += "?"
                self.current_symbol = ""
                
                # Пробел
                if duration_ms > DOT_MS * 5.5 and len(self.decoded_word) > 0: 
                    if self.is_synced:
                        sys.stdout.write(f"\r\n[RECEIVED]: {self.decoded_word}\n")
                    self.decoded_word = ""

            self.is_sounding = True
            self.last_change_time = now

        elif not level_high and self.is_sounding:
            # Звук ЗАКОНЧИЛСЯ. Оцениваем длительность писка.
            if duration_ms > 1000:
                self.is_synced = True
                self.current_symbol = ""
                self.decoded_word = ""
                sys.stdout.write(f"\r\n[SYNC]: Preamble ({duration_ms:.0f} ms). Ready.\n")
            elif duration_ms > 20: 
                if duration_ms < DOT_MS * 2.5: # Точка
                    self.current_symbol += "."
                else:                          # Тире
                    self.current_symbol += "-"
            self.is_sounding = False
            self.last_change_time = now

decoder = MorseDecoder()
smoothed_energy = 0.0

def audio_callback(indata, frames, time_info, status):
    global smoothed_energy
    
    fft_data = np.fft.rfft(indata[:, 0])
    freqs = np.fft.rfftfreq(len(indata[:, 0]), 1.0/SAMPLE_RATE)
    magnitudes = np.abs(fft_data) / frames
    
    # Bandpass filter
    target_idxs = np.where((freqs >= FREQ_TARGET - 100) & (freqs <= FREQ_TARGET + 100))[0]
    band_energy = np.sum(magnitudes[target_idxs]) if len(target_idxs) > 0 else 0
    
    # EMA Smoothing
    smoothed_energy = 0.3 * smoothed_energy + 0.7 * band_energy

    # Schmitt Trigger
    if not decoder.is_sounding and smoothed_energy > THRESHOLD_HIGH: is_high = True
    elif decoder.is_sounding and smoothed_energy < THRESHOLD_LOW: is_high = False
    else: is_high = decoder.is_sounding

    decoder.process_level(is_high)

    # Принудительный вывод в конце
    if not decoder.is_sounding and (time.time() - decoder.last_change_time) > 1.0:
        if len(decoder.current_symbol) > 0:
            if decoder.current_symbol in MORSE_DECODE:
                decoder.decoded_word += MORSE_DECODE[decoder.current_symbol]
            else:
                decoder.decoded_word += "?"
            decoder.current_symbol = ""
            
        if decoder.is_synced and len(decoder.decoded_word) > 0:
            sys.stdout.write(f"\r\n[RECEIVED]: {decoder.decoded_word}\n")
            decoder.decoded_word = ""
            decoder.is_synced = False 

    state_char = "█" if is_high else "░"
    sys.stdout.write(f"\r[{state_char}] Energy @ {FREQ_TARGET}Hz: {smoothed_energy:.3f}   ")
    sys.stdout.flush()

sd.default.device = 1 # Указать ID микрофона
with sd.InputStream(callback=audio_callback, channels=1, samplerate=SAMPLE_RATE, blocksize=CHUNK_SIZE):
    try:
        while True: time.sleep(0.1)
    except KeyboardInterrupt:
        pass
```

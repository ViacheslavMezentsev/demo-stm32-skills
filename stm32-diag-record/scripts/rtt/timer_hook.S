@ Универсальная заглушка диагностики для stm32-diag-record (--backend rtt-timer).
@
@ НАЗНАЧЕНИЕ. Прерывание СВОБОДНОГО таймера выбирает заданные поля прошивки и пишет их кадром в кольцевой
@ буфер RTT. Выполняется на самой цели с полной скоростью; отладчик нужен только для установки и чтения.
@ Это аддитивный приём: код прошивки во флеше не меняется, вектор таймера берётся из копии таблицы в SRAM
@ (регистр VTOR), заглушка и буферы лежат в свободном окне SRAM. После сброса или снятия перехвата следов
@ не остаётся.
@
@ УНИВЕРСАЛЬНОСТЬ. Код заглушки одинаков для всех Cortex-M3/M4 (Thumb-2) и не зависит ни от платы, ни от
@ набора полей. Все адреса (регистр таймера, управляющий блок RTT, буфер, таблица полей, счётчики, адрес
@ метки времени) лежат в блоке параметров сразу за кодом; его заполняет build_rtt_image.py. Таблица полей —
@ отдельная область: слово N, затем N выровненных адресов. Поэтому один и тот же бинарник обслуживает любой
@ набор полей и любую плату: отличаются только данные, не инструкции.
@
@ КАДР (little-endian): u16 magic=0xD1A6, u16 seq (младшие 16 бит счётчика), u32 метка (значение по TICK),
@ затем N слов по адресам из таблицы. Длина кадра FRAME_LEN = 8 + 4*N передаётся в параметрах.
@
@ ПОТЕРИ. Запись только при наличии места до позиции чтения (RdOff ведёт хост): если места меньше кадра,
@ заглушка не пишет и увеличивает счётчик потерь. Так поток не рвётся посередине кадра, а пропуски видны.
@
@ Регистр состояния таймера очищается записью нуля (флаги rc_w0 у таймеров STM32). Выход из прерывания —
@ по EXC_RETURN в lr (bx lr). Штатный обработчик таймера не вызывается: таймер свободен, своего обработчика
@ у прошивки для него нет.

    .syntax unified
    .cpu cortex-m3
    .thumb

    .equ RTT_WROFF, 36          @ смещение WrOff в управляющем блоке RTT (ABI SEGGER)
    .equ RTT_RDOFF, 40          @ смещение RdOff (ведёт хост)
    .equ MAGIC,     0xD1A6

    .section .text
    .global timer_hook
    .type timer_hook, %function
    .align 2
timer_hook:
    push    {r4-r7, lr}
    adr     r7, params          @ r7 -> блок параметров (PC-relative, тот же бинарник)

    @ --- очистить флаг прерывания таймера ---
    ldr     r0, [r7, #0]        @ p0 = адрес регистра состояния таймера (TIM_SR)
    movs    r1, #0
    str     r1, [r0]

    @ --- счётчик прерываний (и метки окна) ---
    ldr     r4, [r7, #16]       @ p4 = адрес блока счётчиков: [0]=counter [4]=dropped [8]=first_tick [12]=last_tick
    ldr     r0, [r4, #0]
    ldr     r6, [r7, #28]       @ p7 = адрес метки времени (tick прошивки)
    ldr     r6, [r6]            @ r6 = текущее значение tick
    cbnz    r0, 1f
    str     r6, [r4, #8]        @ первый тик (один раз, когда counter==0)
1:
    adds    r0, r0, #1
    str     r0, [r4, #0]        @ counter++
    str     r6, [r4, #12]       @ последний тик

    @ --- место в кольце: free = (RdOff - WrOff - 1) mod SIZE ---
    ldr     r2, [r7, #4]        @ p1 = RTT_CB
    ldr     r5, [r2, #RTT_WROFF]   @ r5 = WrOff
    ldr     r1, [r2, #RTT_RDOFF]   @ r1 = RdOff
    ldr     r3, [r7, #12]       @ p3 = RTT_SIZE
    subs    r1, r1, r5
    subs    r1, r1, #1          @ r1 = RdOff - WrOff - 1
    bpl     2f
    adds    r1, r1, r3          @ если отрицательно, + SIZE
2:
    ldr     r0, [r7, #24]       @ p6 = FRAME_LEN
    cmp     r1, r0
    bge     3f                  @ места хватает -> писать
    ldr     r0, [r4, #4]
    adds    r0, r0, #1
    str     r0, [r4, #4]        @ dropped++
    b       9f

3:
    @ --- указатель записи: ptr = RTT_BUF + WrOff ---
    ldr     r0, [r7, #8]        @ p2 = RTT_BUF
    add     r0, r0, r5          @ r0 = ptr

    @ заголовок: magic(u16), seq(u16 = младшие 16 бит counter), tick(u32)
    movw    r1, #MAGIC
    strh    r1, [r0, #0]
    ldr     r1, [r4, #0]        @ counter
    strh    r1, [r0, #2]        @ seq
    str     r6, [r0, #4]        @ tick
    adds    r0, r0, #8

    @ --- поля: таблица = [N][addr0..addrN-1] ---
    ldr     r3, [r7, #20]       @ p5 = FIELDS
    ldr     r2, [r3, #0]        @ N
    adds    r3, r3, #4          @ -> addr0
4:
    cbz     r2, 5f
    ldr     r1, [r3]            @ адрес поля
    ldr     r1, [r1]            @ слово по адресу
    str     r1, [r0]
    adds    r0, r0, #4
    adds    r3, r3, #4
    subs    r2, r2, #1
    b       4b
5:
    @ --- WrOff += FRAME_LEN (mod SIZE; SIZE кратен FRAME_LEN, кадр выровнен) ---
    ldr     r0, [r7, #24]       @ FRAME_LEN
    adds    r5, r5, r0
    ldr     r3, [r7, #12]       @ SIZE
    cmp     r5, r3
    blt     6f
    subs    r5, r5, r3
6:
    ldr     r2, [r7, #4]        @ RTT_CB
    str     r5, [r2, #RTT_WROFF]

9:
    pop     {r4-r7, lr}
    bx      lr

    @ Блок параметров: 8 слов, заполняет build_rtt_image.py. Выровнен на 4.
    .align 2
    .global params
params:
    .word 0     @ p0: TIM_SR  (адрес регистра состояния таймера)
    .word 0     @ p1: RTT_CB  (управляющий блок RTT)
    .word 0     @ p2: RTT_BUF (кольцевой буфер)
    .word 0     @ p3: RTT_SIZE
    .word 0     @ p4: COUNTERS (counter, dropped, first_tick, last_tick)
    .word 0     @ p5: FIELDS  (N, затем N адресов)
    .word 0     @ p6: FRAME_LEN (байт)
    .word 0     @ p7: TICK    (адрес метки времени прошивки)

    .size timer_hook, .-timer_hook

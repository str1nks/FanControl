# fans.py - ручная регулировка вентиляторов (Windows, запуск от администратора)
import ctypes, math, msvcrt, os, shutil, sys, time
from collections import deque
from HardwareMonitor.Hardware import Computer, HardwareType, SensorType

# (тип, имя датчика без пробелов в нижнем регистре) -> отображаемое имя
WANTED = {
    ("board", "fan#1"):   "Кулер CPU",
    ("board", "fan#2"):   "Корпус",
    ("gpu",   "gpufan1"): "GPU Fan 1",
    ("gpu",   "gpufan2"): "GPU Fan 2",
}
RU = {"ф": "a", "й": "q"}
MIN_SPAN = 20                     # минимальный размах шкалы графика, °C

RST, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
GREEN, YELLOW, RED = "\x1b[32m", "\x1b[33m", "\x1b[31m"

if not ctypes.windll.shell32.IsUserAnAdmin():
    sys.exit("Запустите терминал от имени администратора.")
os.system("")                     # включает ANSI-цвета в консоли Windows

pc = Computer()
pc.IsCpuEnabled = pc.IsGpuEnabled = pc.IsMotherboardEnabled = True
pc.Open()


def walk(h):
    yield h
    for s in h.SubHardware:
        yield from walk(s)


def all_hw():
    for h in pc.Hardware:
        yield from walk(h)


def norm(name):
    return name.replace(" ", "").replace("Control", "").lower()


class Chan:
    def __init__(self, sensor, name, hw):
        self.s, self.name, self.hw = sensor, name, hw
        self.manual = None        # None = управляет система, число = наш процент


def find_channels():
    out = []
    for h in all_hw():
        for s in h.Sensors:
            if s.SensorType == SensorType.Control:
                kind = "gpu" if h.HardwareType == HardwareType.GpuNvidia else "board"
                key = (kind, s.Name.replace(" ", "").lower())
                if key in WANTED:
                    out.append(Chan(s, WANTED[key], h))
    order = list(WANTED.values())
    out.sort(key=lambda c: order.index(c.name))
    return out


chans = find_channels()
if not chans:
    pc.Close()
    sys.exit("Каналы управления вентиляторами не найдены.")


def restore():
    for c in chans:
        try:
            c.s.Control.SetDefault()
        except Exception:
            pass


_HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda e: (restore(), 0)[1])
ctypes.windll.kernel32.SetConsoleCtrlHandler(_HANDLER, True)   # закрыли окно -> штатный режим

idx = 0
temps = {"cpu": None, "gpu": None}
hist = {"cpu": deque(maxlen=1800), "gpu": deque(maxlen=1800)}  # по точке в секунду


def refresh():
    for h in all_hw():
        h.Update()


def temp(src):
    htype, names = ((HardwareType.Cpu, ("CPU Package", "Core Max")) if src == "cpu"
                    else (HardwareType.GpuNvidia, ("GPU Core",)))
    for name in names:
        for h in pc.Hardware:
            if h.HardwareType == htype:
                for s in h.Sensors:
                    if (s.SensorType == SensorType.Temperature
                            and s.Name == name and s.Value is not None):
                        return float(s.Value)
    return None


def rpm(c):
    for s in c.hw.Sensors:
        if s.SensorType == SensorType.Fan and norm(s.Name) == norm(c.s.Name):
            return s.Value
    return None


def apply():
    for c in chans:
        if c.manual is not None:
            c.s.Control.SetSoftware(float(c.manual))


def release(c):
    c.manual = None
    c.s.Control.SetDefault()


def current(c):
    if c.manual is not None:
        return c.manual
    return float(c.s.Value) if c.s.Value is not None else None


def adjust(c, d):
    if c.manual is None:          # стартуем с текущего значения
        v = current(c)
        c.manual = round(v) if v is not None else 50
    c.manual = max(0, min(100, c.manual + d))


def color(p):
    return GREEN if p < 50 else YELLOW if p < 75 else RED


def tcolor(t):
    return GREEN if t < 60 else YELLOW if t < 80 else RED


def bar(p, w=24):
    n = round(p / 100 * w)
    return "█" * n + "░" * (w - n)


def fmt_t(t):
    return f"{t:.0f}°C" if t is not None else "—"


def fmt_dur(sec):
    return f"{sec // 60} мин {sec % 60:02d} с" if sec >= 60 else f"{sec} с"


# ---------- график: сплошная линия из символов рамок ----------
def scale(vals):
    """Шкала подстраивается под данные, шаг 5°C, размах не меньше MIN_SPAN."""
    if not vals:
        return 30, 30 + MIN_SPAN
    lo = max(0, math.floor((min(vals) - 3) / 5) * 5)
    hi = math.ceil((max(vals) + 3) / 5) * 5
    while hi - lo < MIN_SPAN:
        if lo > 0 and min(vals) - lo < hi - max(vals):
            lo -= 5
        else:
            hi += 5
    return lo, hi


def smooth(vals, k=3):
    out = []
    for i in range(len(vals)):
        seg = vals[max(0, i - k + 1):i + 1]
        out.append(sum(seg) / len(seg))
    return out


def graph(src, w, h):
    raw = list(hist[src])[-(w - 1):]
    data = smooth(raw)
    lo, hi = scale(data)
    ch = [[" "] * w for _ in range(h)]
    cl = [[""] * w for _ in range(h)]

    def row(v):
        return round((hi - v) / (hi - lo) * (h - 1))

    prev = None
    for x, v in enumerate(data):
        b, c = row(v), tcolor(v)
        if prev is None or prev == b:
            ch[b][x] = "─"
        else:
            a = prev
            if b > a:                          # температура падает
                ch[a][x], ch[b][x] = "╮", "╰"
            else:                              # растёт
                ch[a][x], ch[b][x] = "╯", "╭"
            for r in range(min(a, b) + 1, max(a, b)):
                ch[r][x] = "│"
            for r in range(min(a, b), max(a, b) + 1):
                cl[r][x] = c
        cl[b][x] = c
        prev = b
    if data:                                   # маркер текущего значения
        x = len(data)
        ch[prev][x] = "●"
        cl[prev][x] = BOLD + tcolor(data[-1])

    ticks = {0, h // 2, h - 1}
    rows = []
    for r in range(h):
        if r in ticks:
            v = hi - r / (h - 1) * (hi - lo)
            lab = f"{v:3.0f}┤"
        else:
            lab = "   │"
        line = ""
        for c in range(w):
            if ch[r][c] != " ":
                line += f"{cl[r][c]}{ch[r][c]}{RST}"
            else:
                line += f"{DIM}┈{RST}" if r in ticks else " "
        rows.append(f"{DIM}{lab}{RST}{line}")
    return rows


def graph_title(src, w):
    vals = list(hist[src])[-(w - 1):]
    t = temps[src]
    head = f" {src.upper()} "
    if t is None or not vals:
        return BOLD + (head + "—").ljust(w + 4) + RST
    info = f"   мин {min(vals):.0f}  сред {sum(vals) / len(vals):.0f}  макс {max(vals):.0f}"
    pad = max(0, w + 4 - len(head) - len(fmt_t(t)) - len(info))
    return f"{BOLD}{head}{tcolor(t)}{fmt_t(t)}{RST}{DIM}{info}{RST}" + " " * pad


def graph_footer(src, w):
    n = min(len(hist[src]), w - 1)
    if n == 0:
        return " " * (w + 4)
    left = f"−{fmt_dur(n)}"
    now = "сейчас"
    s = left.ljust(max(len(left) + 1, n - 2)) + now
    return f"{DIM}    {s[:w].ljust(w)}{RST}"


def draw():
    L = [f"{BOLD} УПРАВЛЕНИЕ ВЕНТИЛЯТОРАМИ{RST}",
         f" CPU {BOLD}{fmt_t(temps['cpu'])}{RST}   GPU {BOLD}{fmt_t(temps['gpu'])}{RST}", ""]
    for i, c in enumerate(chans):
        p = current(c)
        b = (f"{color(p)}{bar(p)}{RST} {p:3.0f}%" if p is not None
             else f"{DIM}{'░' * 24}{RST}   — ")
        mode = "ручной" if c.manual is not None else "система"
        r = rpm(c)
        rs = f"{r:5.0f} об/мин" if r is not None else "      — об/мин"
        sel = i == idx
        L.append(f" {BOLD + '▶' + RST if sel else ' '} {(BOLD if sel else '')}"
                 f"{c.name.ljust(12)}{RST} {b}  {mode:<8}{rs}")
    L += ["",
          f"{DIM} ↑↓ выбор   ←→ ±5%   [ ] ±1%   A вернуть системе   0 всё системе   Q выход{RST}"]

    cols, rows = shutil.get_terminal_size((100, 30))
    w = max(16, (cols - 12) // 2)                 # ширина одного графика
    h = min(14, rows - len(L) - 4)                # сколько строк свободно под график
    if h >= 4:
        L.append("")
        L.append(graph_title("cpu", w) + "   " + graph_title("gpu", w))
        for a, b in zip(graph("cpu", w, h), graph("gpu", w, h)):
            L.append(a + "   " + b)
        L.append(graph_footer("cpu", w) + "   " + graph_footer("gpu", w))

    sys.stdout.write("\x1b[H" + "\n".join(l + "\x1b[K" for l in L) + "\x1b[J")
    sys.stdout.flush()


def on_key():
    global idx
    k = msvcrt.getwch()
    if k in ("\x00", "\xe0"):
        k = {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "")
    else:
        k = RU.get(k.lower(), k.lower())
    c = chans[idx]
    if k in ("q", "\x03"):
        return False
    if k == "up":
        idx = (idx - 1) % len(chans)
    elif k == "down":
        idx = (idx + 1) % len(chans)
    elif k in ("left", "right"):
        adjust(c, 5 if k == "right" else -5)
    elif k in ("[", "]"):
        adjust(c, 1 if k == "]" else -1)
    elif k == "a":
        release(c)
    elif k == "0":
        for x in chans:
            release(x)
    apply()
    return True


def main():
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    last = 0.0
    try:
        while True:
            if time.time() - last >= 1.0:
                refresh()
                temps["cpu"], temps["gpu"] = temp("cpu"), temp("gpu")
                for k in ("cpu", "gpu"):
                    if temps[k] is not None:
                        hist[k].append(temps[k])
                apply()
                last = time.time()
                draw()
            if msvcrt.kbhit():
                if not on_key():
                    break
                draw()
            else:
                time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        restore()
        pc.Close()
        sys.stdout.write("\x1b[?1049l\x1b[?25h")
        print("Вентиляторы возвращены в штатный режим.")


main()
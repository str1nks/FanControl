# fans_gui.py — управление вентиляторами в стиле Windows 11 (Fluent + Mica)
# Установка:  pip install PySide6 HardwareMonitor
# Запуск:     python fans_gui.py   (сам попросит права администратора)
# Без чёрного окна консоли: переименуйте в fans_gui.pyw
# Портативный .exe: запустите build.bat (подробности в нём)
import ctypes
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from types import SimpleNamespace

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPalette, QPen)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QSlider, QVBoxLayout, QWidget)

# ───────────────────────── настройки ─────────────────────────
# (тип, имя датчика без пробелов в нижнем регистре) -> отображаемое имя
WANTED = {
    ("board", "fan#1"):   "Кулер CPU",
    ("board", "fan#2"):   "Корпус",
    ("gpu",   "gpufan1"): "GPU Fan 1",
    ("gpu",   "gpufan2"): "GPU Fan 2",
}
SPANS = [("1 мин", 60), ("5 мин", 300), ("15 мин", 900), ("30 мин", 1800)]   # окно графика, сек
DEFAULT_SPAN = 1                                                         # индекс в SPANS (1 = 5 мин)
ICON = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "icon.ico")
MIN_SPAN = 20          # минимальный размах шкалы графика, °C
SAFE_TEMP = 85         # защита: при такой температуре (CPU/GPU) ...
SAFE_PCT = 60          # ... ручные каналы ниже этого % возвращаются системе. None = выкл.


# ───────────────────────── администратор ─────────────────────────
def ensure_admin():
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv   # в .exe argv[0] — сам файл
    params = " ".join(f'"{a}"' for a in args)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit(0)


# ───────────────────────── железо (отдельный поток) ─────────────────────────
class Chan:
    def __init__(self, sensor, name, hw):
        self.s, self.name, self.hw = sensor, name, hw
        self.manual = None            # None = управляет система, число = наш процент


def norm(name):
    return name.replace(" ", "").replace("Control", "").lower()


class Backend(QObject):
    """Весь доступ к датчикам — в одном потоке, интерфейс никогда не ждёт железо."""
    ready = Signal(list)      # имена найденных каналов
    error = Signal(str)
    snapshot = Signal(dict)   # раз в секунду

    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.chans, self.pc = [], None
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def send(self, *cmd):
        self.q.put(cmd)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._thread.is_alive():
            self.q.put(("stop",))
            self._thread.join(timeout=5)

    def restore(self):
        for c in list(self.chans):
            try:
                c.s.Control.SetDefault()
            except Exception:
                pass

    # --- датчики ---
    def _walk(self, h):
        yield h
        for s in h.SubHardware:
            yield from self._walk(s)

    def _all_hw(self):
        for h in self.pc.Hardware:
            yield from self._walk(h)

    def _find_channels(self):
        out = []
        for h in self._all_hw():
            for s in h.Sensors:
                if s.SensorType == SensorType.Control:
                    kind = "gpu" if h.HardwareType == HardwareType.GpuNvidia else "board"
                    key = (kind, s.Name.replace(" ", "").lower())
                    if key in WANTED:
                        out.append(Chan(s, WANTED[key], h))
        order = list(WANTED.values())
        out.sort(key=lambda c: order.index(c.name))
        return out

    def _temp(self, src):
        htype, names = ((HardwareType.Cpu, ("CPU Package", "Core Max")) if src == "cpu"
                        else (HardwareType.GpuNvidia, ("GPU Core",)))
        for name in names:
            for h in self.pc.Hardware:
                if h.HardwareType == htype:
                    for s in h.Sensors:
                        if (s.SensorType == SensorType.Temperature
                                and s.Name == name and s.Value is not None):
                            return float(s.Value)
        return None

    def _rpm(self, c):
        for s in c.hw.Sensors:
            if s.SensorType == SensorType.Fan and norm(s.Name) == norm(c.s.Name):
                return None if s.Value is None else float(s.Value)
        return None

    # --- управление ---
    def _set(self, c, v):
        c.manual = max(0, min(100, int(v)))
        c.s.Control.SetSoftware(float(c.manual))

    def _release(self, c):
        c.manual = None
        c.s.Control.SetDefault()

    def _handle(self, cmd):
        op = cmd[0]
        if op == "set":
            self._set(self.chans[cmd[1]], cmd[2])
        elif op == "release":
            self._release(self.chans[cmd[1]])
        elif op == "release_all":
            for c in self.chans:
                self._release(c)

    def _poll(self):
        for h in self._all_hw():
            h.Update()
        temps = {"cpu": self._temp("cpu"), "gpu": self._temp("gpu")}
        released = []
        if SAFE_TEMP and any(v is not None and v >= SAFE_TEMP for v in temps.values()):
            for i, c in enumerate(self.chans):
                if c.manual is not None and c.manual < SAFE_PCT:
                    self._release(c)
                    released.append(i)
        for c in self.chans:                      # система любит перезаписывать — подтверждаем
            if c.manual is not None:
                c.s.Control.SetSoftware(float(c.manual))
        fans = []
        for c in self.chans:
            sysv = float(c.s.Value) if c.s.Value is not None else None
            fans.append({"pct": c.manual if c.manual is not None else sysv, "rpm": self._rpm(c)})
        self.snapshot.emit({**temps, "fans": fans, "released": released})

    def _run(self):
        global Computer, HardwareType, SensorType
        try:
            from HardwareMonitor.Hardware import Computer, HardwareType, SensorType
            self.pc = Computer()
            self.pc.IsCpuEnabled = self.pc.IsGpuEnabled = self.pc.IsMotherboardEnabled = True
            self.pc.Open()
            self.chans = self._find_channels()
        except Exception as e:
            self.error.emit(f"Не удалось подключиться к датчикам: {e}")
            return
        if not self.chans:
            self.error.emit("Каналы управления вентиляторами не найдены.")
            try:
                self.pc.Close()
            except Exception:
                pass
            return
        self.ready.emit([c.name for c in self.chans])
        last = 0.0
        try:
            while True:
                try:
                    cmd = self.q.get(timeout=0.05)
                except queue.Empty:
                    cmd = None
                if cmd:
                    if cmd[0] == "stop":
                        break
                    try:
                        self._handle(cmd)
                    except Exception:
                        pass
                if time.time() - last >= 1.0:
                    try:
                        self._poll()
                    except Exception:
                        pass
                    last = time.time()
        finally:
            self.restore()
            try:
                self.pc.Close()
            except Exception:
                pass


# ───────────────────────── тема ─────────────────────────
def is_dark():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


def make_theme(dark):
    C = QColor
    accent = C("#60CDFF" if dark else "#0067C0")
    try:                                          # системный акцентный цвет (Qt ≥ 6.6)
        c = QGuiApplication.palette().color(QPalette.ColorRole.Accent)
        if c.isValid() and c.alpha() > 0:
            if dark and c.lightness() < 150:
                c = c.lighter(150)
            if not dark and c.lightness() > 120:
                c = c.darker(130)
            accent = c
    except Exception:
        pass
    if dark:
        d = dict(bg=C("#202020"), card=C(255, 255, 255, 13), border=C(255, 255, 255, 22),
                 text=C(255, 255, 255), text2=C(255, 255, 255, 199), text3=C(255, 255, 255, 140),
                 ctrl=C(255, 255, 255, 15), ctrl_h=C(255, 255, 255, 22), ctrl_p=C(255, 255, 255, 9),
                 seg=C(255, 255, 255, 10), seg_sel=C(255, 255, 255, 26),
                 grid=C(255, 255, 255, 22), track=C(255, 255, 255, 90), idle=C(255, 255, 255, 150),
                 ring=C("#454545"), tip=C("#2c2c2c"),
                 green=C("#6CCB5F"), yellow=C("#FCE100"), red=C("#FF99A4"))
    else:
        d = dict(bg=C("#f3f3f3"), card=C(255, 255, 255, 179), border=C(0, 0, 0, 20),
                 text=C(0, 0, 0, 228), text2=C(0, 0, 0, 158), text3=C(0, 0, 0, 114),
                 ctrl=C(255, 255, 255, 179), ctrl_h=C(249, 249, 249, 128), ctrl_p=C(249, 249, 249, 77),
                 seg=C(0, 0, 0, 12), seg_sel=C(255, 255, 255, 255),
                 grid=C(0, 0, 0, 20), track=C(0, 0, 0, 115), idle=C(0, 0, 0, 110),
                 ring=C("#ffffff"), tip=C("#ffffff"),
                 green=C("#0F7B0F"), yellow=C("#9D5D00"), red=C("#C42B1C"))
    on_accent = C(0, 0, 0) if accent.lightness() > 128 else C(255, 255, 255)
    return SimpleNamespace(**d, accent=accent, on_accent=on_accent, dark=dark)


def rgba(c, a=None):
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha() if a is None else a})"


def blend(a, b, f):
    return QColor(int(a.red() + (b.red() - a.red()) * f),
                  int(a.green() + (b.green() - a.green()) * f),
                  int(a.blue() + (b.blue() - a.blue()) * f))


def temp_color(t, v):
    if v <= 55:
        return t.green
    if v < 62:
        return blend(t.green, t.yellow, (v - 55) / 7)
    if v <= 72:
        return t.yellow
    if v < 80:
        return blend(t.yellow, t.red, (v - 72) / 8)
    return t.red


QSS = """
* { font-family: "Segoe UI Variable Text", "Segoe UI"; font-size: 14px; color: %(text)s; }
#root { background: %(root_bg)s; }
QLabel { background: transparent; }
#card { background: %(card)s; border: 1px solid %(border)s; border-radius: 8px; }
#title { font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 28px; font-weight: 600; }
#section { font-size: 16px; font-weight: 600; }
#cardTitle { font-size: 15px; font-weight: 600; }
#big { font-family: "Segoe UI Variable Display", "Segoe UI"; font-size: 30px; font-weight: 600; }
#sub { color: %(text2)s; }
QPushButton { background: %(ctrl)s; border: 1px solid %(border)s; border-radius: 4px; padding: 6px 14px; }
QPushButton:hover { background: %(ctrl_h)s; }
QPushButton:pressed { background: %(ctrl_p)s; color: %(text2)s; }
QPushButton#accent { background: %(accent)s; border: 1px solid %(accent)s; color: %(on_accent)s; }
QPushButton#accent:hover { background: %(accent_h)s; }
QPushButton#accent:pressed { background: %(accent_p)s; }
#seg { background: %(seg)s; border-radius: 6px; }
#seg QPushButton { background: transparent; border: none; border-radius: 4px; padding: 4px 14px; color: %(text2)s; }
#seg QPushButton:hover { background: %(ctrl)s; }
#seg QPushButton:checked { background: %(seg_sel)s; color: %(text)s; }
#seg QPushButton#segManual:checked { background: %(accent)s; color: %(on_accent)s; }
"""


def build_qss(t, solid):
    return QSS % dict(
        text=rgba(t.text), text2=rgba(t.text2), card=rgba(t.card), border=rgba(t.border),
        ctrl=rgba(t.ctrl), ctrl_h=rgba(t.ctrl_h), ctrl_p=rgba(t.ctrl_p),
        seg=rgba(t.seg), seg_sel=rgba(t.seg_sel),
        accent=rgba(t.accent, 255), accent_h=rgba(t.accent, 230), accent_p=rgba(t.accent, 200),
        on_accent=rgba(t.on_accent), root_bg=rgba(t.bg) if solid else "transparent")


def apply_backdrop(hwnd, dark):
    """Тёмный заголовок + материал Mica (Windows 11 22H2+). Возвращает True, если Mica включён."""
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        v = ctypes.c_int(1 if dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4)       # IMMERSIVE_DARK_MODE
        if sys.getwindowsversion().build < 22621:
            return False

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]

        m = MARGINS(-1, -1, -1, -1)
        dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(MARGINS)]
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
        kind = ctypes.c_int(2)                                        # SYSTEMBACKDROP: Mica
        return dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(kind), 4) == 0
    except Exception:
        return False


# ───────────────────────── утилиты ─────────────────────────
def fmt_t(t):
    return f"{t:.0f} °C" if t is not None else "—"


def fmt_dur(sec):
    return f"{sec // 60} мин {sec % 60:02d} с" if sec >= 60 else f"{sec} с"


def smooth(vals, k=3):
    out = []
    for i in range(len(vals)):
        seg = vals[max(0, i - k + 1):i + 1]
        out.append(sum(seg) / len(seg))
    return out


def scale(vals):
    """Шкала подстраивается под данные, шаг 5 °C, размах не меньше MIN_SPAN."""
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


def make_seg(labels):
    f = QFrame()
    f.setObjectName("seg")
    lay = QHBoxLayout(f)
    lay.setContentsMargins(2, 2, 2, 2)
    lay.setSpacing(2)
    group = QButtonGroup(f)
    group.setExclusive(True)
    btns = []
    for text in labels:
        b = QPushButton(text)
        b.setCheckable(True)
        b.setFocusPolicy(Qt.NoFocus)
        group.addButton(b)
        lay.addWidget(b)
        btns.append(b)
    btns[0].setChecked(True)
    return f, btns


# ───────────────────────── виджеты ─────────────────────────
class FluentSlider(QSlider):
    PAD = 12

    def __init__(self, theme):
        super().__init__(Qt.Horizontal)
        self.t, self.manual, self._hover, self._drag = theme, False, False, False
        self.setRange(0, 100)
        self.setSingleStep(1)
        self.setPageStep(5)
        self.setFixedHeight(30)
        self.setMouseTracking(True)

    def _x(self, v):
        return self.PAD + (self.width() - 2 * self.PAD) * v / 100

    def _val(self, x):
        return round(max(0, min(100, (x - self.PAD) / max(1, self.width() - 2 * self.PAD) * 100)))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = True
            self.setSliderDown(True)
            self.setValue(self._val(e.position().x()))
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag:
            self.setValue(self._val(e.position().x()))

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.setSliderDown(False)
            self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, _):
        t = self.t
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        x0, x1, xv = self.PAD, self.width() - self.PAD, self._x(self.value())
        p.setPen(QPen(t.track, 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x0, cy), QPointF(x1, cy))
        dot = t.accent if self.manual else t.idle
        if xv > x0:
            p.setPen(QPen(dot, 4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x0, cy), QPointF(xv, cy))
        p.setPen(QPen(t.border, 1))
        p.setBrush(t.ring)
        p.drawEllipse(QPointF(xv, cy), 10, 10)
        p.setPen(Qt.NoPen)
        p.setBrush(dot)
        r = 5 if self._drag else 7 if self._hover else 6
        p.drawEllipse(QPointF(xv, cy), r, r)


class FanCard(QFrame):
    def __init__(self, idx, name, theme, backend):
        super().__init__()
        self.idx, self.backend, self.manual = idx, backend, None
        self.setObjectName("card")
        self.setMinimumHeight(118)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 10)
        v.setSpacing(2)

        top = QHBoxLayout()
        t = QLabel(name)
        t.setObjectName("cardTitle")
        self.rpm = QLabel("— об/мин")
        self.rpm.setObjectName("sub")
        top.addWidget(t)
        top.addStretch()
        top.addWidget(self.rpm)
        v.addLayout(top)

        mid = QHBoxLayout()
        self.pct = QLabel("—")
        self.pct.setObjectName("big")
        self.pct.setMinimumWidth(100)
        seg, (self.b_auto, self.b_man) = make_seg(["Система", "Вручную"])
        self.b_man.setObjectName("segManual")
        self.b_auto.clicked.connect(self._to_auto)
        self.b_man.clicked.connect(self._to_manual)
        mid.addWidget(self.pct)
        mid.addStretch()
        mid.addWidget(seg)
        v.addLayout(mid)

        self.slider = FluentSlider(theme)
        self.slider.sliderPressed.connect(self._pressed)
        self.slider.valueChanged.connect(self._changed)
        v.addWidget(self.slider)

    # состояние
    def set_manual(self, val):
        was, now = self.manual is not None, val is not None
        self.manual = val
        if was != now:
            (self.b_man if now else self.b_auto).setChecked(True)
            self.slider.manual = now
            self.slider.update()

    def _show(self, p):
        self.pct.setText(f"{p:.0f}%")
        self.slider.blockSignals(True)
        self.slider.setValue(round(p))
        self.slider.blockSignals(False)

    def force(self, val):                     # из быстрых режимов
        self.set_manual(val)
        if val is not None:
            self._show(val)

    # действия пользователя
    def _to_auto(self):
        self.set_manual(None)
        self.backend.send("release", self.idx)

    def _to_manual(self):
        v = self.slider.value()
        self.set_manual(v)
        self.backend.send("set", self.idx, v)

    def _pressed(self):
        if self.manual is None:
            self._to_manual()

    def _changed(self, v):
        self.set_manual(v)
        self.pct.setText(f"{v}%")
        self.backend.send("set", self.idx, v)

    # данные от железа
    def update_state(self, pct, rpm):
        if self.manual is None and pct is not None and not self.slider.isSliderDown():
            self._show(pct)
        self.rpm.setText(f"{rpm:,.0f}".replace(",", " ") + " об/мин" if rpm is not None else "— об/мин")


class Graph(QWidget):
    def __init__(self, theme, hist):
        super().__init__()
        self.t, self.hist, self.span, self.hx = theme, hist, SPANS[DEFAULT_SPAN][1], None
        self.setMinimumHeight(110)
        self.setMouseTracking(True)

    def set_span(self, s):
        self.span = s
        self.update()

    def mouseMoveEvent(self, e):
        self.hx = e.position().x()
        self.update()

    def leaveEvent(self, e):
        self.hx = None
        self.update()

    def paintEvent(self, _):
        t = self.t
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = p.font()
        f.setPixelSize(11)
        p.setFont(f)
        W, H = self.width(), self.height()
        L, R, T, B = 34, 8, 8, 22
        pw, ph = W - L - R, H - T - B
        data = smooth(list(self.hist)[-self.span:])
        n = len(data)
        lo, hi = scale(data)

        def X(i):
            return L + i * pw / (self.span - 1)

        def Y(v):
            return T + (hi - v) / (hi - lo) * ph

        # сетка и подписи
        step = 5 if hi - lo <= 25 else 10 if hi - lo <= 60 else 20
        v = math.ceil(lo / step) * step
        while v <= hi:
            y = Y(v)
            p.setPen(QPen(t.grid, 1))
            p.drawLine(QPointF(L, y), QPointF(L + pw, y))
            p.setPen(t.text3)
            p.drawText(QRectF(0, y - 8, L - 6, 16), Qt.AlignRight | Qt.AlignVCenter, f"{v:.0f}")
            v += step
        if not n:
            return
        # подписи времени: слева начало данных, справа — текущий момент
        p.setPen(t.text3)
        xr = X(n - 1)
        if xr - L > 150:
            p.drawText(QRectF(L, H - B + 4, 100, 16), Qt.AlignLeft | Qt.AlignVCenter, f"−{fmt_dur(n - 1)}")
        if xr - L > 50:
            p.drawText(QRectF(xr - 100, H - B + 4, 100, 16), Qt.AlignRight | Qt.AlignVCenter, "сейчас")
        else:
            p.drawText(QRectF(L, H - B + 4, 100, 16), Qt.AlignLeft | Qt.AlignVCenter, "сейчас")

        # линия + заливка
        path = QPainterPath(QPointF(X(0), Y(data[0])))
        for i in range(1, n):
            path.lineTo(X(i), Y(data[i]))
        area = QPainterPath(path)
        area.lineTo(X(n - 1), T + ph)
        area.lineTo(X(0), T + ph)
        area.closeSubpath()
        cur = temp_color(t, data[-1])
        g = QLinearGradient(0, T, 0, T + ph)
        c1, c2 = QColor(cur), QColor(cur)
        c1.setAlpha(70)
        c2.setAlpha(0)
        g.setColorAt(0, c1)
        g.setColorAt(1, c2)
        p.fillPath(area, QBrush(g))
        sg = QLinearGradient(0, T, 0, T + ph)
        for k in range(25):
            sg.setColorAt(k / 24, temp_color(t, hi - k * (hi - lo) / 24))
        p.setPen(QPen(QBrush(sg), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        # маркер «сейчас»
        p.setPen(QPen(t.ring, 2))
        p.setBrush(cur)
        p.drawEllipse(QPointF(X(n - 1), Y(data[-1])), 4.5, 4.5)

        # подсказка под курсором
        if self.hx is not None:
            i = round((self.hx - L) * (self.span - 1) / pw)
            if 0 <= i < n:
                x, y = X(i), Y(data[i])
                p.setPen(QPen(t.grid, 1))
                p.drawLine(QPointF(x, T), QPointF(x, T + ph))
                p.setPen(Qt.NoPen)
                p.setBrush(temp_color(t, data[i]))
                p.drawEllipse(QPointF(x, y), 4, 4)
                ago = n - 1 - i
                txt = f"{data[i]:.0f} °C · " + (f"−{fmt_dur(ago)}" if ago else "сейчас")
                tw = p.fontMetrics().horizontalAdvance(txt) + 18
                bx = x + 10 if x + 10 + tw < W - R else x - 10 - tw
                rect = QRectF(bx, T + 4, tw, 22)
                p.setPen(QPen(t.border, 1))
                p.setBrush(t.tip)
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(t.text)
                p.drawText(rect, Qt.AlignCenter, txt)


class GraphCard(QFrame):
    def __init__(self, title, theme, hist):
        super().__init__()
        self.t, self.hist = theme, hist
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 12, 8)
        v.setSpacing(0)
        top = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("cardTitle")
        self.stats = QLabel("")
        self.stats.setObjectName("sub")
        top.addWidget(name)
        top.addStretch()
        top.addWidget(self.stats)
        v.addLayout(top)
        self.temp = QLabel("—")
        self.temp.setObjectName("big")
        v.addWidget(self.temp)
        self.graph = Graph(theme, hist)
        v.addWidget(self.graph, 1)

    def refresh(self, temp):
        self.temp.setText(fmt_t(temp))
        self.temp.setStyleSheet(f"color: {temp_color(self.t, temp).name()};" if temp is not None else "")
        vals = list(self.hist)[-self.graph.span:]
        if vals:
            self.stats.setText(f"мин {min(vals):.0f}  ·  сред {sum(vals) / len(vals):.0f}  ·  макс {max(vals):.0f}")
        self.graph.update()


# ───────────────────────── главное окно ─────────────────────────
class MainWindow(QWidget):
    def __init__(self, backend):
        super().__init__()
        self.backend, self.cards, self.warn_until, self._bd = backend, [], 0.0, False
        self.dark = is_dark()
        self.t = make_theme(self.dark)
        self.mica = sys.getwindowsversion().build >= 22621
        self.hist = {"cpu": deque(maxlen=1800), "gpu": deque(maxlen=1800)}

        self.setObjectName("root")
        self.setAttribute(Qt.WA_StyledBackground, True)
        if self.mica:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Управление вентиляторами")
        if os.path.exists(ICON):
            self.setWindowIcon(QIcon(ICON))
        self.resize(1000, 780)
        self.setMinimumSize(900, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 24)
        root.setSpacing(0)

        title = QLabel("Вентиляторы")
        title.setObjectName("title")
        self.status = QLabel("Подключение к датчикам…")
        self.status.setObjectName("sub")
        root.addWidget(title)
        root.addWidget(self.status)
        root.addSpacing(18)

        # температуры
        row = QHBoxLayout()
        lab = QLabel("Температура")
        lab.setObjectName("section")
        seg, span_btns = make_seg([s[0] for s in SPANS])
        for b, (_, sec) in zip(span_btns, SPANS):
            b.clicked.connect(lambda _=False, s=sec: self.set_span(s))
        span_btns[DEFAULT_SPAN].setChecked(True)
        row.addWidget(lab)
        row.addStretch()
        row.addWidget(seg)
        root.addLayout(row)
        root.addSpacing(8)
        g = QHBoxLayout()
        g.setSpacing(12)
        self.gc = {"cpu": GraphCard("Процессор", self.t, self.hist["cpu"]),
                   "gpu": GraphCard("Видеокарта", self.t, self.hist["gpu"])}
        g.addWidget(self.gc["cpu"])
        g.addWidget(self.gc["gpu"])
        root.addLayout(g, 1)
        root.addSpacing(18)

        # вентиляторы
        row = QHBoxLayout()
        row.setSpacing(8)
        lab = QLabel("Вентиляторы")
        lab.setObjectName("section")
        row.addWidget(lab)
        row.addStretch()
        b = QPushButton("Всё системе")
        b.setObjectName("accent")
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(self.release_all)
        row.addWidget(b)
        root.addLayout(row)
        root.addSpacing(8)
        self.grid = QGridLayout()
        self.grid.setSpacing(12)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        root.addLayout(self.grid)

        self.apply_qss(solid=not self.mica)
        backend.ready.connect(self.on_ready)
        backend.error.connect(self.on_error)
        backend.snapshot.connect(self.on_snapshot)

    def apply_qss(self, solid):
        self.setStyleSheet(build_qss(self.t, solid))

    def showEvent(self, e):
        super().showEvent(e)
        if not self._bd:
            self._bd = True
            ok = apply_backdrop(int(self.winId()), self.dark)
            if self.mica and not ok:
                self.apply_qss(solid=True)

    def closeEvent(self, e):
        self.backend.stop()
        super().closeEvent(e)

    # действия
    def set_span(self, sec):
        for gc in self.gc.values():
            gc.graph.set_span(sec)
            gc.refresh(None if not gc.hist else gc.hist[-1])

    def release_all(self):
        for c in self.cards:
            c.force(None)
        self.backend.send("release_all")

    # события бэкенда
    def on_ready(self, names):
        for i, name in enumerate(names):
            card = FanCard(i, name, self.t, self.backend)
            self.cards.append(card)
            self.grid.addWidget(card, i // 2, i % 2)
        self.status.setText("Датчики подключены")

    def on_error(self, msg):
        self.status.setText(msg)
        self.status.setStyleSheet(f"color: {self.t.red.name()};")

    def on_snapshot(self, s):
        for k in ("cpu", "gpu"):
            if s[k] is not None:
                self.hist[k].append(s[k])
            self.gc[k].refresh(s[k])
        for card, f in zip(self.cards, s["fans"]):
            card.update_state(f["pct"], f["rpm"])
        if s["released"]:
            self.warn_until = time.time() + 15
            for i in s["released"]:
                self.cards[i].force(None)
        if time.time() < self.warn_until:
            self.status.setText(f"⚠ Температура ≥ {SAFE_TEMP} °C — вентиляторы возвращены системе")
            self.status.setStyleSheet(f"color: {self.t.red.name()};")
        else:
            m = sum(1 for c in self.cards if c.manual is not None)
            self.status.setStyleSheet("")
            self.status.setText("Все вентиляторы под управлением системы" if m == 0
                                else f"Вручную: {m} из {len(self.cards)}")


# ───────────────────────── запуск ─────────────────────────
def main():
    ensure_admin()
    try:                                          # своя иконка в панели задач, а не python.exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("fans.control.gui")
    except Exception:
        pass
    app = QApplication(sys.argv)
    if os.path.exists(ICON):
        app.setWindowIcon(QIcon(ICON))
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    font.setPixelSize(14)
    app.setFont(font)

    backend = Backend()
    win = MainWindow(backend)
    win.show()
    backend.start()
    app.aboutToQuit.connect(backend.stop)

    # закрыли консоль/сессию -> вентиляторы в штатный режим
    handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)(lambda e: (backend.restore(), 0)[1])
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)

    code = app.exec()
    backend.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
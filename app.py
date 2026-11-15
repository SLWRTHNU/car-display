# Car BG display. Lives in GitHub, pulled to the Pico on boot when version.txt changes.
# Board: Raspberry Pi Pico 2 W (MicroPython)  Display: Waveshare Pico-ResTouch-LCD-2.8
import time, gc, framebuf, network, ntptime
from machine import Pin, PWM, SPI
import config
import font

try:
    import requests
except ImportError:
    import urequests as requests

# ---------- Settings ----------
W, H = 320, 240
MADCTL = 0x70        # if the screen is upside down or mirrored try 0xA0, 0xB0 or 0xC0
INVERT = True        # if colors look inverted set to False
FETCH_MS = 30000
LOW_MMOL = 3.9
HIGH_MMOL = 10.0
LEVELS = [100, 75, 50, 25, 1, 50]   # boot at 100%, each tap steps to the next, then loops
# Nightscout uses the 1970 epoch. Some MicroPython builds count from 2000, some from 1970.
UNIX_OFFSET = 946684800 if time.gmtime(0)[0] == 2000 else 0


_RGB = {}


def rgb(r, g, b):
    c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    v = ((c & 0xFF) << 8) | (c >> 8)   # byte swapped for the ST7789
    _RGB[v] = (r, g, b)
    return v


BLACK = rgb(0, 0, 0)
WHITE = rgb(255, 255, 255)
RED = rgb(255, 0, 0)
GREEN = rgb(0, 230, 0)
YELLOW = rgb(255, 220, 0)
ORANGE = rgb(255, 150, 0)
GREY = rgb(70, 70, 70)


# ---------- Display ----------
class LCD:
    def __init__(self):
        self.spi = SPI(1, baudrate=40_000_000, polarity=0, phase=0,
                       sck=Pin(10), mosi=Pin(11), miso=Pin(12))
        self.cs = Pin(9, Pin.OUT, value=1)
        self.dc = Pin(8, Pin.OUT, value=0)
        self.rst = Pin(15, Pin.OUT, value=1)
        self.tp_cs = Pin(16, Pin.OUT, value=1)   # keep touch chip off the shared SPI bus
        self.init()

    def cmd(self, c, data=None):
        self.cs(0)
        self.dc(0)
        self.spi.write(bytes([c]))
        if data:
            self.dc(1)
            self.spi.write(data)
        self.cs(1)

    def init(self):
        self.rst(1); time.sleep_ms(50)
        self.rst(0); time.sleep_ms(50)
        self.rst(1); time.sleep_ms(150)
        self.cmd(0x36, bytes([MADCTL]))
        self.cmd(0x3A, bytes([0x05]))
        self.cmd(0xB2, bytes([0x0C, 0x0C, 0x00, 0x33, 0x33]))
        self.cmd(0xB7, bytes([0x35]))
        self.cmd(0xBB, bytes([0x1F]))
        self.cmd(0xC0, bytes([0x2C]))
        self.cmd(0xC2, bytes([0x01]))
        self.cmd(0xC3, bytes([0x12]))
        self.cmd(0xC4, bytes([0x20]))
        self.cmd(0xC6, bytes([0x0F]))
        self.cmd(0xD0, bytes([0xA4, 0xA1]))
        self.cmd(0xE0, bytes([0xD0, 0x08, 0x11, 0x08, 0x0C, 0x15, 0x39, 0x33, 0x50, 0x36, 0x13, 0x14, 0x29, 0x2D]))
        self.cmd(0xE1, bytes([0xD0, 0x08, 0x10, 0x08, 0x06, 0x06, 0x39, 0x44, 0x51, 0x0B, 0x16, 0x14, 0x2F, 0x31]))
        self.cmd(0x21 if INVERT else 0x20)
        self.cmd(0x11); time.sleep_ms(120)
        self.cmd(0x29)

    def show(self, buf):
        self.cmd(0x2A, bytes([0, 0, (W - 1) >> 8, (W - 1) & 0xFF]))
        self.cmd(0x2B, bytes([0, 0, (H - 1) >> 8, (H - 1) & 0xFF]))
        self.cmd(0x2C)
        self.cs(0)
        self.dc(1)
        self.spi.write(buf)
        self.cs(1)


# ---------- Drawing helpers ----------
_pals = {}
_gbuf = bytearray(font.GLYPH_MAX)
_gmv = memoryview(_gbuf)


def palette(fg, bg):
    """16 shades from bg to fg, used to colorize antialiased glyphs."""
    k = (fg, bg)
    p = _pals.get(k)
    if p is None:
        p = framebuf.FrameBuffer(bytearray(32), 16, 1, framebuf.RGB565)
        r1, g1, b1 = _RGB[fg]
        r0, g0, b0 = _RGB[bg]
        for i in range(16):
            p.pixel(i, 0, rgb(r0 + (r1 - r0) * i // 15,
                              g0 + (g1 - g0) * i // 15,
                              b0 + (b1 - b0) * i // 15))
        _pals[k] = p
    return p


def text_w(s, f):
    idx = f[1]
    return sum(idx[c][0] for c in s if c in idx)


def text_big(fb, s, x, y, f, color, bg=BLACK):
    H, idx, fn = f
    pal = palette(color, bg)
    with open(fn, "rb") as fh:
        for ch in s:
            g = idx.get(ch)
            if g is None:
                continue
            w, off = g
            n = ((w + 1) // 2) * H
            fh.seek(off)
            fh.readinto(_gmv[:n])
            fb.blit(framebuf.FrameBuffer(_gmv[:n], w, H, framebuf.GS4_HMSB), x, y, -1, pal)
            x += w


def icon(fb, name, x, y, color):
    w, h, off = font.ICONS[name]
    n = ((w + 1) // 2) * h
    with open(font.ICONS_FILE, "rb") as fh:
        fh.seek(off)
        fh.readinto(_gmv[:n])
    fb.blit(framebuf.FrameBuffer(_gmv[:n], w, h, framebuf.GS4_HMSB), x, y, -1, palette(color, BLACK))


# ---------- State ----------
fb_buf = bytearray(W * H * 2)
fb = framebuf.FrameBuffer(fb_buf, W, H, framebuf.RGB565)

have = False
sgv = 0
has_delta = False
delta = 0
direction = "Flat"
last_epoch = 0

def bg_color():
    m = sgv / 18.0
    if m < LOW_MMOL:
        return RED
    if m > HIGH_MMOL:
        return YELLOW
    return GREEN


def age_text():
    if not have:
        return "No data", ORANGE
    now = time.time() + UNIX_OFFSET
    if now < 1700000000:
        return "--", GREY
    mins = max(0, int((now - last_epoch) // 60))
    col = ORANGE if mins >= 10 else WHITE
    if mins >= 60:
        return "%dh %dm" % (mins // 60, mins % 60), col
    return "%d min" % mins, col


def render(heart_on, overlay):
    fb.fill(BLACK)
    # age, top
    s, col = age_text()
    text_big(fb, s, 10, 2, font.MED, col)
    # heart, top right
    if heart_on:
        icon(fb, "heart", 277, 6, RED)
    # BG, middle
    if have:
        s = "%.1f" % (sgv / 18.0)
        f = font.BIG1 if len(s) <= 3 else font.BIG2
        text_big(fb, s, (W - text_w(s, f)) // 2, 58 + (font.BIG1[0] - f[0]) // 2, f, bg_color())
        # trend arrow, bottom left
        if direction in font.ICONS:
            icon(fb, direction, 39, 168, bg_color())
        else:
            text_big(fb, "?", 62, 176, font.MED, GREY)
        # delta, bottom right
        if has_delta:
            s = "%+.1f" % (delta / 18.0)
            text_big(fb, s, 312 - text_w(s, font.MED), 176, font.MED, WHITE)
    else:
        text_big(fb, "--", (W - text_w("--", font.BIG1)) // 2, 58, font.BIG1, GREY)
    # brightness overlay
    if overlay:
        fb.fill_rect(5, 94, 310, 52, GREY)
        text_big(fb, overlay, (W - text_w(overlay, font.SMALL)) // 2, 101, font.SMALL, WHITE, GREY)


def fetch():
    global have, sgv, delta, has_delta, direction, last_epoch
    try:
        r = requests.get(config.NS_URL, timeout=10)
    except TypeError:
        r = requests.get(config.NS_URL)
    try:
        arr = r.json()
    finally:
        r.close()
        gc.collect()
    if not arr:
        return False
    v = int(arr[0]["sgv"])
    if v <= 0:
        return False
    sgv = v
    direction = arr[0].get("direction", "Flat")
    last_epoch = int(arr[0]["date"]) // 1000
    if len(arr) > 1 and int(arr[1].get("sgv", 0)) > 0:
        has_delta = True
        delta = sgv - int(arr[1]["sgv"])
    else:
        has_delta = False
    have = True
    return True


# ---------- Main ----------
def run():
    lcd = LCD()
    bl = PWM(Pin(13), freq=5000)
    level = 0
    bl.duty_u16(LEVELS[level] * 65535 // 100)

    tp_irq = Pin(17, Pin.IN, Pin.PULL_UP)   # low while the screen is touched
    prev_touch = 1

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    heart_on = False
    overlay = None
    overlay_until = 0
    last_fetch = -FETCH_MS
    last_wifi = time.ticks_ms()
    last_heart = time.ticks_ms()
    last_touch = 0
    last_ntp = -60000
    ntp_ok = False
    dirty = True

    ntptime.host = "pool.ntp.org"

    while True:
        now = time.ticks_ms()

        # touch: one step per tap
        t = tp_irq.value()
        if t == 0 and prev_touch == 1 and time.ticks_diff(now, last_touch) > 300:
            last_touch = now
            level = (level + 1) % len(LEVELS)
            bl.duty_u16(LEVELS[level] * 65535 // 100)
            overlay = "Brightness: %d%%" % LEVELS[level]
            overlay_until = time.ticks_add(now, 3000)
            dirty = True
        prev_touch = t

        # WiFi keep-alive (hotspot may come up after boot)
        if not wlan.isconnected():
            if time.ticks_diff(now, last_wifi) > 15000:
                last_wifi = now
                try:
                    wlan.disconnect()
                    wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
                except Exception as e:
                    print("wifi:", e)
        else:
            if not ntp_ok and time.ticks_diff(now, last_ntp) > 60000:
                last_ntp = now
                try:
                    ntptime.settime()
                    ntp_ok = True
                except Exception as e:
                    print("ntp:", e)
            if time.ticks_diff(now, last_fetch) >= FETCH_MS:
                last_fetch = now
                try:
                    if fetch():
                        dirty = True
                except Exception as e:
                    print("fetch:", e)

        # heart blink, 1 s (also refreshes the age text)
        if time.ticks_diff(now, last_heart) >= 1000:
            last_heart = now
            heart_on = not heart_on
            dirty = True

        # overlay expiry
        if overlay and time.ticks_diff(now, overlay_until) > 0:
            overlay = None
            dirty = True

        if dirty:
            render(heart_on, overlay)
            lcd.show(fb_buf)
            dirty = False

        time.sleep_ms(20)

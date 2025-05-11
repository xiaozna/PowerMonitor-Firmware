"""PicoW PowerMonitor"""

import sys
import rp2
import network
import ntptime
import ubinascii
import utime as time
import st7789py as st7789
from ina219 import INA219
import ch_font_24 as ch_font
import zh_font_24 as zh_font
from umqttrobust import MQTTClient
from machine import Pin, SPI, Timer, I2C, RTC

# ---------- INA219 register addresses ----------
INA219_CONFIG_REG      = 0x00
INA219_SHUNT_VOLTAGE   = 0x01
INA219_BUS_VOLTAGE     = 0x02
INA219_POWER           = 0x03
INA219_CURRENT         = 0x04
INA219_CALIBRATION     = 0x05

# ---------- LCD pinout ----------
PIN_BL   = 13
PIN_DC   = 8
PIN_RST  = 12
PIN_MOSI = 11
PIN_SCK  = 10
PIN_CS   = 9

# ---------- INA219 I²C pins ----------
SDA = 14
SCL = 15

# ---------- Global variables ----------
cur_vol_v   = 0.000      # Real‑time voltage (V)
cur_cur_mA  = 0.000      # Real‑time current (mA)
cur_pwr_W   = 0.000      # Real‑time power   (W)
con_pwr_mWh = 0.000      # Accumulated energy (mWh)
cum_tim_s   = 0          # Accumulated time   (s)
upt_tim_str = ""         # Timestamp string

ntc_flag  = 0            # NTP sync flag
mqtt_flag = 0            # MQTT connection flag
loop_rtc  = 5
loop_mqtt = 5

# ---------- String representations ----------
str_vol_v    = "0.0"
str_vol_ma   = "0"
str_pwr_W    = "0.0"
str_vol_mWh  = "0"
str_tim      = "00:00:00"

# ---------- Background colors ----------
BG_LINE_1 = st7789.color565(106, 255,  42)
BG_LINE_2 = st7789.color565( 80, 191,  32)
BG_LINE_3 = st7789.color565( 30, 129, 232)
BG_LINE_4 = st7789.color565( 22,  95, 172)
BG_LINE_5 = st7789.color565(235, 131, 107)
BG_LINE_6 = st7789.color565(176,  98,  80)

UPD_X = 104  # X‑coordinate for updated numerical values

# ---------- Hardware initialization ----------
print("MicroPython version:")
print(sys.implementation)

tim = Timer(-1)
led = Pin(25, Pin.OUT)
i2c = I2C(1, scl=Pin(SCL), sda=Pin(SDA), freq=400_000)

key1 = Pin(2, Pin.IN, Pin.PULL_UP)
key2 = Pin(1, Pin.IN, Pin.PULL_UP)

tft = st7789.ST7789(
    SPI(1, baudrate=60_000_000, sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI)),
    240, 240,
    reset=Pin(PIN_RST, Pin.OUT),
    cs   =Pin(PIN_CS , Pin.OUT),
    dc   =Pin(PIN_DC , Pin.OUT),
    backlight=Pin(PIN_BL, Pin.OUT),
    rotation=1
)

# ---------- Wi‑Fi ----------
ssid = 'iiPhone'
pswd = '88888888'

rp2.country('CN')
sta_if = network.WLAN(network.STA_IF)
sta_if.active(True)

mac = ubinascii.hexlify(network.WLAN().config('mac'), ':').decode()
print('Pico W MAC address = ' + mac)

# ---------- RTC ----------
rtc = RTC()
rtc.datetime((2024, 1, 1, 0, 0, 0, 0, 0))  # (year, month, day, weekday, hour, minute, second, subseconds)

# ---------- MQTT ----------
mqtt_c = MQTTClient("picow9999", "broker.emqx.io", 1883, "admin", "admin", 60)

# ---------- INA219 ----------
addr_list = i2c.scan()
if len(addr_list) == 1:                      # exactly one I²C device found
    ina = INA219(i2c, addr=0x45)             # A0 = 1, A1 = 1
    ina.set_calibration_32V_1A()             # 32 V / 1 A range

# -------------------------------------------------
# Low‑level register read (for debugging)

def read_ina219_register(register):
    """Read a raw 16‑bit value from a specific INA219 register."""
    try:
        raw = i2c.readfrom_mem(0x45, register, 2)
        return (raw[0] << 8) | raw[1]
    except Exception as e:
        print("Register read error:", e)
        return None


def real_time_register_read():
    """Read INA219 registers in real time and print them over UART."""
    try:
        data = {
            "bus_voltage_raw":   read_ina219_register(INA219_BUS_VOLTAGE),
            "current_raw":       read_ina219_register(INA219_CURRENT),
            "shunt_voltage_raw": read_ina219_register(INA219_SHUNT_VOLTAGE),
            "power_raw":         read_ina219_register(INA219_POWER),
        }
        print("{bus_voltage_raw},{current_raw},{shunt_voltage_raw},{power_raw}".format(**data))
        return data
    except Exception as e:
        print("Register read error:", e)
        return None
# -------------------------------------------------

# Pad integer with leading zeroes

def i2s_l(val, length=2):
    s = str(val)
    return '0' * (length - len(s)) + s


def get_strftime():
    t_s = time.time() + 3600  # UTC+1 (example: adjust as needed)
    y, m, d, hh, mm, ss, *_ = time.localtime(t_s)
    return f"{i2s_l(y,4)}-{i2s_l(m)}-{i2s_l(d)} {i2s_l(hh)}:{i2s_l(mm)}:{i2s_l(ss)}"

# -------- Obtain V, I, P --------

def get_pwr_V_ma():
    """Fetch voltage and current from INA219 and compute real‑time power."""
    global cur_vol_v, cur_cur_mA, cur_pwr_W
    if len(addr_list) == 1:
        cur_vol_v  = ina.bus_voltage               # V
        cur_cur_mA = ina.current * 10              # mA (0.01 Ω shunt)
    if cur_cur_mA < 0:
        cur_cur_mA = 0
    # P(W) = V * I(A)
    cur_pwr_W = (cur_vol_v * cur_cur_mA) / 1000.0  # mA → A


def get_pwr_mWh():
    """Integrate power over 1 s interval → energy (mWh)."""
    global con_pwr_mWh
    con_pwr_mWh += (cur_vol_v * cur_cur_mA) / 3600.0  # mW·s → mWh

# ------------ RTC & MQTT keep‑alive ------------

def check_rtc():
    global loop_rtc
    loop_rtc = (loop_rtc + 1) % 40
    if loop_rtc >= 3 and ntc_flag == 0 and sta_if.isconnected():
        print("Attempting NTP sync…")
        upd_rtc()
        loop_rtc = 0


def check_mqtt_con():
    global loop_mqtt
    loop_mqtt = (loop_mqtt + 1) % 40
    if loop_mqtt >= 3 and mqtt_flag == 0 and sta_if.isconnected():
        print("Attempting MQTT connection…")
        con_mqtt_server()
        loop_mqtt = 0


def mqtt_check_msg():
    if sta_if.isconnected():
        try:
            mqtt_c.publish('str_tim',     str_tim)
            mqtt_c.publish('str_vol_v',   str_vol_v)
            mqtt_c.publish('str_vol_ma',  str_vol_ma)
            mqtt_c.publish('str_pwr_W',   str_pwr_W)
            mqtt_c.publish('str_vol_mWh', str_vol_mWh)
            mqtt_c.publish('upt_tim_str', upt_tim_str)
        except Exception as e:
            print("MQTT publish error:", e)

# ------------ String conversion ------------

def tran_to_str():
    global str_vol_v, str_vol_ma, str_pwr_W, str_vol_mWh, str_tim
    str_vol_v   = "{:.3f}".format(cur_vol_v)
    str_vol_ma  = "{:.1f}".format(cur_cur_mA)
    str_pwr_W   = "{:.3f}".format(cur_pwr_W)
    str_vol_mWh = "{:.0f}".format(con_pwr_mWh)
    str_tim     = sec_to_str(cum_tim_s)


# Seconds → hh:mm:ss

def sec_to_str(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{i2s_l(h)}:{i2s_l(m)}:{i2s_l(s)}"

# ------------------- LCD -------------------

def init_msg():
    # 1. Voltage label
    tft.fill_rect(0, 4, 240, 36, BG_LINE_1)
    tft.write(zh_font, "Voltage", 0, 8, st7789.BLACK, BG_LINE_1)

    # 2. Current label
    tft.fill_rect(0, 44, 240, 36, BG_LINE_2)
    tft.write(zh_font, "Current", 0, 48, st7789.BLACK, BG_LINE_2)

    # 3. Accumulated energy label (mWh)
    tft.fill_rect(0, 84, 240, 36, BG_LINE_3)
    tft.write(zh_font, "Energy", 0, 88, st7789.BLACK, BG_LINE_3)

    # 4. Real‑time power label (W)
    tft.fill_rect(0, 124, 240, 36, BG_LINE_4)
    tft.write(zh_font, "P(W)", 0, 128, st7789.BLACK, BG_LINE_4)

    # 5. Wi‑Fi label
    tft.fill_rect(0, 164, 240, 36, BG_LINE_5)
    tft.write(zh_font, "WiFi", 0, 168, st7789.BLACK, BG_LINE_5)

    # 6. Timestamp label (background only)
    tft.fill_rect(0, 204, 240, 36, BG_LINE_6)


def show_msg():
    # 1. Voltage value
    tft.write(zh_font, str_vol_v + "V   ", UPD_X, 8,  st7789.BLACK, BG_LINE_1)
    # 2. Current value
    tft.write(zh_font, str_vol_ma + "mA  ", UPD_X, 48, st7789.BLACK, BG_LINE_2)
    # 3. Energy value
    tft.write(zh_font, str_vol_mWh + "mWh ", UPD_X, 88, st7789.BLACK, BG_LINE_3)
    # 4. Real‑time power value
    tft.write(zh_font, str_pwr_W + "W   ",  UPD_X, 128, st7789.BLACK, BG_LINE_4)
    # 5. Wi‑Fi state
    wifi_txt = "Connected" if sta_if.isconnected() else "Disconnected"
    tft.write(zh_font, wifi_txt, UPD_X, 168, st7789.BLACK, BG_LINE_5)
    # 6. Timestamp
    tft.write(zh_font, upt_tim_str, 12, 208, st7789.BLACK, BG_LINE_6)

# ------------------- Keys -------------------

def key_scan():
    """KEY1 resets counters; KEY2 tries to reconnect Wi‑Fi."""
    global cum_tim_s, con_pwr_mWh
    if key1.value() == 0:
        cum_tim_s   = 0
        con_pwr_mWh = 0
    if key2.value() == 0:
        con_wifi()

# ------------------- Network -------------------

def con_wifi():
    timeout = 10
    sta_if.connect(ssid, pswd)
    while timeout > 0:
        if sta_if.status() < 0 or sta_if.status() >= 3:
            break
        timeout -= 1
        print('Waiting for Wi‑Fi connection…')
        time.sleep(1)
    print('Wi‑Fi config:', sta_if.ifconfig())


def upd_rtc():
    global ntc_flag
    try:
        ntptime.host = 'ntp1.aliyun.com'
        ntptime.settime()
        ntc_flag = 1
        print("NTP sync succeeded")
    except Exception as e:
        ntc_flag = 0
        print("NTP sync failed:", e)


def con_mqtt_server():
    global mqtt_flag
    try:
        mqtt_c.connect(False)
        mqtt_flag = 1
        print("MQTT connected")
    except Exception as e:
        mqtt_flag = 0
        print("MQTT connection failed:", e)


def mqtt_sub_callback(topic, msg):
    print(topic, msg)

# ------------------- Timer callback -------------------

def timer_fun(tim):
    global cum_tim_s, upt_tim_str
    upt_tim_str = get_strftime()
    get_pwr_V_ma()
    get_pwr_mWh()
    led.toggle()
    check_rtc()
    tran_to_str()
    show_msg()
    check_mqtt_con()
    mqtt_check_msg()
    cum_tim_s += 1

# ------------------- Main -------------------

def main():
    tim.init(period=1000, mode=Timer.PERIODIC, callback=timer_fun)
    mqtt_c.set_callback(mqtt_sub_callback)
    init_msg()
    show_msg()
    con_wifi()

    while True:
        key_scan()
        real_time_register_read()  # Debug read
        time.sleep(1)         # 1 sampling

main()


#!/usr/bin/env python3
"""
epd2in13_v4.py - Pilote autonome pour écran Waveshare 2.13inch e-Paper HAT (V4)
Résolution : 122 x 250 (ou 250 x 122 en mode paysage)
Contrôleur : SSD1680
"""

import time
import logging

EPD_WIDTH = 122
EPD_HEIGHT = 250

# Broches GPIO standard pour Raspberry Pi (BCM)
RST_PIN = 17
DC_PIN = 25
CS_PIN = 8
BUSY_PIN = 24


class EPD2in13_V4:
    def __init__(self, spi_bus=0, spi_dev=0, rst=RST_PIN, dc=DC_PIN, cs=CS_PIN, busy=BUSY_PIN):
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT
        self.rst_pin = rst
        self.dc_pin = dc
        self.cs_pin = cs
        self.busy_pin = busy
        self.spi_bus = spi_bus
        self.spi_dev = spi_dev
        self.spi = None
        self.gpio = None
        self._is_initialized = False

    def _init_hardware(self):
        """Initialise la liaison SPI et les broches GPIO via gpiozero (Bookworm)."""
        try:
            import spidev
            self.spi = spidev.SpiDev()
            self.spi.open(self.spi_bus, self.spi_dev)
            self.spi.max_speed_hz = 4000000
            self.spi.mode = 0b00
        except Exception as e:
            logging.warning("Liaison SPI non disponible : %s", e)
            self.spi = None

        self._rst = None
        self._dc = None
        self._busy = None

        # Broches manuelles requises : RST (17), DC (25), BUSY (24)
        # Note : CS (GPIO 8 / CE0) est piloté matériellement par spidev0.0
        try:
            from gpiozero import OutputDevice, InputDevice
            self._rst = OutputDevice(self.rst_pin, active_high=True, initial_value=True)
            self._dc = OutputDevice(self.dc_pin, active_high=True, initial_value=False)
            self._busy = InputDevice(self.busy_pin)
            self.gpio = "gpiozero"
        except Exception as e:
            try:
                import RPi.GPIO as GPIO
                self.gpio = GPIO
                self.gpio.setmode(GPIO.BCM)
                self.gpio.setwarnings(False)
                self.gpio.setup(self.rst_pin, GPIO.OUT)
                self.gpio.setup(self.dc_pin, GPIO.OUT)
                self.gpio.setup(self.busy_pin, GPIO.IN)
            except Exception as e2:
                logging.warning("GPIO non disponible : %s", e2)
                self.gpio = None

    def digital_write(self, pin, value):
        if self._rst and pin == self.rst_pin:
            self._rst.value = bool(value)
        elif self._dc and pin == self.dc_pin:
            self._dc.value = bool(value)
        elif self.gpio and self.gpio != "gpiozero":
            self.gpio.output(pin, value)

    def digital_read(self, pin):
        if self._busy and pin == self.busy_pin:
            return 1 if self._busy.value else 0
        elif self.gpio and self.gpio != "gpiozero":
            return self.gpio.input(pin)
        return 0

    def spi_writebyte(self, data):
        if self.spi:
            self.spi.writebytes(data if isinstance(data, list) else [data])

    def send_command(self, command):
        self.digital_write(self.dc_pin, 0)
        self.spi_writebyte(command)

    def send_data(self, data):
        self.digital_write(self.dc_pin, 1)
        self.spi_writebyte(data)

    def wait_until_idle(self):
        """Attend que l'écran ait fini son rafraîchissement (BUSY=0)."""
        if not self.gpio:
            return
        # Sur V4 : BUSY = 1 lorsque l'écran travaille, 0 au repos
        while self.digital_read(self.busy_pin) == 1:
            time.sleep(0.01)

    def reset(self):
        if not self.gpio:
            return
        self.digital_write(self.rst_pin, 1)
        time.sleep(0.02)
        self.digital_write(self.rst_pin, 0)
        time.sleep(0.01)
        self.digital_write(self.rst_pin, 1)
        time.sleep(0.02)

    def init(self, fast_mode=False):
        """Initialisation de l'écran V4."""
        if not self._is_initialized:
            self._init_hardware()
            self._is_initialized = True

        self.reset()
        self.wait_until_idle()

        # SWRESET
        self.send_command(0x12)
        self.wait_until_idle()

        # Driver Output control (250 MUX)
        self.send_command(0x01)
        self.send_data(0xF9)
        self.send_data(0x00)
        self.send_data(0x00)

        # Data Entry mode (X increment, Y increment)
        self.send_command(0x11)
        self.send_data(0x03)

        # Set RAM X Address Start/End
        self.send_command(0x44)
        self.send_data(0x00)
        self.send_data(0x0F)  # 16 octets * 8 = 128 colonnes (122 utiles)

        # Set RAM Y Address Start/End (0 à 249)
        self.send_command(0x45)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0xF9)
        self.send_data(0x00)

        # Border Waveform Control
        self.send_command(0x3C)
        self.send_data(0x05)

        # Température interne
        self.send_command(0x18)
        self.send_data(0x80)

        # Position initiale du curseur
        self.set_cursor(0, 0)
        self.wait_until_idle()
        return 0

    def set_cursor(self, x, y):
        self.send_command(0x4E)
        self.send_data(x & 0x1F)
        self.send_command(0x4F)
        self.send_data(y & 0xFF)
        self.send_data((y >> 8) & 0x01)

    def get_buffer(self, image):
        """Convertit une image Pillow monochrome (1-bit) en tampon d'octets pour l'e-Paper."""
        # Adapter l'orientation de l'image (l'écran natif est en 122x250 portrait)
        imwidth, imheight = image.size
        if imwidth == self.width and imheight == self.height:
            img = image
        elif imwidth == self.height and imheight == self.width:
            # Mode paysage (250x122) -> rotation 90°
            img = image.rotate(90, expand=True)
        else:
            img = image.resize((self.width, self.height))

        # Convertir en mode 1 bit noir et blanc
        img = img.convert("1")
        buf = bytearray(self.height * ((self.width + 7) // 8))
        img_bytes = img.tobytes()

        # Sur l'e-Paper SSD1680, 0 = Noir, 1 = Blanc
        for i in range(len(img_bytes)):
            buf[i] = img_bytes[i]

        return list(buf)

    def display(self, image_buffer):
        """Rafraîchissement complet de l'écran."""
        self.set_cursor(0, 0)
        self.send_command(0x24)  # Write Black/White RAM
        self.send_data(image_buffer)

        # Display Update Control (Full Refresh)
        self.send_command(0x22)
        self.send_data(0xF7)
        self.send_command(0x20)  # Master Activation
        self.wait_until_idle()

    def display_partial(self, image_buffer):
        """Rafraîchissement partiel rapide sans scintillement."""
        self.set_cursor(0, 0)
        self.send_command(0x24)
        self.send_data(image_buffer)

        # Display Update Control (Partial Refresh)
        self.send_command(0x22)
        self.send_data(0xFF)
        self.send_command(0x20)
        self.wait_until_idle()

    def clear(self, color=0xFF):
        """Efface l'écran."""
        buf = [color] * (self.height * ((self.width + 7) // 8))
        self.display(buf)

    def sleep(self):
        """Met l'écran en veille profonde (consommation < 5 µA). L'image reste intacte."""
        self.send_command(0x10)
        self.send_data(0x01)
        time.sleep(0.1)

    def close(self):
        if self.spi:
            try:
                self.spi.close()
            except Exception:
                pass
            self.spi = None
        if self.gpio:
            try:
                self.gpio.cleanup()
            except Exception:
                pass
            self.gpio = None

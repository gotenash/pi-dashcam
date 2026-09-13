#!/usr/bin/env python3
"""
display_epaper.py - Démon d'affichage dynamique pour Waveshare 2.13inch e-Paper HAT V4
Génère l'affichage en mode paysage 250 x 122 pixels avec Pillow.
Conserve l'état affiché même lorsque le Raspberry Pi est hors tension.
"""

import os
import sys
import time
import signal
import shutil
import logging
import subprocess
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# Importer les pilotes et fonctions du projet
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from epd2in13_v4 import EPD2in13_V4
try:
    from power_monitor import MAX17040, PowerInputDetector, load_config
except ImportError:
    def load_config():
        return {
            "EPAPER_ENABLED": 1,
            "EPAPER_REFRESH_SEC": 20,
            "EPAPER_FULL_REFRESH_INTERVAL": 20,
            "STORAGE_DIR": "/var/media/dashcam",
            "HOTSPOT_SSID": "Pi-Dashcam",
            "HOTSPOT_IP": "10.42.0.1",
            "UPS_I2C_BUS": 1,
            "UPS_I2C_ADDR": 0x36,
            "POWER_DETECT_PIN": 4,
        }
    MAX17040 = None
    PowerInputDetector = None

WIDTH = 250
HEIGHT = 122


def get_font(size=12, bold=False):
    """Charge une police TrueType système ou la police par défaut."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf"
    ]
    for p in font_paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


class EPaperDashboard:
    def __init__(self):
        self.config = load_config()
        self.epd = EPD2in13_V4()
        self.running = True
        self.full_refresh_counter = 0

        # Capteur UPS I2C uniquement (pas de conflit GPIO)
        self.ups = None
        if MAX17040:
            try:
                self.ups = MAX17040(
                    bus_num=self.config.get("UPS_I2C_BUS", 1),
                    address=self.config.get("UPS_I2C_ADDR", 0x36)
                )
            except Exception:
                self.ups = None

        # Polices
        self.font_large = get_font(16, bold=True)
        self.font_med = get_font(12, bold=True)
        self.font_sm = get_font(10, bold=False)

    def is_dashcam_recording(self) -> bool:
        """Vérifie si le service dashcam est en cours d'enregistrement."""
        try:
            res = subprocess.run(
                ["systemctl", "is-active", "dashcam.service"],
                capture_output=True,
                text=True,
                timeout=2
            )
            return res.stdout.strip() == "active"
        except Exception:
            return False

    def get_cpu_temp(self) -> float:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                return round(float(f.read().strip()) / 1000.0, 1)
        except Exception:
            return 40.0

    def get_ip_address(self) -> str:
        """Récupère l'adresse IP active du Pi."""
        try:
            res = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                timeout=2
            )
            ips = res.stdout.strip().split()
            if ips:
                return ips[0]
        except Exception:
            pass
        return self.config.get("HOTSPOT_IP", "10.42.0.1")

    def get_disk_info(self):
        storage_dir = self.config.get("STORAGE_DIR", "/var/media/dashcam")
        try:
            total, used, free = shutil.disk_usage(storage_dir)
            free_gb = round(free / (1024 ** 3), 1)
            percent = int((used / total) * 100) if total > 0 else 0
            return free_gb, percent
        except Exception:
            return 24.0, 10

    def get_battery_info(self):
        v, pct = 4.10, 95.0
        if self.ups:
            try:
                v, pct = self.ups.read_status()
            except Exception:
                pass
        ext = bool(v >= 4.05 or pct >= 95.0)
        return round(v, 2), round(pct, 0), ext

    def render_dashboard(self) -> Image.Image:
        """Génère l'image 250x122 monochrome pour le tableau de bord actif."""
        image = Image.new("1", (WIDTH, HEIGHT), 255)  # 255 = Fond blanc
        draw = ImageDraw.Draw(image)

        is_rec = self.is_dashcam_recording()
        voltage, percent, ext_power = self.get_battery_info()
        free_gb, disk_pct = self.get_disk_info()
        cpu_temp = self.get_cpu_temp()
        ip_addr = self.get_ip_address()
        now_str = datetime.now().strftime("%H:%M")

        # --- BANDEAU SUPÉRIEUR ---
        if is_rec:
            # Badge REC inversé noir
            draw.rounded_rectangle([(4, 4), (88, 24)], radius=4, fill=0)
            draw.text((10, 6), "● REC", font=self.font_med, fill=255)
        else:
            draw.rounded_rectangle([(4, 4), (88, 24)], radius=4, outline=0, width=1)
            draw.text((10, 6), "⏸ PAUSE", font=self.font_med, fill=0)

        # Heure & Nom
        draw.text((100, 6), "PI-DASHCAM", font=self.font_med, fill=0)
        draw.text((205, 6), now_str, font=self.font_med, fill=0)

        # Ligne de séparation
        draw.line([(4, 28), (246, 28)], fill=0, width=1)

        # --- LIGNE 1 : RÉSEAU & ALIMENTATION ---
        # Wi-Fi / IP
        draw.text((6, 34), f"WiFi : {ip_addr}", font=self.font_med, fill=0)

        # Batterie
        power_icon = "⚡" if ext_power else "🔋"
        bat_str = f"{power_icon} {int(percent)}% ({voltage}V)"
        draw.text((140, 34), bat_str, font=self.font_med, fill=0)

        # Ligne de séparation discrète
        draw.line([(4, 56), (246, 56)], fill=0, width=1)

        # --- LIGNE 2 : ESPACE DISQUE ---
        draw.text((6, 62), f"MicroSD : {free_gb}Go libres ({disk_pct}%)", font=self.font_med, fill=0)

        # Jauge horizontale d'espace disque
        draw.rectangle([(6, 80), (244, 90)], outline=0, width=1)
        fill_width = int(6 + ((244 - 6) * (disk_pct / 100.0)))
        if fill_width > 6:
            draw.rectangle([(6, 80), (fill_width, 90)], fill=0)

        # --- BANDEAU INFÉRIEUR ---
        draw.line([(4, 96), (246, 96)], fill=0, width=1)
        draw.text((6, 102), f"Temp CPU : {cpu_temp}°C", font=self.font_sm, fill=0)
        draw.text((150, 102), "1080p H.264 30fps", font=self.font_sm, fill=0)

        return image

    def render_shutdown(self) -> Image.Image:
        """Génère l'écran final persistant lors de l'arrêt complet de la machine."""
        image = Image.new("1", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)

        # Cadre double élégant
        draw.rectangle([(2, 2), (247, 119)], outline=0, width=2)
        draw.rectangle([(5, 5), (244, 116)], outline=0, width=1)

        # Titre
        draw.rounded_rectangle([(20, 15), (230, 42)], radius=4, fill=0)
        draw.text((38, 20), "PI-DASHCAM ÉTEINT", font=self.font_large, fill=255)

        # Messages
        draw.text((35, 52), "Coupure de contact détectée", font=self.font_med, fill=0)
        draw.text((25, 70), "Dernier enregistrement sauvegardé", font=self.font_sm, fill=0)

        _, percent, _ = self.get_battery_info()
        draw.text((45, 92), f"Batterie restante : {int(percent)}%", font=self.font_med, fill=0)

        return image

    def handle_signal(self, signum, frame):
        logging.info("Signal reçu (%s), affichage de l'écran d'extinction...", signum)
        self.running = False
        try:
            img = self.render_shutdown()
            buf = self.epd.get_buffer(img)
            self.epd.init()
            self.epd.display(buf)
            self.epd.sleep()
            logging.info("Écran d'extinction affiché avec succès.")
        except Exception as e:
            logging.error("Erreur affichage écran d'extinction : %s", e)
        sys.exit(0)

    def run(self):
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        logging.info("Démarrage du service d'affichage Waveshare 2.13 e-Paper V4...")
        try:
            self.epd.init()
            self.epd.clear()
        except Exception as e:
            logging.warning("Écran non disponible à l'initialisation : %s", e)

        refresh_delay = int(self.config.get("EPAPER_REFRESH_SEC", 20))
        full_interval = int(self.config.get("EPAPER_FULL_REFRESH_INTERVAL", 20))

        while self.running:
            try:
                img = self.render_dashboard()
                buf = self.epd.get_buffer(img)

                # Rafraîchissement complet périodique anti-ghosting, sinon partiel
                self.full_refresh_counter += 1
                if self.full_refresh_counter >= full_interval:
                    self.epd.init()
                    self.epd.display(buf)
                    self.full_refresh_counter = 0
                else:
                    self.epd.display_partial(buf)

            except Exception as e:
                logging.error("Erreur lors du rafraîchissement e-Paper : %s", e)

            # Attente avant prochain cycle
            for _ in range(refresh_delay):
                if not self.running:
                    break
                time.sleep(1)

        self.epd.sleep()
        self.epd.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [epaper] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    dashboard = EPaperDashboard()
    dashboard.run()


if __name__ == "__main__":
    main()

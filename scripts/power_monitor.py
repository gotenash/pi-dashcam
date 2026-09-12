#!/usr/bin/env python3
"""
power_monitor.py - Surveillance d'alimentation et gestion d'extinction sécurisée.
Conçu pour Raspberry Pi Zero / Zero 2 W avec module UPS-Lite V1.2 (MAX17040G).
"""

import sys
import os
import time
import signal
import logging
import subprocess
import argparse
from typing import Dict, Any, Tuple, Optional

# Configuration par défaut
DEFAULT_CONFIG_PATHS = [
    "/etc/pi-dashcam/dashcam.conf",
    os.path.join(os.path.dirname(__file__), "..", "config", "dashcam.conf"),
]

DEFAULTS = {
    "UPS_I2C_BUS": 1,
    "UPS_I2C_ADDR": 0x36,
    "POWER_DETECT_PIN": 4,
    "SHUTDOWN_DELAY_SEC": 30,
    "CRITICAL_BATTERY_PERCENT": 10.0,
    "CRITICAL_BATTERY_VOLTAGE": 3.40,
    "LOG_LEVEL": "INFO",
}


def load_config() -> Dict[str, Any]:
    """Charge la configuration depuis dashcam.conf avec fallback sur les valeurs par défaut."""
    config = dict(DEFAULTS)
    loaded_file = None

    for path in DEFAULT_CONFIG_PATHS:
        if os.path.exists(path):
            loaded_file = path
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, val = line.split("=", 1)
                            key = key.strip()
                            val = val.strip().strip('"').strip("'")
                            int_keys = (
                                "UPS_I2C_BUS", "POWER_DETECT_PIN", "SHUTDOWN_DELAY_SEC",
                                "SEGMENT_DURATION_SEC", "VIDEO_WIDTH", "VIDEO_HEIGHT", "VIDEO_FPS",
                                "VIDEO_BITRATE", "MAX_DISK_USAGE_PERCENT", "MIN_FREE_SPACE_MB", "WEB_PORT"
                            )
                            float_keys = ("CRITICAL_BATTERY_PERCENT", "CRITICAL_BATTERY_VOLTAGE")

                            if key in int_keys:
                                try:
                                    config[key] = int(val)
                                except ValueError:
                                    config[key] = val
                            elif key in float_keys:
                                try:
                                    config[key] = float(val)
                                except ValueError:
                                    config[key] = val
                            elif key == "UPS_I2C_ADDR":
                                config[key] = int(val, 16) if val.startswith("0x") else int(val)
                            elif key == "LOG_LEVEL":
                                config[key] = val.upper()
                            else:
                                config[key] = val
                break
            except Exception as e:
                print(f"Avertissement : Erreur de lecture de {path}: {e}", file=sys.stderr)

    return config


class MAX17040:
    """
    Pilote pour la jauge de batterie MAX17040 / MAX17040G (I2C adresse par défaut 0x36).
    """
    REG_VCELL = 0x02
    REG_SOC = 0x04
    REG_MODE = 0x06
    REG_CONFIG = 0x0C
    REG_COMMAND = 0xFE

    def __init__(self, bus_num: int = 1, address: int = 0x36):
        self.bus_num = bus_num
        self.address = address
        self._bus = None

    def connect(self):
        try:
            from smbus2 import SMBus
            self._bus = SMBus(self.bus_num)
        except ImportError:
            try:
                from smbus import SMBus
                self._bus = SMBus(self.bus_num)
            except ImportError:
                raise RuntimeError(
                    "Aucun module smbus disponible. Installez python3-smbus2 ou python3-smbus."
                )

    def close(self):
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None

    def read_voltage(self) -> float:
        """Retourne la tension de la cellule LiPo en Volts (résolution 1.25 mV)."""
        if self._bus is None:
            self.connect()
        # Lecture de 2 octets en mode bloc
        data = self._bus.read_i2c_block_data(self.address, self.REG_VCELL, 2)
        raw = (data[0] << 4) | (data[1] >> 4)
        return raw * 0.00125

    def read_percentage(self) -> float:
        """Retourne l'état de charge (SOC) en pourcentage (0.0% à 100.0%)."""
        if self._bus is None:
            self.connect()
        data = self._bus.read_i2c_block_data(self.address, self.REG_SOC, 2)
        percent = data[0] + (data[1] / 256.0)
        return max(0.0, min(100.0, percent))

    def read_status(self) -> Tuple[float, float]:
        """Retourne un tuple (tension_V, pourcentage_SOC)."""
        return self.read_voltage(), self.read_percentage()


class PowerInputDetector:
    """
    Gestionnaire pour la détection de présence d'alimentation externe (GPIO 4 sur UPS-Lite V1.2).
    HIGH (1) = Alimentation externe USB connectée.
    LOW (0)  = Déconnecté, fonctionnement sur batterie.
    """
    def __init__(self, pin: int = 4):
        self.pin = pin
        self._device = None
        self._setup()

    def _setup(self):
        try:
            from gpiozero import DigitalInputDevice
            # UPS-Lite V1.2 tire la broche vers le haut quand l'alimentation est présente
            self._device = DigitalInputDevice(self.pin, pull_up=False)
        except Exception as e:
            logging.warning(
                "Impossible d'initialiser gpiozero sur GPIO %d: %s. Fallback RPi.GPIO...",
                self.pin, e
            )
            try:
                import RPi.GPIO as GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.pin, GPIO.IN)
                self._device = "RPI_GPIO"
            except Exception as e2:
                logging.error("Échec d'initialisation GPIO: %s", e2)
                self._device = None

    def is_external_power_connected(self) -> Optional[bool]:
        """Retourne True si alimenté par USB externe, False sur batterie, None si indéterminé."""
        if self._device is None:
            return None
        if self._device == "RPI_GPIO":
            import RPi.GPIO as GPIO
            return bool(GPIO.input(self.pin) == 1)
        # via gpiozero DigitalInputDevice
        return bool(self._device.value == 1)


class PowerMonitorDaemon:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.running = True
        self.ups = MAX17040(
            bus_num=config["UPS_I2C_BUS"],
            address=config["UPS_I2C_ADDR"]
        )
        self.power_detector = PowerInputDetector(pin=config["POWER_DETECT_PIN"])
        self.shutdown_in_progress = False

    def handle_signal(self, signum, frame):
        logging.info("Signal reçu (%s), arrêt du démon power_monitor...", signum)
        self.running = False

    def initiate_safe_shutdown(self, reason: str):
        """Procédure d'extinction ordonnée et sécurisée."""
        if self.shutdown_in_progress:
            return
        self.shutdown_in_progress = True

        logging.warning("INITIATION DE L'EXTINCTION PROPRE DU SYSTÈME. Raison : %s", reason)

        # 1. Arrêter le service dashcam pour clore proprement la vidéo en cours
        logging.info("Arrêt du service d'enregistrement dashcam...")
        try:
            subprocess.run(
                ["systemctl", "stop", "dashcam.service"],
                check=False,
                timeout=15
            )
        except Exception as e:
            logging.error("Erreur lors de l'arrêt de dashcam.service: %s", e)

        # 2. Forcer la synchronisation des tampons disque vers la carte SD
        logging.info("Synchronisation des disques (sync)...")
        try:
            os.sync()
        except Exception as e:
            logging.error("Erreur lors de sync: %s", e)

        # 3. Éteindre le système
        logging.info("Arrêt de la machine (poweroff)...")
        try:
            subprocess.run(["shutdown", "-h", "now"], check=False)
        except Exception as e:
            logging.error("Erreur lors de l'appel à shutdown: %s", e)

    def run(self):
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        logging.info("Démarrage du démon power_monitor (UPS-Lite V1.2)...")
        logging.info(
            "Config: I2C Bus %d, Addr 0x%02X, GPIO Détection %d, Délai extinction: %ds, Seuil critique: %.1f%% / %.2fV",
            self.config["UPS_I2C_BUS"],
            self.config["UPS_I2C_ADDR"],
            self.config["POWER_DETECT_PIN"],
            self.config["SHUTDOWN_DELAY_SEC"],
            self.config["CRITICAL_BATTERY_PERCENT"],
            self.config["CRITICAL_BATTERY_VOLTAGE"]
        )

        countdown_start: Optional[float] = None
        last_log_time = 0.0

        while self.running and not self.shutdown_in_progress:
            try:
                # Lecture tension et jauge
                try:
                    voltage, percent = self.ups.read_status()
                except Exception as e:
                    logging.warning("Erreur lecture I2C MAX17040: %s", e)
                    voltage, percent = 3.8, 50.0

                # Détection alimentation externe
                ext_power = self.power_detector.is_external_power_connected()
                now = time.time()

                # Log d'état périodique (toutes les 60 secondes si stable)
                if now - last_log_time >= 60.0:
                    status_str = "USB ALIMENTÉ" if ext_power else "SUR BATTERIE"
                    logging.info(
                        "Statut: %s | Batterie: %.1f%% | Tension: %.2fV",
                        status_str, percent, voltage
                    )
                    last_log_time = now

                # 1. Vérification seuil d'urgence absolu (protection LiPo)
                if (percent <= self.config["CRITICAL_BATTERY_PERCENT"] or 
                    voltage <= self.config["CRITICAL_BATTERY_VOLTAGE"]):
                    self.initiate_safe_shutdown(
                        f"Batterie critique (SOC={percent:.1f}%, U={voltage:.2f}V)"
                    )
                    break

                # 2. Gestion de la perte d'alimentation externe
                if ext_power is False:
                    if countdown_start is None:
                        countdown_start = now
                        logging.warning(
                            "Alimentation externe coupée ! Compte à rebours avant extinction : %d secondes.",
                            self.config["SHUTDOWN_DELAY_SEC"]
                        )

                    elapsed = now - countdown_start
                    remaining = self.config["SHUTDOWN_DELAY_SEC"] - elapsed

                    if remaining <= 0:
                        self.initiate_safe_shutdown(
                            f"Fin du compte à rebours d'extinction ({self.config['SHUTDOWN_DELAY_SEC']}s après coupure contact)"
                        )
                        break
                    else:
                        # Log du décompte toutes les 5 secondes
                        if int(elapsed) % 5 == 0:
                            logging.warning(
                                "Extinction programmée dans %d secondes (Batterie: %.1f%%, %.2fV)...",
                                int(remaining), percent, voltage
                            )
                else:
                    # Alimentation externe présente ou rétablie
                    if countdown_start is not None:
                        logging.info("Alimentation externe rétablie. Annulation du compte à rebours d'extinction.")
                        countdown_start = None

            except Exception as e:
                logging.error("Erreur inattendue dans la boucle de surveillance: %s", e)

            time.sleep(1.0)

        self.ups.close()
        logging.info("Démon power_monitor terminé.")


def print_status_and_exit(config: Dict[str, Any]):
    """Affiche l'état courant de l'alimentation et de la batterie puis quitte."""
    print("=== Diagnostic Alimentation & Batterie (UPS-Lite V1.2) ===")
    ups = MAX17040(bus_num=config["UPS_I2C_BUS"], address=config["UPS_I2C_ADDR"])
    detector = PowerInputDetector(pin=config["POWER_DETECT_PIN"])

    try:
        voltage, percent = ups.read_status()
        print(f"Jauge MAX17040 (0x{config['UPS_I2C_ADDR']:02X}) :")
        print(f"  - Tension batterie : {voltage:.3f} V")
        print(f"  - Charge restante  : {percent:.1f} %")
    except Exception as e:
        print(f"  - Erreur de communication I2C: {e}")
    finally:
        ups.close()

    ext_power = detector.is_external_power_connected()
    if ext_power is True:
        power_str = "CONNECTÉE (Alimentation USB active)"
    elif ext_power is False:
        power_str = "DÉCONNECTÉE (Fonctionnement sur batterie)"
    else:
        power_str = "INDÉTERMINÉ (Vérifier GPIO 4 ou permissions)"
    print(f"Alimentation externe (GPIO {config['POWER_DETECT_PIN']}) : {power_str}")


def main():
    parser = argparse.ArgumentParser(description="Moniteur d'alimentation Pi-Dashcam (UPS-Lite V1.2)")
    parser.add_argument(
        "--status", action="store_true", help="Affiche la tension, le pourcentage et l'état d'alimentation puis quitte."
    )
    args = parser.parse_args()

    config = load_config()

    if args.status:
        print_status_and_exit(config)
        return

    log_level = getattr(logging, config.get("LOG_LEVEL", "INFO"), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] [%(levelname)s] [power_monitor] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    daemon = PowerMonitorDaemon(config)
    daemon.run()


if __name__ == "__main__":
    main()

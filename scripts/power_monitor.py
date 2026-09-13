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
    "POWER_DETECT_ACTIVE_LOW": 0,
    "ENABLE_AUTO_SHUTDOWN": 0,
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
                                "UPS_I2C_BUS", "POWER_DETECT_PIN", "POWER_DETECT_ACTIVE_LOW", "SHUTDOWN_DELAY_SEC",
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
    Sur UPS-Lite V1.2 (avec les 2 pads pontés à l'étain au dos de la carte) :
    HIGH (1) = Alimentation externe USB 5V connectée.
    LOW (0)  = Déconnecté, fonctionnement sur batterie LiPo.
    """
    def __init__(self, pin: int = 4, active_low: bool = False):
        self.pin = pin
        self.active_low = active_low
        self._method = None
        self._h = None
        self._line = None
        self._setup()

    def _setup(self):
        # 1. Tenter lgpio (standard officiel Bookworm)
        try:
            import lgpio
            self._h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(self._h, self.pin)
            self._method = "lgpio"
            return
        except Exception:
            pass

        # 2. Tenter gpiod
        try:
            import gpiod
            chip = gpiod.Chip('gpiochip0')
            self._line = chip.get_line(self.pin)
            self._line.request(consumer="power_detect", type=gpiod.LINE_REQ_DIR_IN)
            self._method = "gpiod"
            return
        except Exception:
            pass

        # 3. Fallback pinctrl natif Bookworm
        import shutil
        if shutil.which("pinctrl"):
            self._method = "pinctrl"
            return

        # 4. Fallback RPi.GPIO
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.IN)
            self._method = "rpi_gpio"
            return
        except Exception:
            pass

    def read_pin(self) -> Optional[int]:
        """Lit l'état logique de la broche GPIO (0 ou 1)."""
        if self._method == "lgpio" and self._h is not None:
            try:
                import lgpio
                return int(lgpio.gpio_read(self._h, self.pin))
            except Exception:
                pass

        if self._method == "gpiod" and self._line is not None:
            try:
                return int(self._line.get_value())
            except Exception:
                pass

        if self._method == "rpi_gpio":
            try:
                import RPi.GPIO as GPIO
                return int(GPIO.input(self.pin))
            except Exception:
                pass

        # Lecture universelle via pinctrl (binaire natif Bookworm)
        try:
            res = subprocess.run(
                ["pinctrl", "get", str(self.pin)],
                capture_output=True,
                text=True,
                timeout=1
            )
            out = res.stdout.lower()
            if "| hi" in out or " hi " in out:
                return 1
            elif "| lo" in out or " lo " in out:
                return 0
        except Exception:
            pass

        return None

    def is_external_power_connected(self) -> Optional[bool]:
        """
        Retourne True si alimenté par USB 5V, False sur batterie, None si indéterminé.
        """
        val = self.read_pin()
        if val is None:
            return None
        if self.active_low:
            return bool(val == 0)
        return bool(val == 1)


class PowerMonitorDaemon:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.running = True
        self.ups = MAX17040(
            bus_num=config["UPS_I2C_BUS"],
            address=config["UPS_I2C_ADDR"]
        )
        self.power_detector = PowerInputDetector(
            pin=config["POWER_DETECT_PIN"],
            active_low=bool(config.get("POWER_DETECT_ACTIVE_LOW", 1))
        )
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

                # Détection alimentation externe (avec fallback intelligent I2C pour UPS-Lite V1.2)
                ext_power = self.power_detector.is_external_power_connected()
                if ext_power is None:
                    ext_power = bool(voltage >= 4.02 or percent >= 90.0)
                now = time.time()

                # Log d'état périodique (toutes les 60 secondes si stable)
                if now - last_log_time >= 60.0:
                    status_str = "USB ALIMENTÉ" if ext_power else "SUR BATTERIE"
                    logging.info(
                        "Statut: %s | Batterie: %.1f%% | Tension: %.2fV",
                        status_str, percent, voltage
                    )
                    last_log_time = now

                # Vérification si l'extinction automatique est activée
                auto_shutdown_enabled = bool(self.config.get("ENABLE_AUTO_SHUTDOWN", 0))

                # 1. Vérification seuil d'urgence absolu (protection LiPo)
                if (percent <= self.config["CRITICAL_BATTERY_PERCENT"] or 
                    voltage <= self.config["CRITICAL_BATTERY_VOLTAGE"]):
                    if auto_shutdown_enabled:
                        self.initiate_safe_shutdown(
                            f"Batterie critique (SOC={percent:.1f}%, U={voltage:.2f}V)"
                        )
                        break
                    else:
                        logging.warning("Batterie critique mais extinction automatique désactivée (ENABLE_AUTO_SHUTDOWN=0).")

                # 2. Gestion de la perte d'alimentation externe
                if ext_power is False:
                    if countdown_start is None:
                        countdown_start = now
                        if auto_shutdown_enabled:
                            logging.warning(
                                "Alimentation externe coupée ! Compte à rebours avant extinction : %d secondes.",
                                self.config["SHUTDOWN_DELAY_SEC"]
                            )
                        else:
                            logging.info("Alimentation externe coupée (extinction désactivée, fonctionnement continu).")

                    elapsed = now - countdown_start
                    remaining = self.config["SHUTDOWN_DELAY_SEC"] - elapsed

                    if auto_shutdown_enabled and remaining <= 0:
                        self.initiate_safe_shutdown(
                            f"Fin du compte à rebours d'extinction ({self.config['SHUTDOWN_DELAY_SEC']}s après coupure contact)"
                        )
                        break
                    elif auto_shutdown_enabled:
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

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
    "ENABLE_AUTO_SHUTDOWN": 0,
    "PARKING_SHUTDOWN_BATTERY_PERCENT": 85.0,
    "PARKING_MAX_DURATION_SEC": 600,
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
                                "UPS_I2C_BUS", "PARKING_MAX_DURATION_SEC", "SHUTDOWN_DELAY_SEC",
                                "SEGMENT_DURATION_SEC", "VIDEO_WIDTH", "VIDEO_HEIGHT", "VIDEO_FPS",
                                "VIDEO_BITRATE", "MAX_DISK_USAGE_PERCENT", "MIN_FREE_SPACE_MB", "WEB_PORT"
                            )
                            float_keys = (
                                "PARKING_SHUTDOWN_BATTERY_PERCENT",
                                "CRITICAL_BATTERY_PERCENT",
                                "CRITICAL_BATTERY_VOLTAGE"
                            )

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


class PowerMonitorDaemon:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.running = True
        self.ups = MAX17040(
            bus_num=config["UPS_I2C_BUS"],
            address=config["UPS_I2C_ADDR"]
        )
        self.shutdown_in_progress = False
        self.discharge_start_time: Optional[float] = None
        self.last_percent: Optional[float] = None

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

        logging.info("Démarrage du démon power_monitor (I2C MAX17040G)...")
        logging.info(
            "Config: I2C Bus %d, Addr 0x%02X, Seuil extinction parking: %.1f%%, Durée max parking: %ds, Seuil critique: %.1f%% / %.2fV",
            self.config["UPS_I2C_BUS"],
            self.config["UPS_I2C_ADDR"],
            self.config.get("PARKING_SHUTDOWN_BATTERY_PERCENT", 85.0),
            self.config.get("PARKING_MAX_DURATION_SEC", 600),
            self.config["CRITICAL_BATTERY_PERCENT"],
            self.config["CRITICAL_BATTERY_VOLTAGE"]
        )

        last_log_time = 0.0

        while self.running and not self.shutdown_in_progress:
            try:
                # Lecture tension et jauge
                try:
                    voltage, percent = self.ups.read_status()
                except Exception as e:
                    logging.warning("Erreur lecture I2C MAX17040: %s", e)
                    voltage, percent = 3.8, 50.0

                now = time.time()
                parking_threshold = float(self.config.get("PARKING_SHUTDOWN_BATTERY_PERCENT", 85.0))
                max_parking_duration = int(self.config.get("PARKING_MAX_DURATION_SEC", 600))
                auto_shutdown_enabled = bool(self.config.get("ENABLE_AUTO_SHUTDOWN", 0))

                # Détection alimentation vs décharge (basée sur tension et niveau)
                is_charging_or_full = bool(voltage >= 4.08 and percent >= 92.0)

                # Log d'état périodique (toutes les 60 secondes si stable)
                if now - last_log_time >= 60.0:
                    status_str = "USB ALIMENTÉ (Plein/Charge)" if is_charging_or_full else "SUR BATTERIE (Décharge)"
                    logging.info(
                        "Statut: %s | Batterie: %.1f%% | Tension: %.2fV",
                        status_str, percent, voltage
                    )
                    last_log_time = now

                # 1. Vérification seuil d'urgence absolu (protection physique de la cellule LiPo)
                if (percent <= self.config["CRITICAL_BATTERY_PERCENT"] or 
                    voltage <= self.config["CRITICAL_BATTERY_VOLTAGE"]):
                    if auto_shutdown_enabled:
                        self.initiate_safe_shutdown(
                            f"Batterie critique (SOC={percent:.1f}%, U={voltage:.2f}V)"
                        )
                        break
                    else:
                        logging.warning("Batterie critique mais extinction automatique désactivée (ENABLE_AUTO_SHUTDOWN=0).")

                # 2. Gestion du mode Parking / Décharge en voiture
                if not is_charging_or_full:
                    if self.discharge_start_time is None:
                        self.discharge_start_time = now
                        logging.info("Passage sur batterie LiPo détecté.")

                    elapsed_discharge = now - self.discharge_start_time

                    if auto_shutdown_enabled:
                        # Déclenchement si la batterie descend sous le seuil de parking (ex: 85%)
                        if percent <= parking_threshold:
                            self.initiate_safe_shutdown(
                                f"Seuil de batterie parking atteint ({percent:.1f}% <= {parking_threshold:.1f}%)"
                            )
                            break

                        # Déclenchement si la durée max sur batterie est dépassée (ex: 10 minutes)
                        if max_parking_duration > 0 and elapsed_discharge >= max_parking_duration:
                            self.initiate_safe_shutdown(
                                f"Durée maximale de surveillance parking atteinte ({int(elapsed_discharge)}s / {max_parking_duration}s)"
                            )
                            break
                else:
                    # Batterie pleine / en charge
                    if self.discharge_start_time is not None:
                        logging.info("Alimentation USB rétablie / Batterie rechargée.")
                        self.discharge_start_time = None

                self.last_percent = percent

            except Exception as e:
                logging.error("Erreur inattendue dans la boucle de surveillance: %s", e)

            time.sleep(2.0)

        self.ups.close()
        logging.info("Démon power_monitor terminé.")


def print_status_and_exit(config: Dict[str, Any]):
    """Affiche l'état courant de la batterie via I2C puis quitte."""
    print("=== Diagnostic Alimentation & Batterie (MAX17040G I2C) ===")
    ups = MAX17040(bus_num=config["UPS_I2C_BUS"], address=config["UPS_I2C_ADDR"])

    try:
        voltage, percent = ups.read_status()
        print(f"Jauge MAX17040 (0x{config['UPS_I2C_ADDR']:02X}) :")
        print(f"  - Tension batterie : {voltage:.3f} V")
        print(f"  - Charge restante  : {percent:.1f} %")
        if voltage >= 4.08 and percent >= 92.0:
            print("  - État estimé     : USB Alimenté (Batterie pleine ou en floating)")
        else:
            print("  - État estimé     : Sur batterie (Fonctionnement autonome)")
    except Exception as e:
        print(f"  - Erreur de communication I2C: {e}")
    finally:
        ups.close()


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

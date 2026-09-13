#!/usr/bin/env bash
# ==============================================================================
# Script d'installation complet - Pi-Dashcam
# ==============================================================================
set -e

# Vérification des privilèges sudo/root
if [ "$EUID" -ne 0 ]; then
    echo "Ce script doit être exécuté avec les privilèges root (sudo ./install.sh)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================================="
echo "    Installation du système Pi-Dashcam (Pi Zero 2 W)     "
echo "=========================================================="

echo ""
echo "=== 1. Installation des paquets et dépendances système ==="
apt-get update -y
apt-get install -y \
    python3-gpiozero \
    python3-smbus \
    python3-smbus2 \
    python3-spidev \
    python3-pil \
    python3-flask \
    i2c-tools \
    ffmpeg \
    network-manager \
    dnsmasq-base

# Vérification de rpicam-apps / libcamera-apps
if ! command -v rpicam-vid >/dev/null 2>&1 && ! command -v libcamera-vid >/dev/null 2>&1; then
    echo "Installation des outils de capture caméra (rpicam-apps / libcamera-apps)..."
    apt-get install -y rpicam-apps || apt-get install -y libcamera-apps || true
fi

echo ""
echo "=== 2. Activation des interfaces matérielles (I2C et SPI) ==="
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0
    raspi-config nonint do_spi 0
    echo "Interfaces I2C et SPI activées via raspi-config."
fi

# Vérification du fichier de configuration boot
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"

if [ -f "$BOOT_CONFIG" ]; then
    if ! grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
        echo "dtparam=i2c_arm=on" >> "$BOOT_CONFIG"
        echo "Ligne 'dtparam=i2c_arm=on' ajoutée à $BOOT_CONFIG."
    fi
    if ! grep -q "^dtparam=spi=on" "$BOOT_CONFIG"; then
        echo "dtparam=spi=on" >> "$BOOT_CONFIG"
        echo "Ligne 'dtparam=spi=on' ajoutée à $BOOT_CONFIG."
    fi
fi

# Chargement des modules noyau
modprobe i2c-dev 2>/dev/null || true
modprobe spidev 2>/dev/null || true

echo ""
echo "=== 3. Configuration et répertoires système ==="
mkdir -p /etc/pi-dashcam
mkdir -p /var/media/dashcam
mkdir -p /opt/pi-dashcam/web
chmod 777 /var/media/dashcam

if [ ! -f /etc/pi-dashcam/dashcam.conf ]; then
    echo "Copie du fichier de configuration par défaut vers /etc/pi-dashcam/dashcam.conf..."
    cp "$SCRIPT_DIR/config/dashcam.conf" /etc/pi-dashcam/dashcam.conf
else
    echo "Le fichier /etc/pi-dashcam/dashcam.conf existe déjà (conservé)."
fi

# Déploiement de l'application Web mobile
echo "Déploiement de l'interface Web mobile dans /opt/pi-dashcam/web..."
cp -r "$SCRIPT_DIR/web/"* /opt/pi-dashcam/web/

echo ""
echo "=== 4. Déploiement des scripts exécutables ==="
cp "$SCRIPT_DIR/scripts/dashcam.sh" /usr/local/bin/dashcam.sh
cp "$SCRIPT_DIR/scripts/power_monitor.py" /usr/local/bin/power_monitor.py
cp "$SCRIPT_DIR/scripts/setup_hotspot.sh" /usr/local/bin/setup_hotspot.sh
cp "$SCRIPT_DIR/scripts/epd2in13_v4.py" /usr/local/bin/epd2in13_v4.py
cp "$SCRIPT_DIR/scripts/display_epaper.py" /usr/local/bin/display_epaper.py

chmod +x /usr/local/bin/dashcam.sh
chmod +x /usr/local/bin/power_monitor.py
chmod +x /usr/local/bin/setup_hotspot.sh
chmod +x /usr/local/bin/epd2in13_v4.py
chmod +x /usr/local/bin/display_epaper.py

echo ""
echo "=== 5. Configuration et activation des services systemd ==="
cp "$SCRIPT_DIR/systemd/dashcam.service" /etc/systemd/system/dashcam.service
cp "$SCRIPT_DIR/systemd/power-monitor.service" /etc/systemd/system/power-monitor.service
cp "$SCRIPT_DIR/systemd/dashcam-web.service" /etc/systemd/system/dashcam-web.service
cp "$SCRIPT_DIR/systemd/dashcam-epaper.service" /etc/systemd/system/dashcam-epaper.service

systemctl daemon-reload
systemctl enable --now dashcam.service
systemctl enable --now dashcam-web.service
systemctl enable --now dashcam-epaper.service

# SÉCURITÉ : power-monitor est volontairement désactivé par défaut
# pour éviter toute extinction intempestive sur le bureau sans UPS-Lite
systemctl disable power-monitor.service 2>/dev/null || true
systemctl stop power-monitor.service 2>/dev/null || true

echo ""
echo "=========================================================="
echo "          Installation terminée avec succès !             "
echo "=========================================================="
echo ""
echo "Commandes utiles :"
echo "  - Configurer le Hotspot Wi-Fi : sudo setup_hotspot.sh enable"
echo "  - Tester l'affichage e-Paper  : python3 /usr/local/bin/display_epaper.py"
echo "  - Tester l'UPS-Lite           : python3 /usr/local/bin/power_monitor.py --status"
echo "  - Voir les périphériques I2C  : i2cdetect -y 1"
echo "  - Suivre les logs dashcam     : journalctl -u dashcam.service -f"
echo "  - Suivre les logs e-Paper     : journalctl -u dashcam-epaper.service -f"
echo "  - Suivre les logs Web         : journalctl -u dashcam-web.service -f"
echo "  - Interface Mobile            : http://192.168.4.1:5000 (ou http://<IP-du-Pi>:5000)"
echo "  - Répertoire des vidéos      : /var/media/dashcam"
echo ""
echo "NOTE : Si l'I2C ou le SPI viennent d'être activés pour la première fois,"
echo "un redémarrage est recommandé : sudo reboot"
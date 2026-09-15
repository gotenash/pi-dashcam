#!/usr/bin/env bash
# ==============================================================================
# setup_hotspot.sh - Configuration du Point d'Accès Wi-Fi (Hotspot)
# Utilise NetworkManager (natif sous Raspberry Pi OS Bookworm)
# ==============================================================================
set -euo pipefail

CONFIG_FILE="/etc/pi-dashcam/dashcam.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_CONFIG="${SCRIPT_DIR}/../config/dashcam.conf"

if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
elif [ -f "$LOCAL_CONFIG" ]; then
    # shellcheck source=/dev/null
    source "$LOCAL_CONFIG"
fi

HOTSPOT_SSID="${HOTSPOT_SSID:-Pi-Dashcam}"
HOTSPOT_PASS="${HOTSPOT_PASS:-dashcam1234}"
HOTSPOT_IP="${HOTSPOT_IP:-192.168.4.1}"
HOTSPOT_CON_NAME="Pi-Dashcam-Hotspot"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [hotspot] $*"
}

if [ "$EUID" -ne 0 ]; then
    echo "Ce script doit être exécuté avec les privilèges root (sudo)."
    exit 1
fi

if ! command -v nmcli >/dev/null 2>&1; then
    echo "ERREUR: nmcli (NetworkManager) n'est pas installé. Nécessaire sous Bookworm."
    exit 1
fi

case "${1:-enable}" in
    enable|setup)
        log "Configuration du point d'accès Wi-Fi NetworkManager..."
        log "  SSID : $HOTSPOT_SSID"
        log "  IP   : $HOTSPOT_IP"

        # 1. Débloquer l'interface Wi-Fi via rfkill
        rfkill unblock wifi 2>/dev/null || true

        # 2. Arrêter un éventuel service dnsmasq autonome qui entrerait en conflit
        # avec le serveur DHCP interne de NetworkManager
        if systemctl is-active --quiet dnsmasq 2>/dev/null; then
            log "Arrêt et désactivation du service dnsmasq autonome (conflit de port DHCP)..."
            systemctl stop dnsmasq 2>/dev/null || true
            systemctl disable dnsmasq 2>/dev/null || true
        fi

        # 3. Supprimer les anciens profils hotspot conflictuels
        nmcli connection delete "$HOTSPOT_CON_NAME" 2>/dev/null || true
        nmcli connection delete "Hotspot" 2>/dev/null || true

        # 4. Créer le profil Access Point permanent avec autoconnect
        log "Création du profil Wi-Fi AP permanent ($HOTSPOT_CON_NAME)..."
        nmcli connection add \
            type wifi \
            ifname wlan0 \
            con-name "$HOTSPOT_CON_NAME" \
            ssid "$HOTSPOT_SSID"

        # 5. Configurer les paramètres WPA2-PSK, canal 2.4GHz et DHCP partagé (shared)
        nmcli connection modify "$HOTSPOT_CON_NAME" \
            connection.autoconnect yes \
            connection.autoconnect-priority 10 \
            802-11-wireless.mode ap \
            802-11-wireless.band bg \
            802-11-wireless.channel 7 \
            802-11-wireless-security.key-mgmt wpa-psk \
            802-11-wireless-security.proto rsn \
            802-11-wireless-security.pairwise ccmp \
            802-11-wireless-security.group ccmp \
            802-11-wireless-security.psk "$HOTSPOT_PASS" \
            ipv4.method shared \
            ipv4.addresses "${HOTSPOT_IP}/24" \
            ipv6.method disabled

        # 6. Configurer les Wi-Fi clients existants (ex: Wi-Fi de la maison) avec une priorité plus haute (50)
        # et limiter les tentatives (autoconnect-retries 2) pour basculer rapidement sur le Hotspot en voiture
        for con in $(nmcli -t -f NAME,TYPE connection show | grep ':802-11-wireless$' | cut -d: -f1); do
            if [ "$con" != "$HOTSPOT_CON_NAME" ] && [ "$con" != "Hotspot" ]; then
                nmcli connection modify "$con" \
                    connection.autoconnect-priority 50 \
                    connection.autoconnect-retries 2 2>/dev/null || true
            fi
        done

        log "Démarrage du point d'accès Wi-Fi..."
        nmcli connection up "$HOTSPOT_CON_NAME" || true
        log "Point d'accès configuré avec succès (SSID: $HOTSPOT_SSID, IP: $HOTSPOT_IP) !"
        ;;

    start|up)
        log "Démarrage forcé du point d'accès Wi-Fi..."
        rfkill unblock wifi 2>/dev/null || true
        nmcli connection up "$HOTSPOT_CON_NAME"
        log "Point d'accès actif."
        ;;

    disable|stop)
        log "Désactivation du point d'accès Wi-Fi..."
        nmcli connection down "$HOTSPOT_CON_NAME" 2>/dev/null || true
        log "Point d'accès désactivé."
        ;;

    status)
        active_con=$(nmcli -t -f NAME,TYPE,STATE connection show --active 2>/dev/null | grep ':802-11-wireless:activated$' | cut -d: -f1 || true)
        if [ "$active_con" = "$HOTSPOT_CON_NAME" ]; then
            ip=$(hostname -I | awk '{print $1}')
            echo "STATUT : Hotspot Wi-Fi ACTIF (SSID: $HOTSPOT_SSID, IP: ${ip:-$HOTSPOT_IP})"
        elif [ -n "$active_con" ]; then
            ip=$(hostname -I | awk '{print $1}')
            echo "STATUT : Connecté au Wi-Fi client '$active_con' (IP: $ip)"
        else
            echo "STATUT : Wi-Fi INACTIF / NON CONNECTÉ"
        fi
        ;;

    *)
        echo "Usage: $0 {enable|disable|start|status}"
        exit 1
        ;;
esac

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

        # Supprimer l'ancienne connexion si existante
        if nmcli connection show "$HOTSPOT_CON_NAME" >/dev/null 2>&1; then
            log "Suppression de l'ancienne configuration hotspot..."
            nmcli connection delete "$HOTSPOT_CON_NAME" || true
        fi

        # Création du profil de connexion Access Point (Hotspot)
        log "Création du profil Wi-Fi AP..."
        nmcli connection add \
            type wifi \
            ifname wlan0 \
            con-name "$HOTSPOT_CON_NAME" \
            autoconnect yes \
            ssid "$HOTSPOT_SSID"

        nmcli connection modify "$HOTSPOT_CON_NAME" \
            802-11-wireless.mode ap \
            802-11-wireless.band bg \
            802-11-wireless-security.key-mgmt wpa-psk \
            802-11-wireless-security.psk "$HOTSPOT_PASS" \
            ipv4.method shared \
            ipv4.addresses "${HOTSPOT_IP}/24" \
            ipv6.method disabled

        log "Démarrage du point d'accès Wi-Fi..."
        nmcli connection up "$HOTSPOT_CON_NAME"
        log "Point d'accès actif avec succès ! Connectez votre smartphone au réseau '$HOTSPOT_SSID'."
        ;;

    disable|stop)
        log "Désactivation du point d'accès Wi-Fi..."
        nmcli connection down "$HOTSPOT_CON_NAME" || true
        log "Point d'accès désactivé."
        ;;

    status)
        if nmcli connection show --active | grep -q "$HOTSPOT_CON_NAME"; then
            echo "STATUT : Hotspot Wi-Fi ACTIF ($HOTSPOT_SSID sur $HOTSPOT_IP)"
        else
            echo "STATUT : Hotspot Wi-Fi INACTIF"
        fi
        ;;

    *)
        echo "Usage: $0 {enable|disable|status}"
        exit 1
        ;;
esac

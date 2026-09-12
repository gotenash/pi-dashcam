#!/usr/bin/env bash
# ==============================================================================
# dashcam.sh - Enregistrement vidéo continu et gestion de l'espace disque
# Compatible Raspberry Pi Zero 2 W / Pi OS Bookworm & Bullseye
# ==============================================================================

set -u

# Emplacements de configuration
CONFIG_FILE="/etc/pi-dashcam/dashcam.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_CONFIG="${SCRIPT_DIR}/../config/dashcam.conf"

# Chargement de la configuration
if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
elif [ -f "$LOCAL_CONFIG" ]; then
    # shellcheck source=/dev/null
    source "$LOCAL_CONFIG"
fi

# Valeurs par défaut si non définies
STORAGE_DIR="${STORAGE_DIR:-/var/media/dashcam}"
SEGMENT_DURATION_SEC="${SEGMENT_DURATION_SEC:-180}"
VIDEO_WIDTH="${VIDEO_WIDTH:-1920}"
VIDEO_HEIGHT="${VIDEO_HEIGHT:-1080}"
VIDEO_FPS="${VIDEO_FPS:-30}"
VIDEO_BITRATE="${VIDEO_BITRATE:-8000000}"
FILENAME_PREFIX="${FILENAME_PREFIX:-dashcam_}"
MAX_DISK_USAGE_PERCENT="${MAX_DISK_USAGE_PERCENT:-85}"
MIN_FREE_SPACE_MB="${MIN_FREE_SPACE_MB:-1024}"

LOG_TAG="pi-dashcam"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$LOG_TAG] $*"
}

# 1. Vérification et création du répertoire de stockage
if [ ! -d "$STORAGE_DIR" ]; then
    log "Création du dossier de stockage : $STORAGE_DIR"
    mkdir -p "$STORAGE_DIR" || {
        log "ERREUR: Impossible de créer $STORAGE_DIR"
        exit 1
    }
fi

# 2. Détection de l'outil de capture caméra (rpicam-vid ou libcamera-vid)
CAMERA_BIN=""
if command -v rpicam-vid >/dev/null 2>&1; then
    CAMERA_BIN="rpicam-vid"
elif command -v libcamera-vid >/dev/null 2>&1; then
    CAMERA_BIN="libcamera-vid"
else
    log "ERREUR: Aucun outil de capture trouvé (ni rpicam-vid, ni libcamera-vid)."
    log "Veuillez installer rpicam-apps ou libcamera-apps."
    exit 1
fi
log "Utilisation du binaire caméra : $CAMERA_BIN"

# 3. Fonction de nettoyage de l'espace disque (rotation circulaire)
cleanup_disk_space() {
    # Vérification du pourcentage d'utilisation
    local current_usage
    current_usage=$(df -P "$STORAGE_DIR" | awk 'NR==2 {gsub("%",""); print $5}')
    
    # Vérification de l'espace libre en Mo
    local free_mb
    free_mb=$(df -P -B 1M "$STORAGE_DIR" | awk 'NR==2 {print $4}')

    if [ -z "$current_usage" ]; then
        return 0
    fi

    while [ "$current_usage" -ge "$MAX_DISK_USAGE_PERCENT" ] || [ "$free_mb" -lt "$MIN_FREE_SPACE_MB" ]; do
        # Trouver la vidéo .mp4 la plus ancienne
        local oldest_file
        oldest_file=$(find "$STORAGE_DIR" -maxdepth 1 -type f -name "*.mp4" -printf '%T+ %p\n' 2>/dev/null | sort | head -n 1 | cut -d' ' -f2-)

        if [ -n "$oldest_file" ] && [ -f "$oldest_file" ]; then
            log "Espace disque critique (Usage: ${current_usage}%, Libre: ${free_mb}MB). Suppression de l'ancienne vidéo : $(basename "$oldest_file")"
            rm -f "$oldest_file"
            
            # Recalculer
            current_usage=$(df -P "$STORAGE_DIR" | awk 'NR==2 {gsub("%",""); print $5}')
            free_mb=$(df -P -B 1M "$STORAGE_DIR" | awk 'NR==2 {print $4}')
        else
            log "AVERTISSEMENT: Disque plein mais aucun fichier vidéo .mp4 supprimable trouvé."
            break
        fi
    done
}

# 4. Tâche de fond pour rotation disque et renommage propre des segments
ROTATION_PID=""
start_background_manager() {
    (
        while true; do
            sleep 30
            cleanup_disk_space

            # Renommage des segments bruts terminés (raw_segment_XXXXXX.mp4) avec date de modification
            find "$STORAGE_DIR" -maxdepth 1 -type f -name "raw_segment_*.mp4" -mmin +1 2>/dev/null | while IFS= read -r raw_file; do
                if [ -f "$raw_file" ]; then
                    local file_time
                    file_time=$(date -r "$raw_file" '+%Y-%m-%d_%H-%M-%S')
                    local new_name="${STORAGE_DIR}/${FILENAME_PREFIX}${file_time}.mp4"
                    # Éviter d'écraser si le fichier existe déjà
                    if [ ! -f "$new_name" ]; then
                        mv "$raw_file" "$new_name"
                        log "Segment finalisé : $(basename "$new_name")"
                    fi
                fi
            done
        done
    ) &
    ROTATION_PID=$!
}

# 5. Gestion propre de l'arrêt (SIGINT / SIGTERM)
CAM_PID=""
cleanup_and_exit() {
    log "Signal d'arrêt reçu. Arrêt propre des processus..."
    if [ -n "$ROTATION_PID" ]; then
        kill "$ROTATION_PID" 2>/dev/null || true
    fi

    if [ -n "$CAM_PID" ]; then
        log "Envoi de SIGINT au processus caméra ($CAM_PID) pour finaliser le conteneur MP4..."
        kill -SIGINT "$CAM_PID" 2>/dev/null || true
        # Laisser le temps à rpicam-vid de finaliser les métadonnées MP4
        wait "$CAM_PID" 2>/dev/null || true
    fi

    # Renommer les éventuels segments bruts restants
    find "$STORAGE_DIR" -maxdepth 1 -type f -name "raw_segment_*.mp4" 2>/dev/null | while IFS= read -r raw_file; do
        if [ -f "$raw_file" ]; then
            local file_time
            file_time=$(date -r "$raw_file" '+%Y-%m-%d_%H-%M-%S')
            local new_name="${STORAGE_DIR}/${FILENAME_PREFIX}${file_time}.mp4"
            [ ! -f "$new_name" ] && mv "$raw_file" "$new_name"
        fi
    done

    # Nettoyage final
    cleanup_disk_space
    sync
    log "Enregistrement dashcam arrêté proprement."
    exit 0
}

trap cleanup_and_exit SIGINT SIGTERM SIGHUP

# Nettoyage initial avant de démarrer
cleanup_disk_space

# Lancement de la surveillance de l'espace disque
start_background_manager

# 6. Lancement de la capture en continu par segments
# --segment prend des millisecondes
SEGMENT_MS=$((SEGMENT_DURATION_SEC * 1000))
RAW_PATTERN="${STORAGE_DIR}/raw_segment_%06d.mp4"

log "Démarrage de la capture vidéo :"
log "  - Résolution : ${VIDEO_WIDTH}x${VIDEO_HEIGHT} @ ${VIDEO_FPS} fps"
log "  - Débit      : $((VIDEO_BITRATE / 1000000)) Mbps"
log "  - Segment    : ${SEGMENT_DURATION_SEC} s (${SEGMENT_MS} ms)"
log "  - Dossier    : ${STORAGE_DIR}"

# Paramètres optimisés pour dashcam :
# --nopreview : économise CPU/GPU
# --inline : injecte SPS/PPS à chaque keyframe pour que chaque segment soit autonome
# --codec h264 : encodage matériel
# --segment : découpe sans perte d'images
$CAMERA_BIN \
    -t 0 \
    --nopreview \
    --codec h264 \
    --inline \
    --width "$VIDEO_WIDTH" \
    --height "$VIDEO_HEIGHT" \
    --framerate "$VIDEO_FPS" \
    --bitrate "$VIDEO_BITRATE" \
    --segment "$SEGMENT_MS" \
    -o "$RAW_PATTERN" &

CAM_PID=$!
wait "$CAM_PID"
cleanup_and_exit

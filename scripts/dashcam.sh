#!/usr/bin/env bash
# ==============================================================================
# dashcam.sh - Enregistrement vidéo continu et gestion de l'espace disque
# Compatible Raspberry Pi Zero 2 W / Pi OS Bookworm
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
    exit 1
fi
log "Utilisation du binaire caméra : $CAMERA_BIN"

# 3. Vérification de ffmpeg pour le multiplexage MP4
if ! command -v ffmpeg >/dev/null 2>&1; then
    log "ERREUR: ffmpeg n'est pas installé. Indispensable pour créer des conteneurs MP4 valides."
    exit 1
fi

# 4. Fonction de nettoyage de l'espace disque (rotation circulaire)
cleanup_disk_space() {
    local current_usage
    current_usage=$(df -P "$STORAGE_DIR" | awk 'NR==2 {gsub("%",""); print $5}')
    
    local free_mb
    free_mb=$(df -P -B 1M "$STORAGE_DIR" | awk 'NR==2 {print $4}')

    if [ -z "$current_usage" ]; then
        return 0
    fi

    while [ "$current_usage" -ge "$MAX_DISK_USAGE_PERCENT" ] || [ "$free_mb" -lt "$MIN_FREE_SPACE_MB" ]; do
        local oldest_file
        oldest_file=$(find "$STORAGE_DIR" -maxdepth 1 -type f -name "*.mp4" -printf '%T+ %p\n' 2>/dev/null | sort | head -n 1 | cut -d' ' -f2-)

        if [ -n "$oldest_file" ] && [ -f "$oldest_file" ]; then
            log "Espace disque critique (Usage: ${current_usage}%, Libre: ${free_mb}MB). Purge : $(basename "$oldest_file")"
            rm -f "$oldest_file"
            
            current_usage=$(df -P "$STORAGE_DIR" | awk 'NR==2 {gsub("%",""); print $5}')
            free_mb=$(df -P -B 1M "$STORAGE_DIR" | awk 'NR==2 {print $4}')
        else
            break
        fi
    done
}

# 5. Tâche de fond pour rotation disque
ROTATION_PID=""
start_background_manager() {
    (
        while true; do
            sleep 30
            cleanup_disk_space
        done
    ) &
    ROTATION_PID=$!
}

# 6. Gestion propre de l'arrêt (SIGINT / SIGTERM)
PIPE_PID=""
cleanup_and_exit() {
    log "Signal d'arrêt reçu. Arrêt propre des flux..."
    if [ -n "$ROTATION_PID" ]; then
        kill "$ROTATION_PID" 2>/dev/null || true
    fi

    # Tuer le groupe de processus du pipeline caméra + ffmpeg
    if [ -n "$PIPE_PID" ]; then
        kill -SIGINT -- "-$PIPE_PID" 2>/dev/null || kill -SIGINT "$PIPE_PID" 2>/dev/null || true
        wait "$PIPE_PID" 2>/dev/null || true
    fi

    # Arrêt de secours si des processus orphelins subsistent
    pkill -SIGINT -f "$CAMERA_BIN" 2>/dev/null || true
    pkill -SIGINT -f "ffmpeg.*segment_format.*mp4" 2>/dev/null || true

    cleanup_disk_space
    sync
    log "Enregistrement dashcam arrêté proprement."
    exit 0
}

trap cleanup_and_exit SIGINT SIGTERM SIGHUP

# Nettoyage initial
cleanup_disk_space
start_background_manager

# 7. Pipeline de capture et d'encodage MP4 robuste
# Format cible : dashcam_YYYY-MM-DD_HH-MM-SS.mp4
TARGET_PATTERN="${STORAGE_DIR}/${FILENAME_PREFIX}%Y-%m-%d_%H-%M-%S.mp4"

log "Démarrage du flux d'enregistrement :"
log "  - Résolution : ${VIDEO_WIDTH}x${VIDEO_HEIGHT} @ ${VIDEO_FPS} fps"
log "  - Débit      : $((VIDEO_BITRATE / 1000000)) Mbps (H.264 matériel)"
log "  - Séquences  : ${SEGMENT_DURATION_SEC} secondes"
log "  - Format     : MP4 Fragmenté (fMP4, résistant aux coupures)"
log "  - Dossier    : ${STORAGE_DIR}"

# Configuration de la rotation / orientation
VIDEO_ROTATION="${VIDEO_ROTATION:-0}"
CAM_ROT_ARGS=""
FFMPEG_ROT_ARGS=()

case "$VIDEO_ROTATION" in
    180)
        # 180° : inversion matérielle native via le capteur (0% CPU)
        CAM_ROT_ARGS="--hflip --vflip"
        log "  - Orientation: 180° (Inversé tête en bas)"
        ;;
    90)
        # 90° : matrice de rotation MP4 dans ffmpeg (0% CPU)
        FFMPEG_ROT_ARGS=("-metadata:s:v" "rotate=90")
        log "  - Orientation: 90° (Sens horaire)"
        ;;
    270)
        # 270° : matrice de rotation MP4 dans ffmpeg (0% CPU)
        FFMPEG_ROT_ARGS=("-metadata:s:v" "rotate=270")
        log "  - Orientation: 270° (Sens anti-horaire)"
        ;;
    *)
        log "  - Orientation: 0° (Normal)"
        ;;
esac

# Pipeline optimisé :
# 1. rpicam-vid encode en H.264 matériel (0% CPU) et envoie le flux brut sur stdout
# 2. ffmpeg lit le flux, n'effectue AUCUN réencodage (-c:v copy, <1% CPU), découpe
#    en segments de durée précise, et encapsule en MP4 fragmenté (+frag_keyframe+empty_moov)
#    garantissant que le fichier est 100% lisible même en cas de coupure de courant brutale !

set -m # Active la gestion de groupe de processus pour tuer le pipeline proprement
(
    $CAMERA_BIN \
        -t 0 \
        --nopreview \
        --codec h264 \
        --inline \
        --width "$VIDEO_WIDTH" \
        --height "$VIDEO_HEIGHT" \
        --framerate "$VIDEO_FPS" \
        --bitrate "$VIDEO_BITRATE" \
        $CAM_ROT_ARGS \
        -o - | ffmpeg -hide_banner -loglevel error \
            -f h264 -i - \
            -c:v copy \
            "${FFMPEG_ROT_ARGS[@]}" \
            -f segment \
            -segment_time "$SEGMENT_DURATION_SEC" \
            -segment_format mp4 \
            -segment_format_options "movflags=+faststart+frag_keyframe+empty_moov" \
            -reset_timestamps 1 \
            -strftime 1 \
            "$TARGET_PATTERN"
) &

PIPE_PID=$!
wait "$PIPE_PID"
cleanup_and_exit

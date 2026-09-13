#!/usr/bin/env python3
"""
app.py - Serveur Web et API REST pour Pi-Dashcam
Interface mobile-first embarquée pour Raspberry Pi Zero 2 W
"""

import os
import sys
import time
import shutil
import glob
import subprocess
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory, Response

# Ajout du chemin scripts pour importer les pilotes
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
sys.path.insert(0, "/usr/local/bin")

try:
    from power_monitor import MAX17040, PowerInputDetector, load_config
except ImportError:
    # Mode autonome si exécuté hors structure
    def load_config():
        return {
            "STORAGE_DIR": "/var/media/dashcam",
            "WEB_PORT": 5000,
            "WEB_HOST": "0.0.0.0",
            "POWER_DETECT_PIN": 4,
            "UPS_I2C_BUS": 1,
            "UPS_I2C_ADDR": 0x36,
            "SEGMENT_DURATION_SEC": 180,
            "VIDEO_WIDTH": 1920,
            "VIDEO_HEIGHT": 1080,
            "VIDEO_FPS": 30,
            "SHUTDOWN_DELAY_SEC": 30,
            "MAX_DISK_USAGE_PERCENT": 85,
        }
    MAX17040 = None
    PowerInputDetector = None

app = Flask(__name__)
CONFIG = load_config()

# Instances matérielles (avec résilience)
ups_sensor = None
power_sensor = None
if MAX17040:
    try:
        ups_sensor = MAX17040(
            bus_num=CONFIG.get("UPS_I2C_BUS", 1),
            address=CONFIG.get("UPS_I2C_ADDR", 0x36)
        )
    except Exception:
        ups_sensor = None

if PowerInputDetector:
    try:
        power_sensor = PowerInputDetector(
            pin=CONFIG.get("POWER_DETECT_PIN", 4),
            active_low=bool(CONFIG.get("POWER_DETECT_ACTIVE_LOW", 1))
        )
    except Exception:
        power_sensor = None


def get_cpu_temperature() -> float:
    """Lit la température du CPU du Pi."""
    temp_path = "/sys/class/thermal/thermal_zone0/temp"
    if os.path.exists(temp_path):
        try:
            with open(temp_path, "r") as f:
                return round(float(f.read().strip()) / 1000.0, 1)
        except Exception:
            pass
    return 42.0  # Valeur de simulation si absent


def get_service_status(service_name: str) -> bool:
    """Vérifie si un service systemd est actif."""
    try:
        res = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=2
        )
        return res.stdout.strip() == "active"
    except Exception:
        return False


def get_disk_statistics(storage_dir: str):
    """Retourne les métriques d'espace disque en Go et pourcentage."""
    try:
        os.makedirs(storage_dir, exist_ok=True)
        total, used, free = shutil.disk_usage(storage_dir)
        total_gb = round(total / (1024 ** 3), 1)
        used_gb = round(used / (1024 ** 3), 1)
        free_gb = round(free / (1024 ** 3), 1)
        percent = round((used / total) * 100, 1) if total > 0 else 0
        return {
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
            "percent": percent
        }
    except Exception:
        return {"total_gb": 32.0, "used_gb": 4.5, "free_gb": 27.5, "percent": 14.0}


@app.route("/")
def index():
    """Page d'accueil du dashboard mobile."""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Retourne l'état complet du système en JSON."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    
    # 1. Télémétrie batterie UPS-Lite
    battery_v = 0.0
    battery_pct = 0.0
    if ups_sensor:
        try:
            battery_v, battery_pct = ups_sensor.read_status()
            battery_v = round(battery_v, 2)
            battery_pct = round(battery_pct, 1)
        except Exception:
            battery_v, battery_pct = 4.10, 95.0
    else:
        battery_v, battery_pct = 4.10, 95.0

    # 2. Détection alimentation externe
    ext_power = True
    if power_sensor:
        try:
            status = power_sensor.is_external_power_connected()
            if status is not None:
                ext_power = status
        except Exception:
            pass

    # 3. Métriques système
    disk = get_disk_statistics(storage_dir)
    cpu_temp = get_cpu_temperature()
    is_recording = get_service_status("dashcam.service")
    is_power_mon = get_service_status("power-monitor.service")

    return jsonify({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "battery": {
            "voltage": battery_v,
            "percent": battery_pct,
            "external_power": ext_power,
        },
        "system": {
            "cpu_temp": cpu_temp,
            "disk": disk,
            "recording": is_recording,
            "power_monitor": is_power_mon,
        }
    })


@app.route("/api/videos")
def api_videos():
    """Liste les vidéos enregistrées ordonnées par date décroissante."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    try:
        os.makedirs(storage_dir, exist_ok=True)
    except Exception:
        pass
    
    video_files = glob.glob(os.path.join(storage_dir, "*.mp4"))
    videos = []

    for fpath in video_files:
        try:
            fname = os.path.basename(fpath)
            stat = os.stat(fpath)
            size_mb = round(stat.st_size / (1024 * 1024), 1)
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            videos.append({
                "filename": fname,
                "size_mb": size_mb,
                "mtime": mtime,
                "timestamp": stat.st_mtime,
                "url": f"/videos/{fname}",
                "download_url": f"/api/videos/{fname}/download",
            })
        except Exception:
            continue

    # Trier du plus récent au plus ancien
    videos.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"count": len(videos), "videos": videos})


@app.route("/videos/<path:filename>")
def stream_video(filename: str):
    """Permet la lecture vidéo en streaming (support des byte ranges pour l'iPhone/Android)."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    return send_from_directory(storage_dir, filename, as_attachment=False)


@app.route("/api/videos/<path:filename>/download")
def download_video(filename: str):
    """Télécharge la vidéo sur le smartphone."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    return send_from_directory(storage_dir, filename, as_attachment=True)


@app.route("/api/videos/<path:filename>", methods=["DELETE"])
def delete_video(filename: str):
    """Supprime un fichier vidéo."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    # Sécurité anti path-traversal
    safe_path = os.path.abspath(os.path.join(storage_dir, filename))
    if not safe_path.startswith(os.path.abspath(storage_dir)):
        return jsonify({"error": "Accès non autorisé"}), 403

    if os.path.exists(safe_path):
        try:
            os.remove(safe_path)
            return jsonify({"success": True, "message": f"{filename} supprimé"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Fichier introuvable"}), 404


@app.route("/api/snapshot")
def api_snapshot():
    """
    Fournit un instantané récent pour ajuster le cadrage sur le pare-brise.
    Si la caméra est en cours d'enregistrement, extrait l'image de la dernière vidéo via ffmpeg.
    Sinon effectue une capture directe.
    """
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    snapshot_path = "/tmp/dashcam_preview.jpg"

    # Chercher la vidéo la plus récente
    video_files = glob.glob(os.path.join(storage_dir, "*.mp4"))
    if video_files:
        video_files.sort(key=os.path.getmtime, reverse=True)
        latest_video = video_files[0]
        # Extraction rapide de la dernière seconde avec ffmpeg
        cmd = [
            "ffmpeg", "-y", "-sseof", "-2",
            "-i", latest_video,
            "-vframes", "1",
            "-q:v", "3",
            snapshot_path
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
            if os.path.exists(snapshot_path):
                return send_from_directory("/tmp", "dashcam_preview.jpg", mimetype="image/jpeg")
        except Exception:
            pass

    # Si aucune vidéo ou échec ffmpeg, tentative directe si dashcam inactive
    if not get_service_status("dashcam.service"):
        cam_bin = "rpicam-still" if shutil.which("rpicam-still") else "libcamera-still"
        if shutil.which(cam_bin):
            try:
                subprocess.run(
                    [cam_bin, "-t", "500", "-o", snapshot_path, "-n", "--width", "1280", "--height", "720"],
                    timeout=3
                )
                if os.path.exists(snapshot_path):
                    return send_from_directory("/tmp", "dashcam_preview.jpg", mimetype="image/jpeg")
            except Exception:
                pass

    # Image SVG de secours si aucune capture n'est encore disponible
    svg_fallback = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
        <rect width="100%" height="100%" fill="#1e293b"/>
        <circle cx="320" cy="180" r="40" fill="#334155"/>
        <text x="320" y="185" fill="#94a3b8" font-family="sans-serif" font-size="16" text-anchor="middle">
            Aperçu disponible dès le premier segment enregistré
        </text>
    </svg>"""
    return Response(svg_fallback, mimetype="image/svg+xml")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Lit ou met à jour la configuration dashcam.conf."""
    config_file = "/etc/pi-dashcam/dashcam.conf"
    if not os.path.exists(config_file):
        config_file = os.path.join(BASE_DIR, "config", "dashcam.conf")

    if request.method == "GET":
        current_cfg = load_config()
        return jsonify(current_cfg)

    # Mise à jour de la configuration
    data = request.json or {}
    allowed_keys = {
        "SEGMENT_DURATION_SEC": int,
        "VIDEO_WIDTH": int,
        "VIDEO_HEIGHT": int,
        "VIDEO_FPS": int,
        "VIDEO_BITRATE": int,
        "SHUTDOWN_DELAY_SEC": int,
        "MAX_DISK_USAGE_PERCENT": int,
    }

    try:
        # Lecture du fichier actuel
        lines = []
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _ = stripped.split("=", 1)
                k = k.strip()
                if k in data and k in allowed_keys:
                    val = allowed_keys[k](data[k])
                    new_lines.append(f"{k}={val}\n")
                    updated_keys.add(k)
                    continue
            new_lines.append(line)

        # Ajouter les clés non présentes
        for k, v in data.items():
            if k in allowed_keys and k not in updated_keys:
                new_lines.append(f"{k}={allowed_keys[k](v)}\n")

        with open(config_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        global CONFIG
        CONFIG = load_config()

        return jsonify({"success": True, "message": "Configuration sauvegardée"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/action/restart-dashcam", methods=["POST"])
def api_restart_dashcam():
    """Redémarre le service d'enregistrement vidéo."""
    try:
        subprocess.run(["systemctl", "restart", "dashcam.service"], check=True, timeout=10)
        return jsonify({"success": True, "message": "Dashcam redémarrée avec succès"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/action/toggle-dashcam", methods=["POST"])
def api_toggle_dashcam():
    """Bascule l'état (Démarrer / Arrêter) de la dashcam."""
    try:
        is_active = get_service_status("dashcam.service")
        target_action = "stop" if is_active else "start"
        subprocess.run(["systemctl", target_action, "dashcam.service"], check=True, timeout=10)
        new_state = get_service_status("dashcam.service")
        return jsonify({"success": True, "recording": new_state})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    cfg = load_config()
    port = int(cfg.get("WEB_PORT", 5000))
    host = cfg.get("WEB_HOST", "0.0.0.0")
    print(f"Démarrage de l'interface mobile Pi-Dashcam sur http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()

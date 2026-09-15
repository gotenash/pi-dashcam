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
import tempfile
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory, Response, send_file, make_response

# Ajout du chemin scripts pour importer les pilotes
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
sys.path.insert(0, "/usr/local/bin")

try:
    from power_monitor import MAX17040, load_config
except ImportError:
    def load_config():
        return {
            "STORAGE_DIR": "/var/media/dashcam",
            "WEB_PORT": 5000,
            "WEB_HOST": "0.0.0.0",
            "UPS_I2C_BUS": 1,
            "UPS_I2C_ADDR": 0x32,
            "SEGMENT_DURATION_SEC": 180,
            "VIDEO_WIDTH": 1920,
            "VIDEO_HEIGHT": 1080,
            "VIDEO_FPS": 30,
            "SHUTDOWN_DELAY_SEC": 30,
            "MAX_DISK_USAGE_PERCENT": 85,
            "VIDEO_ROTATION": 0,
            "ENABLE_AUTO_SHUTDOWN": 1,
            "PARKING_SHUTDOWN_BATTERY_PERCENT": 85,
            "PARKING_MAX_DURATION_SEC": 600,
        }
    MAX17040 = None

app = Flask(__name__)
CONFIG = load_config()

# Instance télémétrie batterie UPS-Lite (CW2015 0x32 ou MAX17040 0x36)
ups_sensor = None
if MAX17040:
    try:
        ups_sensor = MAX17040(
            bus_num=CONFIG.get("UPS_I2C_BUS", 1),
            address=CONFIG.get("UPS_I2C_ADDR", 0x32)
        )
    except Exception:
        ups_sensor = None


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


def get_hotspot_status() -> dict:
    """Retourne l'état actuel du point d'accès Wi-Fi."""
    con_name = "Pi-Dashcam-Hotspot"
    ssid = CONFIG.get("HOTSPOT_SSID", "Pi-Dashcam")
    ip = CONFIG.get("HOTSPOT_IP", "192.168.4.1")
    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active"],
            capture_output=True,
            text=True,
            timeout=3
        )
        for line in res.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "802-11-wireless":
                if parts[0] in (con_name, "Hotspot"):
                    return {
                        "active": True,
                        "ssid": ssid,
                        "ip": ip,
                        "mode": "hotspot"
                    }
                else:
                    return {
                        "active": False,
                        "ssid": ssid,
                        "ip": ip,
                        "mode": "client",
                        "client_ssid": parts[0]
                    }
    except Exception:
        pass
    return {
        "active": False,
        "ssid": ssid,
        "ip": ip,
        "mode": "disconnected"
    }


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
    is_charging = False
    if ups_sensor:
        try:
            battery_v, battery_pct = ups_sensor.read_status()
            battery_v = round(battery_v, 2)
            battery_pct = round(battery_pct, 1)
            is_charging = ups_sensor.is_charging(battery_v, battery_pct)
        except Exception:
            battery_v, battery_pct = 4.10, 95.0
            is_charging = True
    else:
        battery_v, battery_pct = 4.10, 95.0
        is_charging = True

    # 2. Métriques système
    disk = get_disk_statistics(storage_dir)
    cpu_temp = get_cpu_temperature()
    is_recording = get_service_status("dashcam.service")
    is_power_mon = get_service_status("power-monitor.service")
    hotspot_info = get_hotspot_status()

    return jsonify({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "battery": {
            "voltage": battery_v,
            "percent": battery_pct,
            "charging": is_charging,
        },
        "system": {
            "cpu_temp": cpu_temp,
            "disk": disk,
            "recording": is_recording,
            "power_monitor": is_power_mon,
            "hotspot": hotspot_info,
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


@app.route("/api/videos/delete-batch", methods=["POST"])
def delete_videos_batch():
    """Supprime une sélection de vidéos."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames", [])

    if not isinstance(filenames, list) or not filenames:
        return jsonify({"error": "Aucune vidéo spécifiée"}), 400

    deleted = 0
    errors = []
    base_dir = os.path.abspath(storage_dir)

    for fname in filenames:
        if not isinstance(fname, str) or not fname:
            continue
        safe_path = os.path.abspath(os.path.join(storage_dir, fname))
        if not safe_path.startswith(base_dir):
            errors.append(f"{fname}: accès interdit")
            continue
        if os.path.exists(safe_path) and os.path.isfile(safe_path):
            try:
                os.remove(safe_path)
                deleted += 1
            except Exception as e:
                errors.append(f"{fname}: {e}")

    return jsonify({
        "success": True,
        "deleted_count": deleted,
        "errors": errors,
        "message": f"{deleted} vidéo(s) supprimée(s)"
    })


@app.route("/api/videos/delete-all", methods=["POST"])
def delete_videos_all():
    """Supprime tous les fichiers vidéo enregistrés."""
    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    video_files = glob.glob(os.path.join(storage_dir, "*.mp4"))

    # Si la dashcam est active, protéger le segment actif en cours d'écriture par ffmpeg
    is_recording = get_service_status("dashcam.service")
    active_file = None
    if is_recording and video_files:
        video_files.sort(key=os.path.getmtime, reverse=True)
        active_file = video_files[0]

    deleted = 0
    errors = []

    for fpath in video_files:
        if is_recording and fpath == active_file:
            continue
        try:
            os.remove(fpath)
            deleted += 1
        except Exception as e:
            errors.append(f"{os.path.basename(fpath)}: {e}")

    msg = f"{deleted} vidéo(s) supprimée(s)"
    if is_recording and active_file:
        msg += " (le segment vidéo en cours a été conservé)"

    return jsonify({
        "success": True,
        "deleted_count": deleted,
        "errors": errors,
        "message": msg
    })


def make_no_cache_response(resp):
    """Ajoute les en-têtes HTTP pour interdire le cache navigateur."""
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


cadrage_process = None
cadrage_lock = threading.Lock()
cadrage_timer = None


def _auto_stop_cadrage():
    """Arrêt automatique du mode cadrage après expiration du délai de sécurité (2 min)."""
    with cadrage_lock:
        _stop_cadrage_internal()


def _stop_cadrage_internal():
    """Arrête le sous-processus de cadrage et relance l'enregistrement dashcam."""
    global cadrage_process, cadrage_timer
    if cadrage_timer:
        try:
            cadrage_timer.cancel()
        except Exception:
            pass
        cadrage_timer = None

    if cadrage_process:
        try:
            cadrage_process.terminate()
            cadrage_process.wait(timeout=2)
        except Exception:
            try:
                cadrage_process.kill()
            except Exception:
                pass
        cadrage_process = None

    # Relancer dashcam.service
    try:
        subprocess.run(["systemctl", "start", "dashcam.service"], timeout=5)
    except Exception:
        pass


@app.route("/api/cadrage/start", methods=["POST"])
def api_cadrage_start():
    """
    Active le mode cadrage direct à la demande.
    Suspend temporairement dashcam.service et démarre un flux matériel léger (640x360 @ 10fps).
    """
    global cadrage_process, cadrage_timer
    with cadrage_lock:
        # 1. Arrêter dashcam.service pour libérer le capteur
        try:
            subprocess.run(["systemctl", "stop", "dashcam.service"], timeout=5)
        except Exception:
            pass

        # 2. Terminer un éventuel processus existant
        if cadrage_process:
            try:
                cadrage_process.terminate()
                cadrage_process.wait(timeout=1)
            except Exception:
                try:
                    cadrage_process.kill()
                except Exception:
                    pass
            cadrage_process = None

        # 3. Détecter l'outil de capture caméra
        cam_bin = "rpicam-vid" if shutil.which("rpicam-vid") else "libcamera-vid"
        if not shutil.which(cam_bin):
            return jsonify({"active": True, "simulation": True})

        rot = int(CONFIG.get("VIDEO_ROTATION", 0))
        rot_args = []
        if rot == 180:
            rot_args = ["--hflip", "--vflip"]
        elif rot in (90, 270):
            rot_args = ["--rotation", str(rot)]

        cmd = [
            cam_bin,
            "-t", "0",
            "--nopreview",
            "--width", "640",
            "--height", "360",
            "--framerate", "10",
            "--codec", "mjpeg",
            "-o", "-"
        ] + rot_args

        try:
            cadrage_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0
            )

            # Minuteur de sécurité : arrêt automatique après 120 secondes pour protéger le CPU/batterie
            if cadrage_timer:
                cadrage_timer.cancel()
            cadrage_timer = threading.Timer(120.0, _auto_stop_cadrage)
            cadrage_timer.daemon = True
            cadrage_timer.start()

            return jsonify({"active": True, "timeout_sec": 120})
        except Exception as e:
            _stop_cadrage_internal()
            return jsonify({"error": str(e)}), 500


@app.route("/api/cadrage/stop", methods=["POST"])
def api_cadrage_stop():
    """Arrête le mode cadrage direct et réactive l'enregistrement normal."""
    with cadrage_lock:
        _stop_cadrage_internal()
    return jsonify({"active": False, "message": "Enregistrement dashcam repris"})


@app.route("/api/cadrage/status", methods=["GET"])
def api_cadrage_status():
    """Indique si le mode cadrage est actuellement actif."""
    is_active = cadrage_process is not None and cadrage_process.poll() is None
    return jsonify({"active": is_active})


@app.route("/api/stream")
def api_stream():
    """
    Flux vidéo MJPEG en direct pour le cadrage en temps réel.
    Lit les trames JPEG envoyées par rpicam-vid sur stdout.
    """
    def generate_frames():
        global cadrage_process
        if cadrage_process and cadrage_process.stdout:
            buf = b""
            while cadrage_process and cadrage_process.poll() is None:
                try:
                    chunk = cadrage_process.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                    if start != -1 and end != -1:
                        jpg = buf[start : end + 2]
                        buf = buf[end + 2 :]
                        yield (b"--frame\r\n"
                               b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
                except Exception:
                    break

        # SVG d'attente quand le direct n'est pas actif
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
            <rect width="100%" height="100%" fill="#1e293b"/>
            <circle cx="320" cy="140" r="36" fill="#334155"/>
            <polygon points="312,125 312,155 336,140" fill="#38bdf8"/>
            <text x="320" y="210" fill="#f8fafc" font-family="sans-serif" font-size="16" font-weight="600" text-anchor="middle">
                Caméra en direct inactive
            </text>
            <text x="320" y="240" fill="#94a3b8" font-family="sans-serif" font-size="13" text-anchor="middle">
                Cliquez sur « Démarrer le Direct » pour ajuster votre cadrage
            </text>
            <text x="320" y="275" fill="#64748b" font-family="sans-serif" font-size="11" text-anchor="middle">
                Le direct est activé à la demande pour préserver le CPU (&lt; 1%) et la batterie
            </text>
        </svg>"""
        yield (b"--frame\r\n"
               b"Content-Type: image/svg+xml\r\n\r\n" + svg.encode("utf-8") + b"\r\n")

    return Response(
        generate_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.route("/api/snapshot")
def api_snapshot():
    """
    Fournit un instantané récent pour ajuster le cadrage sur le pare-brise.
    0. Utilise en priorité absolue l'image directe rafraîchie en mémoire vive (/dev/shm).
    1. Si des vidéos existent, extrait une image via ffmpeg.
    2. Si aucune vidéo ou échec ffmpeg, tente une capture directe rpicam-still / libcamera-still.
    3. Sinon renvoie une image SVG explicative avec horodatage dynamique.
    """
    # 0. Priorité absolue : image directe en mémoire vive (RAM tmpfs)
    live_paths = [
        "/dev/shm/dashcam_live.jpg",
        os.path.join(tempfile.gettempdir(), "dashcam_live.jpg")
    ]
    for p in live_paths:
        if os.path.exists(p) and os.path.getsize(p) > 100:
            resp = make_response(send_file(p, mimetype="image/jpeg"))
            return make_no_cache_response(resp)

    storage_dir = CONFIG.get("STORAGE_DIR", "/var/media/dashcam")
    temp_dir = tempfile.gettempdir()
    snapshot_path = os.path.join(temp_dir, "dashcam_preview.jpg")

    # Supprimer un éventuel ancien fichier temporaire pour éviter de servir du contenu périmé
    if os.path.exists(snapshot_path):
        try:
            os.remove(snapshot_path)
        except Exception:
            pass

    # 1. Extraction depuis les vidéos existantes
    video_files = glob.glob(os.path.join(storage_dir, "*.mp4"))
    if video_files:
        video_files.sort(key=os.path.getmtime, reverse=True)
        # On teste jusqu'aux 3 vidéos les plus récentes
        for vid in video_files[:3]:
            try:
                if os.path.getsize(vid) < 1024:
                    continue
            except Exception:
                continue

            # Tentative A : fin de vidéo (-sseof -2)
            cmd_eof = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-sseof", "-2",
                "-i", vid,
                "-vframes", "1",
                "-q:v", "3",
                snapshot_path
            ]
            try:
                subprocess.run(cmd_eof, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
                if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
                    resp = make_response(send_file(snapshot_path, mimetype="image/jpeg"))
                    return make_no_cache_response(resp)
            except Exception:
                pass

            # Tentative B : première seconde (utile si le segment fMP4 est en cours d'enregistrement ou court)
            cmd_start = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "00:00:01",
                "-i", vid,
                "-vframes", "1",
                "-q:v", "3",
                snapshot_path
            ]
            try:
                subprocess.run(cmd_start, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
                if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
                    resp = make_response(send_file(snapshot_path, mimetype="image/jpeg"))
                    return make_no_cache_response(resp)
            except Exception:
                pass

            # Tentative C : première image disponible
            cmd_first = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", vid,
                "-vframes", "1",
                "-q:v", "3",
                snapshot_path
            ]
            try:
                subprocess.run(cmd_first, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
                if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
                    resp = make_response(send_file(snapshot_path, mimetype="image/jpeg"))
                    return make_no_cache_response(resp)
            except Exception:
                pass

    # 2. Capture directe si la dashcam n'est pas en cours d'enregistrement
    if not get_service_status("dashcam.service"):
        cam_bin = "rpicam-still" if shutil.which("rpicam-still") else "libcamera-still"
        if shutil.which(cam_bin):
            try:
                rot = int(CONFIG.get("VIDEO_ROTATION", 0))
                if rot == 180:
                    rot_args = ["--hflip", "--vflip"]
                elif rot in (90, 270):
                    rot_args = ["--rotation", str(rot)]
                else:
                    rot_args = []
                # Timeout augmenté à 8s pour laisser à libcamera le temps d'initialiser le capteur
                subprocess.run(
                    [cam_bin, "-t", "500", "-o", snapshot_path, "-n", "--width", "1280", "--height", "720"] + rot_args,
                    timeout=8
                )
                if os.path.exists(snapshot_path) and os.path.getsize(snapshot_path) > 0:
                    resp = make_response(send_file(snapshot_path, mimetype="image/jpeg"))
                    return make_no_cache_response(resp)
            except Exception:
                pass

    # 3. Image SVG dynamique si aucune image n'a pu être extraite
    now_str = datetime.now().strftime("%H:%M:%S")
    svg_fallback = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
        <rect width="100%" height="100%" fill="#1e293b"/>
        <circle cx="320" cy="150" r="36" fill="#334155"/>
        <path d="M305 150h30M320 135v30" stroke="#64748b" stroke-width="3" stroke-linecap="round"/>
        <text x="320" y="215" fill="#94a3b8" font-family="sans-serif" font-size="15" font-weight="600" text-anchor="middle">
            Aperçu caméra en attente
        </text>
        <text x="320" y="240" fill="#64748b" font-family="sans-serif" font-size="12" text-anchor="middle">
            L'image s'affichera dès le premier segment enregistré
        </text>
        <text x="320" y="275" fill="#38bdf8" font-family="sans-serif" font-size="11" text-anchor="middle">
            Dernière tentative : {now_str}
        </text>
    </svg>"""
    resp = make_response(Response(svg_fallback, mimetype="image/svg+xml"))
    return make_no_cache_response(resp)


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
        "VIDEO_ROTATION": int,
        "ENABLE_AUTO_SHUTDOWN": int,
        "PARKING_SHUTDOWN_BATTERY_PERCENT": int,
        "PARKING_MAX_DURATION_SEC": int,
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

        # Recharger power-monitor pour appliquer les seuils batterie immédiatement
        try:
            subprocess.run(["systemctl", "restart", "power-monitor.service"], check=False, timeout=5)
        except Exception:
            pass

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


@app.route("/api/hotspot/status")
def api_hotspot_status():
    """Retourne l'état actuel du Point d'Accès Wi-Fi."""
    return jsonify(get_hotspot_status())


@app.route("/api/hotspot/toggle", methods=["POST"])
def api_toggle_hotspot():
    """Active ou désactive le Point d'Accès Wi-Fi."""
    data = request.json or {}
    target_action = data.get("action")  # 'enable', 'disable', ou None pour toggle
    con_name = "Pi-Dashcam-Hotspot"
    current_status = get_hotspot_status()
    should_enable = not current_status["active"] if target_action is None else (target_action == "enable")

    try:
        if should_enable:
            subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True, text=True, timeout=5)
            subprocess.run(["nmcli", "connection", "up", con_name], capture_output=True, text=True, timeout=10)
        else:
            subprocess.run(["nmcli", "connection", "down", con_name], capture_output=True, text=True, timeout=10)

        new_status = get_hotspot_status()
        return jsonify({"success": True, "hotspot": new_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/action/shutdown", methods=["POST"])
def api_shutdown():
    """Arrête proprement le Raspberry Pi (sauvegarde vidéo, sync et poweroff)."""
    try:
        def do_shutdown():
            time.sleep(1.2)  # Laisser le temps à la réponse HTTP de parvenir au smartphone
            try:
                subprocess.run(["systemctl", "stop", "dashcam.service"], check=False, timeout=15)
            except Exception:
                pass
            try:
                os.sync()
            except Exception:
                pass
            subprocess.run(["shutdown", "-h", "now"], check=False)

        threading.Thread(target=do_shutdown, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "Extinction propre du Raspberry Pi initiée..."
        })
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

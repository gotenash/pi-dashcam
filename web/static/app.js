/**
 * app.js - Logique interactive pour l'interface mobile Pi-Dashcam
 */

let statusPollInterval = null;
let currentRecordingState = false;

document.addEventListener("DOMContentLoaded", () => {
    initTabs();
    initStatusPolling();
    loadConfig();
    loadVideos();
});

// 1. Gestion des Onglets
function initTabs() {
    const tabButtons = document.querySelectorAll(".tab-btn");
    const tabContents = document.querySelectorAll(".tab-content");

    tabButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");

            tabButtons.forEach(b => b.classList.remove("active"));
            tabContents.forEach(c => c.classList.remove("active"));

            btn.classList.add("active");
            const targetEl = document.getElementById(targetTab);
            if (targetEl) targetEl.classList.add("active");

            // Si on ouvre l'onglet vidéo, rafraîchir
            if (targetTab === "tab-videos") {
                loadVideos();
            } else if (targetTab === "tab-preview") {
                refreshSnapshot();
            }
        });
    });
}

// 2. Télémétrie en temps réel (/api/status)
function initStatusPolling() {
    fetchStatus();
    statusPollInterval = setInterval(fetchStatus, 3000);
}

async function fetchStatus() {
    try {
        const res = await fetch("/api/status");
        if (!res.ok) return;
        const data = await res.json();
        updateDashboardUI(data);
    } catch (err) {
        console.warn("Échec de la télémétrie:", err);
    }
}

function updateDashboardUI(data) {
    // Horodatage
    const lastUpdateEl = document.getElementById("last-update");
    if (lastUpdateEl) lastUpdateEl.textContent = `MàJ : ${data.timestamp.split(" ")[1]}`;

    // Batterie
    const batPercentEl = document.getElementById("battery-percent");
    const batVoltageEl = document.getElementById("battery-voltage");
    const batBarEl = document.getElementById("battery-bar");
    const powerTagEl = document.getElementById("power-source-tag");

    const percent = data.battery.percent;
    const voltage = data.battery.voltage;
    const extPower = data.battery.external_power;

    if (batPercentEl) batPercentEl.textContent = `${percent}%`;
    if (batVoltageEl) batVoltageEl.textContent = `${voltage} V`;
    if (batBarEl) {
        batBarEl.style.width = `${Math.min(100, Math.max(0, percent))}%`;
        if (percent < 15) {
            batBarEl.style.background = "var(--accent-red)";
        } else if (percent < 30) {
            batBarEl.style.background = "var(--accent-amber)";
        } else {
            batBarEl.style.background = "var(--accent-green)";
        }
    }

    if (powerTagEl) {
        const isCharging = data.battery && data.battery.charging;
        if (isCharging) {
            powerTagEl.className = "status-pill status-usb";
            if (percent >= 98) {
                powerTagEl.textContent = "⚡ USB Branché (Plein)";
            } else {
                powerTagEl.textContent = "⚡ En charge (USB)";
            }
        } else {
            if (percent <= 15) {
                powerTagEl.className = "status-pill status-danger";
                powerTagEl.textContent = "🚨 Batterie Critique";
            } else if (percent <= 30) {
                powerTagEl.className = "status-pill status-battery";
                powerTagEl.textContent = "⚠️ Batterie Faible";
            } else {
                powerTagEl.className = "status-pill status-battery";
                powerTagEl.textContent = "🔋 Sur Batterie LiPo";
            }
        }
    }

    // Espace Disque
    const disk = data.system.disk;
    const diskPercentEl = document.getElementById("disk-percent");
    const diskFreeEl = document.getElementById("disk-free");
    const diskBarEl = document.getElementById("disk-bar");
    const diskUsedTotalEl = document.getElementById("disk-used-total");

    if (diskPercentEl) diskPercentEl.textContent = `${disk.percent}%`;
    if (diskFreeEl) diskFreeEl.textContent = `${disk.free_gb} Go libres`;
    if (diskBarEl) {
        diskBarEl.style.width = `${disk.percent}%`;
        diskBarEl.style.background = disk.percent > 85 ? "var(--accent-red)" : "var(--accent-blue)";
    }
    if (diskUsedTotalEl) {
        diskUsedTotalEl.textContent = `${disk.used_gb} Go / ${disk.total_gb} Go`;
    }

    // CPU Temp & Moniteur
    const cpuTempEl = document.getElementById("cpu-temp");
    if (cpuTempEl) cpuTempEl.textContent = `${data.system.cpu_temp} °C`;

    const powerMonEl = document.getElementById("power-mon-status");
    if (powerMonEl) {
        powerMonEl.textContent = data.system.power_monitor ? "Actif" : "Inactif";
        powerMonEl.style.color = data.system.power_monitor ? "var(--accent-green)" : "var(--accent-red)";
    }

    // État Enregistrement Dashcam
    currentRecordingState = data.system.recording;
    const recBadge = document.getElementById("rec-badge");
    const recText = document.getElementById("rec-text");
    const recTitle = document.getElementById("recording-status-title");
    const toggleBtn = document.getElementById("btn-toggle-rec");

    if (currentRecordingState) {
        recBadge.className = "badge badge-rec";
        recText.textContent = "REC";
        recTitle.textContent = "Enregistrement actif";
        recTitle.style.color = "var(--text-primary)";
        if (toggleBtn) {
            toggleBtn.className = "btn btn-warning";
            toggleBtn.textContent = "Arrêter";
        }
    } else {
        recBadge.className = "badge";
        recBadge.style.background = "rgba(100, 116, 139, 0.2)";
        recBadge.style.color = "var(--text-muted)";
        recBadge.style.border = "1px solid var(--border-color)";
        recText.textContent = "STOP";
        recTitle.textContent = "Enregistrement en pause";
        recTitle.style.color = "var(--accent-amber)";
        if (toggleBtn) {
            toggleBtn.className = "btn btn-primary";
            toggleBtn.textContent = "Démarrer";
        }
    }
}

// 3. Basculer l'enregistrement (Start / Stop)
async function toggleRecording() {
    const actionName = currentRecordingState ? "l'arrêt" : "le démarrage";
    if (!confirm(`Confirmer ${actionName} de l'enregistrement vidéo ?`)) return;

    try {
        const res = await fetch("/api/action/toggle-dashcam", { method: "POST" });
        const data = await res.json();
        if (data.success) {
            showToast(`Statut mis à jour : ${data.recording ? "Enregistrement actif" : "En pause"}`);
            fetchStatus();
        } else {
            showToast("Erreur lors de l'action", true);
        }
    } catch (err) {
        showToast("Erreur de connexion", true);
    }
}

// 4. Galerie Vidéos
async function loadVideos() {
    const listEl = document.getElementById("videos-list");
    const countEl = document.getElementById("videos-count");
    if (!listEl) return;

    try {
        const res = await fetch("/api/videos");
        const data = await res.json();

        if (countEl) countEl.textContent = data.count;

        if (data.videos.length === 0) {
            listEl.innerHTML = '<div class="loading-spinner">Aucune vidéo enregistrée pour le moment.</div>';
            return;
        }

        let html = "";
        data.videos.forEach(v => {
            html += `
                <div class="video-item">
                    <div class="video-info">
                        <span class="video-name">${escapeHtml(v.filename)}</span>
                        <span class="video-meta">📅 ${v.mtime} | 📦 ${v.size_mb} Mo</span>
                    </div>
                    <div class="video-actions">
                        <button class="btn btn-secondary btn-sm" onclick="playVideo('${v.url}', '${escapeHtml(v.filename)}')">
                            ▶️ Lire
                        </button>
                        <a href="${v.download_url}" class="btn btn-primary btn-sm" download>
                            ⬇️
                        </a>
                        <button class="btn btn-danger btn-sm" onclick="deleteVideo('${escapeHtml(v.filename)}')">
                            🗑️
                        </button>
                    </div>
                </div>
            `;
        });
        listEl.innerHTML = html;
    } catch (err) {
        listEl.innerHTML = '<div class="loading-spinner">Erreur de chargement des vidéos.</div>';
    }
}

function playVideo(url, title) {
    const modal = document.getElementById("video-player-modal");
    const player = document.getElementById("html5-player");
    const titleEl = document.getElementById("player-title");

    if (modal && player) {
        titleEl.textContent = title;
        player.src = url;
        modal.classList.remove("hidden");
        player.scrollIntoView({ behavior: "smooth" });
        player.play().catch(() => {});
    }
}

function closePlayer() {
    const modal = document.getElementById("video-player-modal");
    const player = document.getElementById("html5-player");
    if (modal && player) {
        player.pause();
        player.src = "";
        modal.classList.add("hidden");
    }
}

async function deleteVideo(filename) {
    if (!confirm(`Supprimer définitivement la vidéo "${filename}" ?`)) return;

    try {
        const res = await fetch(`/api/videos/${encodeURIComponent(filename)}`, { method: "DELETE" });
        const data = await res.json();
        if (data.success) {
            showToast(`Vidéo ${filename} supprimée`);
            loadVideos();
        } else {
            showToast(data.error || "Erreur lors de la suppression", true);
        }
    } catch (err) {
        showToast("Erreur réseau", true);
    }
}

// 5. Cadrage Caméra
function refreshSnapshot() {
    const img = document.getElementById("snapshot-img");
    if (img) {
        img.src = `/api/snapshot?t=${Date.now()}`;
    }
}

function toggleAlignmentGrid() {
    const cb = document.getElementById("toggle-grid");
    const grid = document.getElementById("preview-grid");
    if (grid && cb) {
        grid.style.display = cb.checked ? "block" : "none";
    }
}

// 6. Configuration
async function loadConfig() {
    try {
        const res = await fetch("/api/config");
        const cfg = await res.json();

        const durationEl = document.getElementById("cfg-duration");
        const delayEl = document.getElementById("cfg-delay");
        const diskMaxEl = document.getElementById("cfg-disk-max");
        const resSelect = document.getElementById("cfg-resolution");

        if (durationEl && cfg.SEGMENT_DURATION_SEC) durationEl.value = cfg.SEGMENT_DURATION_SEC;
        if (delayEl && cfg.SHUTDOWN_DELAY_SEC) delayEl.value = cfg.SHUTDOWN_DELAY_SEC;
        if (diskMaxEl && cfg.MAX_DISK_USAGE_PERCENT) diskMaxEl.value = cfg.MAX_DISK_USAGE_PERCENT;

        if (resSelect && cfg.VIDEO_HEIGHT) {
            resSelect.value = cfg.VIDEO_HEIGHT >= 1080 ? "1080p30" : "720p30";
            updateResolutionInputs(resSelect.value);
        }
    } catch (err) {
        console.warn("Impossible de charger la configuration:", err);
    }
}

function updateResolutionInputs(val) {
    const widthEl = document.getElementById("cfg-width");
    const heightEl = document.getElementById("cfg-height");
    const fpsEl = document.getElementById("cfg-fps");

    if (val === "1080p30") {
        if (widthEl) widthEl.value = "1920";
        if (heightEl) heightEl.value = "1080";
        if (fpsEl) fpsEl.value = "30";
    } else {
        if (widthEl) widthEl.value = "1280";
        if (heightEl) heightEl.value = "720";
        if (fpsEl) fpsEl.value = "30";
    }
}

async function saveConfig(event) {
    event.preventDefault();
    const form = document.getElementById("config-form");
    const formData = new FormData(form);

    const payload = {
        SEGMENT_DURATION_SEC: parseInt(formData.get("SEGMENT_DURATION_SEC")),
        VIDEO_WIDTH: parseInt(formData.get("VIDEO_WIDTH")),
        VIDEO_HEIGHT: parseInt(formData.get("VIDEO_HEIGHT")),
        VIDEO_FPS: parseInt(formData.get("VIDEO_FPS")),
        SHUTDOWN_DELAY_SEC: parseInt(formData.get("SHUTDOWN_DELAY_SEC")),
        MAX_DISK_USAGE_PERCENT: parseInt(formData.get("MAX_DISK_USAGE_PERCENT")),
    };

    try {
        const res = await fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) {
            showToast("Réglages enregistrés avec succès !");
        } else {
            showToast(data.error || "Erreur de sauvegarde", true);
        }
    } catch (err) {
        showToast("Erreur de communication", true);
    }
}

async function restartDashcamService() {
    if (!confirm("Redémarrer l'enregistrement vidéo avec les nouveaux réglages ?")) return;
    try {
        const res = await fetch("/api/action/restart-dashcam", { method: "POST" });
        const data = await res.json();
        if (data.success) {
            showToast("Dashcam redémarrée !");
            fetchStatus();
        } else {
            showToast("Erreur lors du redémarrage", true);
        }
    } catch (err) {
        showToast("Erreur réseau", true);
    }
}

// 7. Utilitaires
function showToast(msg, isError = false) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = msg;
    toast.style.borderColor = isError ? "var(--accent-red)" : "var(--accent-blue)";
    toast.classList.remove("hidden");
    setTimeout(() => {
        toast.classList.add("hidden");
    }, 3000);
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

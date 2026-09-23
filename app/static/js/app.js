// T-Pot Cowrie Sensor Deployer - Frontend Application Logic

const BASE_PATH = window.location.pathname.startsWith('/sensors') ? '/sensors' : '';
function apiUrl(path) {
    if (!path.startsWith('/')) {
        path = '/' + path;
    }
    return BASE_PATH + path;
}

let currentConfig = {};
let currentOptions = {};
let availableSensorTypes = {};
let currentActiveMainTab = 'all';
let isPollingActive = false;
let pollingTimer = null;
let pollCounter = 0;
let activeEventSource = null;
let currentHive = {};

// --------------------------------------------------------------------
// Presentation helpers
// --------------------------------------------------------------------
// Sensor icons: one single-color Font Awesome glyph per type (the API's emoji icons are multicolor).
const SENSOR_ICON_CLASS = {
    cowrie: "fa-terminal", dionaea: "fa-bug", conpot: "fa-industry", elasticpot: "fa-magnifying-glass",
    mailoney: "fa-envelope", heralding: "fa-key", ciscoasa: "fa-shield-halved", citrixhoneypot: "fa-globe",
    redishoneypot: "fa-database", sentrypeer: "fa-phone", adbhoney: "fa-mobile-screen", multi_sensor: "fa-layer-group",
};
function sensorIcon(id) {
    return `<i class="fa-solid ${SENSOR_ICON_CLASS[id] || "fa-shield-halved"}"></i>`;
}

// Strip colorful emoji from text we don't control (backend log lines, task messages). The shield and
// rocket are single-color and are kept.
const KEEP_EMOJI = new Set(["\u{1F6E1}\uFE0F", "\u{1F6E1}", "\u{1F680}"]);
const EMOJI_RE = /\p{Extended_Pictographic}\uFE0F?(\u200D\p{Extended_Pictographic}\uFE0F?)*/gu;
function plain(text) {
    return String(text ?? "").replace(EMOJI_RE, m => (KEEP_EMOJI.has(m) ? m : "")).replace(/[ \t]{2,}/g, " ").trim();
}

function portLabel(p) {
    if (p && typeof p === "object") return `${p.port}/${p.proto || "tcp"}`;
    return String(p);
}

// EDL URL as the firewall should use it (through T-Pot's nginx), not the loopback address the page may be on.
function edlUrl() {
    if (window.location.port === "64297") return `${window.location.origin}/edl/sensors.txt`;
    if (currentHive && currentHive.ip) return `https://${currentHive.ip}:64297/edl/sensors.txt`;
    return `${window.location.origin}/edl/sensors.txt`;
}

function fmtWhen(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

// base.html injects this script dynamically, and a dynamically injected script does not hold up
// DOMContentLoaded, so the event may already have fired by the time we run. Handle both orders.
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initApp);
} else {
    initApp();
}

document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
        // Immediately resume fresh polling when user returns to tab
        if (pollingTimer) clearTimeout(pollingTimer);
        runAdaptivePoll();
    }
});

async function initApp() {
    await fetchSensorTypes();
    await fetchStatus();
    await fetchOptions();

    // Parallel initial load of dashboard panels
    await Promise.allSettled([
        loadDroplets(),
        loadSchedules(),
        loadEDLInfo(),
        loadWorkerStatus()
    ]);

    if (window.location.hash === "#workers" || window.location.hash === "#section-workers" || window.location.hash === "#tasks") {
        switchMainTab("tasks");
    } else if (window.location.hash === "#gcp") {
        switchProviderTab("gcp");
    }

    // Start adaptive polling loop (12s interval, non-overlapping)
    if (pollingTimer) clearTimeout(pollingTimer);
    pollingTimer = setTimeout(runAdaptivePoll, 12000);
}

async function runAdaptivePoll() {
    if (isPollingActive) return;
    isPollingActive = true;
    pollCounter++;

    try {
        if (!document.hidden) {
            // 1. Droplets / fleet: poll every cycle
            await loadDroplets();

            // 2. Dedicated RQ workers: every cycle if on tasks or all, else every 36s (3rd cycle)
            if (currentActiveMainTab === 'tasks' || currentActiveMainTab === 'all' || pollCounter % 3 === 0) {
                await loadWorkerStatus();
            }

            // 4. Schedules / Campaigns: every cycle if active tab, else every 36s (3rd cycle)
            if (currentActiveMainTab === 'campaigns' || pollCounter % 3 === 0) {
                await loadSchedules();
            }

            // 5. EDL Info: every cycle if active tab, else every 60s (5th cycle)
            if (currentActiveMainTab === 'edl' || pollCounter % 5 === 0) {
                await loadEDLInfo();
            }
        }
    } catch (e) {
        console.warn("Adaptive poll error:", e);
    } finally {
        isPollingActive = false;
        const nextDelay = document.hidden ? 30000 : 12000;
        if (pollingTimer) clearTimeout(pollingTimer);
        pollingTimer = setTimeout(runAdaptivePoll, nextDelay);
    }
}

// --------------------------------------------------------------------
// Core Data Fetchers
// --------------------------------------------------------------------
async function fetchStatus() {
    try {
        const res = await fetch(apiUrl("/api/status"));
        const data = await res.json();
        currentConfig = data.config || {};

        // Update Hive info
        const hive = data.hive || {};
        currentHive = hive;
        const hiveIpEl = document.getElementById("hive-ip-display");
        if (hiveIpEl) hiveIpEl.innerText = hive.ip || "Not detected";
        const hivePortEl = document.getElementById("hive-port-display");
        if (hivePortEl) hivePortEl.innerText = hive.port || "64294";
        const hiveSensorsCountEl = document.getElementById("hive-sensors-count");
        if (hiveSensorsCountEl) hiveSensorsCountEl.innerText = `${hive.registered_sensors_count || 0}`;
        
        const hiveBadge = document.getElementById("hive-status-badge");
        if (hiveBadge) {
            if (hive.installed) {
                hiveBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center gap-1 sm:gap-1.5 shrink-0";
                hiveBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse shrink-0"></span><span class="hidden sm:inline">Hive Ready (${escapeHtml(hive.ip)})</span><span class="sm:hidden font-mono">Hive ✓</span>`;
            } else {
                hiveBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-amber-500/20 text-amber-400 border border-amber-500/30 flex items-center gap-1 sm:gap-1.5 shrink-0";
                hiveBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-amber-400 shrink-0"></span><span class="hidden sm:inline">Standalone Mode</span><span class="sm:hidden font-mono">Alone</span>`;
            }
        }

        // Update DO connection info
        const doStat = data.digitalocean || {};
        const doBadge = document.getElementById("do-status-badge");
        const tokenMissingBanner = document.getElementById("token-missing-banner");
        const btnDeployMain = document.getElementById("btn-deploy-main");

        if (doStat.configured && doStat.account) {
            if (doBadge) {
                doBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 flex items-center gap-1 sm:gap-1.5 shrink-0";
                doBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-cyan-400 shrink-0"></span><span class="hidden sm:inline">DO Connected (${doStat.account.email})</span><span class="sm:hidden font-mono">DO ✓</span>`;
                doBadge.onclick = null;
            }
            if (tokenMissingBanner) tokenMissingBanner.classList.add("hidden");
            if (btnDeployMain) btnDeployMain.disabled = false;
        } else if (doStat.configured) {
            if (doBadge) {
                doBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 flex items-center gap-1 sm:gap-1.5 cursor-pointer shrink-0";
                doBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-cyan-400 shrink-0"></span><span class="hidden sm:inline">DO Configured (Click to Verify)</span><span class="sm:hidden font-mono">DO Ready</span>`;
                doBadge.onclick = () => navigateTo("/admin");
            }
            if (tokenMissingBanner) tokenMissingBanner.classList.add("hidden");
            if (btnDeployMain) btnDeployMain.disabled = false;
        } else {
            if (doBadge) {
                doBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-rose-500/20 text-rose-400 border border-rose-500/30 flex items-center gap-1 sm:gap-1.5 cursor-pointer shrink-0";
                doBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-400 shrink-0"></span><span class="hidden sm:inline">Missing DO Token (See Admin)</span><span class="sm:hidden font-mono">No DO!</span>`;
                doBadge.onclick = () => navigateTo("/admin");
            }
            if (tokenMissingBanner) tokenMissingBanner.classList.remove("hidden");
            if (btnDeployMain) btnDeployMain.disabled = true;
        }

        // Update settings inputs if empty
        const settingsHiveIp = document.getElementById("settings-hive-ip");
        if (settingsHiveIp) {
            settingsHiveIp.value = hive.ip || "";
        }

    } catch (err) {
        console.error("Error fetching status:", err);
    }
}

async function fetchOptions() {
    try {
        const res = await fetch(apiUrl("/api/options"));
        const data = await res.json();
        currentOptions = data;

        // Render Regions
        const regionSelect = document.getElementById("deploy-region");
        if (regionSelect) {
            regionSelect.innerHTML = "";
            (data.regions || []).forEach(r => {
                const opt = document.createElement("option");
                opt.value = r.slug;
                opt.innerText = `${r.name} (${r.slug.toUpperCase()})`;
                if (r.slug === (currentConfig.region || "nyc1")) opt.selected = true;
                regionSelect.appendChild(opt);
            });
        }

        // Render Sizes
        const sizeSelect = document.getElementById("deploy-size");
        if (sizeSelect) {
            sizeSelect.innerHTML = "";
            (data.sizes || []).forEach(s => {
                const opt = document.createElement("option");
                opt.value = s.slug;
                const price = s.price_monthly ? `$${s.price_monthly}/mo` : "";
                const desc = s.description || `${s.memory / 1024}GB / ${s.vcpus} vCPU`;
                opt.innerText = `${s.slug} - ${desc} (${price})`;
                if (s.slug === (currentConfig.size || "s-1vcpu-2gb")) opt.selected = true;
                sizeSelect.appendChild(opt);
            });
        }

        // SSH key: always the deployer's own key
        const sshInfo = document.getElementById("deploy-ssh-key-info");
        if (sshInfo) {
            sshInfo.innerHTML = data.local_ssh_key
                ? `<i class="fa-solid fa-key mr-2 text-slate-500"></i>${escapeHtml(data.local_ssh_fingerprint || "deployer key")}`
                : `<i class="fa-solid fa-triangle-exclamation mr-2 text-amber-400"></i>No key found: put secrets/ssh_key and secrets/ssh_key.pub next to docker-compose.yml`;
        }

        // Set generated sensor name (same suggestion on both tabs; each has its own field)
        if (data.generated_creds && data.generated_creds.username) {
            const deployName = document.getElementById("deploy-name");
            if (deployName) deployName.value = data.generated_creds.username;
            const gcpName = document.getElementById("gcp-name");
            if (gcpName && !gcpName.value) gcpName.value = data.generated_creds.username;
        }

        // Populate schedule modal region & size if present
        const schedRegion = document.getElementById("sched-region");
        if (schedRegion && (data.regions || []).length > 0) {
            schedRegion.innerHTML = "";
            data.regions.forEach(r => {
                const opt = document.createElement("option");
                opt.value = r.slug;
                opt.innerText = `${r.name} (${r.slug.toUpperCase()})`;
                if (r.slug === "nyc1") opt.selected = true;
                schedRegion.appendChild(opt);
            });
        }
        const schedSize = document.getElementById("sched-size");
        if (schedSize && (data.sizes || []).length > 0) {
            schedSize.innerHTML = "";
            data.sizes.forEach(s => {
                const opt = document.createElement("option");
                opt.value = s.slug;
                opt.innerText = `${s.slug} (${s.description || '1vCPU/2GB'})`;
                if (s.slug === "s-1vcpu-2gb") opt.selected = true;
                schedSize.appendChild(opt);
            });
        }

        // Update Cloud Metadata section in settings modal
        updateMetadataUI(data);

        renderGCPOptions(data.gcp, data.defaults);

    } catch (err) {
        console.error("Error fetching options:", err);
    }
}

function renderGCPOptions(gcp, defaults) {
    gcp = gcp || {};
    defaults = defaults || {};

    // Same deployer key DO uses, injected into GCP instance metadata instead of uploaded - same info,
    // mirrored into the GCP tab's own SSH-key display so the two tabs read the same at a glance.
    const gcpSshInfo = document.getElementById("gcp-ssh-key-info");
    if (gcpSshInfo) {
        gcpSshInfo.innerHTML = !gcp.configured
            ? `<i class="fa-solid fa-triangle-exclamation mr-2 text-amber-400"></i>GCP not configured: set a project id and secrets/gcp-sa.json (see Admin)`
            : (currentOptions && currentOptions.local_ssh_key
                ? `<i class="fa-solid fa-key mr-2 text-slate-500"></i>${escapeHtml(currentOptions.local_ssh_fingerprint || "deployer key")}`
                : `<i class="fa-solid fa-triangle-exclamation mr-2 text-amber-400"></i>No key found: put secrets/ssh_key and secrets/ssh_key.pub next to docker-compose.yml`);
    }

    const zoneSelect = document.getElementById("gcp-zone");
    if (zoneSelect) {
        zoneSelect.innerHTML = "";
        const zones = (gcp.zones || []).length ? gcp.zones : [{ name: defaults.gcp_zone || "us-central1-a" }];
        zones.forEach(z => {
            const opt = document.createElement("option");
            opt.value = z.name;
            opt.innerText = z.name;
            if (z.name === (defaults.gcp_zone || "us-central1-a")) opt.selected = true;
            zoneSelect.appendChild(opt);
        });
    }

    const machineSelect = document.getElementById("gcp-machine-type");
    if (machineSelect) {
        machineSelect.innerHTML = "";
        const types = (gcp.machine_types || []).length ? gcp.machine_types : [{ name: defaults.gcp_machine_type || "e2-small" }];
        types.forEach(t => {
            const opt = document.createElement("option");
            opt.value = t.name;
            opt.innerText = t.memory_mb ? `${t.name} - ${t.vcpus} vCPU / ${Math.round(t.memory_mb / 1024)}GB RAM` : t.name;
            if (t.name === (defaults.gcp_machine_type || "e2-small")) opt.selected = true;
            machineSelect.appendChild(opt);
        });
    }
}

function updateMetadataUI(data) {
    if (!data) return;
    const lastSyncEl = document.getElementById("cloud-metadata-last-sync");
    if (lastSyncEl) {
        if (data.last_synced_at) {
            const date = new Date(data.last_synced_at);
            lastSyncEl.innerText = `Synced: ${date.toLocaleTimeString()} (${date.toLocaleDateString()})`;
        } else {
            lastSyncEl.innerText = "Pre-seeded Defaults";
        }
    }
    const rCount = document.getElementById("meta-count-regions");
    if (rCount) rCount.innerText = (data.regions || []).length;
    const sCount = document.getElementById("meta-count-sizes");
    if (sCount) sCount.innerText = (data.sizes || []).length;
    const iCount = document.getElementById("meta-count-images");
    if (iCount) iCount.innerText = (data.images || []).length;
    const kCount = document.getElementById("meta-count-keys");
    if (kCount) kCount.innerText = (data.ssh_keys || []).length;
}

async function fetchSensorTypes() {
    try {
        const res = await fetch(apiUrl("/api/sensor-types"));
        const data = await res.json();
        availableSensorTypes = data.sensor_types || {};
        Object.entries(availableSensorTypes).forEach(([id, st]) => { st.icon = sensorIcon(id); });
        renderSensorControls();
    } catch (err) {
        console.error("Error fetching sensor types:", err);
    }
}

// A type is deployable unless the API says otherwise (older backends don't send the flag).
function isDeployable(st) { return !st || st.deployable !== false; }

function sensorCardClass(st, active) {
    const base = "sensor-select-card p-3 rounded-xl border transition-all ";
    if (!isDeployable(st)) return base + "border-slate-800 bg-slate-900/20 opacity-50 cursor-not-allowed";
    return base + (active
        ? "border-2 border-cyan-500 bg-cyan-950/20 cursor-pointer hover:bg-cyan-950/30"
        : "border-slate-800 bg-slate-900/40 hover:border-slate-700 cursor-pointer");
}

// Sensor cards and type dropdowns are built from the API so names, ports and availability live in one place.
function renderSensorControls() {
    const types = Object.entries(availableSensorTypes);
    const grid = document.getElementById("sensor-cards-grid");
    if (grid) {
        const current = (document.getElementById("deploy-sensor-type") || {}).value || "cowrie";
        const ordered = types.slice().sort((a, b) => Number(isDeployable(b[1])) - Number(isDeployable(a[1])));
        grid.innerHTML = ordered.map(([id, st]) => {
            const ports = (st.ports || []).slice(0, 2).map(portLabel).join(", ");
            const more = (st.ports || []).length > 2 ? ` +${st.ports.length - 2}` : "";
            const soon = isDeployable(st) ? "" : `<span class="text-[9px] uppercase tracking-wider text-slate-500">not available yet</span>`;
            return `
                <div id="sensor-card-${id}" onclick="selectDeploySensorType('${id}')" class="${sensorCardClass(st, id === current)}"
                     ${isDeployable(st) ? "" : 'data-tip="No playbook for this sensor type yet, so it cannot be deployed."'}>
                    <div class="flex items-center justify-between mb-1 text-slate-300"><span class="text-sm">${st.icon}</span>${soon}</div>
                    <div class="font-bold text-xs text-white">${escapeHtml(st.short_name || id)}</div>
                    <div class="text-[10px] text-slate-500 font-mono mt-0.5">${escapeHtml(ports + more)}</div>
                </div>`;
        }).join("");
    }
    // <select>s of sensor types (campaign modal) - the GCP deploy form reuses the shared
    // #deploy-sensor-type cards grid instead of its own select.
    const fill = (sel, { onlyDeployable }) => {
        if (!sel) return;
        const keep = sel.value;
        sel.innerHTML = types.map(([id, st]) => {
            const off = onlyDeployable && !isDeployable(st);
            return `<option value="${id}" ${off ? "disabled" : ""}>${escapeHtml(st.short_name || id)}${off ? " (not available yet)" : ""}</option>`;
        }).join("");
        if (keep && types.some(([id]) => id === keep)) sel.value = keep;
    };
    fill(document.getElementById("sched-sensor-type"), { onlyDeployable: true });
    if (grid) selectDeploySensorType((document.getElementById("deploy-sensor-type") || {}).value || "cowrie", true);
}

function selectDeploySensorType(sensorType, quiet) {
    // Never select a type that can't be deployed (checked before anything is changed).
    const requested = availableSensorTypes[sensorType];
    if (requested && !isDeployable(requested)) {
        if (!quiet) showNotification(`${requested.short_name || sensorType} has no playbook yet, so it can't be deployed.`, "warn");
        return;
    }
    const hiddenInput = document.getElementById("deploy-sensor-type");
    if (hiddenInput) hiddenInput.value = sensorType;

    const sensorInfo = availableSensorTypes[sensorType] || {
        id: sensorType,
        name: sensorType.toUpperCase(),
        short_name: sensorType.toUpperCase(),
        icon: "🛡️",
        category: "Deception",
        description: "Honeypot sensor",
        ports: ["22/tcp"],
        recommended_size: "s-1vcpu-2gb"
    };

    // 1. Update active card styling
    document.querySelectorAll(".sensor-select-card").forEach(c => {
        const id = c.id.replace("sensor-card-", "");
        c.className = sensorCardClass(availableSensorTypes[id], id === sensorType);
    });

    // 2. Update badge
    const badge = document.getElementById("selected-sensor-badge");
    if (badge) {
        badge.innerHTML = `${sensorInfo.icon} ${escapeHtml(sensorInfo.name)}`;
    }

    // 3. Update profile detail box
    const pIcon = document.getElementById("profile-sensor-icon");
    const pName = document.getElementById("profile-sensor-name");
    const pDesc = document.getElementById("profile-sensor-desc");
    const pPorts = document.getElementById("profile-sensor-ports");

    if (pIcon) pIcon.innerHTML = sensorInfo.icon;
    if (pName) pName.innerText = sensorInfo.name;
    if (pDesc) pDesc.innerText = sensorInfo.tagline || sensorInfo.description || "";

    if (pPorts && sensorInfo.ports) {
        pPorts.innerHTML = sensorInfo.ports.slice(0, 6).map(p => 
            `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 font-mono text-[11px]">${portLabel(p)}</span>`
        ).join("");
        if (sensorInfo.ports.length > 6) {
            pPorts.innerHTML += `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono text-[11px]">+${sensorInfo.ports.length - 6} more</span>`;
        }
    }

    // 4. If multi_sensor, adjust size recommendation - both tabs' size pickers, whichever is present
    const selectByRecommended = (selectId, recSize) => {
        const sel = document.getElementById(selectId);
        if (!sel || !recSize) return;
        for (let opt of sel.options) {
            if (opt.value === recSize) {
                opt.selected = true;
                break;
            }
        }
    };
    selectByRecommended("deploy-size", sensorInfo.recommended_size_do || sensorInfo.recommended_size);
    selectByRecommended("gcp-machine-type", sensorInfo.recommended_size_gcp);

    // 5. Reroll both tabs' sensor names with the sensor prefix (not when just re-rendering the grid)
    if (!quiet) {
        rerollSensorName("deploy-name");
        rerollSensorName("gcp-name");
    }
}

function rerollSensorName(targetId = "deploy-name") {
    const adjectives = ["shadow", "cyber", "iron", "stealth", "ghost", "swift", "rapid", "dark", "frost", "prime", "vivid", "silent", "noble"];
    const nouns = ["badger", "falcon", "beacon", "hound", "warden", "canary", "citadel", "drone", "tracer", "panther", "raven", "sentinel"];
    const rAdj = adjectives[Math.floor(Math.random() * adjectives.length)];
    const rNoun = nouns[Math.floor(Math.random() * nouns.length)];
    const sensorType = document.getElementById("deploy-sensor-type") ? document.getElementById("deploy-sensor-type").value : "cowrie";
    const prefix = sensorType.replace(/_/g, "-");
    const newName = `${prefix}-sensor-${rAdj}-${rNoun}`;
    const target = document.getElementById(targetId);
    if (target) target.value = newName;
}

function updateExposurePorts(droplets) {
    const box = document.getElementById("exposure-ports");
    if (!box) return;
    const ports = new Set();
    droplets.forEach(d => {
        const st = availableSensorTypes[d.sensor_type || "cowrie"];
        ((st && st.ports) || []).forEach(p => ports.add(portLabel(p).replace("/tcp", "")));
    });
    const list = Array.from(ports).slice(0, 8);
    box.innerHTML = list.length
        ? list.map(p => `<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">${escapeHtml(p)}</span>`).join("")
          + (ports.size > 8 ? `<span class="text-slate-500 text-[11px]">+${ports.size - 8}</span>` : "")
        : `<span class="text-slate-500 text-xs">none</span>`;
}

// Quick ping/TCP health check for one sensor (the deep playbook check is available from the API).
async function checkSensorHealth(id, btn) {
    const label = btn ? btn.innerText : "";
    if (btn) { btn.disabled = true; btn.innerText = "Checking..."; }
    try {
        const res = await fetch(apiUrl(`/api/droplets/${id}/health`));
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || "Health check failed");
        const ports = Object.entries(d.honeypot_ports || {});
        const open = ports.filter(([, ok]) => ok).map(([p]) => p);
        const closed = ports.filter(([, ok]) => !ok).map(([p]) => p);
        const msg = `${d.healthy ? "Healthy" : "Not healthy"}: admin SSH ${d.admin_ssh ? "up" : "down"}, ping ${d.ping ? "ok" : "no reply"}`
            + `, ports open: ${open.join(", ") || "none"}${closed.length ? `, closed: ${closed.join(", ")}` : ""}`;
        showNotification(msg, d.healthy ? "success" : "warn");
    } catch (err) {
        showNotification(err.message, "error");
    } finally {
        if (btn) { btn.disabled = false; btn.innerText = label; }
    }
}

function renderDashboardFleet(droplets, totalCount) {
    updateExposurePorts(droplets);
    const dashFleet = document.getElementById("dashboard-fleet-list");
    if (!dashFleet) return;

    if (totalCount === 0) {
        dashFleet.innerHTML = `
            <div class="p-3.5 rounded-lg bg-slate-900/40 border border-slate-800/80 text-center space-y-1">
                <p class="text-xs text-slate-400 font-medium">No sensors yet</p>
                <p class="text-[11px] text-slate-500">Deploy one from the Deploy page.</p>
            </div>
        `;
        return;
    }

    let fleetHtml = '<div class="space-y-2">';
    droplets.slice(0, 4).forEach(d => {
        const pubIp = d.public_ip || "Pending...";
        const dStype = d.sensor_type || "cowrie";
        const dSinfo = availableSensorTypes[dStype] || { icon: "🛡️", short_name: dStype.toUpperCase() };
        const providerBadge = d.provider === "gcp"
            ? `<span class="px-1.5 py-0.5 text-[9px] font-mono rounded bg-red-900/60 text-red-300 font-bold shrink-0">GCP</span>`
            : `<span class="px-1.5 py-0.5 text-[9px] font-mono rounded bg-cyan-950 text-cyan-400 font-bold shrink-0">DO</span>`;
        let ttlText = "No TTL";
        if (d.ttl_info) {
            ttlText = d.ttl_info.is_expired ? "Expired" : `<i class="fa-regular fa-clock"></i> ${d.ttl_info.remaining_formatted}`;
        }
        fleetHtml += `
            <div class="p-2.5 rounded-lg bg-[#090e1a] border border-slate-800 flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="w-2 h-2 rounded-full ${d.status === 'active' ? 'bg-emerald-400' : 'bg-amber-400'} shrink-0"></span>
                    ${providerBadge}
                    <span class="font-bold text-xs text-white truncate">${escapeHtml(d.name)}</span>
                    <span class="text-[10px] text-slate-400 font-mono hidden sm:inline">(${dSinfo.icon} ${dSinfo.short_name})</span>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    <span class="font-mono text-xs text-cyan-300 font-bold">${pubIp}</span>
                    ${pubIp !== 'Pending...' ? `<button onclick="copyToClipboard('${pubIp}')" class="text-slate-500 hover:text-slate-300 text-xs" title="Copy IP"><i class="fa-regular fa-copy"></i></button>` : ''}
                    <span class="px-2 py-0.5 text-[10px] rounded bg-slate-800 text-slate-300 border border-slate-700 font-mono">${ttlText}</span>
                </div>
            </div>
        `;
    });
    if (droplets.length > 4) {
        fleetHtml += `
            <div class="text-center pt-1">
                <a href="#" data-app-path="/fleet" class="text-xs font-mono text-cyan-400 hover:underline">+ ${droplets.length - 4} more sensors in Fleet &rarr;</a>
            </div>
        `;
    }
    fleetHtml += '</div>';
    dashFleet.innerHTML = fleetHtml;
}

// --------------------------------------------------------------------
// Droplet Management & Listing
// --------------------------------------------------------------------
async function loadDroplets() {
    try {
        const res = await fetch(apiUrl("/api/droplets"));
        const data = await res.json();
        const droplets = data.droplets || [];

        const tableBody = document.getElementById("droplets-table-body");
        const cardsList = document.getElementById("droplets-cards-list");
        const emptyState = document.getElementById("droplets-empty-state");
        const countBadge = document.getElementById("active-sensors-count");

        const totalCount = droplets.length;
        if (countBadge) countBadge.innerText = totalCount;

        // Render Dashboard Fleet preview if on Dashboard
        renderDashboardFleet(droplets, totalCount);

        if (totalCount === 0) {
            if (tableBody) tableBody.innerHTML = "";
            if (cardsList) cardsList.innerHTML = "";
            if (emptyState) emptyState.classList.remove("hidden");
            return;
        }

        if (emptyState) emptyState.classList.add("hidden");
        if (tableBody) tableBody.innerHTML = "";
        if (cardsList) cardsList.innerHTML = "";

        // Render each sensor - DO and GCP rows are both ordinary active_droplets rows now,
        // distinguished only by d.provider (see the badge/ssh-user handling below).
        droplets.forEach(d => {
            const tr = document.createElement("tr");
            tr.className = "border-b border-slate-800/80 hover:bg-slate-800/30 transition-colors";

            const pubIp = d.public_ip || "Pending...";
            const statusClass = d.status === "active" ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
                : (d.status === "provision_failed" ? "bg-rose-500/20 text-rose-400 border-rose-500/30" : "bg-amber-500/20 text-amber-400 border-amber-500/30");
            const statusText = (d.status || "").replace(/_/g, " ");
            const dStype = d.sensor_type || "cowrie";
            const dSinfo = availableSensorTypes[dStype] || { icon: d.sensor_icon || "🛡️", short_name: d.sensor_name || dStype.toUpperCase(), ports: ["22/tcp", "23/tcp"] };
            const dPortsList = (dSinfo.ports || ["22/tcp", "23/tcp"]).slice(0, 3).map(portLabel).join(", ");
            const dSize = (d.size && d.size.slug) || d.size_slug || "";
            const isGcp = d.provider === "gcp";
            const providerBadge = isGcp
                ? `<span class="px-1.5 py-0.5 text-[10px] font-mono rounded bg-red-900/60 text-red-300 border border-red-700/60 font-bold">GCP</span>`
                : `<span class="px-1.5 py-0.5 text-[10px] font-mono rounded bg-cyan-950/80 text-cyan-300 border border-cyan-800/60 font-bold">DO</span>`;
            const sshUser = isGcp ? "tpotadmin" : "root";

            // TTL info
            let ttlHtml = `<span class="text-xs text-slate-500">None</span>`;
            if (d.ttl_info) {
                const ttl = d.ttl_info;
                if (ttl.is_expired) {
                    ttlHtml = `<span class="px-2 py-0.5 text-xs rounded bg-rose-500/20 text-rose-400 border border-rose-500/30">Expired</span>`;
                } else {
                    ttlHtml = `
                        <div class="flex items-center gap-1.5">
                            <span class="px-2 py-0.5 text-xs font-mono rounded badge-ttl whitespace-nowrap"><i class="fa-regular fa-clock"></i> ${ttl.remaining_formatted}</span>
                            <button onclick="promptExtendTTL(${d.id})" title="Extend TTL" class="text-xs text-amber-400 hover:text-amber-300">+</button>
                            <button onclick="cancelTTL(${d.id})" title="Cancel Auto-Destroy" class="text-xs text-slate-500 hover:text-slate-300">✕</button>
                        </div>
                    `;
                }
            } else {
                ttlHtml = `
                    <button onclick="promptSetTTL(${d.id})" class="text-xs text-slate-400 hover:text-cyan-400 flex items-center gap-1">
                        <span>+ Set TTL</span>
                    </button>
                `;
            }

            tr.innerHTML = `
                <td class="py-3.5 px-4">
                    <div class="font-medium text-slate-200 flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full ${d.status === 'active' ? 'bg-emerald-400' : 'bg-amber-400'}"></span>
                        ${providerBadge}
                        <span>${escapeHtml(d.name)}</span>
                        ${d.is_scheduled ? `<span class="px-1.5 py-0.5 text-[9px] font-mono rounded bg-cyan-900/50 text-cyan-300 border border-cyan-700/60 font-semibold" title="Managed by Campaign Schedule">SCHEDULED</span>` : ''}
                    </div>
                    <div class="text-xs text-slate-500 mt-0.5">${[d.region ? d.region.slug.toUpperCase() : '', dSize].filter(Boolean).join(' · ')}</div>
                </td>
                <td class="py-3.5 px-4">
                    <span class="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded-lg bg-slate-800 text-slate-200 border border-slate-700">
                        <span>${dSinfo.icon || '🛡️'}</span>
                        <span>${dSinfo.short_name || dStype.toUpperCase()}</span>
                    </span>
                </td>
                <td class="py-3.5 px-4 font-mono text-sm text-cyan-300">
                    <div class="flex items-center gap-2">
                        <span>${pubIp}</span>
                        ${pubIp !== "Pending..." ? `
                            <button onclick="copyToClipboard('${pubIp}')" title="Copy IP" class="text-slate-500 hover:text-slate-300 text-xs">
                                <i class="fa-regular fa-copy"></i>
                            </button>
                        ` : ''}
                    </div>
                </td>
                <td class="py-3.5 px-4">
                    <div class="flex flex-wrap gap-1.5">
                        <span class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-300 border border-slate-700 font-mono">${dPortsList}</span>
                        <span class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-400 border border-slate-700 font-mono">Admin:64295</span>
                    </div>
                </td>
                <td class="py-3.5 px-4">
                    <span class="px-2.5 py-0.5 text-xs rounded-full border ${statusClass}" ${d.status === "provision_failed" ? 'data-tip="Setup failed. The sensor is kept for debugging and is destroyed when its TTL expires."' : ""}>
                        ${statusText}
                    </span>
                </td>
                <td class="py-3.5 px-4">
                    ${ttlHtml}
                </td>
                <td class="py-3.5 px-4 text-right">
                    <div class="flex items-center justify-end gap-2">
                        ${pubIp !== "Pending..." ? `
                            <button onclick="copyToClipboard('ssh -p 64295 ${sshUser}@${pubIp}')" class="px-2.5 py-1 text-xs rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-colors whitespace-nowrap" data-tip="Copies the admin SSH command (port 64295, from the Hive host).">
                                SSH Admin
                            </button>
                            <button onclick="copyToClipboard('${d.test_command || `ssh root@${pubIp} -p 22`}')" class="px-2.5 py-1 text-xs rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-colors" data-tip="Copies a command that pokes the honeypot, to generate a test event.">
                                Test
                            </button>
                            <button onclick="checkSensorHealth(${d.id}, this)" class="px-2.5 py-1 text-xs rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-colors" data-tip="Ping and port check from this host.">
                                Health
                            </button>
                        ` : ''}
                        <button onclick="confirmDestroyDroplet(this.dataset.id, this.dataset.name, this.dataset.provider)" data-id="${d.id}" data-name="${escapeHtml(d.name)}" data-provider="${d.provider || 'digitalocean'}" class="px-2.5 py-1 text-xs rounded bg-rose-950/40 hover:bg-rose-900/60 text-rose-400 border border-rose-800/40 transition-colors">
                            Destroy
                        </button>
                    </div>
                </td>
            `;
            if (tableBody) tableBody.appendChild(tr);

            // Mobile Card for DO Droplet
            if (cardsList) {
                const cardDo = document.createElement("div");
                cardDo.className = "p-4 rounded-xl bg-[#0b101d] border border-slate-800/90 shadow-sm space-y-3";
                
                let mobileTtlHtml = `<span class="text-xs text-slate-500">None</span>`;
                if (d.ttl_info) {
                    const ttl = d.ttl_info;
                    if (ttl.is_expired) {
                        mobileTtlHtml = `<span class="px-2 py-0.5 text-xs rounded bg-rose-500/20 text-rose-400 border border-rose-500/30">Expired</span>`;
                    } else {
                        mobileTtlHtml = `
                            <div class="flex items-center gap-1.5">
                                <span class="px-2 py-0.5 text-xs font-mono rounded badge-ttl whitespace-nowrap"><i class="fa-regular fa-clock"></i> ${ttl.remaining_formatted}</span>
                                <button onclick="promptExtendTTL(${d.id})" title="Extend TTL" class="p-1 px-1.5 text-xs text-amber-400 hover:text-amber-300 bg-slate-800 rounded border border-slate-700">+</button>
                                <button onclick="cancelTTL(${d.id})" title="Cancel Auto-Destroy" class="p-1 px-1.5 text-xs text-slate-400 hover:text-slate-300 bg-slate-800 rounded border border-slate-700">✕</button>
                            </div>
                        `;
                    }
                } else {
                    mobileTtlHtml = `
                        <button onclick="promptSetTTL(${d.id})" class="text-xs text-slate-400 hover:text-cyan-400 flex items-center gap-1 py-1">
                            <span>+ Set TTL</span>
                        </button>
                    `;
                }

                cardDo.innerHTML = `
                    <div class="flex items-start justify-between gap-2">
                        <div class="min-w-0">
                            <div class="flex items-center gap-1.5 flex-wrap">
                                <span class="w-2 h-2 rounded-full ${d.status === 'active' ? 'bg-emerald-400' : 'bg-amber-400'} shrink-0"></span>
                                ${providerBadge}
                                <span class="font-semibold text-slate-200 text-sm truncate">${escapeHtml(d.name)}</span>
                                ${d.is_scheduled ? `<span class="px-1.5 py-0.5 text-[9px] font-mono rounded bg-cyan-900/50 text-cyan-300 border border-cyan-700/60 font-semibold shrink-0">SCHED</span>` : ''}
                            </div>
                            <div class="text-[11px] text-slate-500 mt-0.5 font-mono truncate">
                                ${[d.region ? d.region.slug.toUpperCase() : '', dSize].filter(Boolean).join(' · ')}
                            </div>
                        </div>
                        <span class="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-lg bg-slate-800 text-slate-200 border border-slate-700 shrink-0">
                            <span>${dSinfo.icon || '🛡️'}</span>
                            <span>${dSinfo.short_name || dStype.toUpperCase()}</span>
                        </span>
                    </div>

                    <div class="p-2.5 rounded-lg bg-[#070b14] border border-slate-800/80 space-y-2">
                        <div class="flex items-center justify-between">
                            <span class="text-[10px] font-mono uppercase tracking-wider text-slate-500">Public IP</span>
                            <div class="flex items-center gap-2">
                                <span class="font-mono text-sm font-bold text-cyan-300">${pubIp}</span>
                                ${pubIp !== "Pending..." ? `
                                    <button onclick="copyToClipboard('${pubIp}')" title="Copy IP" class="p-1 px-1.5 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 rounded border border-slate-700">
                                        <i class="fa-regular fa-copy"></i>
                                    </button>
                                ` : ''}
                            </div>
                        </div>
                        <div class="flex items-center justify-between text-xs pt-1 border-t border-slate-800/50">
                            <span class="text-[10px] font-mono uppercase tracking-wider text-slate-500">Honeypot</span>
                            <span class="px-2 py-0.5 text-[11px] rounded bg-slate-800 text-slate-300 border border-slate-700 font-mono">${dPortsList}</span>
                        </div>
                        <div class="flex items-center justify-between text-xs pt-1 border-t border-slate-800/50">
                            <span class="text-[10px] font-mono uppercase tracking-wider text-slate-500">Lifecycle</span>
                            <div>${mobileTtlHtml}</div>
                        </div>
                    </div>

                    <div class="grid ${pubIp !== "Pending..." ? 'grid-cols-3' : 'grid-cols-1'} gap-2 pt-1">
                        ${pubIp !== "Pending..." ? `
                            <button onclick="copyToClipboard('ssh -p 64295 ${sshUser}@${pubIp}')" class="py-2.5 px-2 text-xs font-medium rounded-lg bg-slate-800 active:bg-slate-900 text-slate-200 border border-slate-700 text-center min-h-[40px] flex items-center justify-center">
                                SSH Admin
                            </button>
                            <button onclick="copyToClipboard('${d.test_command || `ssh root@${pubIp} -p 22`}')" class="py-2.5 px-2 text-xs font-medium rounded-lg bg-cyan-950/70 active:bg-cyan-900 text-cyan-300 border border-cyan-800/60 text-center min-h-[40px] flex items-center justify-center">
                                Test Attack
                            </button>
                        ` : ''}
                        <button onclick="confirmDestroyDroplet(this.dataset.id, this.dataset.name, this.dataset.provider)" data-id="${d.id}" data-name="${escapeHtml(d.name)}" data-provider="${d.provider || 'digitalocean'}" class="py-2.5 px-2 text-xs font-medium rounded-lg bg-rose-950/40 active:bg-rose-900/60 text-rose-400 border border-rose-800/40 text-center min-h-[40px] flex items-center justify-center">
                            Destroy
                        </button>
                    </div>
                `;
                cardsList.appendChild(cardDo);
            }
        });

    } catch (err) {
        console.error("Error loading sensors fleet:", err);
    }
}

async function syncDropletsWithCloud() {
    const btn = document.getElementById("btn-sync-droplets");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> <span class="hidden sm:inline">Syncing...</span><span class="sm:hidden">Sync</span>`;
    }

    try {
        const res = await fetch(apiUrl("/api/droplets/sync"), {
            method: "POST"
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Fleet sync failed");

        showNotification(data.message || "Fleet synced successfully!", "success");
        await loadDroplets();
        await loadEDLInfo();
    } catch (err) {
        showNotification(err.message, "error");
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i class="fa-solid fa-cloud-arrow-down"></i> <span class="hidden sm:inline">Sync Cloud Fleet</span><span class="sm:hidden">Sync</span>`;
        }
    }
}

// --------------------------------------------------------------------
// Deployment Workflow & Live Stream
// --------------------------------------------------------------------
async function startDeployment(event) {
    if (event) event.preventDefault();

    const name = document.getElementById("deploy-name").value.trim();
    const region = document.getElementById("deploy-region").value;
    const size = document.getElementById("deploy-size").value;
    const count = parseInt(document.getElementById("deploy-count").value) || 1;
    const ttl = document.getElementById("deploy-ttl").value;
    const attachFw = document.getElementById("deploy-firewall").checked;
    const restrictSsh = document.getElementById("deploy-restrict-ssh").checked;
    const autoRegisterHive = document.getElementById("deploy-auto-register").checked;
    const sensorType = document.getElementById("deploy-sensor-type") ? document.getElementById("deploy-sensor-type").value : "cowrie";

    const payload = {
        name: name || undefined,
        sensor_type: sensorType,
        count: count,
        region: region,
        size: size,
        image: "ubuntu-24-04-x64",
        ttl: ttl || undefined,
        attach_firewall: attachFw,
        restrict_ssh: restrictSsh,
        auto_register_hive: autoRegisterHive
    };

    // Set modal title & open
    const modalTitle = document.getElementById("deploy-modal-title");
    const sensorTitle = (availableSensorTypes[sensorType]?.short_name || sensorType).toUpperCase();
    if (modalTitle) modalTitle.innerText = `Deploying ${sensorTitle} Sensor (DigitalOcean)`;
    openModal("deploy-modal");
    const term = document.getElementById("deploy-terminal");
    term.innerHTML = `<div class="text-slate-500 mb-2">// Initializing deployment pipeline...</div>`;
    document.getElementById("deploy-progress-bar").style.width = "5%";
    document.getElementById("deploy-progress-bar").className = "bg-cyan-500 h-2 rounded-full transition-all duration-300";
    document.getElementById("deploy-status-text").innerText = "Connecting to deployment engine...";
    const closeBtn = document.getElementById("deploy-modal-close-btn");
    const closeText = document.getElementById("deploy-modal-close-text");
    if (closeBtn) closeBtn.classList.remove("hidden");
    if (closeText) closeText.innerText = "Close to Background";

    try {
        const res = await fetch(apiUrl("/api/deploy"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to start deployment");

        const taskId = data.task_id;
        connectDeploymentStream(taskId);

    } catch (err) {
        appendLog(`❌ Error starting deployment: ${err.message}`, "error");
        if (closeText) closeText.innerText = "Close";
    }
}

function closeDeployModal() {
    closeModal('deploy-modal');
    if (activeEventSource) {
        try { activeEventSource.close(); } catch (e) {}
        activeEventSource = null;
    }
    loadWorkerStatus(false);
    showNotification("Task continues running in the background. Check Tasks tab for live progress.", "info");
}

function reopenDeploymentStream(taskId, taskType = "Task", sensorName = "Sensor") {
    const modalTitle = document.getElementById("deploy-modal-title");
    if (modalTitle) modalTitle.innerText = `${taskType}: ${sensorName}`;
    const term = document.getElementById("deploy-terminal");
    term.innerHTML = `<div class="text-slate-500 mb-2">// Reconnecting to task stream [${taskId.slice(0, 8)}]...</div>`;
    document.getElementById("deploy-progress-bar").className = "bg-cyan-500 h-2 rounded-full transition-all duration-300";
    document.getElementById("deploy-progress-bar").style.width = "10%";
    document.getElementById("deploy-status-text").innerText = "Connecting to log stream...";
    const closeBtn = document.getElementById("deploy-modal-close-btn");
    const closeText = document.getElementById("deploy-modal-close-text");
    if (closeBtn) closeBtn.classList.remove("hidden");
    if (closeText) closeText.innerText = "Close to Background";
    openModal("deploy-modal");
    connectDeploymentStream(taskId);
}

function connectDeploymentStream(taskId) {
    if (activeEventSource) {
        activeEventSource.close();
    }

    activeEventSource = new EventSource(apiUrl(`/api/deploy/stream/${taskId}`));

    activeEventSource.addEventListener("log", (e) => {
        try {
            const data = JSON.parse(e.data);
            appendLog(data.message, data.level);

            if (data.percent) {
                document.getElementById("deploy-progress-bar").style.width = `${data.percent}%`;
            }
            // Debug-level lines are raw Ansible output (which contains words like "failed:" for items and
            // retries); they belong in the terminal only, never in the status headline.
            if (data.message && data.level !== "debug") {
                document.getElementById("deploy-status-text").innerText = plain(data.message);
            }
        } catch (err) {}
    });

    activeEventSource.addEventListener("complete", (e) => {
        // Only a terminal status ends the stream; anything else (e.g. "running") is just progress.
        let data = {};
        try { data = JSON.parse(e.data); } catch (err) {}
        if (data.status !== "completed" && data.status !== "failed") return;

        const closeText = document.getElementById("deploy-modal-close-text");
        if (closeText) closeText.innerText = "Close";

        try {
            if (data.status === "completed") {
                document.getElementById("deploy-progress-bar").style.width = "100%";
                document.getElementById("deploy-progress-bar").className = "bg-emerald-500 h-2 rounded-full transition-all duration-300";
                document.getElementById("deploy-status-text").innerText = "Operation Complete!";
                appendLog("✅ Deployment pipeline finished successfully.", "success");
            } else {
                document.getElementById("deploy-progress-bar").className = "bg-rose-500 h-2 rounded-full transition-all duration-300";
                document.getElementById("deploy-status-text").innerText = "Operation Failed";
                if (data.error) appendLog(`❌ ${data.error}`, "error");
            }
        } catch (err) {}

        activeEventSource.close();
        activeEventSource = null;
        loadDroplets();
        loadEDLInfo();
        fetchStatus();
        rerollSensorName();
        loadWorkerStatus(false);
    });

    activeEventSource.onerror = () => {
        appendLog("⚠️ Event stream disconnected.", "warn");
        const closeText = document.getElementById("deploy-modal-close-text");
        if (closeText) closeText.innerText = "Close";
        activeEventSource.close();
        activeEventSource = null;
        loadDroplets();
        loadEDLInfo();
        loadWorkerStatus(false);
    };
}

function appendLog(text, level = "info") {
    const term = document.getElementById("deploy-terminal");
    const div = document.createElement("div");
    div.className = "py-0.5 leading-relaxed font-mono text-xs";

    const timestamp = new Date().toLocaleTimeString();

    if (level === "error") {
        div.className += " text-rose-400";
    } else if (level === "warn") {
        div.className += " text-amber-300";
    } else if (level === "success") {
        div.className += " text-emerald-400 font-semibold";
    } else if (level === "debug") {
        div.className += " text-slate-500";
    } else {
        div.className += " text-slate-300";
    }

    const levelIcon = { error: "fa-xmark", warn: "fa-triangle-exclamation", success: "fa-check" }[level];
    const mark = levelIcon ? `<i class="fa-solid ${levelIcon} w-3 inline-block text-center"></i> ` : "";
    div.innerHTML = `<span class="text-slate-600 select-none">[${timestamp}]</span> ${mark}${escapeHtml(plain(text))}`;
    term.appendChild(div);
    term.scrollTop = term.scrollHeight;
}

// --------------------------------------------------------------------
// Sensor Fleet Destruction Handlers
// --------------------------------------------------------------------
let sensorToDestroy = null;

function confirmDestroyDroplet(id, name, provider) {
    // Same destroy path for both providers now: DELETE /api/droplets/{id} looks the row's provider up
    // itself and dispatches to the right cloud client.
    sensorToDestroy = { id, name };
    const header = document.getElementById("destroy-modal-header");
    if (header) header.innerText = provider === "gcp" ? "Destroy GCP Sensor" : "Destroy DigitalOcean Droplet";
    document.getElementById("destroy-droplet-name").innerText = name;
    document.getElementById("destroy-deregister-hive").checked = true;
    openModal("destroy-modal");
}

async function executeDestroy() {
    if (!sensorToDestroy) return;
    const deregister = document.getElementById("destroy-deregister-hive").checked;
    closeModal("destroy-modal");

    try {
        const url = apiUrl(`/api/droplets/${sensorToDestroy.id}?deregister_hive=${deregister}&sensor_name=${encodeURIComponent(sensorToDestroy.name)}`);
        const res = await fetch(url, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to destroy droplet");

        showNotification(data.message || "Droplet destroyed successfully", "success");
        await loadDroplets();
        await fetchStatus();
        await loadEDLInfo();
    } catch (err) {
        showNotification(err.message, "error");
    } finally {
        sensorToDestroy = null;
    }
}

async function cancelTTL(id) {
    if (!confirm("Cancel TTL auto-destroy for this droplet?")) return;
    try {
        const res = await fetch(apiUrl(`/api/droplets/${id}/ttl`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cancel: true })
        });
        if (res.ok) {
            showNotification("TTL auto-destroy cancelled.", "info");
            loadDroplets();
        }
    } catch (err) {
        showNotification("Failed to cancel TTL", "error");
    }
}

async function promptExtendTTL(id) {
    const extra = prompt("Enter additional TTL to add (e.g. 1h, 2h, 1d):", "2h");
    if (!extra) return;
    try {
        const res = await fetch(apiUrl(`/api/droplets/${id}/ttl`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ttl: extra })
        });
        if (res.ok) {
            showNotification(`TTL extended by ${extra}`, "success");
            loadDroplets();
        }
    } catch (err) {
        showNotification("Failed to extend TTL", "error");
    }
}

async function promptSetTTL(id) {
    const ttl = prompt("Enter TTL auto-destroy duration (e.g. 1h, 2h, 1d):", "2h");
    if (!ttl) return;
    try {
        const res = await fetch(apiUrl(`/api/droplets/${id}/ttl`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ttl })
        });
        if (res.ok) {
            showNotification(`TTL set to ${ttl}`, "success");
            loadDroplets();
        }
    } catch (err) {
        showNotification("Failed to set TTL", "error");
    }
}

// --------------------------------------------------------------------
// Settings & Tests
// --------------------------------------------------------------------
async function saveSettings(event) {
    if (event) event.preventDefault();

    const hiveIp = document.getElementById("settings-hive-ip").value.trim();

    const payload = {};
    if (hiveIp) payload.hive_ip = hiveIp;

    try {
        const res = await fetch(apiUrl("/api/settings"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error("Failed to save settings");

        showNotification("Settings saved successfully!", "success");
        closeModal("settings-modal");
        await fetchStatus();
        await fetchOptions();
        await loadDroplets();
    } catch (err) {
        showNotification(err.message, "error");
    }
}

async function testHiveConnection() {
    const ip = document.getElementById("settings-hive-ip").value.trim();
    const resultBox = document.getElementById("hive-test-result");
    resultBox.classList.remove("hidden");
    resultBox.className = "mt-2 p-2.5 rounded text-xs bg-slate-800 text-slate-300";
    resultBox.innerText = "Testing TLS connection to Hive port 64294...";

    try {
        const res = await fetch(apiUrl("/api/test/hive"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ip, port: 64294 })
        });
        const data = await res.json();

        if (data.reachable && data.tls_handshake) {
            resultBox.className = "mt-2 p-2.5 rounded text-xs bg-emerald-950/50 text-emerald-300 border border-emerald-800/60";
            resultBox.innerHTML = `<i class="fa-solid fa-check"></i> Hive reachable. TLS handshake succeeded on port 64294 (${data.message})`;
        } else {
            resultBox.className = "mt-2 p-2.5 rounded text-xs bg-amber-950/50 text-amber-300 border border-amber-800/60";
            resultBox.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> ${data.message}`;
        }
    } catch (err) {
        resultBox.className = "mt-2 p-2.5 rounded text-xs bg-rose-950/50 text-rose-300 border border-rose-800/60";
        resultBox.innerHTML = `<i class="fa-solid fa-xmark"></i> Connection test failed: ${err.message}`;
    }
}

async function syncCloudMetadata() {
    const btn = document.getElementById("btn-sync-cloud-meta");
    const statusEl = document.getElementById("cloud-meta-sync-status");
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Syncing...`;
    }
    if (statusEl) {
        statusEl.classList.remove("hidden");
        statusEl.className = "text-[11px] text-cyan-400 font-mono";
        statusEl.innerText = "Querying DigitalOcean API...";
    }

    try {
        const res = await fetch(apiUrl("/api/cloud/sync"), {
            method: "POST"
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Metadata sync failed");

        showNotification(data.message || "Cloud metadata synced successfully!", "success");
        if (statusEl) {
            statusEl.className = "text-[11px] text-emerald-400 font-mono";
            statusEl.innerText = "Synced successfully!";
        }
        await fetchOptions();
    } catch (err) {
        showNotification(err.message, "error");
        if (statusEl) {
            statusEl.className = "text-[11px] text-rose-400 font-mono";
            statusEl.innerText = err.message;
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i class="fa-solid fa-cloud-arrow-down"></i> Sync Options from DigitalOcean`;
        }
    }
}

// --------------------------------------------------------------------
// Modal & Utility Helpers
// --------------------------------------------------------------------
function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.remove("hidden");
        modal.classList.add("flex");
    }
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.add("hidden");
        modal.classList.remove("flex");
    }
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showNotification(`Copied to clipboard: ${text}`, "info");
    }).catch(() => {
        showNotification("Failed to copy", "error");
    });
}

function showNotification(msg, type = "info") {
    const toast = document.createElement("div");
    let bg = "bg-slate-800 border-slate-700 text-slate-200";
    if (type === "success") bg = "bg-emerald-900/90 border-emerald-700 text-emerald-200";
    if (type === "error") bg = "bg-rose-900/90 border-rose-700 text-rose-200";
    if (type === "warn") bg = "bg-amber-900/90 border-amber-700 text-amber-200";

    toast.className = `fixed bottom-5 right-5 z-50 px-4 py-3 rounded-lg border shadow-xl text-sm transition-all duration-300 transform translate-y-2 opacity-0 ${bg}`;
    toast.innerText = plain(msg);
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.classList.remove("translate-y-2", "opacity-0");
    }, 10);

    setTimeout(() => {
        toast.classList.add("opacity-0", "translate-y-2");
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

function escapeHtml(str) {
    // Full entity escaping (incl. quotes): safe as HTML text content AND as an HTML attribute value.
    // Not safe to then embed inside a JS string literal in an inline onclick="..." - the browser
    // decodes entities before treating the attribute as JS, so a bare quote would still break out.
    // For that, use data-* attributes (escaped with this) and read them back via el.dataset in the handler.
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// --------------------------------------------------------------------
// Palo Alto Networks EDL Feed Handlers
// --------------------------------------------------------------------
async function loadEDLInfo() {
    try {
        const res = await fetch(apiUrl("/api/edl"));
        const data = await res.json();

        // The URL the firewall should use (through T-Pot's nginx), not necessarily this page's address
        const fullUrl = edlUrl();
        const urlInput = document.getElementById("edl-full-url");
        if (urlInput) {
            urlInput.value = fullUrl;
        }

        const ipCountElem = document.getElementById("edl-ip-count");
        if (ipCountElem) {
            ipCountElem.innerText = data.total_ips || 0;
        }

        const previewCountElem = document.getElementById("edl-preview-count");
        if (previewCountElem) {
            previewCountElem.innerText = data.total_ips || 0;
        }

        const previewBox = document.getElementById("edl-preview-box");
        if (previewBox) {
            previewBox.innerText = data.raw_preview || "# No active sensor IPs";
        }

        const staticBox = document.getElementById("edl-static-list");
        if (staticBox) {
            const statics = (data.entries || []).filter(e => (e.source || "").startsWith("Static"));
            staticBox.innerHTML = statics.length ? statics.map(e => `
                <div class="flex items-center justify-between gap-2 p-2 rounded bg-slate-900/60 border border-slate-800 font-mono">
                    <span class="text-slate-200">${escapeHtml(e.ip)}</span>
                    <span class="text-slate-500 truncate flex-1 text-right">${escapeHtml(e.name && e.name !== "Manual Entry" ? e.name : "")}</span>
                    <button onclick="openStaticIPModal(this.dataset.ip, this.dataset.comment)" data-ip="${escapeHtml(e.ip)}" data-comment="${escapeHtml(e.name && e.name !== "Manual Entry" ? e.name : "")}" class="px-2 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-300 hover:bg-slate-700">Edit</button>
                    <button onclick="removeStaticIP('${escapeHtml(e.ip)}')" class="px-2 py-0.5 rounded bg-rose-950/40 border border-rose-800/40 text-rose-400 hover:bg-rose-900/60">Remove</button>
                </div>`).join("") : `<span class="text-slate-500">None.</span>`;
        }
    } catch (err) {
        console.error("Error loading EDL info:", err);
    }
}

async function removeStaticIP(ip) {
    if (!confirm(`Remove ${ip} from the firewall feed? The firewall will stop admitting it on its next refresh.`)) return;
    try {
        const res = await fetch(apiUrl("/api/edl/static"), {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ip })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Could not remove the entry");
        showNotification(`Removed ${ip}`, "success");
        loadEDLInfo();
    } catch (err) {
        showNotification(err.message, "error");
    }
}

function copyEDLUrl() {
    copyToClipboard(edlUrl());
}

let editingStaticIP = null;

// Open the static-entry dialog: empty to add, or filled in to edit an existing entry.
function openStaticIPModal(ip, comment) {
    editingStaticIP = ip || null;
    document.getElementById("static-ip-input").value = ip || "";
    document.getElementById("static-ip-comment").value = comment || "";
    document.getElementById("static-ip-title").textContent = ip ? "Edit static entry" : "Add static IP to the EDL";
    document.getElementById("static-ip-submit").textContent = ip ? "Save changes" : "Add to EDL Feed";
    openModal("static-ip-modal");
}

async function addStaticIP(event) {
    if (event) event.preventDefault();
    const ip = document.getElementById("static-ip-input").value.trim();
    const comment = document.getElementById("static-ip-comment").value.trim();

    if (!ip) return;

    try {
        const res = await fetch(apiUrl("/api/edl/static"), {
            method: editingStaticIP ? "PUT" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(editingStaticIP ? { old_ip: editingStaticIP, ip, comment } : { ip, comment })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to add static IP");

        showNotification(data.message || "Added static IP to EDL", "success");
        closeModal("static-ip-modal");
        document.getElementById("static-ip-input").value = "";
        document.getElementById("static-ip-comment").value = "";
        editingStaticIP = null;
        await loadEDLInfo();
    } catch (err) {
        showNotification(err.message, "error");
    }
}

// --------------------------------------------------------------------
// GCP Terraform Handlers
// --------------------------------------------------------------------
function switchProviderTab(tab) {
    const doPanel = document.getElementById("provider-panel-do");
    const gcpPanel = document.getElementById("provider-panel-gcp");
    const doBtn = document.getElementById("tab-btn-do");
    const gcpBtn = document.getElementById("tab-btn-gcp");

    if (!doPanel || !gcpPanel) return;

    if (tab === "gcp") {
        doPanel.classList.add("hidden");
        gcpPanel.classList.remove("hidden");

        if (doBtn) doBtn.className = "px-4 py-2 text-xs font-bold rounded-t-lg border-b-2 border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/40 flex items-center gap-2 transition-all";
        if (gcpBtn) gcpBtn.className = "px-4 py-2 text-xs font-bold rounded-t-lg border-b-2 border-red-400 bg-slate-800/80 text-red-300 flex items-center gap-2 transition-all";
    } else {
        gcpPanel.classList.add("hidden");
        doPanel.classList.remove("hidden");

        if (doBtn) doBtn.className = "px-4 py-2 text-xs font-bold rounded-t-lg border-b-2 border-cyan-400 bg-slate-800/80 text-cyan-300 flex items-center gap-2 transition-all";
        if (gcpBtn) gcpBtn.className = "px-4 py-2 text-xs font-bold rounded-t-lg border-b-2 border-transparent text-slate-400 hover:text-slate-200 hover:bg-slate-800/40 flex items-center gap-2 transition-all";
    }
}

async function startGCPDeployment(event) {
    if (event) event.preventDefault();

    const sensorType = document.getElementById("deploy-sensor-type").value || "cowrie";
    const name = document.getElementById("gcp-name").value.trim() || undefined;
    const zone = document.getElementById("gcp-zone").value;
    const machineType = document.getElementById("gcp-machine-type").value;
    const ttl = document.getElementById("gcp-ttl").value;
    const count = parseInt(document.getElementById("gcp-count").value) || 1;
    const autoRegister = document.getElementById("gcp-auto-register").checked;
    const attachFirewall = document.getElementById("gcp-firewall").checked;
    const restrictSsh = document.getElementById("gcp-restrict-ssh").checked;

    const payload = {
        provider: "gcp",
        name: name,
        sensor_type: sensorType,
        count: count,
        region: zone,
        size: machineType,
        ttl: ttl,
        attach_firewall: attachFirewall,
        restrict_ssh: restrictSsh,
        auto_register_hive: autoRegister,
    };

    const modalTitle = document.getElementById("deploy-modal-title");
    const sensorTitle = (availableSensorTypes[sensorType]?.short_name || sensorType).toUpperCase();
    if (modalTitle) modalTitle.innerText = `Deploying ${sensorTitle} Sensor on GCP`;
    openModal("deploy-modal");

    const term = document.getElementById("deploy-terminal");
    term.innerHTML = `<div class="text-slate-500 mb-2">// Initializing GCP deployment...</div>`;
    document.getElementById("deploy-progress-bar").style.width = "5%";
    document.getElementById("deploy-progress-bar").className = "bg-red-500 h-2 rounded-full transition-all duration-300";
    document.getElementById("deploy-status-text").innerText = "Creating instance...";
    const closeBtn = document.getElementById("deploy-modal-close-btn");
    const closeText = document.getElementById("deploy-modal-close-text");
    if (closeBtn) closeBtn.classList.remove("hidden");
    if (closeText) closeText.innerText = "Close to Background";

    try {
        const res = await fetch(apiUrl("/api/deploy"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to start GCP deployment");

        connectDeploymentStream(data.task_id);
    } catch (err) {
        appendLog(`❌ Error starting deployment: ${err.message}`, "error");
        if (closeText) closeText.innerText = "Close";
    }
}

/* GCP deploy is handled by startGCPDeployment() above (posts to the unified /api/deploy, same as DO).
   Destroy is handled by the same confirmDestroyDroplet()/executeDestroy() path as DO sensors, since
   both providers' sensors are now ordinary rows behind DELETE /api/droplets/{id}. */

// --------------------------------------------------------------------
// Automated Honeypot Schedules Handlers
// --------------------------------------------------------------------
let allSchedules = [];

function renderDashboardSchedules(schedules) {
    const dashSched = document.getElementById("dashboard-schedules-summary");
    if (!dashSched) return;

    if (!schedules || schedules.length === 0) {
        dashSched.innerHTML = `
            <div class="p-3.5 rounded-lg bg-slate-900/40 border border-slate-800/80 text-center space-y-1">
                <p class="text-xs text-slate-400 font-medium">No campaigns yet</p>
                <p class="text-[11px] text-slate-500">Rebuild sensors on fresh IPs on a schedule.</p>
            </div>
        `;
        return;
    }

    let schedHtml = '<div class="space-y-2">';
    schedules.slice(0, 3).forEach(s => {
        const state = s.state || {};
        const sType = s.sensor_type || (s.config && s.config.sensor_type) || "cowrie";
        const sInfo = availableSensorTypes[sType] || { icon: "🛡️", short_name: sType.toUpperCase() };
        let statusColor = "text-slate-400";
        let statusLabel = state.status || "idle";
        if (state.status === "active") {
            statusColor = "text-emerald-400";
            statusLabel = `Active #${state.cycle_number || 1} (${state.remaining_formatted || ''})`;
        } else if (state.status === "cooling_down") {
            statusColor = "text-cyan-400";
            statusLabel = `Cooldown (${state.remaining_formatted || ''})`;
        } else if (state.status === "provisioning") {
            statusColor = "text-amber-300";
            statusLabel = "Provisioning...";
        } else if (state.status === "suspended") {
            statusColor = "text-rose-400";
            statusLabel = "Suspended";
        }
        schedHtml += `
            <div class="p-2.5 rounded-lg bg-[#090d16] border border-slate-800 flex items-center justify-between gap-2">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="text-sm">${sInfo.icon}</span>
                    <span class="font-bold text-xs text-white truncate">${escapeHtml(s.name)}</span>
                    <span class="text-[10px] text-slate-500 font-mono uppercase">(${s.timing ? s.timing.mode : 'recurring'})</span>
                </div>
                <span class="text-xs font-mono font-semibold ${statusColor}">${statusLabel}</span>
            </div>
        `;
    });
    if (schedules.length > 3) {
        schedHtml += `
            <div class="text-center pt-1">
                <a href="#" data-app-path="/campaigns" class="text-xs font-mono text-cyan-400 hover:underline">+ ${schedules.length - 3} more campaigns &rarr;</a>
            </div>
        `;
    }
    schedHtml += '</div>';
    dashSched.innerHTML = schedHtml;
}

async function loadSchedules() {
    try {
        const res = await fetch(apiUrl("/api/schedules"));
        const data = await res.json();
        allSchedules = data.schedules || [];

        // Render Dashboard Schedules preview if present
        renderDashboardSchedules(allSchedules);

        const listElem = document.getElementById("schedules-list");
        const emptyElem = document.getElementById("schedules-empty-state");

        if (!listElem) return;

        if (allSchedules.length === 0) {
            listElem.innerHTML = "";
            if (emptyElem) emptyElem.classList.remove("hidden");
            return;
        }

        if (emptyElem) emptyElem.classList.add("hidden");
        listElem.innerHTML = "";

        allSchedules.forEach(s => {
            const state = s.state || {};
            const timing = s.timing || {};
            const config = s.config || {};
            const isPaused = !s.enabled;

            let statusBadge = "";
            let timerText = "";
            let cardBorder = "border-slate-800";
            let bgGlow = "bg-[#090d16]";

            if (isPaused) {
                statusBadge = `<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-slate-800 text-slate-400 border border-slate-700">Paused</span>`;
                timerText = `<span class="text-slate-500">Execution paused</span>`;
            } else if (state.status === "active") {
                statusBadge = `<span class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> Active Cycle #${state.cycle_number || 1}</span>`;
                timerText = `<span class="text-emerald-400 font-mono text-xs"><i class="fa-regular fa-clock"></i> Teardown in ${state.remaining_formatted || '0m'}</span>`;
                cardBorder = "border-emerald-500/30";
                bgGlow = "bg-emerald-950/5";
            } else if (state.status === "cooling_down") {
                statusBadge = `<span class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-cyan-400"></span> Cooldown / Sleep</span>`;
                timerText = `<span class="text-cyan-400 font-mono text-xs"><i class="fa-solid fa-hourglass-half"></i> Fresh Rebuild in ${state.remaining_formatted || '0m'}</span>`;
                cardBorder = "border-cyan-500/20";
                bgGlow = "bg-cyan-950/5";
            } else if (state.status === "provisioning") {
                statusBadge = `<span class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30 flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span> Provisioning...</span>`;
                timerText = `<span class="text-amber-300 font-mono text-xs">🚀 Launching cloud instance & dynamic IP</span>`;
                cardBorder = "border-amber-500/40";
                bgGlow = "bg-amber-950/15";
            } else if (state.status === "tearing_down") {
                statusBadge = `<span class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-orange-500/20 text-orange-300 border border-orange-500/30 flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-orange-400 animate-pulse"></span> Tearing Down...</span>`;
                timerText = `<span class="text-orange-400 font-mono text-xs"><i class="fa-solid fa-broom"></i> Releasing IP and credentials</span>`;
                cardBorder = "border-orange-500/30";
                bgGlow = "bg-orange-950/10";
            } else if (state.status === "suspended") {
                statusBadge = `<span class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-rose-500/20 text-rose-400 border border-rose-500/40 flex items-center gap-1.5">⚠️ Suspended (Circuit Breaker)</span>`;
                timerText = `<span class="text-rose-400 text-xs">${escapeHtml(state.last_message || 'Circuit breaker tripped')}</span>`;
                cardBorder = "border-rose-500/30";
                bgGlow = "bg-rose-950/10";
            } else {
                statusBadge = `<span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-500/20 text-amber-400 border border-amber-500/30">${state.status || 'Pending'}</span>`;
                timerText = `<span class="text-slate-400 text-xs">${escapeHtml(state.last_message || '')}</span>`;
            }

            // Mode description
            let modeBadge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-slate-800 text-slate-300 border border-slate-700 uppercase">${timing.mode || 'interval'}</span>`;
            if (timing.mode === "weekly") {
                modeBadge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-purple-950/80 text-purple-300 border border-purple-800/60 font-semibold uppercase">Weekly Cycle</span>`;
            } else if (timing.mode === "random_window") {
                modeBadge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-amber-950/80 text-amber-300 border border-amber-800/60 font-semibold uppercase">Random 48-72h Window</span>`;
            }

            const sType = s.sensor_type || config.sensor_type || "cowrie";
            const sInfo = availableSensorTypes[sType] || { icon: "🛡️", short_name: sType.toUpperCase() };

            const activeIp = state.current_public_ip;
            const card = document.createElement("div");
            card.className = `p-4 rounded-xl ${bgGlow} border ${cardBorder} shadow-md space-y-3.5 transition-all`;

            card.innerHTML = `
                <div class="flex items-start justify-between gap-2">
                    <div>
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-bold text-sm text-slate-100">${escapeHtml(s.name)}</span>
                            <span class="px-2 py-0.5 text-[10px] font-mono rounded bg-slate-800 text-cyan-300 border border-slate-700">${sInfo.icon || '🛡️'} ${(sInfo.short_name || sType).toUpperCase()}</span>
                            ${modeBadge}
                        </div>
                        <p class="text-xs text-slate-400 mt-1 leading-snug">${escapeHtml(s.description || 'Automated ephemeral honeypot campaign')}</p>
                    </div>
                    <div>${statusBadge}</div>
                </div>

                <!-- Middle Status Block -->
                <div class="p-3 rounded-lg bg-[#070b13] border border-slate-800/80 space-y-2 text-xs font-mono">
                    <div class="flex items-center justify-between">
                        <span class="text-slate-500">Dynamic Public IP:</span>
                        ${activeIp ? `
                            <div class="flex items-center gap-1.5 text-cyan-300 font-bold">
                                <span>${activeIp}</span>
                                <button onclick="copyToClipboard('${activeIp}')" title="Copy IP" class="text-slate-500 hover:text-slate-300 text-xs"><i class="fa-regular fa-copy"></i></button>
                            </div>
                        ` : `
                            <span class="text-slate-500 italic">Destroyed (IP Released)</span>
                        `}
                    </div>
                    <div class="flex items-center justify-between text-[11px]">
                        <span class="text-slate-500">Lifespan / Active:</span>
                        <span class="text-slate-300">${timing.active_duration || '24h'} per cycle</span>
                    </div>
                    <div class="flex items-center justify-between text-[11px]">
                        <span class="text-slate-500">Next Scheduled Event:</span>
                        <div>${timerText}</div>
                    </div>
                    ${config.rotate_regions ? `
                        <div class="flex items-center justify-between text-[11px] pt-1 border-t border-slate-800/60">
                            <span class="text-slate-500">Geographic Rotation:</span>
                            <span class="text-amber-400">Active (Multi-Region)</span>
                        </div>
                    ` : ''}
                </div>

                <!-- Footer Control Buttons -->
                <div class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-2.5 pt-2 border-t border-slate-800/60 text-xs">
                    <div class="text-[11px] font-mono text-slate-500">
                        Completed: <b class="text-slate-300">${state.cycle_number || 0} cycles</b>
                    </div>
                    <div class="flex items-center gap-1.5">
                        <button onclick="triggerScheduleAction('${s.id}')" class="flex-1 sm:flex-none px-3 py-2 sm:py-1 text-xs rounded bg-slate-800 hover:bg-slate-700 active:bg-slate-900 text-cyan-300 border border-slate-700 transition-colors text-center min-h-[36px] sm:min-h-0" title="${state.status === 'active' ? 'Force Teardown and Enter Cooldown' : 'Force Deploy Now'}">
                            ${state.status === 'active' ? '<i class="fa-solid fa-bolt"></i> Teardown Now' : '<i class="fa-solid fa-bolt"></i> Deploy Now'}
                        </button>
                        ${s.enabled ? `
                            <button onclick="pauseSchedule('${s.id}')" class="flex-1 sm:flex-none px-3 py-2 sm:py-1 text-xs rounded bg-slate-800 hover:bg-slate-700 active:bg-slate-900 text-slate-300 border border-slate-700 transition-colors text-center min-h-[36px] sm:min-h-0" title="Pause schedule">
                                Pause
                            </button>
                        ` : `
                            <button onclick="resumeSchedule('${s.id}')" class="flex-1 sm:flex-none px-3 py-2 sm:py-1 text-xs rounded bg-slate-800 hover:bg-slate-700 active:bg-slate-900 text-emerald-400 border border-slate-700 transition-colors text-center min-h-[36px] sm:min-h-0" title="Resume schedule">
                                Resume
                            </button>
                        `}
                        <button onclick="deleteScheduleConfirm('${s.id}', this.dataset.name, ${Boolean(state.current_droplet_id)})" data-name="${escapeHtml(s.name)}" class="px-2.5 py-2 sm:py-1 text-xs rounded bg-rose-950/40 hover:bg-rose-900/60 text-rose-400 border border-rose-800/40 transition-colors min-h-[36px] sm:min-h-0 flex items-center justify-center shrink-0" title="Delete Schedule">
                            <i class="fa-solid fa-trash-can"></i>
                        </button>
                    </div>
                </div>
            `;
            listElem.appendChild(card);
        });

    } catch (err) {
        console.error("Error loading schedules:", err);
    }
}

function selectSchedulePreset(presetId) {
    document.getElementById("sched-preset-id").value = presetId;

    const presetKeys = ["weekly", "shift", "dionaea", "conpot", "elasticpot", "custom"];
    presetKeys.forEach(k => {
        const btn = document.getElementById(`preset-btn-${k}`);
        if (btn) {
            btn.className = "p-3 text-left rounded-lg border border-slate-800 bg-slate-900/40 hover:border-slate-700 transition-all";
            const icon = btn.querySelector("i");
            if (icon) icon.className = "fa-solid fa-circle text-slate-600 text-xs";
        }
    });

    const activeDurInput = document.getElementById("sched-active-dur");
    const modeSelect = document.getElementById("sched-mode");
    const nameInput = document.getElementById("sched-name");
    const rotateCheck = document.getElementById("sched-rotate-regions");
    const sensorTypeSelect = document.getElementById("sched-sensor-type");

    function setActiveBtn(btnId, borderColor, textColor) {
        const b = document.getElementById(btnId);
        if (b) {
            b.className = `p-3 text-left rounded-lg border-2 ${borderColor} transition-all`;
            const icon = b.querySelector("i");
            if (icon) icon.className = `fa-solid fa-circle-check ${textColor} text-xs`;
        }
    }

    if (presetId === "weekly_24h") {
        setActiveBtn("preset-btn-weekly", "border-cyan-500 bg-cyan-950/20", "text-cyan-400");
        nameInput.value = "Weekly 24h Deception Trap";
        activeDurInput.value = "24h";
        modeSelect.value = "weekly";
        rotateCheck.checked = false;
        if (sensorTypeSelect) sensorTypeSelect.value = "cowrie";
    } else if (presetId === "shift_8h_random") {
        setActiveBtn("preset-btn-shift", "border-amber-500 bg-amber-950/20", "text-amber-400");
        nameInput.value = "8h Shift / 48-72h Randomized Rebuild";
        activeDurInput.value = "8h";
        modeSelect.value = "random_window";
        if (document.getElementById("sched-cooldown-min")) document.getElementById("sched-cooldown-min").value = "48h";
        if (document.getElementById("sched-cooldown-max")) document.getElementById("sched-cooldown-max").value = "72h";
        rotateCheck.checked = true;
        if (sensorTypeSelect) sensorTypeSelect.value = "cowrie";
    } else if (presetId === "dionaea_malware_48h") {
        setActiveBtn("preset-btn-dionaea", "border-amber-500 bg-amber-950/20", "text-amber-400");
        nameInput.value = "Dionaea 48h Malware Trap (SMB/RPC)";
        activeDurInput.value = "48h";
        modeSelect.value = "random_window";
        if (document.getElementById("sched-cooldown-min")) document.getElementById("sched-cooldown-min").value = "48h";
        if (document.getElementById("sched-cooldown-max")) document.getElementById("sched-cooldown-max").value = "72h";
        rotateCheck.checked = true;
        if (sensorTypeSelect) sensorTypeSelect.value = "dionaea";
    } else if (presetId === "conpot_scada_weekend") {
        setActiveBtn("preset-btn-conpot", "border-rose-500 bg-rose-950/20", "text-rose-400");
        nameInput.value = "Conpot Weekend SCADA/ICS Deception";
        activeDurInput.value = "60h";
        modeSelect.value = "weekly";
        rotateCheck.checked = true;
        if (sensorTypeSelect) sensorTypeSelect.value = "conpot";
    } else if (presetId === "elasticpot_cloud_trap") {
        setActiveBtn("preset-btn-elasticpot", "border-emerald-500 bg-emerald-950/20", "text-emerald-400");
        nameInput.value = "Elasticpot Cloud RCE Trap";
        activeDurInput.value = "12h";
        modeSelect.value = "random_window";
        if (document.getElementById("sched-cooldown-min")) document.getElementById("sched-cooldown-min").value = "24h";
        if (document.getElementById("sched-cooldown-max")) document.getElementById("sched-cooldown-max").value = "48h";
        rotateCheck.checked = true;
        if (sensorTypeSelect) sensorTypeSelect.value = "elasticpot";
    } else {
        setActiveBtn("preset-btn-custom", "border-purple-500 bg-purple-950/20", "text-purple-400");
    }

    onScheduleModeChange();
}

function onScheduleSensorTypeChange() {
    const st = document.getElementById("sched-sensor-type") ? document.getElementById("sched-sensor-type").value : "cowrie";
    const sizeSelect = document.getElementById("sched-size");
    const provider = document.getElementById("sched-provider") ? document.getElementById("sched-provider").value : "digitalocean";
    const info = availableSensorTypes[st];
    if (!sizeSelect || !info) return;
    const recommended = provider === "gcp" ? info.recommended_size_gcp : info.recommended_size_do;
    if (recommended) {
        for (const opt of sizeSelect.options) {
            if (opt.value === recommended) { opt.selected = true; break; }
        }
    }
}

// Reuses the same two <select>s (sched-region/sched-size) for both providers rather than a parallel
// GCP row - on GCP they become Zone/Machine type, repopulated from the same currentOptions.gcp data
// the Deploy page's GCP tab already uses; on DigitalOcean they go back to Region/VM Size.
function onScheduleProviderChange() {
    const provider = document.getElementById("sched-provider").value;
    const regionLabel = document.getElementById("sched-region-label");
    const sizeLabel = document.getElementById("sched-size-label");
    const regionSelect = document.getElementById("sched-region");
    const sizeSelect = document.getElementById("sched-size");
    const rotateRow = document.getElementById("sched-rotate-regions-row");
    const opts = currentOptions || {};

    if (provider === "gcp") {
        if (regionLabel) regionLabel.innerText = "Zone";
        if (sizeLabel) sizeLabel.innerText = "Machine type";
        if (regionSelect) {
            regionSelect.innerHTML = "";
            const zones = (opts.gcp && opts.gcp.zones && opts.gcp.zones.length) ? opts.gcp.zones : [{ name: "us-central1-a" }];
            zones.forEach(z => {
                const o = document.createElement("option");
                o.value = z.name; o.innerText = z.name;
                regionSelect.appendChild(o);
            });
        }
        if (sizeSelect) {
            sizeSelect.innerHTML = "";
            const types = (opts.gcp && opts.gcp.machine_types && opts.gcp.machine_types.length) ? opts.gcp.machine_types : [{ name: "e2-small" }];
            types.forEach(t => {
                const o = document.createElement("option");
                o.value = t.name; o.innerText = t.name;
                sizeSelect.appendChild(o);
            });
        }
        if (rotateRow) rotateRow.classList.add("hidden");
        const rotateCb = document.getElementById("sched-rotate-regions");
        if (rotateCb) rotateCb.checked = false;
    } else {
        if (regionLabel) regionLabel.innerText = "Target Region";
        if (sizeLabel) sizeLabel.innerText = "VM Size";
        if (regionSelect) {
            regionSelect.innerHTML = "";
            const regions = (opts.regions || []).length ? opts.regions : [{ slug: "nyc1", name: "New York" }];
            regions.forEach(r => {
                const o = document.createElement("option");
                o.value = r.slug; o.innerText = `${r.name} (${r.slug.toUpperCase()})`;
                regionSelect.appendChild(o);
            });
        }
        if (sizeSelect) {
            sizeSelect.innerHTML = "";
            const sizes = (opts.sizes || []).length ? opts.sizes : [{ slug: "s-1vcpu-2gb", description: "1vCPU/2GB" }];
            sizes.forEach(s => {
                const o = document.createElement("option");
                o.value = s.slug; o.innerText = `${s.slug} (${s.description || '1vCPU/2GB'})`;
                sizeSelect.appendChild(o);
            });
        }
        if (rotateRow) rotateRow.classList.remove("hidden");
    }
    onScheduleSensorTypeChange();  // re-apply the recommended size for the (now different) provider
}

function onScheduleModeChange() {
    const mode = document.getElementById("sched-mode").value;
    const randWindow = document.getElementById("sched-random-window-fields");
    const intervalField = document.getElementById("sched-interval-field");

    if (mode === "random_window") {
        randWindow.classList.remove("hidden");
        randWindow.classList.add("grid");
        intervalField.classList.add("hidden");
    } else if (mode === "interval") {
        randWindow.classList.add("hidden");
        randWindow.classList.remove("grid");
        intervalField.classList.remove("hidden");
    } else {
        // weekly
        randWindow.classList.add("hidden");
        randWindow.classList.remove("grid");
        intervalField.classList.add("hidden");
    }
}

function createPresetSchedule(presetId) {
    selectSchedulePreset(presetId);
    openModal("create-schedule-modal");
}

async function submitCreateSchedule(event) {
    if (event) event.preventDefault();

    const name = document.getElementById("sched-name").value.trim();
    const presetId = document.getElementById("sched-preset-id").value;
    const sensorType = document.getElementById("sched-sensor-type") ? document.getElementById("sched-sensor-type").value : "cowrie";
    const activeDur = document.getElementById("sched-active-dur").value.trim() || "24h";
    const mode = document.getElementById("sched-mode").value;
    const provider = document.getElementById("sched-provider") ? document.getElementById("sched-provider").value : "digitalocean";
    // sched-region/sched-size are repopulated by onScheduleProviderChange() to hold zone/machine-type
    // values when provider is gcp - read them into the right config keys either way.
    const regionOrZone = document.getElementById("sched-region").value;
    const sizeOrMachineType = document.getElementById("sched-size").value;
    const rotateRegions = document.getElementById("sched-rotate-regions").checked;
    const startNow = document.getElementById("sched-start-now").checked;

    const config = {
        provider: provider,
        sensor_type: sensorType,
        rotate_regions: provider === "gcp" ? false : rotateRegions,
        attach_firewall: true,
        auto_register_hive: true
    };
    if (provider === "gcp") {
        config.zone = regionOrZone;
        config.machine_type = sizeOrMachineType;
    } else {
        config.region = regionOrZone;
        config.size = sizeOrMachineType;
    }

    const payload = {
        name: name,
        preset_id: presetId,
        sensor_type: sensorType,
        provider: provider,
        start_immediately: startNow,
        timing: {
            mode: mode,
            active_duration: activeDur,
            cooldown_min: document.getElementById("sched-cooldown-min") ? document.getElementById("sched-cooldown-min").value.trim() : "48h",
            cooldown_max: document.getElementById("sched-cooldown-max") ? document.getElementById("sched-cooldown-max").value.trim() : "72h",
            cooldown_duration: document.getElementById("sched-cooldown-dur") ? document.getElementById("sched-cooldown-dur").value.trim() : "48h"
        },
        config: config
    };

    try {
        const res = await fetch(apiUrl("/api/schedules"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to create schedule");

        showNotification(`Campaign '${name}' activated successfully!`, "success");
        closeModal("create-schedule-modal");
        await loadSchedules();
        await loadDroplets();
        await loadEDLInfo();
    } catch (err) {
        showNotification(err.message, "error");
    }
}

async function triggerScheduleAction(id) {
    try {
        const res = await fetch(apiUrl(`/api/schedules/${id}/trigger`), { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Action trigger failed");

        showNotification(data.message || `Schedule action executed: ${data.action}`, "success");
        await loadSchedules();
        await loadDroplets();
        await loadEDLInfo();
    } catch (err) {
        showNotification(err.message, "error");
    }
}

async function pauseSchedule(id) {
    try {
        const res = await fetch(apiUrl(`/api/schedules/${id}/pause`), { method: "POST" });
        if (res.ok) {
            showNotification("Campaign schedule paused.", "info");
            await loadSchedules();
        }
    } catch (err) {
        showNotification("Failed to pause schedule", "error");
    }
}

async function resumeSchedule(id) {
    try {
        const res = await fetch(apiUrl(`/api/schedules/${id}/resume`), { method: "POST" });
        if (res.ok) {
            showNotification("Campaign schedule resumed.", "success");
            await loadSchedules();
        }
    } catch (err) {
        showNotification("Failed to resume schedule", "error");
    }
}

async function deleteScheduleConfirm(id, name, hasActiveDroplet) {
    let msg = `Permanently delete schedule '${name}'?`;
    if (hasActiveDroplet) {
        msg += "\n\nNote: The active droplet deployed by this campaign will also be destroyed to release the public IP.";
    }
    if (!confirm(msg)) return;

    try {
        const res = await fetch(apiUrl(`/api/schedules/${id}?destroy_droplet=true`), { method: "DELETE" });
        if (res.ok) {
            showNotification(`Schedule '${name}' deleted.`, "info");
            await loadSchedules();
            await loadDroplets();
            await loadEDLInfo();
        }
    } catch (err) {
        showNotification("Failed to delete schedule", "error");
    }
}

// --------------------------------------------------------------------
// Main Tab Navigation & Focused Views
// --------------------------------------------------------------------
function switchMainTab(tab) {
    if (tab === 'workers') tab = 'tasks';
    if (tab === 'all') tab = 'dashboard';
    currentActiveMainTab = tab;

    const routes = {
        'dashboard': '/',
        'deploy': '/deploy',
        'campaigns': '/campaigns',
        'fleet': '/fleet',
        'edl': '/edl',
        'tasks': '/tasks'
    };

    const targetRoute = routes[tab] || ('/' + tab);
    const basePath = window.location.pathname.startsWith('/sensors') ? '/sensors' : '';
    const currentPath = window.location.pathname.replace(/\/+$/, '') || '/';
    const targetPath = (basePath + targetRoute).replace(/\/+$/, '') || '/';

    const sections = {
        'deploy': document.getElementById('section-deploy'),
        'campaigns': document.getElementById('section-campaigns'),
        'fleet': document.getElementById('section-fleet'),
        'edl': document.getElementById('section-edl'),
        'tasks': document.getElementById('section-workers')
    };

    // If target is a different page and section does not exist locally
    if (currentPath !== targetPath && (!sections[tab] || tab === 'dashboard')) {
        if (typeof navigateTo === 'function') {
            navigateTo(targetRoute);
            return;
        } else {
            window.location.href = targetPath;
            return;
        }
    }

    const tabs = ['dashboard', 'deploy', 'campaigns', 'fleet', 'edl', 'tasks'];
    tabs.forEach(t => {
        const btn = document.getElementById(`maintab-${t}`);
        if (!btn) return;
        if (t === tab) {
            btn.className = "maintab-btn flex-1 sm:flex-none py-1.5 px-2.5 sm:px-3 text-center text-xs font-semibold rounded-lg bg-cyan-950/80 text-cyan-300 border border-cyan-800/80 transition-all flex items-center justify-center gap-1.5 shrink-0 min-h-[34px] shadow-sm";
        } else {
            btn.className = "maintab-btn flex-1 sm:flex-none py-1.5 px-2.5 sm:px-3 text-center text-xs font-semibold rounded-lg bg-slate-900/60 hover:bg-slate-800 text-slate-400 hover:text-slate-200 border border-transparent transition-all flex items-center justify-center gap-1.5 shrink-0 min-h-[34px]";
        }
    });

    if (sections[tab]) {
        sections[tab].classList.remove('hidden');
        sections[tab].scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    if (tab === 'tasks') {
        loadWorkerStatus(true);
    } else if (tab === 'fleet') {
        loadDroplets();
    } else if (tab === 'edl') {
        loadEDLInfo();
    } else if (tab === 'campaigns') {
        loadSchedules();
    }
}

// --------------------------------------------------------------------
// Dedicated RQ Workers & Redis Queue Telemetry
// --------------------------------------------------------------------
function renderDashboardTasks(tasks, workers, activeTasks, onlineCount) {
    const dashTasks = document.getElementById("dashboard-tasks-summary");
    if (!dashTasks) return;

    if (!tasks || tasks.length === 0) {
        dashTasks.innerHTML = `
            <div class="p-3.5 rounded-lg bg-slate-900/40 border border-slate-800/80 text-center space-y-1">
                <div class="flex items-center justify-center gap-2 text-xs font-mono text-emerald-400">
                    <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
                    <span>${onlineCount} Dedicated Worker Daemon${onlineCount === 1 ? '' : 's'} Online &amp; Ready</span>
                </div>
                <p class="text-[11px] text-slate-500">Queue is idle. Tasks will display here in real time as they execute.</p>
            </div>
        `;
        return;
    }

    let tasksHtml = `
        <div class="flex items-center justify-between text-xs font-mono text-slate-400 mb-2 px-1">
            <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full ${onlineCount > 0 ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'}"></span> ${onlineCount} Worker${onlineCount === 1 ? '' : 's'} Online</span>
            <span>${activeTasks.length} in flight / ${tasks.length} recorded</span>
        </div>
        <div class="space-y-2">
    `;
    tasks.slice(0, 3).forEach(t => {
        let badge = '';
        if (t.status === 'running') {
            badge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-amber-500/20 text-amber-300 border border-amber-500/30 animate-pulse">Running (${t.percent || 0}%)</span>`;
        } else if (t.status === 'completed') {
            badge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">Completed</span>`;
        } else if (t.status === 'failed') {
            badge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-rose-500/20 text-rose-400 border border-rose-500/30">Failed</span>`;
        } else {
            badge = `<span class="px-2 py-0.5 text-[10px] font-mono rounded bg-cyan-500/20 text-cyan-300 border border-cyan-500/30">Queued</span>`;
        }
        const timeStr = t.updated_at ? new Date(parseFloat(t.updated_at) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
        const safeType = escapeHtml(t.type_label || t.task_type || 'Task');
        const safeSensor = escapeHtml(t.sensor_name || 'Sensor');
        tasksHtml += `
            <div class="p-2.5 rounded-lg bg-[#090d16] border border-slate-800 flex items-center justify-between gap-2">
                <div class="min-w-0 flex items-center gap-2">
                    <span class="px-1.5 py-0.5 text-[9px] font-mono rounded bg-slate-800 text-slate-300 border border-slate-700 font-semibold shrink-0">${safeType}</span>
                    <span class="font-bold text-xs text-white truncate">${safeSensor}</span>
                    <span class="text-[10px] text-slate-500 font-mono hidden sm:inline">${timeStr}</span>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                    ${badge}
                    <button type="button" onclick="reopenDeploymentStream(this.dataset.id, this.dataset.type, this.dataset.sensor)" data-id="${t.id}" data-type="${safeType}" data-sensor="${safeSensor}" class="px-2 py-1 text-[11px] rounded bg-cyan-950/80 hover:bg-cyan-900 text-cyan-300 border border-cyan-800/80" title="View Stream">
                        <i class="fa-solid fa-terminal text-[10px]"></i>
                    </button>
                </div>
            </div>
        `;
    });
    tasksHtml += '</div>';
    dashTasks.innerHTML = tasksHtml;
}

async function loadWorkerStatus(showSpinner = false) {
    try {
        const icon = document.getElementById("worker-refresh-icon");
        if (showSpinner && icon) icon.classList.add("fa-spin");

        const res = await fetch(apiUrl("/api/workers"));
        if (!res.ok) return;
        const data = await res.json();

        const redis = data.redis || {};
        const queue = data.queue || {};
        const workers = data.workers || [];
        const tasks = data.tasks || [];

        // 1. Header & Section Badges
        const headerBadge = document.getElementById("worker-header-badge");
        const globalBadge = document.getElementById("worker-global-status-badge");
        const tabBadge = document.getElementById("tab-tasks-badge") || document.getElementById("tab-workers-badge");

        const onlineCount = workers.length;
        const busyCount = workers.filter(w => w.state === 'busy').length;
        const idleCount = workers.filter(w => w.state === 'idle').length;
        const activeTasks = tasks.filter(t => t.status === 'running' || t.status === 'queued');

        // Render Dashboard Tasks preview if present
        renderDashboardTasks(tasks, workers, activeTasks, onlineCount);

        if (tabBadge) {
            if (activeTasks.length > 0) {
                tabBadge.innerText = `${activeTasks.length} active`;
                tabBadge.className = "px-1.5 py-0.2 text-[10px] font-mono font-bold rounded bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 animate-pulse";
            } else {
                tabBadge.innerText = tasks.length;
                tabBadge.className = "px-1.5 py-0.2 text-[10px] font-mono rounded bg-slate-700/60 text-slate-300 border border-slate-600";
            }
        }

        if (onlineCount > 0) {
            const text = busyCount > 0 ? `${busyCount} Busy / ${onlineCount} Online` : `${onlineCount} Worker${onlineCount > 1 ? 's' : ''} Online`;
            if (headerBadge) {
                headerBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center gap-1 sm:gap-1.5 cursor-pointer hover:border-emerald-400/50 shrink-0";
                headerBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse shrink-0"></span><span class="hidden sm:inline">${text}</span><span class="sm:hidden font-mono">${onlineCount} Wk</span>`;
            }
            if (globalBadge) {
                globalBadge.className = "px-2.5 py-1 text-[11px] font-mono font-semibold rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 flex items-center gap-1.5";
                globalBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span><span>${onlineCount} Worker${onlineCount > 1 ? 's' : ''} Online</span>`;
            }
        } else {
            if (headerBadge) {
                headerBadge.className = "px-2 sm:px-2.5 py-1 text-[11px] sm:text-xs font-semibold rounded-full bg-rose-500/20 text-rose-400 border border-rose-500/30 flex items-center gap-1 sm:gap-1.5 cursor-pointer hover:border-rose-400/50 shrink-0";
                headerBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-400 shrink-0"></span><span class="hidden sm:inline">Workers Offline</span><span class="sm:hidden font-mono">0 Wk</span>`;
            }
            if (globalBadge) {
                globalBadge.className = "px-2.5 py-1 text-[11px] font-mono font-semibold rounded-full bg-rose-500/20 text-rose-400 border border-rose-500/30 flex items-center gap-1.5";
                globalBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-400"></span><span>0 Workers Registered</span>`;
            }
        }

        // 2. Metrics Cards
        if (document.getElementById("worker-stat-count")) document.getElementById("worker-stat-count").innerText = onlineCount;
        if (document.getElementById("worker-stat-busy")) document.getElementById("worker-stat-busy").innerText = `${busyCount} Busy / ${idleCount} Idle`;
        if (document.getElementById("worker-stat-pending")) document.getElementById("worker-stat-pending").innerText = queue.pending_count ?? 0;
        if (document.getElementById("worker-stat-started")) document.getElementById("worker-stat-started").innerText = `${queue.started_count ?? 0} running`;
        if (document.getElementById("worker-stat-finished")) document.getElementById("worker-stat-finished").innerText = tasks.filter(t => t.status === "completed").length;
        if (document.getElementById("worker-stat-failed")) document.getElementById("worker-stat-failed").innerText = `${tasks.filter(t => t.status === "failed").length} failed`;
        if (document.getElementById("worker-stat-redis-mem")) document.getElementById("worker-stat-redis-mem").innerText = redis.used_memory_human || "N/A";
        if (document.getElementById("worker-stat-redis-clients")) document.getElementById("worker-stat-redis-clients").innerText = `${redis.connected_clients ?? 0} connected`;

        if (document.getElementById("workers-count-label")) {
            document.getElementById("workers-count-label").innerText = `${onlineCount} worker daemon${onlineCount === 1 ? '' : 's'} registered`;
        }

        // 3. Render Active & Recent Tasks
        const tasksTbody = document.getElementById("tasks-table-body");
        const tasksCards = document.getElementById("tasks-cards-list");
        const tasksEmpty = document.getElementById("tasks-empty-state");
        const tasksCountLabel = document.getElementById("tasks-count-label");

        if (tasksCountLabel) {
            tasksCountLabel.innerText = `${tasks.length} task${tasks.length === 1 ? '' : 's'} recorded (${activeTasks.length} in flight)`;
        }

        if (tasks.length === 0) {
            if (tasksTbody) tasksTbody.innerHTML = "";
            if (tasksCards) tasksCards.innerHTML = "";
            if (tasksEmpty) tasksEmpty.classList.remove("hidden");
        } else {
            if (tasksEmpty) tasksEmpty.classList.add("hidden");

            // Render Desktop Tasks Table
            if (tasksTbody) {
                tasksTbody.innerHTML = tasks.map(t => {
                    let statusBadge = "";
                    if (t.status === "running") {
                        statusBadge = `
                            <span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-amber-500/20 text-amber-300 border-amber-500/30 flex items-center gap-1.5 w-fit">
                                <span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping"></span>
                                <span>Running (${t.percent || 0}%)</span>
                            </span>
                        `;
                    } else if (t.status === "completed") {
                        statusBadge = `
                            <span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-emerald-500/20 text-emerald-400 border-emerald-500/30 flex items-center gap-1 w-fit">
                                <i class="fa-solid fa-check text-[9px]"></i> <span>Completed</span>
                            </span>
                        `;
                    } else if (t.status === "failed") {
                        statusBadge = `
                            <span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-rose-500/20 text-rose-400 border-rose-500/30 flex items-center gap-1 w-fit">
                                <i class="fa-solid fa-triangle-exclamation text-[9px]"></i> <span>Failed</span>
                            </span>
                        `;
                    } else {
                        statusBadge = `
                            <span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-cyan-500/20 text-cyan-300 border-cyan-500/30 flex items-center gap-1 w-fit">
                                <i class="fa-solid fa-clock text-[9px]"></i> <span>Queued</span>
                            </span>
                        `;
                    }

                    const timeStr = t.updated_at ? new Date(parseFloat(t.updated_at) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
                    const safeType = escapeHtml(t.type_label || t.task_type || 'Task');
                    const safeSensor = escapeHtml(t.sensor_name || 'Sensor');

                    return `
                        <tr class="hover:bg-slate-800/30 transition-colors">
                            <td class="py-2.5 px-4 font-mono text-slate-300">
                                <div class="flex items-center gap-1.5">
                                    <span class="text-cyan-400 font-semibold" title="${t.id}">${t.id.slice(0, 8)}...</span>
                                    <button type="button" onclick="copyToClipboard('${t.id}')" class="text-slate-500 hover:text-slate-300 transition-colors" title="Copy full Task ID">
                                        <i class="fa-solid fa-copy text-[10px]"></i>
                                    </button>
                                </div>
                            </td>
                            <td class="py-2.5 px-4">
                                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-slate-800 text-slate-300 border border-slate-700">
                                    ${safeType}
                                </span>
                            </td>
                            <td class="py-2.5 px-4 text-slate-200">
                                <div class="font-medium text-xs text-white">${safeSensor}</div>
                                <div class="text-[10px] text-slate-500 font-mono">${escapeHtml(t.sensor_type || 'honeypot')}</div>
                            </td>
                            <td class="py-2.5 px-4">
                                ${statusBadge}
                                ${t.status === 'running' ? `
                                    <div class="w-24 bg-slate-900 rounded-full h-1 mt-1.5 overflow-hidden border border-slate-800">
                                        <div class="bg-amber-400 h-1 rounded-full transition-all duration-300" style="width: ${t.percent || 5}%"></div>
                                    </div>
                                ` : ''}
                            </td>
                            <td class="py-2.5 px-4 text-[11px] text-slate-400">
                                <div class="truncate max-w-[200px] text-slate-300" title="${escapeHtml(plain(t.last_message))}">${escapeHtml(plain(t.last_message)) || '—'}</div>
                                <div class="text-[10px] text-slate-500 font-mono">${timeStr}</div>
                            </td>
                            <td class="py-2.5 px-4 text-right">
                                <button type="button" onclick="reopenDeploymentStream(this.dataset.id, this.dataset.type, this.dataset.sensor)" data-id="${t.id}" data-type="${safeType}" data-sensor="${safeSensor}" class="px-2.5 py-1 text-xs font-semibold rounded bg-cyan-950/80 hover:bg-cyan-900 text-cyan-300 border border-cyan-800/80 transition-colors inline-flex items-center gap-1.5 shadow-sm">
                                    <i class="fa-solid fa-terminal text-[10px]"></i> <span>View Stream</span>
                                </button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }

            // Render Mobile Tasks Cards
            if (tasksCards) {
                tasksCards.innerHTML = tasks.map(t => {
                    let statusBadge = "";
                    if (t.status === "running") {
                        statusBadge = `<span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-amber-500/20 text-amber-300 border-amber-500/30 flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping"></span> Running (${t.percent || 0}%)</span>`;
                    } else if (t.status === "completed") {
                        statusBadge = `<span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-emerald-500/20 text-emerald-400 border-emerald-500/30 flex items-center gap-1"><i class="fa-solid fa-check text-[9px]"></i> Completed</span>`;
                    } else if (t.status === "failed") {
                        statusBadge = `<span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-rose-500/20 text-rose-400 border-rose-500/30 flex items-center gap-1"><i class="fa-solid fa-triangle-exclamation text-[9px]"></i> Failed</span>`;
                    } else {
                        statusBadge = `<span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border bg-cyan-500/20 text-cyan-300 border-cyan-500/30 flex items-center gap-1"><i class="fa-solid fa-clock text-[9px]"></i> Queued</span>`;
                    }

                    const timeStr = t.updated_at ? new Date(parseFloat(t.updated_at) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
                    const safeType = escapeHtml(t.type_label || t.task_type || 'Task');
                    const safeSensor = escapeHtml(t.sensor_name || 'Sensor');

                    return `
                        <div class="p-3.5 rounded-xl bg-[#090e1a] border border-slate-800 space-y-2.5">
                            <div class="flex items-center justify-between">
                                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-slate-800 text-cyan-300 border border-slate-700">
                                    ${safeType}
                                </span>
                                ${statusBadge}
                            </div>
                            <div class="flex items-center justify-between text-xs">
                                <div>
                                    <div class="font-bold text-white">${safeSensor}</div>
                                    <div class="text-[10px] text-slate-400 font-mono">ID: ${t.id.slice(0, 8)}...</div>
                                </div>
                                <span class="text-[10px] text-slate-500 font-mono">${timeStr}</span>
                            </div>
                            <div class="text-[11px] text-slate-400 truncate bg-slate-900/60 p-1.5 rounded border border-slate-800/80 font-mono">
                                ${escapeHtml(plain(t.last_message)) || 'No log details'}
                            </div>
                            <button type="button" onclick="reopenDeploymentStream(this.dataset.id, this.dataset.type, this.dataset.sensor)" data-id="${t.id}" data-type="${safeType}" data-sensor="${safeSensor}" class="w-full py-2 text-xs font-semibold rounded-lg bg-cyan-950/80 hover:bg-cyan-900 text-cyan-300 border border-cyan-800/80 transition-colors flex items-center justify-center gap-2">
                                <i class="fa-solid fa-terminal text-xs"></i> <span>View Live Stream / Logs</span>
                            </button>
                        </div>
                    `;
                }).join('');
            }
        }

        // 4. Worker Daemons Table & Cards
        const tbody = document.getElementById("workers-table-body");
        const cardsList = document.getElementById("workers-cards-list");
        const emptyState = document.getElementById("workers-empty-state");

        if (workers.length === 0) {
            if (tbody) tbody.innerHTML = "";
            if (cardsList) cardsList.innerHTML = "";
            if (emptyState) emptyState.classList.remove("hidden");
        } else {
            if (emptyState) emptyState.classList.add("hidden");

            // Desktop Table
            if (tbody) {
                tbody.innerHTML = workers.map(w => {
                    const isBusy = w.state === 'busy';
                    const stateClass = isBusy
                        ? 'bg-amber-500/20 text-amber-300 border-amber-500/30'
                        : 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
                    const stateDot = isBusy
                        ? '<span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping"></span>'
                        : '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>';

                    const currentJobHtml = w.current_job_id
                        ? `<span class="font-mono text-cyan-300 text-[11px]">${w.current_job_id.slice(0, 12)}...</span>`
                        : `<span class="text-slate-500 italic">Idle (Waiting for tasks)</span>`;

                    const queuesHtml = (w.queues || []).map(q => `<code class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 text-[10px] font-mono border border-slate-700">${q}</code>`).join(' ');

                    const birth = w.birth_date ? new Date(w.birth_date).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
                    const heartbeat = w.last_heartbeat ? new Date(w.last_heartbeat).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';

                    return `
                        <tr class="hover:bg-slate-800/30 transition-colors">
                            <td class="py-3 px-4 font-mono font-medium text-slate-200">
                                <div class="flex items-center gap-2">
                                    <i class="fa-solid fa-server text-cyan-400 text-xs"></i>
                                    <div>
                                        <div class="text-white">${w.hostname || 'localhost'}</div>
                                        <div class="text-[10px] text-slate-500">${w.name.slice(0, 16)}...</div>
                                    </div>
                                </div>
                            </td>
                            <td class="py-3 px-4 font-mono text-slate-400 text-[11px]">${w.pid || '—'}</td>
                            <td class="py-3 px-4">
                                <span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border flex items-center gap-1.5 w-fit ${stateClass}">
                                    ${stateDot} ${w.state}
                                </span>
                            </td>
                            <td class="py-3 px-4">${currentJobHtml}</td>
                            <td class="py-3 px-4">${queuesHtml}</td>
                            <td class="py-3 px-4 font-mono text-slate-300">
                                <span class="text-emerald-400 font-semibold">${w.successful_job_count || 0}</span> / 
                                <span class="${(w.failed_job_count || 0) > 0 ? 'text-rose-400 font-semibold' : 'text-slate-500'}">${w.failed_job_count || 0}</span>
                            </td>
                            <td class="py-3 px-4 font-mono text-[11px] text-slate-400" title="Started at ${birth}">${heartbeat}</td>
                        </tr>
                    `;
                }).join('');
            }

            // Mobile Cards
            if (cardsList) {
                cardsList.innerHTML = workers.map(w => {
                    const isBusy = w.state === 'busy';
                    const stateClass = isBusy
                        ? 'bg-amber-500/20 text-amber-300 border-amber-500/30'
                        : 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
                    const stateDot = isBusy
                        ? '<span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-ping"></span>'
                        : '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>';

                    return `
                        <div class="p-3 rounded-xl bg-[#090e1a] border border-slate-800 space-y-2">
                            <div class="flex items-center justify-between">
                                <div class="flex items-center gap-2">
                                    <i class="fa-solid fa-server text-cyan-400 text-xs"></i>
                                    <span class="font-bold text-xs text-white">${w.hostname} (PID ${w.pid})</span>
                                </div>
                                <span class="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold uppercase border flex items-center gap-1 ${stateClass}">
                                    ${stateDot} ${w.state}
                                </span>
                            </div>
                            <div class="text-[11px] text-slate-400 flex items-center justify-between border-t border-slate-800/80 pt-2 font-mono">
                                <span>Worker ID:</span>
                                <span class="text-slate-300">${w.name.slice(0, 14)}...</span>
                            </div>
                            <div class="text-[11px] text-slate-400 flex items-center justify-between font-mono">
                                <span>Task Stats:</span>
                                <span><b class="text-emerald-400">${w.successful_job_count || 0} OK</b> / <b class="${(w.failed_job_count || 0) > 0 ? 'text-rose-400' : 'text-slate-500'}">${w.failed_job_count || 0} Failed</b></span>
                            </div>
                            <div class="text-[11px] text-slate-400 flex items-center justify-between font-mono">
                                <span>Heartbeat:</span>
                                <span class="text-slate-300">${w.last_heartbeat ? new Date(w.last_heartbeat).toLocaleTimeString() : '—'}</span>
                            </div>
                        </div>
                    `;
                }).join('');
            }
        }

        if (showSpinner && icon) {
            setTimeout(() => icon.classList.remove("fa-spin"), 500);
        }
    } catch (err) {
        console.error("Error loading worker status:", err);
    }
}



const state = { config: null, service: null, status: null, devices: [], logKind: "activity", corrections: null, deviceUserScan: null, syncRunActive: false, syncProgress: 0, syncDeviceEstimates: {}, syncObservedRunning: false, syncAwaitingFreshStatus: false, syncPreviousCompletedAt: null, lastNotifiedSyncCompletion: null, auth: null, branding: null, deviceUserTable: { filter: "all", search: "", pageSize: 20, page: 1 } };
const configFormState = { hydrated: false, dirty: false };
let newDeviceSequence = 0;
let themeApplySequence = 0;
let syncProgressPollBusy = false;
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function mixColor(hex, target, amount) {
  const source = hex.replace("#", "");
  const targetHex = target.replace("#", "");
  const channel = (start) => Math.round(parseInt(source.slice(start, start + 2), 16) * (1 - amount) + parseInt(targetHex.slice(start, start + 2), 16) * amount).toString(16).padStart(2, "0");
  return `#${channel(0)}${channel(2)}${channel(4)}`;
}

function logoPalette(dataUrl) {
  return new Promise((resolve) => {
    if (!dataUrl) { resolve(null); return; }
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = 48; canvas.height = 48;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.drawImage(image, 0, 0, 48, 48);
      const pixels = context.getImageData(0, 0, 48, 48).data;
      const buckets = new Map();
      for (let index = 0; index < pixels.length; index += 4) {
        const red = pixels[index], green = pixels[index + 1], blue = pixels[index + 2], alpha = pixels[index + 3];
        const max = Math.max(red, green, blue), min = Math.min(red, green, blue);
        if (alpha < 150 || max > 245 || max < 28 || max - min < 18) continue;
        const key = `${Math.round(red / 32) * 32},${Math.round(green / 32) * 32},${Math.round(blue / 32) * 32}`;
        buckets.set(key, (buckets.get(key) || 0) + (max - min));
      }
      const winner = [...buckets.entries()].sort((a, b) => b[1] - a[1])[0];
      if (!winner) { resolve(null); return; }
      const rgb = winner[0].split(",").map((value) => Math.min(255, Number(value)));
      const primary = `#${rgb.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
      resolve({ primary: mixColor(primary, "#000000", .2), accent: mixColor(primary, "#ffffff", .68), sidebar: mixColor(primary, "#000000", .7) });
    };
    image.onerror = () => resolve(null);
    image.src = dataUrl;
  });
}

async function applyTheme(requestedPreset = state.branding?.theme_preset, requestedMode = state.branding?.theme_mode) {
  const sequence = ++themeApplySequence;
  let preset = ["pulse", "logo", "ocean", "royal", "sunset"].includes(requestedPreset) ? requestedPreset : "pulse";
  const mode = requestedMode === "dark" ? "dark" : "light";
  if (preset === "logo" && !state.branding?.logo_data) preset = "pulse";
  document.body.dataset.theme = preset;
  document.body.dataset.mode = mode;
  ["--green", "--lime", "--sidebar"].forEach((name) => document.body.style.removeProperty(name));
  if (preset === "logo") {
    const palette = await logoPalette(state.branding.logo_data);
    if (sequence !== themeApplySequence) return;
    if (palette) Object.entries({ "--green": palette.primary, "--lime": palette.accent, "--sidebar": palette.sidebar }).forEach(([name, value]) => document.body.style.setProperty(name, value));
  }
  const presetControl = $("#adminThemePreset");
  if (presetControl) presetControl.value = preset;
  const modeControl = $("#adminThemeMode");
  if (modeControl) modeControl.value = mode;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 3600);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(state.auth?.csrf_token ? { "X-CSRF-Token": state.auth.csrf_token } : {}), ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({ ok: false, message: "Unexpected server response." }));
  if (!response.ok || !data.ok) throw new Error(data.message || "Request failed.");
  return data;
}

function formatDate(value) {
  if (!value) return "Never";
  const date = new Date(String(value).replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function displayName(value) {
  const clean = String(value || "").replace(/[_-]+/g, " ").trim();
  return clean.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function goTo(view) {
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === `view-${view}`));
  $$(".nav-link").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  const titles = { dashboard: "Dashboard", workforce: "Workforce", "device-users": "Device users", corrections: "Punch corrections", configuration: "Connection settings", devices: "Device network", logs: "Activity history", admin: "Administration" };
  $("#pageTitle").textContent = titles[view];
  if (view === "logs") loadLogs();
  if (view === "corrections") loadCorrections();
  if (view === "device-users") prepareDeviceUserLocations();
  if (view === "admin") loadUsers();
}

function renderBranding(values, auth) {
  state.branding = values;
  state.auth = auth;
  const name = values.app_name || "PulseBridge";
  $("#brandName").textContent = name;
  $("#footerBrand").textContent = name;
  $("#footerText").textContent = values.footer_text || "Developed by Ryan Brown";
  document.title = `${name} · ERPNext Biometric Sync`;
  const favicon = $("#appFavicon");
  if (favicon) favicon.href = values.logo_data || "data:,";
  const mark = $("#brandMark");
  mark.textContent = values.logo_data ? "" : name.charAt(0).toUpperCase();
  mark.style.backgroundImage = values.logo_data ? `url(${values.logo_data})` : "";
  mark.classList.toggle("has-logo", Boolean(values.logo_data));
  const logoTheme = $('#adminThemePreset option[value="logo"]');
  if (logoTheme) { logoTheme.disabled = !values.logo_data; logoTheme.textContent = values.logo_data ? "Logo colors" : "Logo colors · upload logo"; }
  applyTheme();
  $$(".admin-only").forEach((item) => { item.hidden = auth?.user?.role !== "admin"; });
  renderErpHrmLink();
  if (auth?.user?.role === "admin") {
    $("#adminAppName").value = name;
    $("#adminFooter").value = values.footer_text || "";
    $("#adminThemePreset").value = values.theme_preset || "pulse";
    $("#adminThemeMode").value = values.theme_mode === "dark" ? "dark" : "light";
  }
}

function renderErpHrmLink() {
  const link = $("#erpHrmLink");
  const configuredUrl = String(state.config?.erpnext_url || "").trim();
  let safeUrl = "";
  try {
    const parsed = new URL(configuredUrl);
    if (["http:", "https:"].includes(parsed.protocol)) safeUrl = parsed.href;
  } catch (_) { /* Invalid URLs remain editable on the Configuration page. */ }
  link.hidden = state.auth?.user?.role !== "admin" || !safeUrl;
  if (safeUrl) {
    link.href = safeUrl;
    link.title = `Open ERPNext HRM at ${safeUrl}`;
  } else {
    link.removeAttribute("href");
  }
}

function renderDashboard(data) {
  const running = data.service.running;
  $("#sideStatusDot").classList.toggle("live", running);
  $("#sideStatus").textContent = running ? "Service running" : "Service stopped";
  $("#heroState").textContent = running ? `${data.service.mode === "once" ? "Sync cycle" : "Continuous sync"} in progress` : "Sync engine is standing by";
  $("#lastCycle").textContent = formatDate(data.last_cycle);
  $("#cycleDetail").textContent = data.last_cycle ? "All configured locations were processed" : "Waiting for the first synchronization";
  $("#deviceCount").textContent = data.devices.length;
  $("#employeeCount").textContent = data.employees.available ? Number(data.employees.total).toLocaleString() : "—";
  $("#successCount").textContent = data.devices.reduce((sum, item) => sum + item.success_count, 0).toLocaleString();
  $("#failureCount").textContent = data.devices.reduce((sum, item) => sum + item.failure_count, 0).toLocaleString();
  $("#runOnceButton").disabled = running || !data.configured;
  $("#runOnceButton").textContent = running ? "Sync running…" : "Run sync now";
  $("#serviceToggleButton").disabled = !data.configured;
  $("#serviceToggleButton").textContent = running ? "Stop service" : "Start continuous";
  const erp = data.erpnext || {};
  const erpControl = $("#erpConnection");
  erpControl.className = `erp-connection ${erp.connected === true ? "connected" : erp.connected === false ? "disconnected" : "checking"}`;
  $("small", erpControl).textContent = erp.connected === true ? "Connected" : erp.connected === false ? "Not connected" : "Checking…";
  const root = $("#overviewDevices");
  renderEmployeeSummary(data.employees);
  if (!data.devices.length) {
    root.innerHTML = '<div class="empty">No devices configured yet. Add your first terminal to begin.</div>';
    return;
  }
  root.innerHTML = data.devices.map((device) => `
    <article class="device-card">
      <div class="card-top"><h4>${escapeHtml(displayName(device.device_id))}</h4><div class="card-badges">
        <span class="connection-state ${device.connectivity?.connected === true ? "online" : device.connectivity?.connected === false ? "offline" : "checking"}"><i></i>${device.connectivity?.connected === true ? "Connected" : device.connectivity?.connected === false ? "Not connected" : "Checking"}</span>
        ${device.employee_count !== null && device.employee_count !== undefined
          ? `<span class="health employees">${Number(device.employee_count).toLocaleString()} employees</span>`
          : `<span class="health ${device.last_pull ? "" : "waiting"}">${device.last_pull ? "Reporting" : "Waiting"}</span>`}
      </div></div>
      <p>${escapeHtml(device.ip)}${device.shift ? ` · ${escapeHtml(device.shift)}` : ""}<small>${device.connectivity?.checked_at ? `Ping checked ${escapeHtml(formatDate(device.connectivity.checked_at))}` : "First ping check is starting…"}</small></p>
      <div class="card-stats"><span>LAST PULL<strong>${escapeHtml(formatDate(device.last_pull))}</strong></span><span>SUCCESS / FAILED<strong>${device.success_count} / ${device.failure_count}</strong></span></div>
    </article>`).join("");
}

function renderEmployeeSummary(employees) {
  const root = $("#employeeLocations");
  const status = $("#employeeStatus");
  if (!employees.available) {
    status.textContent = employees.message || "Employee totals unavailable";
    $("#workforceTotal").textContent = "—";
    root.innerHTML = '<div class="empty">Employee location totals are unavailable.</div>';
    return;
  }
  $("#workforceTotal").textContent = Number(employees.total).toLocaleString();
  status.textContent = "Active employees from ERPNext · refreshed every 5 minutes";
  const locations = employees.locations.map((location) => `
    <article class="employee-location"><span>${escapeHtml(displayName(location.name))}</span><strong>${Number(location.count).toLocaleString()}</strong></article>`).join("");
  root.innerHTML = locations;
}

function updateSystemClock() {
  const now = new Date();
  $("#systemClock").textContent = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(now);
  $("#systemDate").textContent = new Intl.DateTimeFormat(undefined, {
    weekday: "short", month: "short", day: "numeric"
  }).format(now);
}

function renderConfig(config, { force = false } = {}) {
  if (configFormState.hydrated && configFormState.dirty && !force) return false;
  $("#erpUrl").value = config.erpnext_url || "";
  $("#apiKey").value = config.erpnext_api_key || "";
  $("#apiKey").type = "password";
  const secretInput = $("#apiSecret");
  const secretButton = $('[data-reveal="apiSecret"]');
  secretInput.value = "";
  secretInput.type = "password";
  secretInput.placeholder = config.has_api_secret ? "••••••••••••  Saved securely" : "Enter your ERPNext API secret";
  $("#apiKey").type = "password";
  $('[data-reveal="apiKey"]').textContent = "Show";
  secretButton.textContent = config.has_api_secret ? "Saved" : "Show";
  secretButton.disabled = Boolean(config.has_api_secret);
  secretButton.setAttribute("aria-label", config.has_api_secret ? "API secret is saved securely" : "Show API secret");
  const secretHint = $("#secretHint");
  secretHint.textContent = config.has_api_secret ? "✓ API secret saved securely. Enter a new value only to replace it." : "No API secret has been saved yet.";
  secretHint.classList.toggle("saved-secret", Boolean(config.has_api_secret));
  $("#erpVersion").value = config.erpnext_version || 15;
  $("#pullFrequency").value = config.pull_frequency || 15;
  const rawStartDate = config.import_start_date || "";
  const rawEndDate = config.import_end_date || "";
  $("#importStartDate").value = rawStartDate.length === 8 ? `${rawStartDate.slice(0,4)}-${rawStartDate.slice(4,6)}-${rawStartDate.slice(6,8)}` : "";
  $("#importEndDate").value = rawEndDate.length === 8 ? `${rawEndDate.slice(0,4)}-${rawEndDate.slice(4,6)}-${rawEndDate.slice(6,8)}` : "";
  $("#defaultShifts").value = prettyJson(config.device_default_shift || {});
  $("#shiftBoundaries").value = prettyJson(config.shift_boundaries || {});
  $("#shiftLogic").value = prettyJson(config.shift_logic || {});
  $("#allowedDeviceShifts").value = prettyJson(config.auto_shift_allowed_by_device || {});
  $("#singlePunchGrace").value = prettyJson(config.single_punch_grace_by_shift || {});
  $("#autoShift").checked = Boolean(config.auto_shift_detection_enabled);
  $("#autoAssignment").checked = config.auto_create_shift_assignment !== false;
  $("#autoCorrectPunches").checked = config.auto_correct_single_punches !== false;
  $("#syncDeviceTime").checked = config.sync_device_time_with_pc !== false;
  $("#maxShiftDistance").value = config.auto_shift_max_distance_minutes || 240;
  $("#assignmentPrefix").value = config.auto_created_shift_assignment_prefix || "AUTO-SYNC";
  renderDevices(config.devices || []);
  configFormState.hydrated = true;
  configFormState.dirty = false;
  return true;
}

function prettyJson(value) { return JSON.stringify(value, null, 2); }

function parseJsonField(selector, label) {
  try { return JSON.parse($(selector).value || "{}"); }
  catch { throw new Error(`${label} must contain valid JSON.`); }
}

function deviceRow(device = {}, { isNew = false } = {}) {
  const row = document.createElement("article");
  row.className = `device-row${isNew ? " is-new" : ""}`;
  row.dataset.isNew = isNew ? "true" : "false";
  row.dataset.newOrder = isNew ? String(++newDeviceSequence) : "0";
  row.innerHTML = `
    <div class="field"><label>Device ID</label><input data-key="device_id" value="${escapeAttr(device.device_id || "")}" placeholder="head_office"></div>
    <div class="field"><label>Private IP address</label><input data-key="ip" value="${escapeAttr(device.ip || "")}" placeholder="192.168.1.201"></div>
    <div class="field"><label>ERPNext shift</label><input data-key="shift" value="${escapeAttr(device.shift || "")}" placeholder="Day Shift"></div>
    <div class="field"><label>Punch direction</label><select data-key="punch_direction"><option>AUTO</option><option>IN</option><option>OUT</option><option>NONE</option></select></div>
    <div class="row-actions"><button class="save-device button primary" type="button">Save</button><button class="test-device" type="button">Test</button><button class="sync-device-time" type="button">Sync clock</button><button class="icon-button" type="button" aria-label="Remove device">×</button></div>`;
  $("select", row).value = device.punch_direction || "AUTO";
  $(".save-device", row).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true; button.textContent = "Saving…";
    try { await saveAll(); }
    catch (error) { showToast(error.message, true); button.disabled = false; button.textContent = "Save"; }
  });
  $(".icon-button", row).addEventListener("click", () => {
    row.remove();
    configFormState.dirty = true;
  });
  $(".test-device", row).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api("/api/test/device", { method: "POST", body: JSON.stringify({ ip: $('[data-key="ip"]', row).value }) });
      showToast(result.message);
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; }
  });
  $(".sync-device-time", row).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const deviceId = $('[data-key="device_id"]', row).value.trim();
    if (!deviceId) { showToast("Save a device ID before synchronizing its clock.", true); return; }
    button.disabled = true;
    button.textContent = "Syncing…";
    try {
      const result = await api("/api/devices/time-sync", { method: "POST", body: JSON.stringify({ device_id: deviceId }) });
      renderClockResults(result.results);
      showToast(result.message);
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; button.textContent = "Sync clock"; }
  });
  return row;
}

function renderDevices(devices) {
  const root = $("#deviceEditor");
  root.innerHTML = "";
  devices.forEach((device) => root.append(deviceRow(device)));
  if (!devices.length) root.append(deviceRow({}, { isNew: true }));
}

function collectDevices() {
  const orderedRows = $$(".device-row").sort((left, right) => {
    const leftNew = left.dataset.isNew === "true";
    const rightNew = right.dataset.isNew === "true";
    if (leftNew !== rightNew) return leftNew ? 1 : -1;
    return Number(left.dataset.newOrder || 0) - Number(right.dataset.newOrder || 0);
  });
  return orderedRows.map((row) => ({
    device_id: $('[data-key="device_id"]', row).value.trim(),
    ip: $('[data-key="ip"]', row).value.trim(),
    shift: $('[data-key="shift"]', row).value.trim(),
    punch_direction: $('[data-key="punch_direction"]', row).value,
    clear_from_device_on_fetch: false,
    latitude: 0,
    longitude: 0,
  })).filter((device) => device.device_id || device.ip);
}

function collectConfig() {
  return {
    ...state.config,
    erpnext_url: $("#erpUrl").value.trim(),
    erpnext_api_key: $("#apiKey").value.trim(),
    erpnext_api_secret: $("#apiSecret").value,
    erpnext_version: Number($("#erpVersion").value),
    pull_frequency: Number($("#pullFrequency").value),
    import_start_date: $("#importStartDate").value,
    import_end_date: $("#importEndDate").value,
    devices: collectDevices(),
    device_default_shift: parseJsonField("#defaultShifts", "Device default shifts"),
    shift_boundaries: parseJsonField("#shiftBoundaries", "Shift boundaries"),
    shift_logic: parseJsonField("#shiftLogic", "IN/OUT time windows"),
    auto_shift_allowed_by_device: parseJsonField("#allowedDeviceShifts", "Allowed shifts by device"),
    single_punch_grace_by_shift: parseJsonField("#singlePunchGrace", "Single-punch grace by shift"),
    auto_shift_detection_enabled: $("#autoShift").checked,
    auto_create_shift_assignment: $("#autoAssignment").checked,
    auto_correct_single_punches: $("#autoCorrectPunches").checked,
    sync_device_time_with_pc: $("#syncDeviceTime").checked,
    auto_shift_max_distance_minutes: Number($("#maxShiftDistance").value),
    auto_created_shift_assignment_prefix: $("#assignmentPrefix").value.trim() || "AUTO-SYNC",
  };
}

function renderClockResults(results = []) {
  const root = $("#clockSyncResults");
  root.innerHTML = results.map((item) => `
    <div class="clock-result ${item.ok ? "" : "failed"}">
      <strong>${escapeHtml(displayName(item.device_id))}</strong>
      <span>${item.ok ? `Before ${escapeHtml(item.before || "unknown")} · Now ${escapeHtml(item.after || item.pc_time)}` : escapeHtml(item.message)}</span>
    </div>`).join("");
}

async function saveAll() {
  const suppliedNewSecret = Boolean($("#apiSecret").value.trim());
  const result = await api("/api/config", { method: "POST", body: JSON.stringify(collectConfig()) });
  state.config = result.config;
  renderConfig(result.config, { force: true });
  showToast(suppliedNewSecret ? "Configuration and API secret saved securely." : result.message);
  await loadState(false);
}

async function loadState(notify = false) {
  try {
    const data = await api(`/api/state${notify ? "?refresh=1" : ""}`);
    state.config = data.config;
    state.service = data.service;
    state.status = data.status;
    state.devices = data.devices;
    renderBranding(data.branding, data.auth);
    renderDashboard(data);
    const preservedEdits = configFormState.dirty;
    // The configuration form is populated once when the page opens. Live
    // dashboard polling must never rebuild it because the operator may be
    // midway through an unsaved edit.
    if (!configFormState.hydrated) renderConfig(data.config, { force: true });
    if (notify) showToast(preservedEdits ? "Dashboard refreshed. Your unsaved changes were kept." : "Dashboard refreshed.");
  } catch (error) { showToast(error.message, true); }
}

function resetUserForm() {
  $("#editUserId").value = "";
  $("#adminUsername").value = "";
  $("#adminPassword").value = "";
  $("#adminRole").value = "user";
  $("#adminUserActive").checked = true;
}

async function loadUsers() {
  try {
    const result = await api("/api/admin/users");
    $("#adminUsers").innerHTML = result.users.length ? result.users.map((user) => `
      <tr><td>${escapeHtml(user.username)}</td><td>${user.role === "admin" ? "Administrator" : "User"}</td><td>${user.active ? "Active" : "Disabled"}</td><td><button class="text-button edit-user" data-user='${escapeAttr(JSON.stringify(user))}'>Edit</button> <button class="text-button delete-user" data-id="${user.id}">Delete</button></td></tr>`).join("") : '<tr><td colspan="4" class="empty-cell">No password accounts yet. This PC still has owner access by IP.</td></tr>';
    $$(".edit-user").forEach((button) => button.addEventListener("click", () => {
      const user = JSON.parse(button.dataset.user);
      $("#editUserId").value = user.id; $("#adminUsername").value = user.username; $("#adminPassword").value = ""; $("#adminRole").value = user.role; $("#adminUserActive").checked = Boolean(user.active);
    }));
    $$(".delete-user").forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("Delete this user account?")) return;
      try { const result = await api(`/api/admin/users/${button.dataset.id}`, { method: "DELETE" }); showToast(result.message); await loadUsers(); } catch (error) { showToast(error.message, true); }
    }));
  } catch (error) { showToast(error.message, true); }
}

function readLogo(file) {
  return new Promise((resolve, reject) => {
    if (!file) { resolve(state.branding?.logo_data || ""); return; }
    if (file.size > 1_500_000 || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) { reject(new Error("Choose a PNG, JPEG, or WebP logo smaller than 1.5 MB.")); return; }
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("Logo could not be read.")); reader.readAsDataURL(file);
  });
}

async function backupResponseError(response) {
  const data = await response.json().catch(() => ({ message: "Unexpected server response." }));
  return new Error(data.message || "Backup operation failed.");
}

async function exportSetupBackup(event) {
  event.preventDefault();
  const password = $("#backupExportPassword").value;
  const confirmation = $("#backupExportConfirm").value;
  if (password.length < 10) { showToast("Backup password must contain at least 10 characters.", true); return; }
  if (password !== confirmation) { showToast("The backup passwords do not match.", true); return; }
  const button = $("button[type='submit']", event.currentTarget);
  button.disabled = true;
  button.textContent = "Encrypting setup…";
  try {
    const response = await fetch("/api/admin/backup/export", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.auth.csrf_token },
      body: JSON.stringify({ password }),
    });
    if (!response.ok) throw await backupResponseError(response);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "pulsebridge-backup.pulsebackup";
    const downloadUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = downloadUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
    event.currentTarget.reset();
    showToast("Encrypted PulseBridge backup downloaded.");
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Download encrypted backup"; }
}

async function restoreSetupBackup(event) {
  event.preventDefault();
  const file = $("#backupRestoreFile").files[0];
  const password = $("#backupRestorePassword").value;
  if (!file) { showToast("Choose a PulseBridge backup file.", true); return; }
  if (file.size > 10 * 1024 * 1024) { showToast("Choose a backup file smaller than 10 MB.", true); return; }
  if (password.length < 10) { showToast("Enter the password used to create this backup.", true); return; }
  if (!confirm("Restore this complete PulseBridge setup? Current configuration, branding, themes, and local user accounts will be replaced.")) return;
  const button = $("button[type='submit']", event.currentTarget);
  button.disabled = true;
  button.textContent = "Restoring setup…";
  const form = new FormData();
  form.append("backup", file, file.name);
  form.append("password", password);
  try {
    const response = await fetch("/api/admin/backup/restore", {
      method: "POST",
      headers: { "X-CSRF-Token": state.auth.csrf_token },
      body: form,
    });
    if (!response.ok) throw await backupResponseError(response);
    const result = await response.json();
    showToast(result.message);
    setTimeout(() => window.location.reload(), 1200);
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
    button.textContent = "Restore complete setup";
  }
}

async function serviceAction(action) {
  try {
    const result = await api(`/api/service/${action}`, { method: "POST", body: "{}" });
    if (action === "run-once") startSyncProgress();
    if (action === "stop") finishSyncProgress(false);
    showToast(result.message);
    await loadState();
  } catch (error) { showToast(error.message, true); }
}

function renderSyncProgress() {
  const root = $("#syncProgress");
  const progress = state.status?.sync_progress || {};
  const completedAt = String(progress.completed_at || "");
  const completed = String(progress.state || "").startsWith("completed");
  const freshCompletion = completedAt && completedAt !== state.syncPreviousCompletedAt;
  if (state.syncAwaitingFreshStatus && (progress.state === "running" || freshCompletion)) {
    if (freshCompletion) state.syncObservedRunning = true;
    state.syncAwaitingFreshStatus = false;
  }
  const starting = state.syncAwaitingFreshStatus;
  const displayCompleted = completed && !starting;
  const runtimeRunning = Boolean(state.service?.running);
  const active = starting || (progress.state === "running" && runtimeRunning);
  if (progress.state === "running" && runtimeRunning) state.syncObservedRunning = true;
  root.hidden = !active && !completed;
  if (progress.state === "running" && !active) {
    state.syncDeviceEstimates = {};
    state.syncProgress = 0;
    return;
  }
  const total = Number(progress.devices_total || 0);
  const done = Number(progress.devices_done || 0);
  const from = formatRangeDate(progress.from || state.config?.import_start_date) || "earliest available";
  const to = formatRangeDate(progress.to || state.config?.import_end_date) || "today";
  const counts = `${Number(progress.uploaded || 0)} uploaded · ${Number(progress.skipped || 0)} already present/skipped · ${Number(progress.failed || 0)} failed`;
  const configuredDevices = state.config?.devices || [];
  const backendDevices = progress.devices && typeof progress.devices === "object" ? progress.devices : {};
  const deviceRows = configuredDevices.map((device, index) => {
    const saved = backendDevices[device.device_id] || {};
    let deviceState = saved.state || (displayCompleted ? "completed" : index < done ? "completed" : index === done && active ? "running" : "waiting");
    if (displayCompleted && ["waiting", "running"].includes(deviceState)) deviceState = Number(progress.failed || 0) ? "completed_with_errors" : "completed";
    if (starting) deviceState = index === 0 ? "running" : "waiting";
    const processed = Number(saved.processed || 0);
    const recordsTotal = Number(saved.records_total || 0);
    const realPercent = recordsTotal ? Math.min(100, processed / recordsTotal * 100) : 0;
    let estimate = Number(state.syncDeviceEstimates[device.device_id] || 0);
    if (deviceState === "completed" || deviceState === "completed_with_errors") estimate = 100;
    else if (deviceState === "running") {
      estimate = Math.max(estimate || 3, realPercent);
      estimate = Math.min(94, estimate + (recordsTotal ? .18 : .32));
    } else estimate = 0;
    state.syncDeviceEstimates[device.device_id] = estimate;
    const currentDate = formatRangeDate(saved.current_date);
    const deviceFrom = formatRangeDate(saved.from || progress.from || state.config?.import_start_date) || from;
    const deviceTo = formatRangeDate(saved.to || progress.to || state.config?.import_end_date) || to;
    const stateLabel = deviceState === "running" ? (recordsTotal ? "Uploading punches" : "Connecting to clock") : deviceState === "completed" ? "Completed" : deviceState === "completed_with_errors" ? "Finished with errors" : "Waiting";
    const dateLabel = currentDate ? `Sync date: ${currentDate}` : deviceState === "running" ? "Reading attendance dates…" : `Dates: ${deviceFrom} – ${deviceTo}`;
    return {
      id: device.device_id,
      index: Number(saved.index || index + 1),
      state: deviceState,
      percent: estimate,
      html: `<div class="sync-device-row ${escapeAttr(deviceState.replaceAll("_", "-"))}">
        <div class="sync-device-identity"><strong>${escapeHtml(displayName(device.device_id))}</strong><span>Device ${Number(saved.index || index + 1)} of ${total || configuredDevices.length}</span></div>
        <div class="sync-device-date"><strong>${escapeHtml(dateLabel)}</strong><span>${escapeHtml(stateLabel)} · ${escapeHtml(deviceFrom)} through ${escapeHtml(deviceTo)}</span><div class="sync-device-bar"><i style="width:${estimate.toFixed(1)}%"></i></div></div>
        <div class="sync-device-stats"><b>${Math.round(estimate)}%</b>${processed}${recordsTotal ? ` / ${recordsTotal}` : ""} punches · ${Number(saved.failed || 0)} failed</div>
      </div>`,
    };
  });
  $("#syncDeviceProgress").innerHTML = deviceRows.map((item) => item.html).join("");
  const percent = deviceRows.length ? deviceRows.reduce((sum, item) => sum + item.percent, 0) / deviceRows.length : displayCompleted ? 100 : state.syncProgress;
  $("#syncProgressBar").style.width = `${percent}%`;
  $("#syncProgressPercent").textContent = `${Math.round(percent)}%`;
  $("#syncProgressCounts").textContent = counts;
  const runningDevice = deviceRows.find((item) => item.state === "running");
  const completedDevices = deviceRows.filter((item) => item.state === "completed" || item.state === "completed_with_errors").length;
  const deviceSummary = runningDevice
    ? `${displayName(runningDevice.id)} ${Math.round(runningDevice.percent)}% · ${completedDevices} of ${deviceRows.length} complete`
    : `${completedDevices} of ${deviceRows.length} devices complete`;
  $("#syncDeviceSummary").textContent = deviceSummary;
  const runningDetails = runningDevice ? backendDevices[runningDevice.id] || {} : {};
  const runningDate = formatRangeDate(runningDetails.current_date);
  if (displayCompleted) {
    $("#syncProgressLabel").textContent = progress.state === "completed" ? `Sync ${from} to ${to} finished successfully` : `Sync ${from} to ${to} finished with errors`;
  } else if (starting) {
    $("#syncProgressLabel").textContent = `Starting sync ${from} to ${to} · preparing device 1 of ${total || configuredDevices.length || "…"}`;
  } else {
    $("#syncProgressLabel").textContent = `Syncing ${from} to ${to} · ${runningDevice ? displayName(runningDevice.id) : `device ${Math.min(done + 1, total || 1)}`} ${runningDate ? `· ${runningDate}` : ""}`;
  }
  if (displayCompleted && completedAt && state.syncObservedRunning && state.lastNotifiedSyncCompletion !== completedAt) {
    const hasErrors = progress.state === "completed_with_errors";
    showToast(hasErrors ? `Synchronization finished with ${Number(progress.failed || 0)} error${Number(progress.failed || 0) === 1 ? "" : "s"}. Review Activity logs.` : `Synchronization finished successfully. ${Number(progress.uploaded || 0)} check-in${Number(progress.uploaded || 0) === 1 ? "" : "s"} uploaded.`, hasErrors);
    state.lastNotifiedSyncCompletion = completedAt;
    state.syncObservedRunning = false;
    state.syncRunActive = false;
  }
}

function formatRangeDate(value) {
  const raw = String(value || "").replaceAll("-", "");
  if (raw.length !== 8) return "";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(`${raw.slice(0,4)}-${raw.slice(4,6)}-${raw.slice(6,8)}T00:00:00`));
}

function startSyncProgress() {
  state.syncRunActive = true;
  state.syncProgress = 6;
  state.syncAwaitingFreshStatus = true;
  state.syncPreviousCompletedAt = state.status?.sync_progress?.completed_at || null;
  state.syncDeviceEstimates = {};
  renderSyncProgress();
}

function finishSyncProgress(success = true) {
  if (!state.syncRunActive) return;
  state.syncProgress = success ? 100 : state.syncProgress;
  renderSyncProgress();
  if (!success) {
    state.syncRunActive = false;
    state.syncAwaitingFreshStatus = false;
    state.syncProgress = 0;
  }
}

async function pollSyncProgress() {
  const progress = state.status?.sync_progress || {};
  const shouldPoll = state.syncRunActive || state.syncAwaitingFreshStatus || (progress.state === "running" && state.service?.running) || (state.service?.running && state.service?.mode === "once");
  if (!shouldPoll || syncProgressPollBusy) return;
  syncProgressPollBusy = true;
  try {
    const result = await api("/api/sync-progress");
    state.status = { ...(state.status || {}), sync_progress: result.sync_progress };
    state.service = result.service;
    renderSyncProgress();
  } catch (_) { /* The full dashboard refresh will retry status shortly. */ }
  finally { syncProgressPollBusy = false; }
}

async function loadLogs() {
  try {
    const result = await api(`/api/logs?kind=${encodeURIComponent(state.logKind)}`);
    $("#logOutput").textContent = result.lines.length ? result.lines.join("\n") : "No log entries yet.";
    $("#logOutput").scrollTop = $("#logOutput").scrollHeight;
  } catch (error) { $("#logOutput").textContent = error.message; }
}

function prepareDeviceUserLocations() {
  const select = $("#deviceUserLocation");
  const current = select.value;
  const devices = state.config?.devices || [];
  select.innerHTML = devices.length
    ? devices.map((device) => `<option value="${escapeAttr(device.device_id)}">${escapeHtml(displayName(device.device_id))} · ${escapeHtml(device.ip)}</option>`).join("")
    : '<option value="">No devices configured</option>';
  if (devices.some((device) => device.device_id === current)) select.value = current;
}

function renderDeviceUserScan(result) {
  state.deviceUserScan = result;
  state.deviceUserTable.page = 1;
  const summary = result.summary;
  $("#deviceUserScanStatus").textContent = `Scanned ${displayName(result.device.device_id)} at ${formatDate(result.scanned_at)}.`;
  $("#deviceUserMetrics").hidden = false;
  $("#deviceUserMetrics").innerHTML = `
    <article><span>Clock users</span><strong>${summary.total}</strong></article>
    <article><span>With fingerprints</span><strong>${summary.with_fingerprint}</strong></article>
    <article><span>Already in ERPNext</span><strong>${summary.in_erp}</strong></article>
    <article><span>Ready to import</span><strong>${summary.eligible}</strong></article>`;
  const counts = {
    all: result.users.length,
    eligible: result.users.filter((user) => user.eligible).length,
    missing: result.users.filter((user) => !user.erp_employee && !user.possible_erp_match).length,
    exists: result.users.filter((user) => Boolean(user.erp_employee)).length,
    possible: result.users.filter((user) => Boolean(user.possible_erp_match)).length,
    "no-fingerprint": result.users.filter((user) => !user.has_fingerprint).length,
  };
  const filterLabels = { all: "All users", eligible: "Eligible", missing: "Missing in ERPNext", exists: "Exists in ERPNext", possible: "Possible match", "no-fingerprint": "No fingerprint" };
  $$("#deviceUserFilter option").forEach((option) => { option.textContent = `${filterLabels[option.value]} (${counts[option.value]})`; });
  const options = result.options;
  $("#deviceUserCompany").innerHTML = '<option value="">Choose company</option>' + options.companies.map((value) => `<option value="${escapeAttr(value)}">${escapeHtml(value)}</option>`).join("");
  $("#deviceUserBranch").innerHTML = '<option value="">No branch selected</option>' + options.branches.map((value) => `<option value="${escapeAttr(value)}">${escapeHtml(value)}</option>`).join("");
  $("#deviceUserCompany").value = options.default_company || "";
  $("#deviceUserBranch").value = options.default_branch || "";
  $("#deviceUserJoiningDate").value = options.default_joining_date || "";
  const genderOptions = '<option value="">Choose</option>' + options.genders.map((value) => `<option value="${escapeAttr(value)}">${escapeHtml(value)}</option>`).join("");
  $("#deviceUserRows").innerHTML = result.users.length ? result.users.map((user) => {
    let erpStatus = "Missing from ERPNext";
    if (user.erp_employee) erpStatus = `Exists: ${user.erp_employee.employee_name}`;
    else if (user.possible_erp_match) erpStatus = `Possible name match: ${user.possible_erp_match.employee_name} (ID ${user.possible_erp_match.attendance_device_id || "missing"})`;
    else if (!user.has_fingerprint) erpStatus = "Not eligible — no fingerprint";
    else if (!user.name || !user.user_id) erpStatus = "Not eligible — missing name or ID";
    const status = user.erp_employee ? "exists" : user.possible_erp_match ? "possible" : user.eligible ? "eligible" : !user.has_fingerprint ? "no-fingerprint" : "missing";
    const searchValue = `${user.user_id || ""} ${user.name || ""} ${erpStatus}`.toLowerCase();
    return `<tr class="${user.eligible ? "eligible-user" : ""}" data-device-user-row data-status="${status}" data-missing="${user.erp_employee || user.possible_erp_match ? "false" : "true"}" data-no-fingerprint="${user.has_fingerprint ? "false" : "true"}" data-search="${escapeAttr(searchValue)}">
      <td><input class="user-import-check" type="checkbox" data-import-user="${escapeAttr(user.user_id)}" ${user.eligible ? "" : "disabled"}></td>
      <td>${escapeHtml(user.user_id || "—")}</td>
      <td><input class="user-name" type="text" value="${escapeAttr(user.name || "")}" placeholder="Employee name" maxlength="24"></td>
      <td><input class="erp-employee-number" type="text" value="${escapeAttr(user.user_id || "")}" placeholder="ERP Employee ID" ${user.eligible ? "" : "disabled"}></td>
      <td><span class="biometric-id" title="ERPNext Attendance Device ID">${escapeHtml(user.user_id || "—")}</span></td>
      <td><button class="button ghost save-clock-name" type="button" data-clock-name-user="${escapeAttr(user.user_id)}" ${user.user_id ? "" : "disabled"}>Save to clock</button></td>
      <td><span class="enrollment ${user.has_fingerprint ? "ready" : "missing"}">${user.has_fingerprint ? `${user.fingerprint_count} enrolled` : "None"}</span></td>
      <td>${escapeHtml(erpStatus)}</td>
      <td><select class="user-gender" ${user.eligible ? "" : "disabled"}>${genderOptions}</select></td>
      <td><input class="user-birth-date" type="date" ${user.eligible ? "" : "disabled"}></td>
    </tr>`;
  }).join("") : '<tr><td colspan="10" class="empty-cell">The device returned no users.</td></tr>';
  $$("[data-clock-name-user]").forEach((button) => button.addEventListener("click", saveDeviceUserName));
  $$(".user-import-check").forEach((checkbox) => checkbox.addEventListener("change", () => {
    checkbox.closest("tr").classList.toggle("selected-user", checkbox.checked);
    updateDeviceUserSelection();
  }));
  $("#deviceUserImport").hidden = false;
  $("#deviceUserImportResults").innerHTML = "";
  applyDeviceUserView();
}

function deviceUserMatches(row) {
  const filter = state.deviceUserTable.filter;
  const search = state.deviceUserTable.search.toLowerCase();
  const filterMatch = filter === "all"
    || (filter === "missing" && row.dataset.missing === "true")
    || (filter === "no-fingerprint" && row.dataset.noFingerprint === "true")
    || row.dataset.status === filter;
  return filterMatch && (!search || row.dataset.search.includes(search));
}

function updateDeviceUserSelection() {
  const selected = $$(".user-import-check:checked").length;
  const root = $("#deviceUserSelectedCount");
  root.classList.toggle("has-selection", selected > 0);
  $("strong", root).textContent = `${selected} selected`;
  $("span", root).textContent = selected ? "Ready for ERPNext upload" : "No users selected";
}

function applyDeviceUserView() {
  const rows = $$("[data-device-user-row]");
  const matches = rows.filter(deviceUserMatches);
  const pages = Math.max(1, Math.ceil(matches.length / state.deviceUserTable.pageSize));
  state.deviceUserTable.page = Math.min(Math.max(1, state.deviceUserTable.page), pages);
  const start = (state.deviceUserTable.page - 1) * state.deviceUserTable.pageSize;
  const visible = new Set(matches.slice(start, start + state.deviceUserTable.pageSize));
  rows.forEach((row) => { row.hidden = !visible.has(row); });
  const shownEnd = Math.min(start + state.deviceUserTable.pageSize, matches.length);
  $("#deviceUserPageLabel").textContent = matches.length ? `${start + 1}–${shownEnd} of ${matches.length}` : "0 users";
  $("#deviceUserPrev").disabled = state.deviceUserTable.page <= 1;
  $("#deviceUserNext").disabled = state.deviceUserTable.page >= pages;
  updateDeviceUserSelection();
}

async function scanDeviceUsers() {
  const button = $("#scanDeviceUsers");
  const deviceId = $("#deviceUserLocation").value;
  if (!deviceId) { showToast("Choose a configured device.", true); return; }
  button.disabled = true;
  button.textContent = "Scanning clock…";
  $("#deviceUserScanStatus").textContent = "Reading user names and fingerprint enrollment directly from the clock…";
  try {
    const result = await api(`/api/device-users/scan?device_id=${encodeURIComponent(deviceId)}`);
    renderDeviceUserScan(result);
    showToast(`Found ${result.summary.eligible} fingerprint user${result.summary.eligible === 1 ? "" : "s"} ready for ERPNext review.`);
  } catch (error) {
    $("#deviceUserScanStatus").textContent = error.message;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Scan device users";
  }
}

async function saveDeviceUserName(event) {
  const button = event.currentTarget;
  const row = button.closest("tr");
  const userId = button.dataset.clockNameUser;
  const name = $(".user-name", row).value.trim().replace(/\s+/g, " ");
  if (!name) { showToast("Enter a name before saving to the clock.", true); return; }
  if (new TextEncoder().encode(name).length > 24) { showToast("Clock names must be 24 bytes or fewer.", true); return; }
  if (!confirm(`Rename clock user ${userId} to ${name}? The fingerprint enrollment will not be changed.`)) return;
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const result = await api("/api/device-users/name", { method: "POST", body: JSON.stringify({
      device_id: state.deviceUserScan.device.device_id,
      user_id: userId,
      name,
    }) });
    await scanDeviceUsers();
    showToast(result.message);
  } catch (error) {
    showToast(error.message, true);
    button.disabled = false;
    button.textContent = "Save to clock";
  }
}

async function importDeviceUsers() {
  const checked = $$("[data-import-user]:checked");
  if (!checked.length) { showToast("Select at least one eligible device user.", true); return; }
  const users = [];
  for (const checkbox of checked) {
    const row = checkbox.closest("tr");
    const name = $(".user-name", row).value.trim().replace(/\s+/g, " ");
    const employeeNumber = $(".erp-employee-number", row).value.trim();
    const gender = $(".user-gender", row).value;
    const dateOfBirth = $(".user-birth-date", row).value;
    if (!name) { showToast(`Enter a name for clock user ${checkbox.dataset.importUser}.`, true); return; }
    if (!employeeNumber) { showToast(`Enter an ERP Employee ID for clock user ${checkbox.dataset.importUser}.`, true); return; }
    if (!gender || !dateOfBirth) { showToast(`Choose gender and date of birth for device ID ${checkbox.dataset.importUser}.`, true); return; }
    users.push({ user_id: checkbox.dataset.importUser, name, employee_number: employeeNumber, gender, date_of_birth: dateOfBirth });
  }
  const company = $("#deviceUserCompany").value;
  const dateOfJoining = $("#deviceUserJoiningDate").value;
  if (!company || !dateOfJoining) { showToast("Choose the company and date of joining.", true); return; }
  if (!confirm(`Create ${users.length} selected employee${users.length === 1 ? "" : "s"} as Inactive in ERPNext? HR must review and activate them.`)) return;
  const button = $("#importDeviceUsers");
  button.disabled = true;
  button.textContent = "Uploading…";
  try {
    const result = await api("/api/device-users/import", { method: "POST", body: JSON.stringify({
      device_id: state.deviceUserScan.device.device_id,
      company,
      branch: $("#deviceUserBranch").value,
      date_of_joining: dateOfJoining,
      users,
    }) });
    $("#deviceUserImportResults").innerHTML = result.results.map((item) => `<div class="import-result ${item.ok ? "" : "failed"}"><strong>${escapeHtml(item.name || item.user_id)}</strong><span>${escapeHtml(item.message)}</span></div>`).join("");
    showToast(result.message, result.created !== result.total);
    await loadState(false);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Upload selected to ERPNext"; }
}

async function loadCorrections() {
  try {
    const result = await api("/api/corrections");
    state.corrections = result;
    initializeCorrectionRange(result.pending || []);
    $("#correctionState").textContent = result.enabled ? "Automatic correction is enabled" : "Automatic correction is disabled";
    $("#correctionOverallToggle").checked = result.enabled;
    $("#locationCorrectionToggles").innerHTML = result.locations.map((location) => `
      <label class="location-toggle">
        <span><strong>${escapeHtml(displayName(location.device_id))}</strong><small>${escapeHtml(location.ip)}</small></span>
        <span class="toggle-control"><input type="checkbox" data-correction-location="${escapeAttr(location.device_id)}" ${location.enabled ? "checked" : ""}><span class="toggle-track"></span><b>${location.enabled ? "ON" : "OFF"}</b></span>
      </label>`).join("");
    $$('[data-correction-location]').forEach((input) => input.addEventListener("change", () => {
      input.parentElement.querySelector("b").textContent = input.checked ? "ON" : "OFF";
      renderPendingCorrections();
    }));
    renderPendingCorrections();
    const disabled = result.shift_status.filter((shift) => !Number(shift.enable_auto_attendance));
    $("#shiftHealth").innerHTML = result.shift_status.length ? result.shift_status.map((shift) => `
      <div class="shift-chip ${Number(shift.enable_auto_attendance) ? "ready" : "warning"}"><strong>${escapeHtml(shift.name)}</strong><span>${Number(shift.enable_auto_attendance) ? "Auto Attendance on" : "Auto Attendance off"}</span></div>`).join("") : '<div class="data-note">ERPNext Shift Type status could not be loaded.</div>';
    $("#correctionHistory").textContent = result.history.length ? result.history.join("\n") : "No automated corrections have been issued yet.";
    if (disabled.length) $("#correctionState").textContent += ` · ${disabled.length} ERPNext shift${disabled.length === 1 ? " has" : "s have"} Auto Attendance off`;
  } catch (error) { showToast(error.message, true); }
}

function renderPendingCorrections() {
  const allPending = state.corrections?.pending || [];
  const overallEnabled = $("#correctionOverallToggle").checked;
  const enabledLocations = Object.fromEntries(
    $$('[data-correction-location]').map((input) => [input.dataset.correctionLocation, input.checked])
  );
  const from = $("#correctionFromDate").value;
  const to = $("#correctionToDate").value;
  const visible = overallEnabled
    ? allPending.filter((item) => (enabledLocations[item.device_id] ?? item.correction_enabled)
      && (!from || String(item.assignment_date || "") >= from)
      && (!to || String(item.assignment_date || "") <= to))
    : [];
  const hiddenCount = allPending.length - visible.length;
  $("#pendingCount").textContent = hiddenCount
    ? `${visible.length} pending · ${hiddenCount} hidden by correction settings`
    : `${visible.length} pending`;
  $("#pendingCorrections").innerHTML = visible.length ? visible.map((item) => `
    <tr><td>${escapeHtml(item.employee_id)}</td><td>${escapeHtml(displayName(item.device_id))}</td><td>${escapeHtml(item.assignment_date || "—")}</td><td>${escapeHtml(item.shift)}</td><td>${escapeHtml(formatDate(item.real_punch))}</td><td>${escapeHtml(item.missing)} at ${escapeHtml(item.scheduled_time || "shift boundary")} · ${item.grace_minutes} min grace</td></tr>`).join("") : `<tr><td colspan="6" class="empty-cell">${allPending.length ? "No unmatched punches match the enabled locations and selected dates." : "No unmatched punches are waiting."}</td></tr>`;
}

function initializeCorrectionRange(pending) {
  if (!pending.length || ($("#correctionFromDate").value && $("#correctionToDate").value)) return;
  const dates = pending.map((item) => String(item.assignment_date || "")).filter(Boolean).sort();
  if (dates.length) { $("#correctionFromDate").value = dates[0]; $("#correctionToDate").value = dates.at(-1); }
}

async function runCorrectionRange(event) {
  const button = event.currentTarget;
  const from = $("#correctionFromDate").value;
  const to = $("#correctionToDate").value;
  const enabled = $("#correctionOverallToggle").checked;
  const locationSettings = Object.fromEntries($$('[data-correction-location]').map((input) => [input.dataset.correctionLocation, input.checked]));
  const locations = Object.entries(locationSettings).filter(([, isEnabled]) => isEnabled).map(([deviceId]) => deviceId);
  if (!from || !to) { showToast("Choose both correction dates.", true); return; }
  if (to < from) { showToast("Correction Through date cannot be earlier than From date.", true); return; }
  if (!enabled || !locations.length) { showToast("Turn correction on for at least one location.", true); return; }
  if (!confirm(`Create missing punches from ${from} through ${to} for the enabled locations?`)) return;
  button.disabled = true; button.textContent = "Correcting…";
  try {
    await api("/api/corrections/settings", { method: "POST", body: JSON.stringify({ enabled, locations: locationSettings }) });
    const result = await api("/api/corrections/run-range", { method: "POST", body: JSON.stringify({ from, to, locations }) });
    showToast(result.message); await loadCorrections(); await loadState(false);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Correct selected range"; }
}

async function saveCorrectionSettings() {
  const enabled = $("#correctionOverallToggle").checked;
  const locations = Object.fromEntries($$('[data-correction-location]').map((input) => [input.dataset.correctionLocation, input.checked]));
  const isDisabling = !enabled || Object.values(locations).some((value) => !value);
  if (isDisabling && state.corrections?.pending?.length && !confirm("Some unmatched punches are waiting. Disabled locations will keep those punches paused until correction is turned on again. Continue?")) return;
  try {
    const result = await api("/api/corrections/settings", { method: "POST", body: JSON.stringify({ enabled, locations }) });
    showToast(result.message);
    await loadState(false);
    await loadCorrections();
  } catch (error) { showToast(error.message, true); }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}
function escapeAttr(value) { return escapeHtml(value); }

$$(".nav-link[data-view]").forEach((button) => button.addEventListener("click", () => goTo(button.dataset.view)));
$$('[data-go]').forEach((button) => button.addEventListener("click", () => goTo(button.dataset.go)));
$("#refreshButton").addEventListener("click", () => loadState(true));
$("#serviceToggleButton").addEventListener("click", () => serviceAction(state.service?.running ? "stop" : "start"));
$("#runOnceButton").addEventListener("click", async () => {
  try { await saveAll(); await serviceAction("run-once"); }
  catch (error) { showToast(error.message, true); }
});
$("#syncAllClocksButton").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "Synchronizing clocks…";
  try {
    const result = await api("/api/devices/time-sync", { method: "POST", body: "{}" });
    renderClockResults(result.results);
    showToast(result.message, result.succeeded !== result.total);
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Sync all device clocks"; }
});
$("#addDeviceButton").addEventListener("click", () => {
  const row = deviceRow({}, { isNew: true });
  $("#deviceEditor").prepend(row);
  configFormState.dirty = true;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  $('[data-key="device_id"]', row).focus();
});
$("#configForm").addEventListener("input", () => { configFormState.dirty = true; });
$("#configForm").addEventListener("change", () => { configFormState.dirty = true; });
$("#configForm").addEventListener("submit", async (event) => { event.preventDefault(); try { await saveAll(); } catch (error) { showToast(error.message, true); } });
$("#testErpButton").addEventListener("click", async () => {
  try { await saveAll(); const result = await api("/api/test/erpnext", { method: "POST", body: "{}" }); showToast(result.message); }
  catch (error) { showToast(error.message, true); }
});
$$('[data-reveal]').forEach((button) => button.addEventListener("click", () => {
  const input = $(`#${button.dataset.reveal}`);
  const revealing = input.type === "password";
  input.type = revealing ? "text" : "password";
  button.textContent = revealing ? "Hide" : "Show";
  button.setAttribute("aria-label", `${revealing ? "Hide" : "Show"} ${button.dataset.reveal === "apiKey" ? "API key" : "API secret"}`);
}));
$("#apiSecret").addEventListener("input", (event) => {
  const button = $('[data-reveal="apiSecret"]');
  const hasValue = Boolean(event.currentTarget.value);
  button.disabled = !hasValue;
  button.textContent = hasValue ? "Show" : (state.config?.has_api_secret ? "Saved" : "Show");
  button.setAttribute("aria-label", hasValue ? "Show API secret" : "API secret is saved securely");
});
$$('[data-log]').forEach((button) => button.addEventListener("click", () => {
  $$('[data-log]').forEach((item) => item.classList.toggle("active", item === button));
  state.logKind = button.dataset.log;
  $("#logLabel").textContent = button.textContent;
  loadLogs();
}));
$("#refreshLogs").addEventListener("click", loadLogs);
$("#scanDeviceUsers").addEventListener("click", scanDeviceUsers);
$("#importDeviceUsers").addEventListener("click", importDeviceUsers);
$("#deviceUserFilter").addEventListener("change", (event) => { state.deviceUserTable.filter = event.currentTarget.value; state.deviceUserTable.page = 1; applyDeviceUserView(); });
$("#deviceUserSearch").addEventListener("input", (event) => { state.deviceUserTable.search = event.currentTarget.value.trim(); state.deviceUserTable.page = 1; applyDeviceUserView(); });
$("#deviceUserPageSize").addEventListener("change", (event) => { state.deviceUserTable.pageSize = Number(event.currentTarget.value) || 20; state.deviceUserTable.page = 1; applyDeviceUserView(); });
$("#deviceUserPrev").addEventListener("click", () => { state.deviceUserTable.page -= 1; applyDeviceUserView(); });
$("#deviceUserNext").addEventListener("click", () => { state.deviceUserTable.page += 1; applyDeviceUserView(); });
$("#refreshCorrections").addEventListener("click", loadCorrections);
$("#saveCorrectionSettings").addEventListener("click", saveCorrectionSettings);
$("#correctionFromDate").addEventListener("change", renderPendingCorrections);
$("#correctionToDate").addEventListener("change", renderPendingCorrections);
$("#runCorrectionRange").addEventListener("click", runCorrectionRange);
$("#correctionOverallToggle").addEventListener("change", (event) => {
  $("#correctionState").textContent = event.currentTarget.checked ? "Automatic correction will be enabled after saving" : "Automatic correction will be disabled after saving";
  renderPendingCorrections();
});
$("#cancelUserEdit").addEventListener("click", resetUserForm);
$("#userForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("#editUserId").value;
  const payload = { username: $("#adminUsername").value, password: $("#adminPassword").value, role: $("#adminRole").value, active: $("#adminUserActive").checked };
  try { const result = await api(id ? `/api/admin/users/${id}` : "/api/admin/users", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) }); showToast(result.message); resetUserForm(); await loadUsers(); } catch (error) { showToast(error.message, true); }
});
$("#brandingForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const logo_data = await readLogo($("#adminLogo").files[0]);
    const result = await api("/api/admin/branding", { method: "POST", body: JSON.stringify({ app_name: $("#adminAppName").value, footer_text: $("#adminFooter").value, logo_data, theme_preset: $("#adminThemePreset").value, theme_mode: $("#adminThemeMode").value }) });
    renderBranding(result.branding, state.auth); $("#adminLogo").value = ""; showToast(result.message);
  } catch (error) { showToast(error.message, true); }
});

$("#adminThemePreset").addEventListener("change", () => applyTheme($("#adminThemePreset").value, $("#adminThemeMode").value));
$("#adminThemeMode").addEventListener("change", () => applyTheme($("#adminThemePreset").value, $("#adminThemeMode").value));
$("#adminLogo").addEventListener("change", (event) => {
  const logoTheme = $('#adminThemePreset option[value="logo"]');
  if (event.currentTarget.files[0]) { logoTheme.disabled = false; logoTheme.textContent = "Logo colors"; }
});
$("#backupExportForm").addEventListener("submit", exportSetupBackup);
$("#backupRestoreForm").addEventListener("submit", restoreSetupBackup);

applyTheme();
loadState();
updateSystemClock();
setInterval(updateSystemClock, 1000);
setInterval(renderSyncProgress, 1000);
setInterval(pollSyncProgress, 2000);
setInterval(() => {
  const wasRunningOnce = state.service?.running && state.service?.mode === "once";
  loadState().then(() => {
    renderSyncProgress();
    if (state.syncRunActive && wasRunningOnce && !state.service?.running && String(state.status?.sync_progress?.state || "").startsWith("completed")) finishSyncProgress(true);
  });
}, 15000);

const $ = (selector) => document.querySelector(selector);
const keyInput = $("#api-key");
keyInput.value = sessionStorage.getItem("auxris-key") || "";
keyInput.addEventListener("input", () => sessionStorage.setItem("auxris-key", keyInput.value));

const request = async (path, options = {}) => {
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${keyInput.value}`, ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({ detail: "Invalid server response" }));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
};

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab, .panel").forEach((element) => element.classList.remove("active"));
    tab.classList.add("active");
    $(`#${tab.dataset.panel}-panel`).classList.add("active");
    if (tab.dataset.panel === "catalog") loadCatalog();
  });
});

const showResult = (html) => { $("#result").innerHTML = html; };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);

$("#scan-panel").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#scan-audio").files[0];
  const form = new FormData();
  form.append("audio", file);
  if ($("#intended-use").value) form.append("intended_use", $("#intended-use").value);
  showResult("<p>Extracting evidence…</p>");
  try {
    const data = await request("/v1/identify", { method: "POST", body: form });
    if (!data.matched) return showResult("<h3>No registered material found</h3><p>This means no match passed the configured evidence threshold—not that the audio is copyright-free.</p>");
    showResult(`<h3>${data.matches.length} evidence match${data.matches.length === 1 ? "" : "es"}</h3>${data.matches.map((match) => `<div class="match"><strong>${escapeHtml(match.asset.title || match.asset.asset_id)}</strong> <span class="score">${Math.round(match.score * 100)} score</span><p>${match.evidence.aligned_votes} aligned landmarks · source ${match.evidence.source_start_seconds}s–${match.evidence.source_end_seconds}s · policy: ${escapeHtml(match.policy.decision)}</p></div>`).join("")}`);
  } catch (error) { showResult(`<h3>Scan failed</h3><p>${escapeHtml(error.message)}</p>`); }
});

$("#register-panel").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData();
  form.append("audio", $("#register-audio").files[0]);
  form.append("asset_id", $("#asset-id").value);
  form.append("title", $("#asset-title").value);
  form.append("creator", $("#asset-creator").value);
  form.append("license_id", $("#asset-license").value);
  form.append("allowed_uses", $("#allowed-uses").value);
  showResult("<p>Registering durable fingerprints…</p>");
  try {
    const data = await request("/v1/assets", { method: "POST", body: form });
    showResult(`<h3>Asset registered</h3><p>${escapeHtml(data.asset_id)} · ${data.fingerprints} fingerprints · ${data.duration_seconds}s</p>`);
    event.target.reset();
  } catch (error) { showResult(`<h3>Registration failed</h3><p>${escapeHtml(error.message)}</p>`); }
});

async function loadCatalog() {
  try {
    const data = await request("/v1/catalog");
    $("#catalog-summary").innerHTML = `<div class="metric"><strong>${data.stats.assets}</strong><span>registered assets</span></div><div class="metric"><strong>${data.stats.fingerprints}</strong><span>fingerprints</span></div><div class="metric"><strong>v${data.stats.schema_version}</strong><span>index schema</span></div>`;
    $("#catalog-list").innerHTML = data.assets.length ? data.assets.map((asset) => `<div class="catalog-item"><strong>${escapeHtml(asset.title || asset.asset_id)}</strong><span>${escapeHtml(asset.license_id || "Policy unspecified")}</span></div>`).join("") : "<p>No assets registered yet.</p>";
  } catch (error) { $("#catalog-list").innerHTML = `<p>${escapeHtml(error.message)}</p>`; }
}

$("#refresh-catalog").addEventListener("click", loadCatalog);
fetch("/healthz").then((response) => response.json()).then(() => { $(".status").classList.add("online"); $("#status-text").textContent = "Local engine online"; }).catch(() => { $("#status-text").textContent = "Engine unavailable"; });

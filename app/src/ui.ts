import { invoke } from "@tauri-apps/api/core";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { ServiceDef, HealthStatus, ServiceControlResult } from "./services";
import { checkHealth } from "./health";
import { showLogPanel, hideLogPanel } from "./logs";

const healthCache = new Map<string, HealthStatus>();

export function renderServiceGrid(services: ServiceDef[]) {
  const grid = document.getElementById("service-grid")!;
  grid.innerHTML = "";

  // Group services
  const groups = new Map<string, ServiceDef[]>();
  for (const s of services) {
    const list = groups.get(s.group) || [];
    list.push(s);
    groups.set(s.group, list);
  }

  for (const [group, items] of groups) {
    const section = document.createElement("div");
    section.className = "service-group";

    const heading = document.createElement("h2");
    heading.className = "group-heading";
    heading.textContent = group;
    section.appendChild(heading);

    const cards = document.createElement("div");
    cards.className = "cards";

    for (const svc of items) {
      cards.appendChild(createServiceCard(svc));
    }

    section.appendChild(cards);
    grid.appendChild(section);
  }
}

function createServiceCard(svc: ServiceDef): HTMLElement {
  const card = document.createElement("div");
  card.className = "service-card";
  card.dataset.serviceId = svc.id;

  const status = healthCache.get(svc.id) || "unknown";

  card.innerHTML = `
    <div class="card-header">
      <span class="status-dot ${status}" id="dot-${svc.id}"></span>
      <span class="card-label">${svc.label}</span>
      <span class="card-port">:${svc.port}</span>
    </div>
    <div class="card-detail" id="detail-${svc.id}"></div>
    <div class="card-actions">
      <button data-action="start" data-id="${svc.id}">Start</button>
      <button data-action="stop" data-id="${svc.id}">Stop</button>
      <button data-action="restart" data-id="${svc.id}">Restart</button>
      <button data-action="open-ui" data-id="${svc.id}">Open UI</button>
      <button data-action="logs" data-id="${svc.id}">Logs</button>
    </div>
  `;

  return card;
}

export function updateHealthDot(serviceId: string, status: HealthStatus, detail: string | null) {
  healthCache.set(serviceId, status);
  const dot = document.getElementById(`dot-${serviceId}`);
  if (dot) {
    dot.className = `status-dot ${status}`;
  }
  const detailEl = document.getElementById(`detail-${serviceId}`);
  if (detailEl) {
    detailEl.textContent = detail || "";
  }
}

export async function pollHealth(services: ServiceDef[]) {
  const results = await Promise.all(services.map((s) => checkHealth(s)));
  for (const r of results) {
    updateHealthDot(r.service_id, r.status as HealthStatus, r.detail);
  }
}

export function setupEventDelegation(services: ServiceDef[]) {
  const grid = document.getElementById("service-grid")!;
  const serviceMap = new Map(services.map((s) => [s.id, s]));

  grid.addEventListener("click", async (e) => {
    const btn = (e.target as HTMLElement).closest("button") as HTMLButtonElement | null;
    if (!btn) return;

    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (!action || !id) return;

    const svc = serviceMap.get(id);
    if (!svc) return;

    if (action === "open-ui") {
      await openUrl(svc.uiUrl);
      return;
    }

    if (action === "logs") {
      showLogPanel(svc.id, svc.logFile);
      return;
    }

    // start / stop / restart
    btn.disabled = true;
    btn.textContent = "...";
    try {
      const result = await invoke<ServiceControlResult>("service_control", {
        serviceId: svc.id,
        script: svc.script || null,
        entrypoint: svc.entrypoint || null,
        action,
      });

      if (!result.success) {
        console.error(`${action} ${svc.id}:`, result.output);
      }

      // Re-check health after action
      setTimeout(async () => {
        const hr = await checkHealth(svc);
        updateHealthDot(hr.service_id, hr.status as HealthStatus, hr.detail);
      }, 2000);
    } finally {
      btn.disabled = false;
      btn.textContent = action.charAt(0).toUpperCase() + action.slice(1);
    }
  });

  // Log panel close button
  document.getElementById("log-close")?.addEventListener("click", hideLogPanel);
}

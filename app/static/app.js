"use strict";

const state = {
  view: "overview", catalogKind: "all", token: "", system: null,
  architecture: null, architectureCategory: "all", architectureQuery: "",
  architectureSelected: "", architectureZoom: 1, architectureLinks: true,
  architectureScenario: 0, architectureScenarioStep: 0, architectureActiveNodes: []
};
const scenarioNames = {
  knowledge: "Conocimiento corporativo",
  report: "Inventario técnico",
  incident: "Incidencia operativa"
};
const scenarioIcons = { knowledge: "⌕", report: "▤", incident: "⚑" };
const scenarioClasses = { knowledge: "", report: "report", incident: "incident" };
const statusNames = {
  running: "En curso",
  completed: "Completada",
  completed_local: "Completada en local",
  awaiting_approval: "Pendiente de aprobación",
  needs_review: "Requiere revisión",
  rejected: "Denegada",
  failed: "Fallida",
  cancelled: "Cancelada",
  pending: "Pendiente",
  approved: "Aprobada"
};

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char];
  });
}

function dateLabel(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"
  }).format(date);
}

function statusBadge(value) {
  const safe = String(value || "unknown").replace(/[^a-z_]/g, "");
  return '<span class="status-badge status-' + safe + '">' +
    escapeHtml(statusNames[value] || value || "Desconocido") + "</span>";
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(function () { toast.classList.remove("show"); }, 2800);
}

async function api(path, options) {
  const config = options || {};
  const headers = new Headers(config.headers || {});
  headers.set("Accept", "application/json");
  if (config.body && !(config.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (state.token) headers.set("Authorization", "Bearer " + state.token);
  const response = await fetch(path, Object.assign({}, config, { headers: headers }));
  if (response.status === 204) return null;
  const data = await response.json().catch(function () { return {}; });
  if (!response.ok) {
    const detail = data.detail || data.error || ("HTTP " + response.status);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function setView(name) {
  state.view = name;
  document.querySelectorAll(".view").forEach(function (view) {
    view.classList.toggle("active", view.id === "view-" + name);
  });
  document.querySelectorAll(".nav-item").forEach(function (item) {
    item.classList.toggle("active", item.dataset.view === name);
  });
  const active = document.querySelector('.nav-item[data-view="' + name + '"]');
  document.getElementById("crumb-title").textContent = active ? active.textContent.trim() : "Resumen";
  document.getElementById("sidebar").classList.remove("open");
  const loaders = {
    overview: loadOverview,
    workflows: updateScenarioForm,
    tasks: loadTasks,
    approvals: loadApprovals,
    catalog: loadCatalog,
    quality: loadQuality,
    activity: loadActivity,
    system: loadSystem
  };
  if (loaders[name]) loaders[name]();
}

function flowCell(scenario) {
  const label = scenarioNames[scenario] || scenario;
  return '<span class="flow-name"><i class="flow-mini ' +
    (scenarioClasses[scenario] || "") + '">' + escapeHtml(scenarioIcons[scenario] || "◇") +
    "</i>" + escapeHtml(label) + "</span>";
}

async function loadOverview() {
  try {
    const data = await api("/api/v1/dashboard");
    document.getElementById("metric-tasks").textContent = data.tasks_total;
    document.getElementById("metric-approvals").textContent = data.pending_approvals;
    document.getElementById("metric-agents").textContent = data.agents;
    document.getElementById("metric-quality").textContent =
      data.evaluation_pass_rate == null ? "—" : data.evaluation_pass_rate + "%";
    document.getElementById("metric-quality-note").textContent =
      data.evaluation_pass_rate == null ? "Sin ejecuciones evaluadas" : "Evaluaciones aprobadas";
    document.getElementById("approval-count").textContent = data.pending_approvals;
    const rows = data.recent_tasks || [];
    document.getElementById("recent-tasks").innerHTML = rows.length
      ? rows.map(function (task) {
        return "<tr><td>" + flowCell(task.scenario) + "</td><td>" +
          statusBadge(task.status) + "</td><td>" + escapeHtml(dateLabel(task.created_at)) +
          '</td><td><button class="row-link" data-task-open="' + escapeHtml(task.id) +
          '" aria-label="Ver tarea">→</button></td></tr>';
      }).join("")
      : '<tr><td colspan="4" class="loading-row">Todavía no hay ejecuciones.</td></tr>';
    updateEnvironment(data.model);
    document.getElementById("health-label").textContent = "API disponible";
    document.getElementById("footer-status").textContent = "Actualizado " + dateLabel(new Date().toISOString());
  } catch (error) {
    document.getElementById("health-label").textContent = "API sin conexión";
    document.getElementById("footer-status").textContent = error.message;
  }
}

function updateEnvironment(model) {
  const configured = model && model.configured;
  document.getElementById("environment-label").textContent =
    state.system && state.system.environment === "production" ? "Producción" : "Local / desarrollo";
  document.getElementById("actor-name").textContent = state.system ? state.system.actor : "Administrador local";
  document.getElementById("actor-role").textContent =
    state.system ? state.system.roles.join(", ") : "conectando";
  document.getElementById("metric-tasks-note").textContent =
    configured ? "Modelo conectado: " + model.model : "Registro persistente";
}

async function loadTasks() {
  const box = document.getElementById("tasks-list");
  box.innerHTML = '<div class="empty-state">Cargando tareas…</div>';
  try {
    const data = await api("/api/v1/tasks");
    const rows = data.items || [];
    box.innerHTML = rows.length ? rows.map(function (task) {
      return '<div class="task-row"><div class="task-main"><span class="flow-mini ' +
        (scenarioClasses[task.scenario] || "") + '">' + escapeHtml(scenarioIcons[task.scenario] || "◇") +
        '</span><div><strong>' + escapeHtml(task.title) + '</strong><small>' +
        escapeHtml(task.request) + '</small></div></div><div>' + statusBadge(task.status) +
        '</div><time>' + escapeHtml(dateLabel(task.created_at)) +
        '</time><button class="task-open" data-task-open="' + escapeHtml(task.id) +
        '" aria-label="Abrir tarea">→</button></div>';
    }).join("") : '<div class="empty-state">No hay tareas registradas.</div>';
  } catch (error) {
    box.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

async function loadApprovals() {
  const box = document.getElementById("approvals-list");
  box.innerHTML = '<div class="empty-state">Cargando aprobaciones…</div>';
  try {
    const data = await api("/api/v1/approvals");
    const rows = data.items || [];
    box.innerHTML = rows.length ? rows.map(function (item) {
      const sourceText = item.source === "jira"
        ? "Jira en vivo · " + (item.issue_key || "incidencia")
        : "Caso sintético local · no escribe en Jira";
      return '<article class="approval-card"><div class="eyebrow">BORRADOR DE ACCIÓN · ' +
        escapeHtml(item.task_id.slice(0, 10)) + '</div><h3>' + escapeHtml(item.task_title || "Incidencia") +
        '</h3><p>Solicitante: ' + escapeHtml(item.requested_by) +
        '</p><div class="approval-meta"><span>' + escapeHtml(sourceText) +
        '</span><span>Operación condicionada a aprobación</span></div><div class="approval-quote">' +
        escapeHtml(item.draft || "Sin borrador") + '</div><div class="approval-actions"><button class="button button-reject button-small" data-decision="reject" data-approval-id="' +
        escapeHtml(item.id) + '">Rechazar</button><button class="button button-approve button-small" data-decision="approve" data-approval-id="' +
        escapeHtml(item.id) + '">Aprobar borrador</button></div></article>';
    }).join("") : '<div class="empty-state">No hay aprobaciones pendientes. Las escrituras externas quedan detenidas hasta que se cree y revise una propuesta.</div>';
  } catch (error) {
    box.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

async function decideApproval(id, decision) {
  const comment = decision === "approve" ? "Aprobado desde la consola." : "Rechazado desde la consola.";
  try {
    const result = await api("/api/v1/approvals/" + encodeURIComponent(id) + "/decision", {
      method: "POST",
      body: JSON.stringify({ decision: decision, comment: comment })
    });
    showToast(decision === "approve"
      ? "Aprobación registrada. Efecto: " + result.external_effect
      : "Borrador rechazado. No se ejecutó ninguna acción.");
    await Promise.all([loadApprovals(), loadOverview()]);
  } catch (error) {
    showToast(error.message);
  }
}

async function loadCatalog() {
  const catalogBox = document.getElementById("catalog-list");
  const knowledgeBox = document.getElementById("knowledge-list");
  catalogBox.innerHTML = '<div class="empty-state">Cargando catálogo…</div>';
  try {
    const values = await Promise.all([api("/api/v1/catalog"), api("/api/v1/knowledge")]);
    const capabilities = values[0].capabilities || [];
    const documents = values[1].items || [];
    const filtered = state.catalogKind === "all"
      ? capabilities
      : capabilities.filter(function (item) { return item.kind === state.catalogKind; });
    catalogBox.innerHTML = filtered.length ? filtered.map(function (item) {
      const tags = (item.tags || []).map(function (tag) { return "<span>" + escapeHtml(tag) + "</span>"; }).join("");
      return '<article class="catalog-card"><div class="catalog-card-top"><span class="kind-pill kind-' +
        escapeHtml(item.kind) + '">' + escapeHtml(item.kind) + '</span><span class="control-state">' +
        escapeHtml(item.status) + '</span></div><h3>' + escapeHtml(item.name) +
        '</h3><p>' + escapeHtml(item.description) + '</p><div class="tag-list">' + tags +
        '</div><div class="catalog-card-foot"><span>' + escapeHtml(item.id) +
        '</span><span>v' + escapeHtml(item.version) + '</span></div></article>';
    }).join("") : '<div class="empty-state">No hay capacidades en esta categoría.</div>';
    knowledgeBox.innerHTML = documents.length ? documents.map(function (doc) {
      return '<article class="knowledge-card"><h3>' + escapeHtml(doc.title) +
        '</h3><p>' + escapeHtml(doc.content) +
        '</p><div class="knowledge-card-foot"><span>Alcance: ' + escapeHtml(doc.scope) +
        '</span><span>' + escapeHtml(doc.source_uri) + '</span></div></article>';
    }).join("") : '<div class="empty-state">No hay documentos visibles para tus permisos.</div>';
  } catch (error) {
    catalogBox.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

async function loadQuality() {
  const box = document.getElementById("evaluations-list");
  const overview = document.getElementById("quality-overview");
  const usageBox = document.getElementById("model-usage-detail");
  box.innerHTML = '<div class="empty-state">Cargando evaluaciones…</div>';
  usageBox.innerHTML = '<div class="empty-state">Cargando consumo de modelos…</div>';
  try {
    const results = await Promise.all([api("/api/v1/evaluations"), api("/api/v1/usage")]);
    const data = results[0];
    const usage = results[1];
    const items = data.items || [];
    const passed = items.filter(function (item) { return item.passed; }).length;
    const percent = items.length ? Math.round(100 * passed / items.length) : null;
    overview.innerHTML = '<article class="quality-card"><small>Ejecuciones evaluadas</small><strong>' +
      items.length + '</strong><small>Suites deterministas</small></article><article class="quality-card"><small>Evaluaciones aprobadas</small><strong>' +
      (percent == null ? "—" : percent + "%") + '</strong><small>Global · últimos resultados</small></article><article class="quality-card"><small>Controles principales</small><strong>3</strong><small>Permisos · procedencia · aprobación</small></article>';
    const cost = usage.estimated_cost_usd == null
      ? (usage.pricing_configured ? "Incompleto" : "No configurado")
      : new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", maximumFractionDigits: 5 }).format(usage.estimated_cost_usd);
    const numberLabel = function (value) { return new Intl.NumberFormat("es-ES").format(value || 0); };
    overview.insertAdjacentHTML("beforeend", '<article class="quality-card usage-card"><small>Llamadas a modelos</small><strong>' +
      numberLabel(usage.calls) + '</strong><small>' + numberLabel(usage.successful_calls) + ' correctas · ventana de 1.000 tareas</small></article><article class="quality-card usage-card"><small>Fallbacks a reglas</small><strong>' +
      numberLabel(usage.fallback_calls) + '</strong><small>Timeout, respuesta inválida o error de proveedor</small></article><article class="quality-card usage-card"><small>Tokens entrada / salida</small><strong>' +
      numberLabel(usage.input_tokens) + ' / ' + numberLabel(usage.output_tokens) + '</strong><small>' + (usage.token_counts_complete ? "Conteos reportados por el proveedor" : "Conteo parcial según respuestas disponibles") + '</small></article><article class="quality-card usage-card"><small>Coste estimado</small><strong>' +
      escapeHtml(cost) + '</strong><small>' + (usage.pricing_configured ? "Según precios unitarios configurados" : "Configura las tarifas por millón de tokens") + '</small></article>');
    const providers = usage.providers || [];
    usageBox.innerHTML = providers.length ? providers.map(function (provider) {
      const providerCost = provider.estimated_cost_usd == null ? "—" :
        new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", maximumFractionDigits: 5 }).format(provider.estimated_cost_usd);
      return '<article class="model-usage-row"><div><strong>' + escapeHtml(provider.provider) + '</strong><small>' + escapeHtml(provider.model) + '</small></div><span>' +
        numberLabel(provider.calls) + ' llamadas</span><span>' + numberLabel(provider.successful_calls) + ' correctas · ' + numberLabel(provider.fallback_calls) + ' fallback</span><span>' +
        numberLabel(provider.input_tokens) + ' / ' + numberLabel(provider.output_tokens) + ' tokens</span><span>' + escapeHtml(providerCost) + '</span></article>';
    }).join("") : '<div class="empty-state">Aún no hay llamadas a proveedores de modelos en la ventana consultada.</div>';
    box.innerHTML = items.length ? items.map(function (item) {
      const checks = item.checks || [];
      const passedChecks = checks.filter(function (check) { return check.passed; }).length;
      return '<div class="evaluation-row"><div><strong>' + escapeHtml(item.suite) +
        '</strong><small>Tarea ' + escapeHtml(item.task_id.slice(0, 10)) + ' · ' +
        escapeHtml(dateLabel(item.created_at)) + '</small></div><span class="score">' +
        escapeHtml(item.score) + '%</span><span class="check-count">' + passedChecks + "/" +
        checks.length + " controles</span>" + statusBadge(item.passed ? "approved" : "needs_review") +
        "</div>";
    }).join("") : '<div class="empty-state">Las evaluaciones aparecerán al ejecutar flujos.</div>';
  } catch (error) {
    overview.innerHTML = "";
    box.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

async function loadActivity() {
  const box = document.getElementById("activity-list");
  box.innerHTML = '<div class="empty-state">Cargando auditoría…</div>';
  try {
    const data = await api("/api/v1/activity");
    const items = data.items || [];
    box.innerHTML = items.length ? items.map(function (item) {
      return '<div class="audit-row"><time>' + escapeHtml(dateLabel(item.created_at)) +
        '</time><div><strong>' + escapeHtml(item.actor) + '</strong><small>' +
        escapeHtml(item.resource_type + (item.resource_id ? " · " + item.resource_id.slice(0, 12) : "")) +
        '</small></div><span class="audit-action">' + escapeHtml(item.action) +
        '</span><span>' + statusBadge(item.decision) + '</span></div>';
    }).join("") : '<div class="empty-state">Todavía no hay acciones auditadas.</div>';
  } catch (error) {
    box.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

const architectureColors = {
  foundation: "#b85b42", channels: "#286fd0", assets: "#178f80",
  data: "#7955b5", execution: "#258b91", control: "#b87520", external: "#53677f"
};

async function loadArchitecture() {
  if (state.architecture) return;
  const response = await fetch("/static/architecture.json", { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error("No se pudo cargar el mapa de arquitectura.");
  state.architecture = await response.json();
  const categories = document.getElementById("architecture-categories");
  categories.innerHTML = '<button class="architecture-category active" data-architecture-category="all">Todas <small>' +
    state.architecture.nodes.length + '</small></button>' +
    Object.entries(state.architecture.categories).map(function (entry) {
      const count = state.architecture.nodes.filter(function (node) { return node.category === entry[0]; }).length;
      return '<button class="architecture-category" data-architecture-category="' + escapeHtml(entry[0]) + '">' +
        escapeHtml(entry[1]) + ' <small>' + count + '</small></button>';
    }).join("");
  renderArchitectureMap();
  renderArchitectureScenarios();
}

function architectureMatches(node) {
  const categoryMatch = state.architectureCategory === "all" || node.category === state.architectureCategory;
  const query = state.architectureQuery.trim().toLocaleLowerCase("es");
  const extra = state.architecture.extra[node.id] || {};
  const searchable = [node.label, node.desc, node.value, node.example, node.question,
    ...(node.tech || []), extra.main, extra.alternatives, extra.implementation]
    .filter(Boolean).join(" ").toLocaleLowerCase("es");
  return categoryMatch && (!query || searchable.includes(query));
}

function renderArchitectureMap() {
  const data = state.architecture;
  if (!data) return;
  const edgeBox = document.getElementById("architecture-edges");
  const nodeBox = document.getElementById("architecture-nodes");
  edgeBox.innerHTML = data.edges.map(function (edge) {
    const id = edge[0];
    return '<path class="architecture-edge" data-architecture-edge="' + escapeHtml(id) + '" d="' +
      escapeHtml(edge[1]) + '" aria-hidden="true"></path>';
  }).join("") + (state.architectureLinks ? data.connections.map(function (edge) {
    const color = edge.type === "api" ? "#286fd0" : edge.type === "event" || edge.type === "mcp" || edge.type === "a2a" ? "#098a83" : "#b87520";
    return '<path class="architecture-proposed-edge" stroke="' + color + '" d="' + escapeHtml(edge.path) + '" aria-hidden="true"><title>' +
      escapeHtml(edge.source + " → " + edge.target + " · " + edge.status) + '</title></path>';
  }).join("") : "");
  nodeBox.innerHTML = data.nodes.map(function (node) {
    const color = architectureColors[node.category] || "#53677f";
    const extra = data.extra[node.id] || {};
    const match = architectureMatches(node);
    const selected = state.architectureSelected === node.id;
    const active = state.architectureActiveNodes.includes(node.id);
    const frame = node.kind === "frame";
    const radius = frame ? 12 : node.kind === "group" ? 9 : 5;
    const inset = frame ? 1 : 0.5;
    const maxChars = Math.max(9, Math.floor((node.w - 12) / (frame ? 8 : 5.4)));
    const words = String(node.label).split(/\s+/);
    const lines = [];
    let line = "";
    words.forEach(function (word) {
      if (line && (line + " " + word).length > maxChars) { lines.push(line); line = word; }
      else line = line ? line + " " + word : word;
    });
    if (line) lines.push(line);
    const fontSize = frame ? 13 : node.kind === "group" ? 10 : 8;
    const lineHeight = frame ? 16 : 10;
    const firstY = node.y + node.h / 2 - ((lines.length - 1) * lineHeight) / 2 + fontSize * 0.34;
    const labels = lines.slice(0, 3).map(function (text, index) {
      return '<tspan x="' + (node.x + node.w / 2) + '" y="' + (firstY + index * lineHeight) + '">' + escapeHtml(text) + '</tspan>';
    }).join("");
    return '<g class="architecture-node' + (match ? "" : " arch-dim") + (selected ? " arch-selected" : "") +
      (active ? " arch-scenario-active" : "") + '" data-architecture-node="' + escapeHtml(node.id) +
      '" tabindex="0" role="button" aria-label="' + escapeHtml(node.label + ": " + node.desc) + '">' +
      '<rect x="' + node.x + '" y="' + node.y + '" width="' + node.w + '" height="' + node.h + '" rx="' + radius +
      '" fill="' + (frame ? color + "12" : "#ffffff") + '" stroke="' + color + '" stroke-width="' + (selected ? 3 : frame ? 2 : 1.4) + '"/>' +
      (extra.decision ? '<circle cx="' + (node.x + node.w - 8) + '" cy="' + (node.y + 8) + '" r="3" fill="' + color + '"/>' : "") +
      '<text text-anchor="middle" font-size="' + fontSize + '" font-weight="' + (frame || node.kind === "group" ? 750 : 600) + '" fill="#26364b">' + labels + '</text></g>';
  }).join("");
  const svg = document.getElementById("architecture-map");
  const width = 2112 / state.architectureZoom;
  const height = 960 / state.architectureZoom;
  svg.setAttribute("viewBox", ((2112 - width) / 2) + " " + ((960 - height) / 2) + " " + width + " " + height);
  const visible = data.nodes.filter(architectureMatches).length;
  document.getElementById("architecture-count").textContent = visible + " de " + data.nodes.length + " componentes";
  document.querySelectorAll("[data-architecture-category]").forEach(function (button) {
    button.classList.toggle("active", button.dataset.architectureCategory === state.architectureCategory);
  });
  if (state.architectureSelected) showArchitectureDetail(state.architectureSelected);
}

function showArchitectureDetail(id) {
  const data = state.architecture;
  const node = data && data.nodes.find(function (item) { return item.id === id; });
  if (!node) return;
  state.architectureSelected = id;
  const extra = data.extra[id] || {};
  const category = data.categories[node.category] || node.category;
  const links = data.connections.filter(function (edge) { return edge.source === id || edge.target === id; });
  const list = function (values) {
    return values && values.length ? '<ul>' + values.map(function (value) { return '<li>' + escapeHtml(value) + '</li>'; }).join("") + '</ul>' : "";
  };
  document.getElementById("architecture-detail").innerHTML =
    '<div class="architecture-detail-head"><span class="architecture-detail-category" style="--arch-color:' +
    (architectureColors[node.category] || "#53677f") + '">' + escapeHtml(category) + '</span>' +
    (extra.decision ? '<span class="architecture-decision">' + escapeHtml(extra.decision) + '</span>' : "") + '</div>' +
    '<h2>' + escapeHtml(node.label) + '</h2><p class="architecture-node-desc">' + escapeHtml(node.desc || "") + '</p>' +
    (node.value ? '<section><h3>Valor</h3><p>' + escapeHtml(node.value) + '</p></section>' : "") +
    (node.example ? '<section><h3>Ejemplo del mapa</h3><p>' + escapeHtml(node.example) + '</p></section>' : "") +
    (extra.main ? '<section><h3>Tecnología seleccionada</h3><p class="architecture-tech-main">' + escapeHtml(extra.main) + '</p>' +
      (extra.alternatives ? '<p><strong>Alternativas:</strong> ' + escapeHtml(extra.alternatives) + '</p>' : "") + '</section>' :
      (node.tech && node.tech.length ? '<section><h3>Tecnologías consideradas</h3>' + list(node.tech) + '</section>' : "")) +
    (extra.implementation ? '<section><h3>Cómo se implementa</h3><p>' + escapeHtml(extra.implementation) + '</p></section>' : "") +
    (extra.gate ? '<section class="architecture-gate"><h3>Criterio de aceptación</h3><p>' + escapeHtml(extra.gate) + '</p></section>' : "") +
    (links.length ? '<section><h3>Configuración de conexiones propuestas</h3><p class="architecture-fine">Son nombres de variables, nunca valores de secretos.</p>' +
      links.map(function (edge) {
        const peer = data.nodes.find(function (item) { return item.id === (edge.source === id ? edge.target : edge.source); });
        const fields = [...(edge.env || []), ...(edge.secrets || [])];
        return '<article class="architecture-link-card"><strong>' + escapeHtml(peer ? peer.label : edge.target) + '</strong><small>' +
          escapeHtml(edge.status) + '</small>' + (fields.length ? '<code>' + escapeHtml(fields.join(" · ")) + '</code>' : "") + '</article>';
      }).join("") + '</section>' : "") +
    (extra.repo ? '<p class="architecture-fine">Repositorio de referencia indicado en el mapa: <code>' + escapeHtml(extra.repo) + '</code></p>' : "");
  document.querySelectorAll("[data-architecture-node]").forEach(function (item) {
    item.classList.toggle("arch-selected", item.dataset.architectureNode === id);
  });
}

function renderArchitectureScenarios() {
  const data = state.architecture;
  if (!data) return;
  const scenario = data.scenarios[state.architectureScenario] || data.scenarios[0];
  const steps = scenario.steps || [];
  state.architectureScenarioStep = Math.min(state.architectureScenarioStep, steps.length - 1);
  const step = steps[state.architectureScenarioStep] || steps[0];
  state.architectureActiveNodes = step.active || [];
  document.getElementById("architecture-scenarios").innerHTML =
    '<div class="architecture-scenario-tabs">' + data.scenarios.map(function (item, index) {
      return '<button class="architecture-scenario-tab' + (index === state.architectureScenario ? " active" : "") +
        '" data-architecture-scenario="' + index + '"><strong>' + escapeHtml(item.name) + '</strong><small>' + escapeHtml(item.short) + '</small></button>';
    }).join("") + '</div><div class="architecture-scenario-body"><nav class="architecture-step-list" aria-label="Pasos del recorrido">' +
    steps.map(function (item, index) {
      return '<button class="architecture-step' + (index === state.architectureScenarioStep ? " active" : "") +
        (index < state.architectureScenarioStep ? " complete" : "") + '" data-architecture-step="' + index + '"><span>' +
        (index < state.architectureScenarioStep ? "✓" : String(index + 1).padStart(2, "0")) + '</span><strong>' + escapeHtml(item.title) + '</strong></button>';
    }).join("") + '</nav><article class="architecture-step-detail"><div class="architecture-step-progress">PASO ' +
    (state.architectureScenarioStep + 1) + ' / ' + steps.length + '</div><h3>' + escapeHtml(step.title) + '</h3><p>' + escapeHtml(step.text) + '</p>' +
    (step.evidence ? '<pre>' + escapeHtml(step.evidence) + '</pre>' : "") +
    (step.value ? '<p class="architecture-step-value"><strong>Valor:</strong> ' + escapeHtml(step.value) + '</p>' : "") +
    '<div class="architecture-step-actions"><button class="button button-light button-small" data-architecture-step-prev ' +
    (state.architectureScenarioStep === 0 ? "disabled" : "") + '>← Anterior</button><button class="button button-dark button-small" data-run-scenario="' +
    escapeHtml(scenario.id) + '">Probar en la plataforma →</button><button class="button button-light button-small" data-architecture-step-next ' +
    (state.architectureScenarioStep >= steps.length - 1 ? "disabled" : "") + '>Siguiente →</button></div></article></div>';
  renderArchitectureMap();
}

async function loadSystem() {
  const box = document.getElementById("system-overview");
  box.innerHTML = '<div class="empty-state">Comprobando servicios…</div>';
  try {
    const data = await api("/api/v1/system");
    state.system = data;
    updateEnvironment(data.model);
    const items = [
      ["Base de datos", data.database, true],
      ["Bus de eventos", data.event_bus, Boolean(data.event_bus)],
      ["Políticas", data.opa, true],
      ["Modelo", data.model.provider + " · " + data.model.model, data.model.configured],
      ["Jira", data.jira, data.jira === "configured"],
      ["A2A", data.protocols.a2a, true],
      ["MCP", data.protocols.mcp, true],
      ["Secretos en BD", data.secrets_in_database ? "sí" : "no", !data.secrets_in_database]
    ];
    box.innerHTML = items.map(function (item) {
      return '<article class="system-card"><div class="system-card-top"><strong>' +
        escapeHtml(item[0]) + '</strong><span class="' +
        (item[2] ? "system-ok" : "system-warn") + '">' +
        (item[2] ? "Listo" : "No configurado") + '</span></div><p>' +
        escapeHtml(item[1]) + "</p></article>";
    }).join("");
    await loadConnectors();
  } catch (error) {
    box.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
    const connectorBox = document.getElementById("connectors-list");
    connectorBox.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

async function loadConnectors() {
  const box = document.getElementById("connectors-list");
  try {
    const data = await api("/api/v1/connectors");
    const items = data.items || [];
    const canManage = Boolean(state.system && state.system.roles.includes("admin"));
    box.innerHTML = items.length ? items.map(function (item) {
      const authState = item.auth_env
        ? (item.auth_configured ? "Token configurado" : "Falta " + item.auth_env)
        : "Sin autenticación";
      const statusClass = !item.enabled || (!item.auth_configured && item.auth_env)
        ? "system-warn" : "system-ok";
      return '<article class="connector-card"><div class="connector-card-top"><span class="protocol-mark ' +
        (item.kind === "mcp" ? "mcp" : "") + '">' + escapeHtml(item.kind.toUpperCase()) +
        '</span><span class="' + statusClass + '">' + escapeHtml(item.enabled ? authState : "Desactivado") +
        '</span></div><h3>' + escapeHtml(item.name) + '</h3><p>' +
        escapeHtml(item.description || "Sin descripción") + '</p><code class="connector-endpoint">' +
        escapeHtml(item.endpoint) + '</code><div class="connector-meta">' +
        escapeHtml(item.kind === "mcp" ? "MCP " + item.protocol_version : "Agent-to-Agent") +
        ' · ' + escapeHtml(item.last_invoked_at ? "Última llamada " + dateLabel(item.last_invoked_at) : "Sin llamadas") +
        '</div><div class="connector-actions">' +
        (item.enabled && item.kind === "mcp" ? '<button class="button button-light button-small" data-connector-tools="' +
          escapeHtml(item.id) + '">Ver herramientas</button>' : "") +
        (item.enabled ? '<button class="button button-dark button-small" data-connector-invoke="' +
          escapeHtml(item.id) + '" data-connector-kind="' + escapeHtml(item.kind) + '">Invocar</button>' : "") +
        (canManage ? '<button class="button button-light button-small" data-connector-toggle="' +
          escapeHtml(item.id) + '" data-connector-enable="' + (!item.enabled) + '">' +
          (item.enabled ? "Desactivar" : "Activar") + '</button>' : "") + '</div></article>';
    }).join("") : '<div class="empty-state">Aún no hay conectores. Registra un agente A2A o servidor MCP para habilitar llamadas remotas.</div>';
  } catch (error) {
    box.innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + "</div>";
  }
}

async function toggleConnector(id, enable) {
  try {
    await api("/api/v1/connectors/" + encodeURIComponent(id) + (enable ? "/enable" : ""), {
      method: enable ? "POST" : "DELETE"
    });
    showToast(enable ? "Conector activado." : "Conector desactivado.");
    await loadConnectors();
  } catch (error) {
    showToast(error.message);
  }
}

async function registerConnector(event) {
  event.preventDefault();
  const errorBox = document.getElementById("connector-error");
  const submit = event.currentTarget.querySelector('button[type="submit"]');
  errorBox.classList.add("hidden");
  submit.disabled = true;
  try {
    const authEnv = document.getElementById("connector-auth-env").value.trim();
    await api("/api/v1/connectors", {
      method: "POST",
      body: JSON.stringify({
        kind: document.getElementById("connector-kind").value,
        name: document.getElementById("connector-name").value.trim(),
        endpoint: document.getElementById("connector-endpoint").value.trim(),
        description: document.getElementById("connector-description").value.trim(),
        auth_env: authEnv || null,
        protocol_version: document.getElementById("connector-kind").value === "mcp"
          ? document.getElementById("connector-version").value : "1.0"
      })
    });
    event.currentTarget.reset();
    showToast("Conector registrado.");
    await loadConnectors();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
}

function openConnectorResult(title, data) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").innerHTML = '<div class="result-block"><pre class="connector-result">' +
    escapeHtml(JSON.stringify(data, null, 2)) + '</pre></div>';
  document.getElementById("task-modal").classList.remove("hidden");
}

async function showConnectorTools(id) {
  try {
    const result = await api("/api/v1/connectors/" + encodeURIComponent(id) + "/tools");
    openConnectorResult("Herramientas MCP · " + id.slice(0, 10), result);
    await loadConnectors();
  } catch (error) {
    showToast(error.message);
  }
}

async function invokeConnector(id, kind) {
  try {
    let body;
    if (kind === "a2a") {
      const message = window.prompt("Mensaje para el agente remoto:");
      if (!message) return;
      body = { message: message, scenario: "knowledge" };
    } else {
      const toolName = window.prompt("Nombre de la herramienta MCP:");
      if (!toolName) return;
      const rawArguments = window.prompt("Argumentos JSON ({} si no necesita argumentos):", "{}");
      if (rawArguments === null) return;
      let argumentsValue;
      try { argumentsValue = JSON.parse(rawArguments); }
      catch (_) { throw new Error("Los argumentos deben ser JSON válido."); }
      if (!argumentsValue || Array.isArray(argumentsValue) || typeof argumentsValue !== "object") {
        throw new Error("Los argumentos deben ser un objeto JSON.");
      }
      body = { tool_name: toolName, arguments: argumentsValue };
    }
    const result = await api("/api/v1/connectors/" + encodeURIComponent(id) + "/invoke", {
      method: "POST", body: JSON.stringify(body)
    });
    openConnectorResult("Respuesta del conector", result);
    await loadConnectors();
  } catch (error) {
    showToast(error.message);
  }
}

function updateScenarioForm() {
  const scenario = document.getElementById("scenario").value;
  const messages = {
    knowledge: {
      label: "Pregunta",
      hint: "La respuesta solo utilizará documentos dentro del alcance autorizado.",
      value: "¿Cómo justifico un gasto de viaje y quién lo aprueba?"
    },
    report: {
      label: "Objetivo del informe",
      hint: "Adjunta archivos de configuración. El inspector extrae declaraciones y conserva la evidencia.",
      value: "Revisa el stack declarado y prepara un inventario de hechos con los archivos adjuntos."
    },
    incident: {
      label: "Contexto de la incidencia",
      hint: "Se prepara un borrador. Ninguna escritura externa ocurre antes de aprobarlo.",
      value: "El estado del pedido no coincide entre sistemas. Prepara una propuesta para revisar."
    }
  };
  const spec = messages[scenario];
  document.getElementById("message-label").textContent = spec.label;
  document.getElementById("message-hint").textContent = spec.hint;
  const message = document.getElementById("message");
  if (!message.value || Object.values(messages).some(function (item) { return item.value === message.value; })) {
    message.value = spec.value;
  }
  document.getElementById("report-files").classList.toggle("hidden", scenario !== "report");
  document.getElementById("incident-fields").classList.toggle("hidden", scenario !== "incident");
}

function fillScenario(scenario) {
  document.getElementById("scenario").value = scenario;
  document.getElementById("message").value = "";
  updateScenarioForm();
  setView("workflows");
  document.getElementById("message").focus();
}

async function submitTask(event) {
  event.preventDefault();
  const errorBox = document.getElementById("form-error");
  const submit = document.getElementById("submit-task");
  errorBox.classList.add("hidden");
  submit.disabled = true;
  submit.textContent = "Ejecutando…";
  try {
    const scenario = document.getElementById("scenario").value;
    const artifacts = [];
    const files = Array.from(document.getElementById("files").files || []);
    let size = 0;
    for (const file of files) {
      size += file.size;
      if (size > 300000) throw new Error("Los archivos superan el límite total de 300 KB.");
      artifacts.push({ path: file.name, content: await file.text() });
    }
    const body = {
      scenario: scenario,
      message: document.getElementById("message").value,
      artifacts: artifacts
    };
    const issueKey = document.getElementById("issue-key").value.trim();
    if (scenario === "incident" && issueKey) body.issue_key = issueKey;
    const task = await api("/api/v1/tasks", { method: "POST", body: JSON.stringify(body) });
    showToast("Flujo ejecutado: " + (statusNames[task.status] || task.status));
    document.getElementById("files").value = "";
    await Promise.all([loadOverview(), loadTasks()]);
    await openTask(task.id);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  } finally {
    submit.disabled = false;
    submit.innerHTML = 'Ejecutar flujo <span>→</span>';
  }
}

function resultModal(task, events) {
  const result = task.result || {};
  let html = '<div class="result-block"><h3>Petición</h3><p>' + escapeHtml(task.request) +
    '</p><p>' + flowCell(task.scenario) + " &nbsp; " + statusBadge(task.status) +
    '</p></div>';
  if (result.answer) {
    html += '<div class="result-block"><h3>Respuesta</h3><p>' + escapeHtml(result.answer) + "</p></div>";
  }
  if (result.citations) {
    html += '<div class="result-block"><h3>Fuentes y citas</h3>' +
      (result.citations.length ? result.citations.map(function (citation) {
        return '<div class="citation-card"><strong>' + escapeHtml(citation.title) +
          '</strong> · ' + escapeHtml(citation.scope) + '<p>' + escapeHtml(citation.quote) +
          '</p><small>' + escapeHtml(citation.source_uri) + '</small></div>';
      }).join("") : '<p>No se encontraron fuentes autorizadas.</p>') + "</div>";
  }
  if (result.findings) {
    html += '<div class="result-block"><h3>Inventario observado · ' +
      (result.findings.length || 0) + " hechos</h3>" +
      (result.findings.length ? result.findings.map(function (finding) {
        return '<div class="finding-card"><strong>' + escapeHtml(finding.kind) +
          '</strong> · ' + escapeHtml(finding.value) + '<p>' + escapeHtml(finding.path) +
          " · evidencia: " + escapeHtml(finding.evidence) + "</p></div>";
      }).join("") : "<p>No se extrajeron declaraciones de los archivos.</p>") +
      '<p>' + escapeHtml(result.summary || "") + "</p></div>";
  }
  if (result.draft || result.blocked_reason) {
    html += '<div class="result-block"><h3>Propuesta operativa</h3><p>' +
      escapeHtml(result.draft || result.blocked_reason) + '</p><p>Origen: ' +
      escapeHtml(result.source || "sin fuente") + " · efecto externo: " +
      escapeHtml(result.external_write || "no ejecutado") + "</p></div>";
  }
  if (result.evaluation) {
    html += '<div class="result-block"><h3>Evaluación · ' + escapeHtml(result.evaluation.score) +
      "%</h3>" + (result.evaluation.checks || []).map(function (check) {
        return '<div class="check-line ' + (check.passed ? "pass" : "fail") +
          '"><strong>' + escapeHtml(check.id) + '</strong><span>' +
          escapeHtml(check.detail) + "</span></div>";
      }).join("") + "</div>";
  }
  if (result.approval) {
    html += '<div class="result-block"><h3>Decisión humana</h3><p>' +
      escapeHtml(result.approval) + " · " + escapeHtml(result.external_effect || "sin efecto externo") +
      "</p></div>";
  }
  html += '<div class="result-block"><h3>Recorrido auditado</h3>' +
    (events.length ? events.map(function (item) {
      return '<div class="event-line"><time>' + escapeHtml(dateLabel(item.created_at)) +
        '</time><span><strong>' + escapeHtml(item.type) +
        '</strong> · ' + escapeHtml(item.message) + "</span></div>";
    }).join("") : "<p>No hay eventos registrados.</p>") + "</div>";
  return html;
}

async function openTask(id) {
  try {
    const values = await Promise.all([
      api("/api/v1/tasks/" + encodeURIComponent(id)),
      api("/api/v1/tasks/" + encodeURIComponent(id) + "/events")
    ]);
    document.getElementById("modal-title").textContent = values[0].title + " · " + values[0].id.slice(0, 10);
    document.getElementById("modal-body").innerHTML = resultModal(values[0], values[1].items || []);
    document.getElementById("task-modal").classList.remove("hidden");
  } catch (error) {
    showToast(error.message);
  }
}

function setToken(value) {
  state.token = value || "";
  if (state.token) localStorage.setItem("agentic-api-token", state.token);
  else localStorage.removeItem("agentic-api-token");
  showToast(state.token ? "Token guardado en este navegador." : "Token eliminado.");
  loadSystem();
}

document.addEventListener("click", function (event) {
  const nav = event.target.closest("[data-view]");
  if (nav) setView(nav.dataset.view);
  const link = event.target.closest("[data-view-link]");
  if (link) setView(link.dataset.viewLink);
  if (event.target.closest("[data-open-workflow]")) setView("workflows");
  const scenario = event.target.closest("[data-run-scenario]");
  if (scenario) fillScenario(scenario.dataset.runScenario);
  const taskButton = event.target.closest("[data-task-open]");
  if (taskButton) openTask(taskButton.dataset.taskOpen);
  const decision = event.target.closest("[data-decision]");
  if (decision) decideApproval(decision.dataset.approvalId, decision.dataset.decision);
  const connectorTools = event.target.closest("[data-connector-tools]");
  if (connectorTools) showConnectorTools(connectorTools.dataset.connectorTools);
  const connectorInvoke = event.target.closest("[data-connector-invoke]");
  if (connectorInvoke) invokeConnector(connectorInvoke.dataset.connectorInvoke, connectorInvoke.dataset.connectorKind);
  const connectorToggle = event.target.closest("[data-connector-toggle]");
  if (connectorToggle) toggleConnector(connectorToggle.dataset.connectorToggle, connectorToggle.dataset.connectorEnable === "true");
  const architectureCategory = event.target.closest("[data-architecture-category]");
  if (architectureCategory) {
    state.architectureCategory = architectureCategory.dataset.architectureCategory;
    renderArchitectureMap();
  }
  const architectureNode = event.target.closest("[data-architecture-node]");
  if (architectureNode) {
    state.architectureSelected = architectureNode.dataset.architectureNode;
    renderArchitectureMap();
  }
  const architectureScenario = event.target.closest("[data-architecture-scenario]");
  if (architectureScenario) {
    state.architectureScenario = Number(architectureScenario.dataset.architectureScenario);
    state.architectureScenarioStep = 0;
    renderArchitectureScenarios();
  }
  const architectureStep = event.target.closest("[data-architecture-step]");
  if (architectureStep) {
    state.architectureScenarioStep = Number(architectureStep.dataset.architectureStep);
    renderArchitectureScenarios();
  }
  if (event.target.closest("[data-architecture-step-prev]")) {
    state.architectureScenarioStep = Math.max(0, state.architectureScenarioStep - 1);
    renderArchitectureScenarios();
  }
  if (event.target.closest("[data-architecture-step-next]")) {
    const scenario = state.architecture.scenarios[state.architectureScenario];
    state.architectureScenarioStep = Math.min(scenario.steps.length - 1, state.architectureScenarioStep + 1);
    renderArchitectureScenarios();
  }
  const tab = event.target.closest("[data-catalog-kind]");
  if (tab) {
    state.catalogKind = tab.dataset.catalogKind;
    document.querySelectorAll(".catalog-tab").forEach(function (item) {
      item.classList.toggle("active", item === tab);
    });
    loadCatalog();
  }
  if (event.target.id === "close-modal" || event.target.id === "task-modal") {
    document.getElementById("task-modal").classList.add("hidden");
  }
});

document.getElementById("scenario").addEventListener("change", updateScenarioForm);
document.getElementById("task-form").addEventListener("submit", submitTask);
document.getElementById("menu-button").addEventListener("click", function () {
  document.getElementById("sidebar").classList.toggle("open");
});
document.getElementById("refresh-tasks").addEventListener("click", loadTasks);
document.getElementById("refresh-activity").addEventListener("click", loadActivity);
document.getElementById("save-token").addEventListener("click", function () {
  setToken(document.getElementById("api-token").value.trim());
});
document.getElementById("clear-token").addEventListener("click", function () {
  document.getElementById("api-token").value = "";
  setToken("");
});
document.getElementById("connector-form").addEventListener("submit", registerConnector);
document.getElementById("refresh-connectors").addEventListener("click", loadConnectors);
document.getElementById("architecture-search").addEventListener("input", function (event) {
  state.architectureQuery = event.currentTarget.value;
  renderArchitectureMap();
});
document.getElementById("architecture-links").addEventListener("change", function (event) {
  state.architectureLinks = event.currentTarget.checked;
  renderArchitectureMap();
});
document.getElementById("architecture-zoom-in").addEventListener("click", function () {
  state.architectureZoom = Math.min(2.5, state.architectureZoom * 1.2);
  renderArchitectureMap();
});
document.getElementById("architecture-zoom-out").addEventListener("click", function () {
  state.architectureZoom = Math.max(0.65, state.architectureZoom / 1.2);
  renderArchitectureMap();
});
document.getElementById("architecture-zoom-reset").addEventListener("click", function () {
  state.architectureZoom = 1;
  state.architectureCategory = "all";
  state.architectureQuery = "";
  document.getElementById("architecture-search").value = "";
  renderArchitectureMap();
});
document.getElementById("connector-kind").addEventListener("change", function (event) {
  const visible = event.currentTarget.value === "mcp";
  document.querySelectorAll(".mcp-version-label,.mcp-version-control").forEach(function (item) {
    item.classList.toggle("hidden", !visible);
  });
});
document.getElementById("token-button").addEventListener("click", function () {
  setView("system");
  document.getElementById("api-token").focus();
});
document.addEventListener("keydown", function (event) {
  if (event.key === "Escape") document.getElementById("task-modal").classList.add("hidden");
  if (event.key === "Enter" || event.key === " ") {
    const node = event.target.closest && event.target.closest("[data-architecture-node]");
    if (node) {
      event.preventDefault();
      showArchitectureDetail(node.dataset.architectureNode);
      renderArchitectureMap();
    }
  }
});

state.token = localStorage.getItem("agentic-api-token") || "";
document.getElementById("api-token").value = state.token;
updateScenarioForm();
loadSystem().then(loadOverview);
loadArchitecture().catch(function (error) {
  document.getElementById("architecture-count").textContent = error.message;
  document.getElementById("architecture-scenarios").innerHTML = '<div class="empty-state">' + escapeHtml(error.message) + '</div>';
});
document.getElementById("connector-kind").dispatchEvent(new Event("change"));
window.setInterval(function () {
  if (state.view === "overview") loadOverview();
}, 12000);

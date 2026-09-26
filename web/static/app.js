"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  userId: $("#user").value,
  trialId: 1,
  summary: null,
  participants: [],
  versions: [],
  unblindings: [],
};

const STATUS_TEXT = {
  reviewing: "审核中（暂停入组）",
  returned: "已退回",
  approved: "已批准生效",
  cancelled: "已撤回",
  pending: "待双人确认",
  rejected: "已拒绝",
  awaiting_materials: "待补材料",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-User-Id": state.userId },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data?.error?.message || `请求失败 (${res.status})`);
    err.code = data?.error?.code;
    err.details = data?.error?.details;
    throw err;
  }
  return data;
}

function setMsg(el, message, ok = false) {
  el.textContent = message;
  el.className = ok ? "msg ok" : "msg err";
}

const role = () => ({
  coord: "coordinator", site1: "site", site2: "site", monitor1: "monitor", monitor2: "monitor",
}[state.userId]);

async function loadAll() {
  state.userId = $("#user").value;
  state.trialId = Number($("#trial-id").value) || 1;
  try {
    const [summary, participants, versions, unblindings] = await Promise.all([
      api("GET", `/api/trials/${state.trialId}/summary`),
      api("GET", `/api/trials/${state.trialId}/participants`),
      api("GET", `/api/trials/${state.trialId}/version-applications`),
      api("GET", `/api/trials/${state.trialId}/unblinding-requests`),
    ]);
    state.summary = summary;
    state.participants = participants.items || [];
    state.versions = versions.items || [];
    state.unblindings = unblindings.items || [];
    renderAll();
  } catch (err) {
    renderBannerError(err);
  }
}

function renderBannerError(err) {
  const banner = $("#banner");
  banner.className = "banner paused";
  banner.textContent = `加载失败：${err.message}`;
}

function renderAll() {
  renderBanner();
  renderRosterPreview();
  renderVersions();
  renderUnblindings();
  prefillVersionForm();
}

function renderBanner() {
  const banner = $("#banner");
  const trial = state.summary?.trial;
  if (!trial) {
    banner.className = "banner hidden";
    banner.textContent = "";
    return;
  }
  if (trial.enrollment_held && state.summary.reviewing_application) {
    const app = state.summary.reviewing_application;
    banner.className = "banner paused";
    banner.innerHTML = `⏸ 方案版本申请 <b>${esc(app.number)}</b>（${esc(app.protocol_version)}）审核中，<b>已暂停新入组</b>；审核结束后自动恢复。当前生效版本：${esc(trial.protocol_version)}`;
  } else {
    banner.className = "banner running";
    banner.textContent = `入组进行中，当前方案版本：${trial.protocol_version}；已可见受试者 ${state.summary.participants_visible} 人。`;
  }
}

function prefillVersionForm() {
  const trial = state.summary?.trial;
  const form = $("#version-form");
  if (!trial) return;
  form.arms.placeholder = trial.arms.join(", ");
  form.strata_factors.placeholder = trial.strata_factors.join(", ");
  form.block_size.placeholder = trial.block_size;
  form.seed.placeholder = trial.seed;
}

function renderRosterPreview() {
  $("#roster-count").textContent = state.participants.length;
  const list = $("#roster-list");
  list.innerHTML = state.participants.map((p) => (
    `<li><span class="mono">#${p.id}</span><span>${esc(p.external_id)}</span>
     <span>${esc(p.site_id)}</span><span class="mono">${esc(p.allocation_code)}</span></li>`
  )).join("") || "<li>暂无已入组受试者</li>";
}

/* ---------------- 方案版本申请 ---------------- */

function renderVersions() {
  const wrap = $("#version-list");
  const isCoordinator = role() === "coordinator";
  const isMonitor = role() === "monitor";
  $("#version-form").classList.toggle("hidden", !isCoordinator);

  if (!state.versions.length) {
    wrap.innerHTML = `<div class="empty">暂无版本申请。入组后可在此提交新版本。</div>`;
    return;
  }
  wrap.innerHTML = state.versions.map((app) => {
    const actions = [];
    if (app.status === "reviewing" && isMonitor) {
      actions.push(`
        <div class="review-form">
          <input placeholder="审核意见（≥4 字）" id="review-note-${app.id}">
          <button type="button" data-review="approve" data-id="${app.id}">批准生效</button>
          <button type="button" data-review="return" data-id="${app.id}">退回</button>
        </div>`);
    }
    if ((app.status === "reviewing" || app.status === "returned") && isCoordinator && app.requested_by === state.userId) {
      actions.push(`<button type="button" data-cancel="${app.id}">撤回申请</button>`);
    }
    const blockNote = app.status === "returned" && app.incompatibility_note
      ? `<div class="blocknote">分层核对未通过（退回）：${esc(app.incompatibility_note)}</div>` : "";
    return `
      <div class="card">
        <div class="row">
          <b>${esc(app.number)} · ${esc(app.protocol_version)}</b>
          <span class="badge ${esc(app.status)}">${STATUS_TEXT[app.status] || esc(app.status)}</span>
        </div>
        <div class="meta">
          申请人 ${esc(app.requested_by)} · ${esc(app.created_at)} · 归档已入组 ${app.enrolled_count} 人
          ${app.reviewed_by ? ` · 审核人 ${esc(app.reviewed_by)}` : ""}
        </div>
        <div class="detail">变更原因：${esc(app.change_reason)}</div>
        ${app.review_note ? `<div class="detail">审核意见：${esc(app.review_note)}</div>` : ""}
        ${blockNote}
        ${actions.length ? `<div class="actions">${actions.join("")}</div>` : ""}
      </div>`;
  }).join("");
}

async function submitVersion(event) {
  event.preventDefault();
  const form = event.target;
  const msg = $("#version-msg");
  const body = {
    protocol_version: form.protocol_version.value.trim(),
    change_reason: form.change_reason.value.trim(),
  };
  const arms = form.arms.value.trim();
  const strata = form.strata_factors.value.trim();
  const block = form.block_size.value.trim();
  const seed = form.seed.value.trim();
  if (arms) body.arms = arms.split(",").map((s) => s.trim()).filter(Boolean);
  if (strata) body.strata_factors = strata.split(",").map((s) => s.trim()).filter(Boolean);
  if (block) body.block_size = Number(block);
  if (seed) body.seed = seed;
  try {
    const app = await api("POST", `/api/trials/${state.trialId}/version-applications`, body);
    if (app.status === "returned") {
      setMsg(msg, `申请 ${app.number} 已被系统退回：${app.incompatibility_note || "旧分层无法沿用"}`, false);
    } else {
      setMsg(msg, `申请 ${app.number} 已进入审核，审核期间该试验暂停新入组。`, true);
    }
    form.reset();
    await loadAll();
  } catch (err) {
    setMsg(msg, err.message, false);
  }
}

async function reviewVersion(button) {
  const id = button.dataset.id;
  const decision = button.dataset.review;
  const note = $(`#review-note-${id}`).value.trim();
  try {
    await api("POST", `/api/version-applications/${id}/review`, { decision, review_note: note });
    await loadAll();
  } catch (err) {
    alert(err.message);
  }
}

async function cancelVersion(button) {
  try {
    await api("POST", `/api/version-applications/${button.dataset.cancel}/cancel`);
    await loadAll();
  } catch (err) {
    alert(err.message);
  }
}

/* ---------------- 紧急揭盲 ---------------- */

function renderUnblindings() {
  const wrap = $("#unblind-list");
  if (!state.unblindings.length) {
    wrap.innerHTML = `<div class="empty">暂无揭盲申请。</div>`;
    return;
  }
  wrap.innerHTML = state.unblindings.map((req) => {
    const parts = [];
    parts.push(`
      <div class="card">
        <div class="row">
          <b>UB-${String(req.id).padStart(4, "0")} · 受试者 ${esc(req.external_id)}（#${req.participant_id}）</b>
          <span class="badge ${esc(req.state)}">${STATUS_TEXT[req.state] || esc(req.state)}</span>
        </div>
        <div class="meta">
          申请人 ${esc(req.requester_id)} · ${esc(req.site_id)} · ${esc(req.created_at)}
          ${req.first_approver ? ` · 第一确认人 ${esc(req.first_approver)}` : ""}
          ${req.second_approver ? ` · 第二确认人 ${esc(req.second_approver)}` : ""}
        </div>
        <div class="detail">原因：${esc(req.reason)}</div>
    `);
    if (req.state === "awaiting_materials") {
      parts.push(`
        <div class="blocknote">
          版本申请 <b>${esc(req.blocking_application_id)}</b> 未完成，揭盲停在“待补材料”。
          版本审核结束后，请补交材料说明再送双人确认。
        </div>
        <div class="review-form">
          <input placeholder="补充材料说明（≥4 字）" id="mat-note-${req.id}">
          <button type="button" data-supplement="${req.id}">补交材料</button>
        </div>`);
    }
    if (req.state === "pending") {
      const canApprove = (role() === "monitor" || role() === "coordinator") && req.requester_id !== state.userId;
      parts.push(`
        <div class="meta">${req.first_approver ? "已完成第一确认，等待第二名不同人员确认。" : "等待第一名人员确认。"}</div>
        <div class="actions">
          <button type="button" data-approve="${req.id}" ${canApprove ? "" : "disabled"}
            title="${canApprove ? "" : "申请人不能确认自己的揭盲申请"}">
            ${req.first_approver ? "第二人确认揭盲" : "第一人确认揭盲"}
          </button>
        </div>`);
    }
    if (req.state === "approved" && req.arm) {
      parts.push(`<div class="arm-reveal">组别已显示：${esc(req.arm)}</div>`);
    }
    if (req.materials_note) {
      parts.push(`<div class="detail">补交材料：${esc(req.materials_note)}</div>`);
    }
    parts.push("</div>");
    return parts.join("");
  }).join("");
}

async function submitUnblind(event) {
  event.preventDefault();
  const form = event.target;
  const msg = $("#unblind-msg");
  try {
    const req = await api(
      "POST",
      `/api/participants/${form.participant_id.value.trim()}/unblinding-requests`,
      { reason: form.reason.value.trim() },
    );
    if (req.state === "awaiting_materials") {
      setMsg(msg, `揭盲申请 UB-${String(req.id).padStart(4, "0")} 已登记：版本申请 ${req.blocking_application_id} 未完成，停在“待补材料”。`, false);
    } else {
      setMsg(msg, `揭盲申请 UB-${String(req.id).padStart(4, "0")} 已提交，等待两名不同人员确认。`, true);
    }
    form.reset();
    await loadAll();
  } catch (err) {
    setMsg(msg, err.message, false);
  }
}

async function approveUnblind(button) {
  try {
    const res = await api("POST", `/api/unblinding-requests/${button.dataset.approve}/approve`);
    if (res.status === "approved") {
      alert(`两人确认完成，受试者组别：${res.arm}`);
    }
    await loadAll();
  } catch (err) {
    alert(err.message);
  }
}

async function supplementUnblind(button) {
  const id = button.dataset.supplement;
  const note = $(`#mat-note-${id}`).value.trim();
  try {
    await api("POST", `/api/unblinding-requests/${id}/supplement`, { materials_note: note });
    await loadAll();
  } catch (err) {
    alert(err.message);
  }
}

/* ---------------- 事件绑定 ---------------- */

function init() {
  $("#user").addEventListener("change", loadAll);
  $("#refresh").addEventListener("click", loadAll);
  $("#version-form").addEventListener("submit", submitVersion);
  $("#unblind-form").addEventListener("submit", submitUnblind);
  $("#toggle-roster").addEventListener("click", () => $("#roster-list").classList.toggle("hidden"));

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("#tab-version").classList.toggle("hidden", tab.dataset.tab !== "version");
    $("#tab-unblinding").classList.toggle("hidden", tab.dataset.tab !== "unblinding");
  }));

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("button");
    if (!btn) return;
    if (btn.dataset.review) return reviewVersion(btn);
    if (btn.dataset.cancel) return cancelVersion(btn);
    if (btn.dataset.approve) return approveUnblind(btn);
    if (btn.dataset.supplement) return supplementUnblind(btn);
  });

  loadAll();
}

init();

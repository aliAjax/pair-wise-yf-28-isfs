"use strict";

const $ = (id) => document.getElementById(id);
const STATUS_TEXT = {
  pending_review: "待审核",
  returned: "已退回",
  approved: "已通过",
  pending: "待确认",
  materials_pending: "待补材料",
  rejected: "已拒绝",
};
let config = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "X-User-Id": $("user").value },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data?.error?.message || `请求失败 (${res.status})`);
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

function showResult(el, message, kind = "ok") {
  el.textContent = message;
  el.className = `result ${kind}`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadTrial() {
  const trialId = $("trialId").value.trim();
  if (!trialId) return;
  try {
    config = await api(`/api/trials/${trialId}/config`);
    renderConfig();
    await Promise.all([loadAmendments(), loadUnblinding()]);
  } catch (err) {
    config = null;
    $("trialMeta").textContent = err.message;
    $("amendmentList").textContent = "无法载入。";
    $("unblindList").textContent = "无法载入。";
    $("banner").classList.add("hidden");
  }
}

function renderConfig() {
  const role = $("user").value;
  const paused = config.enrollment_paused;
  const banner = $("banner");
  if (paused) {
    banner.textContent = `版本申请 ${config.pending_amendment.application_no}（${esc(config.pending_amendment.new_version)}）审核期间，本试验暂停新入组。`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  $("trialMeta").innerHTML =
    `<b>${esc(config.name)}</b> · 当前版本 <b>${esc(config.protocol_version)}</b> · 状态 ${esc(config.status)}<br>` +
    `试验组：${config.arms.map(esc).join(" / ")} ｜ 分层因素：${config.strata_factors.map(esc).join(" / ")} ｜ 区组长度：${config.block_size}`;
  $("newVersion").value = "";
  $("reason").value = "";
  $("arms").value = config.arms.join(", ");
  $("strataFactors").value = config.strata_factors.join(", ");
  $("blockSize").value = config.block_size;
  const coordOnly = role === "coord" && config.status === "running" && !paused;
  ["newVersion", "reason", "arms", "strataFactors", "blockSize"].forEach((id) => ($(id).disabled = !coordOnly));
  $("amendmentForm").querySelector("button").disabled = !coordOnly;
}

async function loadAmendments() {
  const data = await api(`/api/trials/${config.id}/amendments`);
  const role = $("user").value;
  const box = $("amendmentList");
  if (!data.items.length) {
    box.textContent = "暂无版本申请。";
    return;
  }
  box.innerHTML = data.items.map((a) => {
    const enrolled = a.enrolled.length
      ? a.enrolled.map((p) => `${esc(p.external_id)}（${esc(p.site_id)}）`).join("、")
      : "（提交时无已入组受试者）";
    let actions = "";
    if (a.status === "pending_review" && (role === "coord" || role.startsWith("monitor"))) {
      actions = `
        <div class="actions">
          <button class="primary" onclick="reviewAmendment(${a.id}, 'approve')">审核通过（旧分层沿用）</button>
          <button onclick="reviewAmendment(${a.id}, 'return')">退回</button>
          <input class="review-note" id="note-${a.id}" placeholder="退回时必填说明；通过时可留空">
        </div>`;
    }
    const note = a.review_note ? `<div class="sub">审核意见：${esc(a.review_note)}</div>` : "";
    return `
      <div class="item">
        <div class="head">
          <span class="no">${esc(a.application_no)} → ${esc(a.new_version)}</span>
          <span class="tag ${a.status}">${STATUS_TEXT[a.status] || a.status}</span>
        </div>
        <div class="sub">修订原因：${esc(a.reason)}</div>
        <div class="sub">已入组名单（${a.enrolled_count} 人）：</div>
        <div class="enrolled">${enrolled}</div>
        <div class="sub">分层沿用核对：${a.compatibility.compatible ? "✅ 兼容，旧分层可沿用" : "❌ 不兼容，系统退回"}
          （分层因素一致：${a.compatibility.strata_factors_same ? "是" : "否"}；旧分组保留：${a.compatibility.old_arms_preserved ? "是" : "否"}）</div>
        <div class="sub">提交：${esc(a.submitted_by)} · ${esc(a.submitted_at)}</div>
        ${note}${actions}
      </div>`;
  }).join("");
}

async function submitAmendment(event) {
  event.preventDefault();
  const out = $("amendmentResult");
  try {
    const payload = {
      new_version: $("newVersion").value.trim(),
      reason: $("reason").value.trim(),
      arms: $("arms").value.split(",").map((s) => s.trim()).filter(Boolean),
      strata_factors: $("strataFactors").value.split(",").map((s) => s.trim()).filter(Boolean),
      block_size: Number($("blockSize").value),
    };
    const data = await api(`/api/trials/${config.id}/amendments`, { method: "POST", body: JSON.stringify(payload) });
    if (data.status === "returned") {
      showResult(out,
        `申请 ${data.application_no} 已被系统核对退回：\n` + data.incompatible_reasons.map((r) => `• ${r}`).join("\n") +
        "\n入组不受影响，可调整后重新提交。", "bad");
    } else {
      showResult(out,
        `申请 ${data.application_no} 已提交（${data.new_version}），状态：待审核。\n` +
        `已快照 ${data.enrolled_count} 名已入组受试者；审核期间暂停新入组。`, "warn");
      $("newVersion").value = ""; $("reason").value = "";
    }
    await loadTrial();
  } catch (err) {
    showResult(out, err.message, "bad");
  }
}

async function reviewAmendment(id, decision) {
  const note = $(`note-${id}`)?.value.trim() || "";
  try {
    const data = await api(`/api/amendments/${id}/review`, {
      method: "POST", body: JSON.stringify({ decision, note }),
    });
    await loadTrial();
    if (data.released_requests.length) {
      showResult($("unblindResult"),
        `版本申请 ${data.application_no} 已${decision === "approve" ? "通过" : "退回"}，` +
        `${data.released_requests.length} 个挂起的揭盲申请已转为待确认。`, "ok");
    }
  } catch (err) {
    showResult($("amendmentResult"), err.message, "bad");
  }
}

async function loadUnblinding() {
  const data = await api(`/api/trials/${config.id}/unblinding-requests`);
  const role = $("user").value;
  const canConfirm = role === "coord" || role.startsWith("monitor");
  const box = $("unblindList");
  if (!data.items.length) {
    box.textContent = "暂无揭盲申请。";
    return;
  }
  box.innerHTML = data.items.map((r) => {
    let body = "";
    if (r.status === "materials_pending") {
      body = `<div class="sub">版本申请 <b>${esc(r.amendment_application_no)}</b> 未完成，本申请停在「待补材料」；申请编号 <b>${esc(r.request_no)}</b>，版本审核结束后自动转待确认。</div>`;
    }
    if (r.status === "pending") {
      const who = r.first_approver ? `第一确认人：${esc(r.first_approver)}，等待第二人确认` : "等待第一名人员确认";
      body = `<div class="sub">${who}</div>`;
      if (canConfirm) {
        const selfHint = r.requester_id === role ? "（你是发起人，不能确认）" : "";
        body += `<div class="actions"><button class="danger" ${r.requester_id === role ? "disabled" : ""}
          onclick="confirmUnblinding(${r.id})">双人确认揭盲${esc(selfHint)}</button></div>`;
      }
    }
    if (r.status === "approved") {
      body = `<div class="sub">确认人：${esc(r.first_approver)} → ${esc(r.second_approver)} · ${esc(r.decided_at || "")}</div>
              <div class="arm-reveal">受试者 ${esc(r.external_id)} 的组别：${esc(r.arm)}</div>`;
    }
    return `
      <div class="item">
        <div class="head">
          <span class="no">${esc(r.request_no)} · 受试者 ${esc(r.external_id)}</span>
          <span class="tag ${r.status}">${STATUS_TEXT[r.status] || r.status}</span>
        </div>
        <div class="sub">原因：${esc(r.reason)}</div>
        <div class="sub">发起人：${esc(r.requester_id)} · ${esc(r.created_at)}</div>
        ${body}
      </div>`;
  }).join("");
}

async function requestUnblinding(event) {
  event.preventDefault();
  const out = $("unblindResult");
  try {
    const participantId = $("participantId").value.trim();
    const data = await api(`/api/participants/${participantId}/unblinding-requests`, {
      method: "POST",
      body: JSON.stringify({ reason: $("unblindReason").value.trim() }),
    });
    if (data.status === "materials_pending") {
      showResult(out,
        `揭盲申请 ${data.request_no} 已登记。\n${data.message}`, "warn");
    } else {
      showResult(out, `揭盲申请 ${data.request_no} 已提交，等待两名不同人员先后确认。`, "ok");
    }
    $("unblindReason").value = "";
    await loadUnblinding();
  } catch (err) {
    showResult(out, err.message, "bad");
  }
}

async function confirmUnblinding(id) {
  const out = $("unblindResult");
  try {
    const data = await api(`/api/unblinding-requests/${id}/approve`, { method: "POST", body: "{}" });
    if (data.status === "pending") {
      showResult(out, `${data.request_no}：第一名人员（${data.first_approver}）已确认，仍需另一名人员确认。`, "warn");
    } else {
      showResult(out, `${data.request_no}：双人确认完成，受试者组别已在下方列表中显示。`, "ok");
    }
    await loadUnblinding();
  } catch (err) {
    showResult(out, err.message, "bad");
  }
}

$("loadBtn").addEventListener("click", loadTrial);
$("amendmentForm").addEventListener("submit", submitAmendment);
$("unblindForm").addEventListener("submit", requestUnblinding);
$("user").addEventListener("change", () => { if (config) renderConfig(); });
window.reviewAmendment = reviewAmendment;
window.confirmUnblinding = confirmUnblinding;

loadTrial();

const state = {
  status: "pending",
  summaries: [],
  filtered: [],
  currentKey: "",
  currentBundle: null,
  currentBundleStatus: "pending",
};

const bundleList = document.getElementById("bundle-list");
const groupFilter = document.getElementById("group-filter");
const searchInput = document.getElementById("search-input");
const labelerInput = document.getElementById("labeler-input");
const problemTitle = document.getElementById("problem-title");
const problemMeta = document.getElementById("problem-meta");
const statementBody = document.getElementById("statement-body");
const roundsRoot = document.getElementById("rounds-root");
const emptyState = document.getElementById("empty-state");
const problemView = document.getElementById("problem-view");
const progressPill = document.getElementById("progress-pill");

const LABELER_KEY = "xcpc-annotation-labeler";

function escapeHtml(text) {
  return String(text || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function fetchJson(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function getCurrentLabeler() {
  return labelerInput.value.trim();
}

function setStatusTab(status) {
  state.status = status;
  document.querySelectorAll("#status-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.status === status);
  });
}

function buildSummaryKey(item) {
  return `${item.group_id}:${item.problem_id}`;
}

function renderGroupFilter() {
  const current = groupFilter.value;
  const groups = [...new Set(state.summaries.map((item) => String(item.group_id)))];
  groupFilter.innerHTML = '<option value="">全部</option>' +
    groups.map((group) => `<option value="${group}">${group}</option>`).join("");
  if (groups.includes(current)) {
    groupFilter.value = current;
  }
}

function applyFilters() {
  const query = searchInput.value.trim().toLowerCase();
  const group = groupFilter.value;
  state.filtered = state.summaries.filter((item) => {
    const matchesGroup = !group || String(item.group_id) === group;
    const haystack = `${item.problem_id} ${item.problem_name || ""}`.toLowerCase();
    const matchesQuery = !query || haystack.includes(query);
    return matchesGroup && matchesQuery;
  });
}

function renderBundleList() {
  applyFilters();
  if (!state.filtered.length) {
    bundleList.innerHTML = '<div class="secondary">没有匹配的数据。</div>';
    return;
  }

  bundleList.innerHTML = state.filtered.map((item) => {
    const key = buildSummaryKey(item);
    const active = key === state.currentKey ? " active" : "";
    const badgeClass = item.status === "labeled" ? "pill ok" : "pill warn";
    return `
      <button class="bundle-item${active}" data-key="${key}" type="button">
        <div class="bundle-item-head">
          <strong>${escapeHtml(item.problem_id)}</strong>
          <span class="${badgeClass}">${escapeHtml(item.progress)}</span>
        </div>
        <p>${escapeHtml(item.problem_name || item.problem_id)}</p>
        <p class="secondary">群 ${escapeHtml(item.group_id)} · ${escapeHtml(item.source || "")}</p>
        <p class="secondary">${escapeHtml(item.problem_preview || "")}</p>
      </button>
    `;
  }).join("");

  bundleList.querySelectorAll(".bundle-item").forEach((button) => {
    button.addEventListener("click", async () => {
      await loadBundle(button.dataset.key, null, { resetScroll: true });
    });
  });
}

function summarizeProgress(bundle) {
  const total = bundle.rounds.length;
  let labeled = 0;
  for (const round of bundle.rounds) {
    if (round.human_label && round.human_label.expected_verdict) {
      labeled += 1;
    }
  }
  return { labeled, total };
}

function verdictPillClass(verdict) {
  if (verdict === "correct") return "pill ok";
  if (verdict === "incorrect") return "pill bad";
  return "pill warn";
}

function formatStatement(statement) {
  const blocks = [];
  const push = (title, value) => {
    if (!value) return;
    blocks.push(`
      <div class="statement-block">
        <h4>${title}</h4>
        <pre>${escapeHtml(value)}</pre>
      </div>
    `);
  };

  push("题目名", statement.name || "");
  push("中文题意", statement.summary_zh || "正在加载中文题意翻译…");
  push("时间限制", statement.time_limit || "");
  push("内存限制", statement.memory_limit || "");
  if (Array.isArray(statement.tags) && statement.tags.length) {
    push("标签", statement.tags.join(", "));
  }
  if (statement.rating) {
    push("Rating", String(statement.rating));
  }
  push("Description", statement.description || "");
  push("Input", statement.input || "");
  push("Output", statement.output || "");
  if (Array.isArray(statement.samples) && statement.samples.length) {
    const sampleText = statement.samples.map((sample, index) =>
      `Sample ${index + 1}\nInput:\n${sample.input || ""}\nOutput:\n${sample.output || ""}`,
    ).join("\n\n");
    push("Samples", sampleText);
  }
  push("Notes", statement.notes || "");
  return blocks.join("") || '<div class="secondary">题面缓存不可用。</div>';
}

function verdictPill(expected, model) {
  if (!expected) {
    return '<span class="pill warn">未标注</span>';
  }
  if (expected === model) {
    return '<span class="pill ok">模型判定一致</span>';
  }
  return '<span class="pill bad">模型判定不一致</span>';
}

function renderHistory(history) {
  if (!history.length) {
    return '<p class="secondary">这一轮之前没有上下文。</p>';
  }
  return `
    <div class="history-list">
      ${history.map((item) => `
        <div class="history-entry">
          <div class="inline-labels">
            <span class="${verdictPillClass(item.result || "")}">${escapeHtml(item.result || "")}</span>
            <span class="secondary">${escapeHtml(item.timestamp || "")}</span>
          </div>
          <pre>${escapeHtml(item.submission || "")}</pre>
          ${item.reason ? `<p><strong>Reason:</strong> ${escapeHtml(item.reason)}</p>` : ""}
          ${item.reply ? `<p><strong>Reply:</strong> ${escapeHtml(item.reply)}</p>` : ""}
        </div>
      `).join("")}
    </div>
  `;
}

function renderRounds(bundle) {
  const groups = new Map();
  for (const round of bundle.rounds) {
    const key = `${round.user_id}`;
    if (!groups.has(key)) {
      groups.set(key, { nickname: round.nickname || String(round.user_id), rounds: [] });
    }
    groups.get(key).rounds.push(round);
  }

  roundsRoot.innerHTML = [...groups.entries()].map(([uid, group]) => `
    <section class="user-group">
      <h4>${escapeHtml(group.nickname)} <span class="secondary">(${escapeHtml(uid)})</span></h4>
      ${group.rounds.map((round, index) => {
        const label = round.human_label || {};
        const expected = label.expected_verdict || "";
        const radioName = `verdict-${round.round_id}`;
        return `
          <article class="round" data-round-id="${escapeHtml(round.round_id)}">
            <div class="round-head">
              <div>
                <strong>Round ${index + 1}</strong>
                <div class="inline-labels">
                  <span class="${verdictPillClass(round.model_verdict)}">${escapeHtml(round.model_verdict)}</span>
                  ${verdictPill(expected, round.model_verdict)}
                </div>
              </div>
              <span class="secondary">${escapeHtml(round.timestamp || "")}</span>
            </div>

            <div class="round-copy">
              <p><strong>Submission</strong></p>
              <pre>${escapeHtml(round.submission || "")}</pre>
              ${round.reason ? `<p><strong>Reason:</strong> ${escapeHtml(round.reason)}</p>` : ""}
              ${round.reply ? `<p><strong>Reply:</strong> ${escapeHtml(round.reply)}</p>` : ""}
            </div>

            <details>
              <summary>查看这轮之前的上下文</summary>
              ${renderHistory(round.history_before || [])}
            </details>

            <div class="verdict-row">
              <label class="radio-chip">
                <input type="radio" name="${radioName}" value="" ${!expected ? "checked" : ""}>
                <span>未标注</span>
              </label>
              <label class="radio-chip">
                <input type="radio" name="${radioName}" value="correct" ${expected === "correct" ? "checked" : ""}>
                <span>应该判对</span>
              </label>
              <label class="radio-chip">
                <input type="radio" name="${radioName}" value="incorrect" ${expected === "incorrect" ? "checked" : ""}>
                <span>不该判对</span>
              </label>
            </div>

            <label class="field">
              <span>标注备注</span>
              <textarea data-role="comment">${escapeHtml(label.comment || "")}</textarea>
            </label>
          </article>
        `;
      }).join("")}
    </section>
  `).join("");
}

function renderBundle(bundle, status) {
  state.currentBundle = bundle;
  state.currentBundleStatus = status;
  problemTitle.textContent = `${bundle.problem_id} · ${bundle.problem_name || bundle.problem_id}`;

  const progress = summarizeProgress(bundle);
  progressPill.textContent = `${progress.labeled}/${progress.total} 已标注`;
  progressPill.className = `pill ${progress.labeled === progress.total && progress.total ? "ok" : "warn"}`;

  problemMeta.innerHTML = `
    <div class="meta-grid">
      <div class="meta-item">
        <p class="secondary">群号</p>
        <p>${escapeHtml(bundle.group_id)}</p>
      </div>
      <div class="meta-item">
        <p class="secondary">来源</p>
        <p>${escapeHtml(bundle.source || "")}</p>
      </div>
      <div class="meta-item">
        <p class="secondary">首个通过</p>
        <p>${escapeHtml(bundle.first_solver?.nickname || "")}</p>
      </div>
      <div class="meta-item">
        <p class="secondary">状态</p>
        <p>${escapeHtml(status)}</p>
      </div>
    </div>
  `;

  statementBody.innerHTML = formatStatement(bundle.statement || {});
  renderRounds(bundle);

  emptyState.classList.add("hidden");
  problemView.classList.remove("hidden");
}

async function ensureTranslation(key, preferredStatus = null) {
  const [groupId, problemId] = key.split(":");
  const query = preferredStatus || state.currentBundleStatus || state.status;
  const data = await fetchJson(
    `/api/annotations/${groupId}/${problemId}/translate?status=${encodeURIComponent(query)}`,
    { method: "POST" },
  );
  if (!data.summary_zh) {
    return;
  }
  if (state.currentKey !== key || !state.currentBundle) {
    return;
  }
  state.currentBundle.statement = {
    ...(state.currentBundle.statement || {}),
    summary_zh: data.summary_zh,
  };
  statementBody.innerHTML = formatStatement(state.currentBundle.statement || {});
}

function applyFormToBundle() {
  if (!state.currentBundle) return;
  const labeler = getCurrentLabeler();
  const now = new Date().toISOString();
  const roundNodes = roundsRoot.querySelectorAll("[data-round-id]");
  const roundMap = new Map(state.currentBundle.rounds.map((round) => [round.round_id, round]));

  for (const node of roundNodes) {
    const round = roundMap.get(node.dataset.roundId);
    if (!round) continue;
    const checked = node.querySelector("input[type=radio]:checked");
    const comment = node.querySelector("textarea[data-role=comment]")?.value || "";
    const expected = checked ? checked.value : "";
    round.human_label = round.human_label || {};
    round.human_label.comment = comment.trim();

    if (expected === "correct" || expected === "incorrect") {
      round.human_label.expected_verdict = expected;
      round.human_label.labeler = labeler;
      round.human_label.labeled_at = now;
    } else {
      round.human_label.expected_verdict = null;
      round.human_label.labeler = "";
      round.human_label.labeled_at = "";
    }
  }
}

async function saveCurrent(status) {
  if (!state.currentBundle) return;
  applyFormToBundle();
  const progress = summarizeProgress(state.currentBundle);
  if (status === "labeled" && progress.labeled !== progress.total) {
    const okay = window.confirm("还有未标注轮次，仍然标记为已标注吗？");
    if (!okay) return;
  }

  await fetchJson(`/api/annotations/${state.currentBundle.group_id}/${state.currentBundle.problem_id}/save`, {
    method: "POST",
    body: JSON.stringify({ status, bundle: state.currentBundle }),
  });
  if (state.status !== status) {
    setStatusTab(status);
  }
  await loadSummaries();
  await loadBundle(`${state.currentBundle.group_id}:${state.currentBundle.problem_id}`, status);
}

async function loadSummaries() {
  const data = await fetchJson(`/api/annotations?status=${encodeURIComponent(state.status)}`);
  state.summaries = data.items || [];
  renderGroupFilter();
  renderBundleList();
}

async function loadBundle(key, preferredStatus = null, options = {}) {
  const { resetScroll = false } = options;
  const [groupId, problemId] = key.split(":");
  const query = preferredStatus || state.status;
  const data = await fetchJson(`/api/annotations/${groupId}/${problemId}?status=${encodeURIComponent(query)}`);
  state.currentKey = key;
  renderBundleList();
  renderBundle(data.bundle, data.status);
  if (resetScroll) {
    problemView.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }
  if (!data.bundle?.statement?.summary_zh) {
    ensureTranslation(key, data.status).catch((error) => {
      console.error("translation fetch failed", error);
    });
  }
}

async function syncData() {
  const body = {};
  if (groupFilter.value) {
    body.group_id = Number(groupFilter.value);
  }
  await fetchJson("/api/sync", {
    method: "POST",
    body: JSON.stringify(body),
  });
  await loadSummaries();
}

function installEvents() {
  document.querySelectorAll("#status-tabs button").forEach((button) => {
    button.addEventListener("click", async () => {
      setStatusTab(button.dataset.status);
      await loadSummaries();
      problemView.classList.add("hidden");
      emptyState.classList.remove("hidden");
    });
  });

  groupFilter.addEventListener("change", renderBundleList);
  searchInput.addEventListener("input", renderBundleList);

  document.getElementById("save-btn").addEventListener("click", async () => {
    await saveCurrent("pending");
  });
  document.getElementById("complete-btn").addEventListener("click", async () => {
    await saveCurrent("labeled");
  });
  document.getElementById("sync-btn").addEventListener("click", async () => {
    await syncData();
  });

  labelerInput.addEventListener("change", () => {
    localStorage.setItem(LABELER_KEY, labelerInput.value.trim());
  });
}

async function bootstrap() {
  labelerInput.value = localStorage.getItem(LABELER_KEY) || "";
  installEvents();
  setStatusTab("pending");
  await loadSummaries();
}

bootstrap().catch((error) => {
  emptyState.classList.remove("hidden");
  emptyState.innerHTML = `<p>加载失败：${escapeHtml(error.message)}</p>`;
});

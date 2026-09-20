const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const app = {
  state: null,
  quotes: new Map(),
  bars: [],
  selected: null,
  health: null,
  quoteMeta: null,
  historyMeta: null,
  refreshTimer: null,
  lastHistoryRefresh: 0,
  searchResults: [],
  searchSelection: null,
  searchTimer: null,
  searchRequest: 0,
  searchActiveIndex: -1,
  knownRecordIds: new Set(),
  recordsInitialized: false,
  nativeNotifications: false,
  ruleTests: new Map(),
  editingAlertId: null,
  sourceTest: null,
  audit: { items: [], summaries: [], total: 0 },
  company: null,
  market: [],
  contextLoadedAt: 0,
  busy: false,
  chart: { left: 54, right: 62, top: 18, bottom: 72, volumeHeight: 62, bars: [], visibleCount: 90, offset: 0, dragging: false, dragX: 0 },
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok || body.ok === false) throw new Error(body.error || `请求失败 ${response.status}`);
  return body;
}

function fmt(value, digits = 2) {
  return value == null || Number.isNaN(Number(value)) ? "—" : Number(value).toFixed(digits);
}

function compact(value) {
  if (value == null) return "—";
  const number = Number(value);
  if (Math.abs(number) >= 1e8) return `${(number / 1e8).toFixed(2)} 亿`;
  if (Math.abs(number) >= 1e4) return `${(number / 1e4).toFixed(1)} 万`;
  return number.toFixed(0);
}

function localTime(value) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function trendClass(value) {
  return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "";
}

function showError(message) {
  const banner = $("#errorBanner");
  banner.textContent = message;
  banner.classList.remove("warning");
  banner.classList.remove("hidden");
}

function showWarning(message) {
  const banner = $("#errorBanner");
  banner.textContent = message;
  banner.classList.add("warning");
  banner.classList.remove("hidden");
}

function clearError() {
  const banner = $("#errorBanner");
  banner.classList.remove("warning");
  banner.classList.add("hidden");
}

const sourceLabels = {
  hithink: "同花顺官方",
  tencent: "腾讯公开行情",
  eastmoney: "东方财富公开行情",
  public: "公开行情",
  auto: "自动路由",
  scheduler: "后台调度器",
};

function sourceText(sources = []) {
  return sources.length ? sources.map((source) => sourceLabels[source] || source).join(" + ") : "无数据";
}

async function loadHealth() {
  const body = await api("/api/health");
  app.health = body;
  const market = body.market_session || {};
  $("#marketBadge").textContent = market.label || "市场状态未知";
  $("#marketBadge").className = `badge ${market.status === "trading" ? "ok" : "warning"}`;
  $("#marketStatus").textContent = market.label || "未知";
  $("#marketBasis").textContent = market.basis || "交易日历口径未知。";
  const monitor = body.monitor || {};
  const monitorLabels = { success: "后台运行中", idle: "等待规则", paused: "已暂停", source_error: "数据源异常", error: "运行异常", waiting: "正在启动" };
  $("#monitorStatus").textContent = monitor.running ? (monitorLabels[monitor.last_cycle] || "后台运行中") : "已停止";
  $("#monitorStatus").title = monitor.next_check_at ? `下次检查 ${localTime(monitor.next_check_at)}` : "后台监控随本地服务运行";
  app.nativeNotifications = Boolean(body.desktop?.native_notifications);
  syncNotificationButton();
  const successes = (body.sources || []).map((source) => source.last_success_at).filter(Boolean).sort();
  $("#lastSuccess").textContent = successes.length ? localTime(successes.at(-1)) : "尚无成功记录";
  $("#sourceHealthList").innerHTML = (body.sources || []).map((source) => {
    const status = source.status === "ok" ? "正常" : source.status === "cooldown" ? "暂时熔断" : source.status === "error" ? "最近失败" : source.status === "disabled" ? "未配置" : "等待请求";
    const retry = source.retry_after ? ` · 重试 ${localTime(source.retry_after)}` : "";
    const detail = source.last_error ? `错误：${escapeHtml(source.last_error)}${retry}` : source.last_success_at ? `成功：${localTime(source.last_success_at)} · ${source.last_duration_ms ?? "—"} ms` : "尚未请求";
    return `<div class="source-health-item"><strong>${escapeHtml(source.label)} <span class="status-${["disabled", "cooldown"].includes(source.status) ? "idle" : source.status}">${status}</span></strong><span>${detail}</span></div>`;
  }).join("");
  if (body.configuration_warning) showWarning(body.configuration_warning);
}

let toastTimer;
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 3500);
}

async function loadState() {
  const body = await api("/api/state");
  app.state = body.state;
  if (!app.recordsInitialized) {
    app.knownRecordIds = new Set((app.state.records || []).map((item) => item.id));
    app.recordsInitialized = true;
  }
  if (!app.state.watchlist.some((item) => item.symbol === app.selected)) {
    app.selected = app.state.watchlist[0]?.symbol || null;
  }
  $("#sourceSelect").value = app.state.settings.source;
  $("#intervalSelect").value = String(app.state.settings.refresh_seconds);
  $("#autoRefresh").checked = app.state.settings.auto_refresh;
  $("#quietHoursEnabled").checked = app.state.settings.quiet_hours_enabled !== false;
  $("#quietStart").value = app.state.settings.quiet_start || "15:01";
  $("#quietEnd").value = app.state.settings.quiet_end || "09:29";
  syncQuietHoursForm();
  renderWatchControls();
  renderWatchlist();
  renderAlertFormSymbols();
  renderAlerts();
  renderRecords();
  configureRefresh();
}

async function loadAlerts(notify = true) {
  const body = await api("/api/alerts");
  const fresh = (body.records || []).filter((item) => !app.knownRecordIds.has(item.id));
  app.state.alerts = body.items || [];
  app.state.records = body.records || [];
  for (const item of app.state.records) app.knownRecordIds.add(item.id);
  renderAlerts();
  renderRecords();
  if (notify) {
    for (const item of fresh.reverse()) sendNotification(item, true);
  }
}

async function refreshAll({ notify = true, forceHistory = false } = {}) {
  if (app.busy) return;
  app.busy = true;
  $("#refreshButton").disabled = true;
  $("#refreshButton").textContent = "刷新中…";
  $("#dataStatus").textContent = "加载中";
  clearError();
  try {
    await loadHealth();
    const quoteBody = await api("/api/quotes");
    app.quoteMeta = quoteBody;
    app.quotes = new Map(quoteBody.items.map((item) => [item.symbol, item]));
    const actualSource = sourceText(quoteBody.sources);
    $("#sourceBadge").textContent = quoteBody.data_status === "cached" ? `${actualSource} · 缓存` : actualSource;
    $("#sourceBadge").className = `badge ${quoteBody.data_status === "cached" ? "warning" : "ok"}`;
    $("#dataStatus").textContent = quoteBody.data_status === "cached" ? "缓存数据" : "实时请求成功";
    $("#routeSummary").textContent = `${sourceLabels[quoteBody.provider] || quoteBody.provider} → ${actualSource}`;
    const latest = Math.max(0, ...quoteBody.items.map((item) => Number(item.timestamp || 0)));
    $("#updatedAt").textContent = latest ? `数据时间 ${localTime(latest)}` : `刷新于 ${localTime(quoteBody.fetched_at)} · 源时间缺失`;
    if (quoteBody.data_status === "cached") showWarning(`实时来源暂时失败，当前显示 ${localTime(quoteBody.cached_at)} 保存的缓存；预警不会使用缓存行情。${quoteBody.warning ? ` 原因：${quoteBody.warning}` : ""}`);
    else if (app.health?.market_session?.status === "trading" && latest) {
      const timestampMs = latest < 1e12 ? latest * 1000 : latest;
      const ageSeconds = Math.round((Date.now() - timestampMs) / 1000);
      if (ageSeconds > 180) showWarning(`当前处于交易时段，但行情时间已落后约 ${Math.ceil(ageSeconds / 60)} 分钟。请运行“测试当前数据路由”确认来源状态。`);
    }
    renderWatchlist();
    renderQuote();
    if (app.selected && (forceHistory || Date.now() - app.lastHistoryRefresh >= 60_000)) {
      await loadHistory(app.selected);
      app.lastHistoryRefresh = Date.now();
    }
    if (forceHistory || Date.now() - app.contextLoadedAt >= 60_000) await loadContext();
    await loadAlerts(notify);
    await loadHealth();
  } catch (error) {
    showError(`${error.message}。当前数据未更新，预警不会使用伪造行情。`);
    $("#sourceBadge").textContent = "数据源异常";
    $("#sourceBadge").className = "badge error";
    $("#dataStatus").textContent = "暂时失败";
  } finally {
    app.busy = false;
    $("#refreshButton").disabled = false;
    $("#refreshButton").textContent = "立即刷新";
  }
}

async function loadHistory(symbol) {
  const body = await api(`/api/history?symbol=${encodeURIComponent(symbol)}&limit=250`);
  if (app.selected !== symbol) return;
  app.historyMeta = body;
  app.bars = body.items;
  if (app.chart.symbol !== symbol) {
    app.chart.symbol = symbol;
    app.chart.offset = 0;
  }
  app.chart.visibleCount = Math.min(Number($("#chartRange").value) || 90, Math.max(1, app.bars.length));
  app.chart.offset = Math.min(app.chart.offset, Math.max(0, app.bars.length - app.chart.visibleCount));
  $("#chartMeta").textContent = `前复权 · ${body.items.length} 个交易日 · ${sourceText(body.sources)}${body.data_status === "cached" ? " · 缓存" : ""}`;
  if (body.data_status === "cached") showWarning(`历史行情实时请求失败，图表显示 ${localTime(body.cached_at)} 保存的缓存。${body.warning ? ` 原因：${body.warning}` : ""}`);
  renderIndicators();
  drawChart();
  drawIndicatorCharts();
}

async function loadContext() {
  const selected = app.selected;
  const tasks = [api("/api/market-overview")];
  if (selected) tasks.push(api(`/api/company?symbol=${encodeURIComponent(selected)}`));
  const results = await Promise.allSettled(tasks);
  if (results[0].status === "fulfilled") {
    app.market = results[0].value.items || [];
    const meta = results[0].value;
    $("#marketOverview").innerHTML = app.market.map((item) => `<div class="market-card"><span>${escapeHtml(item.name)}</span><strong>${fmt(item.price)}</strong><em class="${trendClass(item.change_pct)}">${Number(item.change_pct) >= 0 ? "+" : ""}${fmt(item.change_pct)}%</em></div>`).join("");
    $("#contextMeta").textContent = `大盘：${meta.data_status === "cached" ? `缓存 ${localTime(meta.cached_at)}` : "腾讯公开行情"}`;
  } else {
    $("#marketOverview").innerHTML = `<div class="empty">大盘指数暂时不可用：${escapeHtml(results[0].reason.message)}</div>`;
  }
  if (selected && results[1]?.status === "fulfilled" && app.selected === selected) {
    app.company = results[1].value.item;
    const item = app.company || {};
    $("#companyOverview").innerHTML = `<div><span>行业</span><strong>${escapeHtml(item.industry || "—")}</strong></div><div><span>地区</span><strong>${escapeHtml(item.region || "—")}</strong></div><div><span>上市日期</span><strong>${escapeHtml(item.listing_date || "—")}</strong></div><div><span>总市值</span><strong>${compact(item.total_market_cap)}</strong></div><div><span>流通市值</span><strong>${compact(item.float_market_cap)}</strong></div><div><span>动态市盈率</span><strong>${fmt(item.pe_dynamic)}</strong></div><div><span>市净率</span><strong>${fmt(item.pb)}</strong></div>`;
    if (results[1].value.data_status === "cached") $("#contextMeta").textContent += ` · 公司信息缓存 ${localTime(results[1].value.cached_at)}`;
  } else if (selected) {
    const reason = results[1]?.reason?.message || "无数据";
    $("#companyOverview").innerHTML = `<div class="empty">公司信息暂时不可用：${escapeHtml(reason)}</div>`;
  }
  app.contextLoadedAt = Date.now();
}

function hideStockSuggestions() {
  $("#stockSuggestions").classList.add("hidden");
  $("#stockCode").setAttribute("aria-expanded", "false");
  app.searchActiveIndex = -1;
}

function renderStockSuggestions(message = "") {
  const panel = $("#stockSuggestions");
  if (message) {
    panel.innerHTML = `<div class="stock-search-empty">${escapeHtml(message)}</div>`;
  } else if (!app.searchResults.length) {
    panel.innerHTML = '<div class="stock-search-empty">未找到沪深北 A 股，可直接输入 6 位代码</div>';
  } else {
    panel.innerHTML = app.searchResults.map((item, index) => `
      <button type="button" role="option" class="stock-suggestion ${index === app.searchActiveIndex ? "active" : ""}" data-search-symbol="${item.symbol}">
        <strong>${escapeHtml(item.name)}</strong><span>${item.symbol}</span>
      </button>`).join("");
    $$('[data-search-symbol]').forEach((button) => button.addEventListener("click", () => {
      const item = app.searchResults.find((candidate) => candidate.symbol === button.dataset.searchSymbol);
      if (item) selectStockSuggestion(item, true);
    }));
  }
  panel.classList.remove("hidden");
  $("#stockCode").setAttribute("aria-expanded", "true");
}

function selectStockSuggestion(item, submit = false) {
  app.searchSelection = item.symbol;
  $("#stockCode").value = item.name;
  hideStockSuggestions();
  if (submit) $("#addStockForm").requestSubmit();
}

async function loadStockSuggestions() {
  const keyword = $("#stockCode").value.trim();
  const requestId = ++app.searchRequest;
  if (!keyword) {
    app.searchResults = [];
    hideStockSuggestions();
    return;
  }
  try {
    const body = await api(`/api/search?q=${encodeURIComponent(keyword)}`);
    if (requestId !== app.searchRequest) return;
    app.searchResults = body.items || [];
    app.searchActiveIndex = -1;
    renderStockSuggestions(body.warning && !app.searchResults.length ? "名称搜索暂不可用，可直接输入 6 位代码" : "");
  } catch (error) {
    if (requestId !== app.searchRequest) return;
    app.searchResults = [];
    renderStockSuggestions("名称搜索暂不可用，可直接输入 6 位代码");
  }
}

function scheduleStockSearch() {
  app.searchSelection = null;
  clearTimeout(app.searchTimer);
  app.searchTimer = setTimeout(loadStockSuggestions, 250);
}

function handleStockSearchKeydown(event) {
  if (!app.searchResults.length || $("#stockSuggestions").classList.contains("hidden")) {
    if (event.key === "Escape") hideStockSuggestions();
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    app.searchActiveIndex = (app.searchActiveIndex + step + app.searchResults.length) % app.searchResults.length;
    renderStockSuggestions();
  } else if (event.key === "Enter") {
    event.preventDefault();
    selectStockSuggestion(app.searchResults[app.searchActiveIndex < 0 ? 0 : app.searchActiveIndex], true);
  } else if (event.key === "Escape") {
    hideStockSuggestions();
  }
}

function renderWatchlist() {
  const allItems = app.state?.watchlist || [];
  const group = $("#watchGroupFilter").value;
  const sort = $("#watchSort").value;
  let watchlist = allItems.filter((item) => !group || (item.group || "默认") === group);
  if (sort === "change_desc") watchlist.sort((a, b) => Number(app.quotes.get(b.symbol)?.change_pct ?? -Infinity) - Number(app.quotes.get(a.symbol)?.change_pct ?? -Infinity));
  if (sort === "change_asc") watchlist.sort((a, b) => Number(app.quotes.get(a.symbol)?.change_pct ?? Infinity) - Number(app.quotes.get(b.symbol)?.change_pct ?? Infinity));
  if (sort === "name") watchlist.sort((a, b) => String(a.name).localeCompare(String(b.name), "zh-CN"));
  if (sort === "manual") watchlist.sort((a, b) => Number(a.position || 0) - Number(b.position || 0));
  $("#watchCount").textContent = `${allItems.length} 只${group ? ` · 本组 ${watchlist.length}` : ""}`;
  $("#watchlist").innerHTML = watchlist.length ? watchlist.map((item) => {
    const quote = app.quotes.get(item.symbol);
    const change = quote?.change_pct;
    return `<div class="watch-item ${app.selected === item.symbol ? "active" : ""}" data-symbol="${item.symbol}">
      <div class="watch-main"><div class="watch-name">${escapeHtml(quote?.name || item.name || item.symbol)}</div><div class="watch-code">${item.symbol}</div><div class="watch-meta"><span class="watch-group">${escapeHtml(item.group || "默认")}</span>${item.note ? `<span class="watch-note" title="${escapeHtml(item.note)}">${escapeHtml(item.note)}</span>` : ""}</div></div>
      <div><div class="watch-price">${fmt(quote?.price)}</div><div class="watch-change ${trendClass(change)}">${change == null ? "—" : `${change >= 0 ? "+" : ""}${fmt(change)}%`}</div></div>
      <div class="watch-item-actions"><button class="watch-edit" data-edit-symbol="${item.symbol}" title="编辑分组和备注">编辑</button><button class="watch-delete" data-delete-symbol="${item.symbol}" title="删除">删除</button></div>
    </div>`;
  }).join("") : `<div class="empty">${allItems.length ? "当前分组没有股票" : "添加一只股票开始观察"}</div>`;

  $$(".watch-item").forEach((node) => node.addEventListener("click", async (event) => {
    if (event.target.closest("[data-delete-symbol], [data-edit-symbol]")) return;
    app.selected = node.dataset.symbol;
    renderWatchlist();
    renderQuote();
    try {
      await loadHistory(app.selected);
      app.lastHistoryRefresh = Date.now();
      app.contextLoadedAt = 0;
      await loadContext();
    } catch (error) { showError(error.message); }
  }));
  $$('[data-delete-symbol]').forEach((button) => button.addEventListener("click", () => removeStock(button.dataset.deleteSymbol)));
  $$('[data-edit-symbol]').forEach((button) => button.addEventListener("click", () => openWatchEdit(button.dataset.editSymbol)));
}

function renderWatchControls() {
  const select = $("#watchGroupFilter");
  const current = select.value;
  const groups = [...new Set((app.state?.watchlist || []).map((item) => item.group || "默认"))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.innerHTML = '<option value="">全部分组</option>' + groups.map((group) => `<option value="${escapeHtml(group)}">${escapeHtml(group)}</option>`).join("");
  if (groups.includes(current)) select.value = current;
}

function openWatchEdit(symbol) {
  const item = app.state.watchlist.find((entry) => entry.symbol === symbol);
  if (!item) return;
  $("#watchEditSymbol").value = symbol;
  $("#watchEditName").textContent = `${item.name} · ${symbol}`;
  $("#watchEditGroup").value = item.group || "默认";
  $("#watchEditNote").value = item.note || "";
  $("#watchEditDialog").showModal();
}

async function saveWatchEdit(event) {
  event.preventDefault();
  const symbol = $("#watchEditSymbol").value;
  try {
    await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "PATCH", body: JSON.stringify({ group: $("#watchEditGroup").value, note: $("#watchEditNote").value }) });
    $("#watchEditDialog").close();
    await loadState();
    toast("自选股分组和备注已保存");
  } catch (error) { showError(error.message); }
}

async function importWatchlistBulk(event) {
  event.preventDefault();
  const items = $("#bulkSymbols").value.split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean);
  try {
    const body = await api("/api/watchlist/bulk", { method: "POST", body: JSON.stringify({ items, group: $("#bulkGroup").value }) });
    $("#bulkImportDialog").close();
    $("#bulkSymbols").value = "";
    await loadState();
    await refreshAll({ notify: false, forceHistory: true });
    toast(`已添加 ${body.items.length} 只，重复或无行情代码已跳过`);
  } catch (error) { showError(error.message); }
}

function renderQuote() {
  const item = app.quotes.get(app.selected);
  const stored = app.state?.watchlist.find((entry) => entry.symbol === app.selected);
  $("#stockName").textContent = item?.name || stored?.name || "选择一只股票";
  $("#stockSymbol").textContent = app.selected || "";
  $("#stockPrice").textContent = fmt(item?.price);
  $("#stockPrice").className = trendClass(item?.change_pct);
  $("#stockChange").textContent = item?.change_pct == null ? "—" : `${Number(item.change) >= 0 ? "+" : ""}${fmt(item.change)}  ${Number(item.change_pct) >= 0 ? "+" : ""}${fmt(item.change_pct)}%`;
  $("#stockChange").className = trendClass(item?.change_pct);
  $("#stockOpen").textContent = fmt(item?.open);
  $("#stockHigh").textContent = fmt(item?.high);
  $("#stockLow").textContent = fmt(item?.low);
  $("#stockTurnover").textContent = compact(item?.turnover);
}

function renderIndicators() {
  const last = app.bars.at(-1);
  $("#ma5").textContent = fmt(last?.ma5);
  $("#ma10").textContent = fmt(last?.ma10);
  $("#ma20").textContent = fmt(last?.ma20);
  const recent = app.bars.slice(-20);
  $("#range20").textContent = recent.length ? `${fmt(Math.min(...recent.map((x) => x.low)))} – ${fmt(Math.max(...recent.map((x) => x.high)))}` : "—";
  if (app.bars.length >= 6) {
    const previous = app.bars.slice(-6, -1).map((x) => Number(x.volume));
    const ratio = Number(last.volume) / (previous.reduce((a, b) => a + b, 0) / previous.length);
    $("#volumeRatio").textContent = `${fmt(ratio)} 倍`;
  } else $("#volumeRatio").textContent = "—";

  $("#rsiValue").textContent = fmt(last?.rsi14);
  if (last?.rsi14 == null) $("#rsiExplain").textContent = "至少需要 15 根 K 线";
  else if (last.rsi14 >= 70) $("#rsiExplain").textContent = "处于相对高位区间，仅描述位置";
  else if (last.rsi14 <= 30) $("#rsiExplain").textContent = "处于相对低位区间，仅描述位置";
  else $("#rsiExplain").textContent = "处于 30–70 中性区间";
  $("#macdValue").textContent = last ? `DIF ${fmt(last.macd_dif, 3)} · DEA ${fmt(last.macd_dea, 3)}` : "—";
  if (!last) $("#macdExplain").textContent = "等待数据";
  else if (last.macd_dif > last.macd_dea) $("#macdExplain").textContent = "DIF 高于 DEA，短期动量相对更强";
  else if (last.macd_dif < last.macd_dea) $("#macdExplain").textContent = "DIF 低于 DEA，短期动量相对更弱";
  else $("#macdExplain").textContent = "DIF 与 DEA 接近";
}

function prepareCanvas(selector) {
  const canvas = $(selector);
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, rect.width, rect.height);
  return { ctx, width: rect.width, height: rect.height };
}

function drawIndicatorCharts() {
  drawRsiChart();
  drawMacdChart();
}

function visibleBars() {
  const end = Math.max(0, app.bars.length - app.chart.offset);
  const start = Math.max(0, end - app.chart.visibleCount);
  return app.bars.slice(start, end);
}

function drawRsiChart() {
  const { ctx, width, height } = prepareCanvas("#rsiCanvas");
  const bars = visibleBars();
  const left = 34, right = 8, top = 8, bottom = 16;
  if (!bars.length || width <= left + right) return;
  const x = (index) => left + index * (width - left - right) / Math.max(1, bars.length - 1);
  const y = (value) => top + (100 - value) / 100 * (height - top - bottom);
  ctx.font = "9px Consolas";
  [70, 50, 30].forEach((level) => {
    ctx.strokeStyle = level === 50 ? "#252c34" : "rgba(243,182,75,.35)";
    ctx.setLineDash(level === 50 ? [] : [4, 4]);
    ctx.beginPath(); ctx.moveTo(left, y(level)); ctx.lineTo(width - right, y(level)); ctx.stroke();
    ctx.fillStyle = "#78838f"; ctx.fillText(String(level), 5, y(level) + 3);
  });
  ctx.setLineDash([]); ctx.strokeStyle = "#f3b64b"; ctx.lineWidth = 1.4; ctx.beginPath();
  let started = false;
  bars.forEach((bar, index) => {
    if (bar.rsi14 == null) return;
    if (!started) { ctx.moveTo(x(index), y(bar.rsi14)); started = true; } else ctx.lineTo(x(index), y(bar.rsi14));
  });
  ctx.stroke();
}

function drawMacdChart() {
  const { ctx, width, height } = prepareCanvas("#macdCanvas");
  const bars = visibleBars();
  const left = 8, right = 8, top = 8, bottom = 16;
  if (!bars.length || width <= left + right) return;
  const values = bars.flatMap((bar) => [bar.macd_dif, bar.macd_dea, bar.macd_hist]).map(Number).filter(Number.isFinite);
  const bound = Math.max(0.01, ...values.map(Math.abs));
  const x = (index) => left + (index + .5) * (width - left - right) / bars.length;
  const y = (value) => top + (bound - value) / (2 * bound) * (height - top - bottom);
  const zero = y(0);
  ctx.strokeStyle = "#39434e"; ctx.beginPath(); ctx.moveTo(left, zero); ctx.lineTo(width - right, zero); ctx.stroke();
  const step = (width - left - right) / bars.length;
  bars.forEach((bar, index) => {
    const value = Number(bar.macd_hist);
    if (!Number.isFinite(value)) return;
    ctx.fillStyle = value >= 0 ? "rgba(255,91,104,.55)" : "rgba(34,201,151,.55)";
    const topY = Math.min(zero, y(value));
    ctx.fillRect(x(index) - Math.max(1, step * .3), topY, Math.max(1, step * .6), Math.max(1, Math.abs(y(value) - zero)));
  });
  const line = (key, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    bars.forEach((bar, index) => index ? ctx.lineTo(x(index), y(Number(bar[key]))) : ctx.moveTo(x(index), y(Number(bar[key]))));
    ctx.stroke();
  };
  line("macd_dif", "#f3b64b");
  line("macd_dea", "#6ba6ff");
}

function drawChart() {
  const canvas = $("#chartCanvas");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width, height = rect.height;
  ctx.clearRect(0, 0, width, height);
  const bars = visibleBars();
  app.chart.bars = bars;
  if (!bars.length) {
    ctx.fillStyle = "#78838f"; ctx.font = "13px Segoe UI"; ctx.fillText("暂无可绘制的历史数据", 20, 36);
    return;
  }

  const p = app.chart;
  const plotW = width - p.left - p.right;
  const priceBottom = height - p.bottom - p.volumeHeight;
  const lows = bars.map((x) => Number(x.low));
  const highs = bars.map((x) => Number(x.high));
  let min = Math.min(...lows), max = Math.max(...highs);
  const pad = (max - min || 1) * .06;
  min -= pad; max += pad;
  const x = (i) => p.left + (i + .5) * plotW / bars.length;
  const y = (v) => p.top + (max - v) / (max - min) * (priceBottom - p.top);

  ctx.strokeStyle = "#222a32"; ctx.lineWidth = 1; ctx.fillStyle = "#78838f"; ctx.font = "10px Consolas";
  for (let i = 0; i <= 4; i++) {
    const gy = p.top + i * (priceBottom - p.top) / 4;
    ctx.beginPath(); ctx.moveTo(p.left, gy); ctx.lineTo(width - p.right, gy); ctx.stroke();
    ctx.fillText((max - i * (max - min) / 4).toFixed(2), width - p.right + 8, gy + 3);
  }

  const step = plotW / bars.length;
  const candleW = Math.max(2, Math.min(8, step * .62));
  bars.forEach((bar, i) => {
    const up = Number(bar.close) >= Number(bar.open);
    ctx.strokeStyle = ctx.fillStyle = up ? "#ff5b68" : "#22c997";
    ctx.beginPath(); ctx.moveTo(x(i), y(bar.high)); ctx.lineTo(x(i), y(bar.low)); ctx.stroke();
    const top = y(Math.max(bar.open, bar.close));
    const bodyH = Math.max(1, Math.abs(y(bar.open) - y(bar.close)));
    if (up) ctx.strokeRect(x(i) - candleW / 2, top, candleW, bodyH);
    else ctx.fillRect(x(i) - candleW / 2, top, candleW, bodyH);
  });

  const drawLine = (key, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath(); let started = false;
    bars.forEach((bar, i) => {
      if (bar[key] == null) return;
      if (!started) { ctx.moveTo(x(i), y(bar[key])); started = true; } else ctx.lineTo(x(i), y(bar[key]));
    });
    ctx.stroke();
  };
  if ($("#showMa").checked) {
    drawLine("ma5", "#f3b64b"); drawLine("ma10", "#6ba6ff"); drawLine("ma20", "#b68cff");
  }

  if ($("#showMarkers").checked) {
    const recordDays = new Set((app.state?.records || []).filter((item) => item.symbol === app.selected).map((item) => String(item.triggered_at || "").slice(0, 10)));
    bars.forEach((bar, index) => {
      if (!recordDays.has(bar.date)) return;
      ctx.fillStyle = "#f3b64b";
      ctx.beginPath(); ctx.arc(x(index), Math.max(p.top + 5, y(bar.high) - 7), 4, 0, Math.PI * 2); ctx.fill();
    });
  }

  const volumeTop = priceBottom + 24;
  const volumeBottom = height - 26;
  const maxVolume = Math.max(...bars.map((bar) => Number(bar.volume))) || 1;
  bars.forEach((bar, i) => {
    const barH = Number(bar.volume) / maxVolume * (volumeBottom - volumeTop);
    ctx.fillStyle = Number(bar.close) >= Number(bar.open) ? "rgba(255,91,104,.55)" : "rgba(34,201,151,.55)";
    ctx.fillRect(x(i) - candleW / 2, volumeBottom - barH, candleW, barH);
  });
  ctx.fillStyle = "#78838f";
  [0, Math.floor(bars.length / 2), bars.length - 1].forEach((i) => ctx.fillText(bars[i].date.slice(5), x(i) - 15, height - 7));
  p.metrics = { width, height, plotW, priceBottom, min, max, x, y, step };
}

function showChartTooltip(event) {
  const metrics = app.chart.metrics;
  if (!metrics || !app.chart.bars.length) return;
  const rect = event.currentTarget.getBoundingClientRect();
  const mouseX = event.clientX - rect.left;
  const index = Math.max(0, Math.min(app.chart.bars.length - 1, Math.floor((mouseX - app.chart.left) / metrics.step)));
  const bar = app.chart.bars[index];
  drawChart();
  const canvas = event.currentTarget;
  const ctx = canvas.getContext("2d");
  ctx.save(); ctx.strokeStyle = "rgba(220,226,234,.38)"; ctx.setLineDash([3, 3]);
  const lineX = metrics.x(index); const lineY = Math.max(app.chart.top, Math.min(metrics.priceBottom, event.clientY - rect.top));
  ctx.beginPath(); ctx.moveTo(lineX, app.chart.top); ctx.lineTo(lineX, metrics.priceBottom); ctx.moveTo(app.chart.left, lineY); ctx.lineTo(rect.width - app.chart.right, lineY); ctx.stroke(); ctx.restore();
  const tip = $("#chartTooltip");
  tip.innerHTML = `${bar.date}<br>开 ${fmt(bar.open)}　高 ${fmt(bar.high)}<br>低 ${fmt(bar.low)}　收 ${fmt(bar.close)}<br>量 ${compact(bar.volume)}　RSI ${fmt(bar.rsi14)}`;
  tip.style.left = `${Math.min(rect.width - 150, Math.max(5, mouseX + 12))}px`;
  tip.style.top = `${Math.max(5, event.clientY - rect.top - 25)}px`;
  tip.classList.remove("hidden");
}

function renderAlertFormSymbols() {
  const select = $("#alertSymbol");
  const current = select.value;
  select.innerHTML = (app.state?.watchlist || []).map((item) => `<option value="${item.symbol}">${escapeHtml(item.name)} · ${item.symbol}</option>`).join("");
  select.value = current || app.selected || select.options[0]?.value || "";
}

const ruleLabels = {
  price_above: "价格高于", price_below: "价格低于", ma_cross: "均线穿越", breakout: "区间突破", volume_surge: "成交量放大",
  rsi_threshold: "RSI 区域", macd_cross: "MACD 金叉/死叉", breakout_volume: "突破并放量",
};

function renderAlertParams() {
  const type = $("#alertType").value;
  const container = $("#alertParams");
  if (type.startsWith("price_")) container.innerHTML = '<div class="param-row single"><label>价格阈值（元）<input name="threshold" type="number" min="0.01" step="0.01" required placeholder="例如 1500"></label></div>';
  if (type === "ma_cross") container.innerHTML = '<div class="param-row"><label>短期均线<input name="short" type="number" value="5" min="2" max="120"></label><label>长期均线<input name="long" type="number" value="20" min="3" max="250"></label></div><div class="param-row single"><label>方向<select name="direction"><option value="above">上穿</option><option value="below">下穿</option></select></label></div>';
  if (type === "breakout") container.innerHTML = '<div class="param-row"><label>观察天数<input name="lookback" type="number" value="20" min="2" max="250"></label><label>方向<select name="direction"><option value="high">突破高点</option><option value="low">跌破低点</option></select></label></div>';
  if (type === "volume_surge") container.innerHTML = '<div class="param-row"><label>平均窗口（日）<input name="window" type="number" value="5" min="2" max="60"></label><label>触发倍数<input name="multiple" type="number" value="2" min="1" max="20" step="0.1"></label></div>';
  if (type === "rsi_threshold") container.innerHTML = '<div class="param-row"><label>RSI 周期<input name="period" type="number" value="14" min="2" max="60"></label><label>区域<select name="direction"><option value="above">高于阈值 / 进入超买区</option><option value="below">低于阈值 / 进入超卖区</option></select></label></div><div class="param-row single"><label>阈值（常用 70 / 30）<input name="threshold" type="number" value="70" min="1" max="99" step="0.1"></label></div>';
  if (type === "macd_cross") container.innerHTML = '<div class="param-row"><label>短期 EMA<input name="short" type="number" value="12" min="2" max="119"></label><label>长期 EMA<input name="long" type="number" value="26" min="3" max="120"></label></div><div class="param-row"><label>信号期<input name="signal" type="number" value="9" min="2" max="60"></label><label>方向<select name="direction"><option value="above">金叉（DIF 上穿 DEA）</option><option value="below">死叉（DIF 下穿 DEA）</option></select></label></div>';
  if (type === "breakout_volume") container.innerHTML = '<div class="param-row"><label>突破观察（日）<input name="lookback" type="number" value="20" min="2" max="250"></label><label>方向<select name="direction"><option value="high">突破高点</option><option value="low">跌破低点</option></select></label></div><div class="param-row"><label>均量窗口（日）<input name="window" type="number" value="5" min="2" max="60"></label><label>放量倍数<input name="multiple" type="number" value="2" min="1" max="20" step="0.1"></label></div>';
  if (type === "rsi_threshold") {
    container.querySelector('[name="direction"]').addEventListener("change", (event) => {
      container.querySelector('[name="threshold"]').value = event.target.value === "below" ? "30" : "70";
    });
  }
}

function ruleDescription(rule) {
  const p = rule.params;
  if (rule.type === "price_above") return `最新价 ≥ ${fmt(p.threshold)} 元`;
  if (rule.type === "price_below") return `最新价 ≤ ${fmt(p.threshold)} 元`;
  if (rule.type === "ma_cross") return `MA${p.short} ${p.direction === "above" ? "上穿" : "下穿"} MA${p.long}`;
  if (rule.type === "breakout") return `${p.direction === "high" ? "收盘突破" : "收盘跌破"}前 ${p.lookback} 日区间`;
  if (rule.type === "volume_surge") return `成交量 ≥ 前 ${p.window} 日均量 ${p.multiple} 倍`;
  if (rule.type === "rsi_threshold") return `RSI(${p.period}) ${p.direction === "above" ? "≥" : "≤"} ${fmt(p.threshold)}`;
  if (rule.type === "macd_cross") return `MACD(${p.short},${p.long},${p.signal}) ${p.direction === "above" ? "金叉" : "死叉"}`;
  if (rule.type === "breakout_volume") return `${p.direction === "high" ? "突破" : "跌破"}前 ${p.lookback} 日，同时成交量 ≥ 前 ${p.window} 日均量 ${p.multiple} 倍`;
  return rule.type;
}

function renderAlerts() {
  const allAlerts = app.state?.alerts || [];
  syncAlertFilters(allAlerts);
  const symbol = $("#alertStockFilter").value;
  const type = $("#alertTypeFilter").value;
  const alerts = allAlerts.filter((rule) => (!symbol || rule.symbol === symbol) && (!type || rule.type === type));
  $("#alertFilterSummary").textContent = `${alerts.length}/${allAlerts.length}`;
  $("#alertList").innerHTML = alerts.length ? alerts.map((rule) => {
    const stock = app.state.watchlist.find((item) => item.symbol === rule.symbol);
    const test = app.ruleTests.get(rule.id);
    return `<article class="alert-card ${rule.enabled ? "" : "off"}">
      <div class="alert-top"><div><div class="alert-title">${escapeHtml(stock?.name || rule.symbol)} · ${ruleLabels[rule.type]}</div><div class="alert-desc">${ruleDescription(rule)}</div></div>
      <div class="alert-actions"><button data-test-alert="${rule.id}">测试</button><button data-backtest-alert="${rule.id}">回放</button><button data-edit-alert="${rule.id}">编辑</button><button data-clone-alert="${rule.id}">复制</button><button data-toggle-alert="${rule.id}" data-enabled="${!rule.enabled}">${rule.enabled ? "暂停" : "启用"}</button><button data-delete-alert="${rule.id}">删除</button></div></div>
      <div class="alert-status">${ruleStatusText(rule)}</div>
      <div class="rule-stats">累计检查 ${rule.stats?.checks || 0} · 条件成立 ${rule.stats?.matched || 0} · 实际触发 ${rule.stats?.triggered || 0} · 跳过/拦截 ${(rule.stats?.skipped || 0) + (rule.stats?.blocked || 0)} · 错误 ${rule.stats?.errors || 0}</div>
      ${test ? `<div class="rule-test-result ${test.matched ? "matched" : ""}">${test.matched ? "试算命中" : "试算未命中"} · ${escapeHtml(test.message)}<br>使用 ${escapeHtml(sourceLabels[test.source] || test.source || "未知来源")} · ${localTime(test.checked_at)} · 不写记录、不通知</div>` : ""}
    </article>`;
  }).join("") : `<div class="empty">${allAlerts.length ? "没有符合筛选条件的规则" : "尚未设置预警规则"}</div>`;
  $$('[data-toggle-alert]').forEach((button) => button.addEventListener("click", () => updateAlert(button.dataset.toggleAlert, { enabled: button.dataset.enabled === "true" })));
  $$('[data-delete-alert]').forEach((button) => button.addEventListener("click", () => deleteAlert(button.dataset.deleteAlert)));
  $$('[data-test-alert]').forEach((button) => button.addEventListener("click", () => testAlert(button.dataset.testAlert, button)));
  $$('[data-backtest-alert]').forEach((button) => button.addEventListener("click", () => backtestAlert(button.dataset.backtestAlert, button)));
  $$('[data-edit-alert]').forEach((button) => button.addEventListener("click", () => startAlertEdit(button.dataset.editAlert)));
  $$('[data-clone-alert]').forEach((button) => button.addEventListener("click", () => cloneAlert(button.dataset.cloneAlert)));
}

function syncAlertFilters(alerts) {
  const stockSelect = $("#alertStockFilter");
  const typeSelect = $("#alertTypeFilter");
  const stockValue = stockSelect.value;
  const typeValue = typeSelect.value;
  const symbols = [...new Set(alerts.map((rule) => rule.symbol))];
  stockSelect.innerHTML = '<option value="">全部股票</option>' + symbols.map((symbol) => {
    const stock = app.state.watchlist.find((item) => item.symbol === symbol);
    return `<option value="${symbol}">${escapeHtml(stock?.name || symbol)}</option>`;
  }).join("");
  const types = [...new Set(alerts.map((rule) => rule.type))];
  typeSelect.innerHTML = '<option value="">全部规则</option>' + types.map((item) => `<option value="${item}">${escapeHtml(ruleLabels[item] || item)}</option>`).join("");
  if (symbols.includes(stockValue)) stockSelect.value = stockValue;
  if (types.includes(typeValue)) typeSelect.value = typeValue;
}

function ruleStatusText(rule) {
  const scope = rule.trading_hours_only !== false ? "仅交易时段" : "全时段";
  if (!rule.enabled) return `${scope} · 已暂停`;
  let cooldown = "";
  if (rule.last_triggered_at) {
    const readyAt = new Date(new Date(rule.last_triggered_at).getTime() + Number(rule.cooldown_minutes || 60) * 60_000);
    if (readyAt > new Date()) cooldown = ` · 冷却至 ${readyAt.toLocaleString("zh-CN", { hour12: false })}`;
  }
  return `${scope}${cooldown} · ${rule.last_checked_at ? `上次检查 ${localTime(rule.last_checked_at)} · ${escapeHtml(rule.last_message || "")}` : "等待首次检查"}`;
}

function startAlertEdit(id) {
  const rule = app.state.alerts.find((item) => item.id === id);
  if (!rule) return;
  app.editingAlertId = id;
  $("#alertSymbol").value = rule.symbol;
  $("#alertType").value = rule.type;
  renderAlertParams();
  for (const [key, value] of Object.entries(rule.params || {})) {
    const field = $("#alertParams").querySelector(`[name="${key}"]`);
    if (field) field.value = String(value);
  }
  $("#oncePerDay").checked = rule.once_per_day !== false;
  $("#tradingHoursOnly").checked = rule.trading_hours_only !== false;
  $("#cooldown").value = String(rule.cooldown_minutes || 60);
  $("#alertSubmit").textContent = "保存修改";
  $("#cancelAlertEdit").classList.remove("hidden");
  $("#alertForm").scrollIntoView({ behavior: "smooth", block: "start" });
}

function cancelAlertEdit() {
  app.editingAlertId = null;
  $("#alertSubmit").textContent = "添加预警";
  $("#cancelAlertEdit").classList.add("hidden");
  $("#alertForm").reset();
  $("#oncePerDay").checked = true;
  $("#tradingHoursOnly").checked = true;
  $("#cooldown").value = "60";
  renderAlertParams();
}

async function cloneAlert(id) {
  try {
    await api(`/api/alerts/${encodeURIComponent(id)}/clone`, { method: "POST", body: "{}" });
    await loadState();
    toast("已复制为暂停状态，编辑后再启用");
  } catch (error) { showError(error.message); }
}

function applyRuleTemplate(name) {
  const templates = {
    rsi_oversold: { type: "rsi_threshold", params: { period: 14, direction: "below", threshold: 30 } },
    macd_golden: { type: "macd_cross", params: { short: 12, long: 26, signal: 9, direction: "above" } },
    breakout_volume: { type: "breakout_volume", params: { lookback: 20, direction: "high", window: 5, multiple: 2 } },
  };
  const template = templates[name];
  if (!template) return;
  $("#alertType").value = template.type;
  renderAlertParams();
  for (const [key, value] of Object.entries(template.params)) {
    const field = $("#alertParams").querySelector(`[name="${key}"]`);
    if (field) field.value = String(value);
  }
}

function syncRecordFilters(records) {
  const stockSelect = $("#recordStockFilter");
  const typeSelect = $("#recordRuleFilter");
  const stockValue = stockSelect.value;
  const typeValue = typeSelect.value;
  const stocks = new Map();
  for (const item of records) stocks.set(item.symbol, item.name || item.symbol);
  stockSelect.innerHTML = '<option value="">全部股票</option>' + [...stocks.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([symbol, name]) => `<option value="${symbol}">${escapeHtml(name)} · ${symbol}</option>`).join("");
  const types = [...new Set(records.map((item) => item.type))];
  typeSelect.innerHTML = '<option value="">全部规则</option>' + types
    .map((type) => `<option value="${type}">${escapeHtml(ruleLabels[type] || type)}</option>`).join("");
  if ([...stockSelect.options].some((option) => option.value === stockValue)) stockSelect.value = stockValue;
  if ([...typeSelect.options].some((option) => option.value === typeValue)) typeSelect.value = typeValue;
}

function renderRecords() {
  const records = app.state?.records || [];
  syncRecordFilters(records);
  const stock = $("#recordStockFilter").value;
  const type = $("#recordRuleFilter").value;
  const date = $("#recordDateFilter").value;
  const filtered = records.filter((item) => (!stock || item.symbol === stock) && (!type || item.type === type) && (!date || String(item.triggered_at || "").slice(0, 10) === date));
  $("#recordCount").textContent = records.length ? String(records.length) : "";
  $("#recordFilterSummary").textContent = `显示 ${filtered.length} / ${records.length} 条`;
  $("#exportRecords").disabled = filtered.length === 0;
  $("#recordList").innerHTML = filtered.length ? filtered.map((item) => `<article class="record-card">
    <div class="alert-title">${escapeHtml(item.name)} · ${ruleLabels[item.type] || item.type}</div>
    <div class="record-message">${escapeHtml(item.message)}</div>
    <div class="record-meta">触发 ${localTime(item.triggered_at)}<br>数据 ${localTime(item.data_timestamp)} · ${item.source === "hithink" ? "同花顺官方" : "公开行情"}</div>
    ${item.notification_suppressed ? `<span class="record-silent">通知已静默 · ${escapeHtml(item.notification_reason || "安静时段")}</span>` : ""}
  </article>`).join("") : `<div class="empty">${records.length ? "没有符合筛选条件的记录" : "暂无触发记录"}</div>`;
}

async function addStock(event) {
  event.preventDefault();
  const query = $("#stockCode").value.trim();
  try {
    let symbol = app.searchSelection;
    if (!symbol && /^\d{6}(?:\.(?:SH|SZ|BJ))?$/i.test(query)) symbol = query;
    if (!symbol) {
      const searchBody = await api(`/api/search?q=${encodeURIComponent(query)}`);
      symbol = searchBody.items?.[0]?.symbol;
    }
    if (!symbol) throw new Error("没有找到匹配的沪深北 A 股，请换一个名称片段或输入 6 位代码");
    const body = await api("/api/watchlist", { method: "POST", body: JSON.stringify({ symbol }) });
    $("#stockCode").value = "";
    app.searchSelection = null;
    app.searchResults = [];
    hideStockSuggestions();
    app.selected = body.item.symbol;
    await loadState();
    await refreshAll({ notify: false, forceHistory: true });
    toast(`已添加 ${body.item.name}`);
  } catch (error) { showError(error.message); }
}

async function removeStock(symbol) {
  try {
    await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
    if (app.selected === symbol) app.selected = null;
    app.quotes.delete(symbol);
    await loadState();
    await refreshAll({ notify: false, forceHistory: true });
  } catch (error) { showError(error.message); }
}

async function addAlert(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const params = Object.fromEntries([...form.entries()].filter(([key]) => ["threshold", "short", "long", "signal", "period", "direction", "lookback", "window", "multiple"].includes(key)));
  const payload = {
      symbol: $("#alertSymbol").value, type: $("#alertType").value, params,
      once_per_day: $("#oncePerDay").checked, cooldown_minutes: $("#cooldown").value,
      trading_hours_only: $("#tradingHoursOnly").checked,
  };
  try {
    const editing = app.editingAlertId;
    const path = editing ? `/api/alerts/${encodeURIComponent(editing)}` : "/api/alerts";
    await api(path, { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
    cancelAlertEdit();
    await loadState();
    toast(editing ? "预警规则已更新" : "预警规则已添加");
  } catch (error) { showError(error.message); }
}

async function updateAlert(id, changes) {
  try { await api(`/api/alerts/${id}`, { method: "PATCH", body: JSON.stringify(changes) }); await loadState(); }
  catch (error) { showError(error.message); }
}

async function deleteAlert(id) {
  try {
    await api(`/api/alerts/${id}`, { method: "DELETE" });
    if (app.editingAlertId === id) cancelAlertEdit();
    await loadState();
  }
  catch (error) { showError(error.message); }
}

async function testSourceRoute() {
  const button = $("#sourceTestButton");
  const result = $("#sourceTestResult");
  button.disabled = true;
  result.textContent = "正在请求报价和历史数据…";
  try {
    const body = await api("/api/source-test", { method: "POST", body: JSON.stringify({ source: $("#sourceSelect").value, symbol: app.selected }) });
    result.textContent = `${sourceLabels[body.provider] || body.provider} · ${body.latency_ms} ms · 报价 ${body.quote_count} / K线 ${body.bar_count} · ${sourceText(body.sources)}`;
    toast("数据路由诊断通过");
    await loadHealth();
  } catch (error) {
    result.textContent = `失败：${error.message}`;
    showError(`数据路由诊断失败：${error.message}`);
  } finally { button.disabled = false; }
}

async function loadBackups() {
  const panel = $("#backupList");
  panel.innerHTML = '<div class="empty">正在读取备份…</div>';
  try {
    const body = await api("/api/backups");
    panel.innerHTML = body.items.length ? body.items.map((item) => `<div class="backup-item">
      <div><strong>${localTime(item.created_at)}</strong><span>${item.invalid ? "文件已损坏" : `自选 ${item.watchlist_count} · 规则 ${item.alert_count} · 记录 ${item.record_count}`} · ${compact(item.size)} B</span></div>
      <button type="button" data-restore-backup="${escapeHtml(item.name)}" ${item.invalid ? "disabled" : ""}>恢复</button>
    </div>`).join("") : '<div class="empty">暂无备份；每次修改数据前会自动保留旧版本</div>';
    $$('[data-restore-backup]').forEach((button) => button.addEventListener("click", () => restoreBackup(button.dataset.restoreBackup)));
  } catch (error) { panel.innerHTML = `<div class="empty">读取失败：${escapeHtml(error.message)}</div>`; }
}

async function createBackup() {
  try {
    await api("/api/backups", { method: "POST", body: "{}" });
    await loadBackups();
    toast("当前数据已备份");
  } catch (error) { showError(error.message); }
}

async function restoreBackup(name) {
  if (!window.confirm("恢复会用该备份替换当前自选股、规则和记录。系统会先自动备份当前数据，是否继续？")) return;
  try {
    await api(`/api/backups/${encodeURIComponent(name)}/restore`, { method: "POST", body: "{}" });
    app.selected = null;
    app.recordsInitialized = false;
    await loadState();
    await refreshAll({ notify: false, forceHistory: true });
    await loadBackups();
    toast("备份已恢复");
  } catch (error) { showError(error.message); }
}

async function importStateFile(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  if (!window.confirm("导入会替换当前自选股、规则和记录。系统会先自动备份当前数据，是否继续？")) return;
  try {
    const payload = JSON.parse(await file.text());
    await api("/api/import", { method: "POST", body: JSON.stringify(payload) });
    app.selected = null;
    app.recordsInitialized = false;
    await loadState();
    await refreshAll({ notify: false, forceHistory: true });
    $("#dataManagerDialog").close();
    toast("数据导入成功");
  } catch (error) { showError(`导入失败：${error.message}`); }
}

function redrawCharts() {
  drawChart();
  drawIndicatorCharts();
}

function resetChartView() {
  app.chart.visibleCount = Math.min(Number($("#chartRange").value) || 90, Math.max(1, app.bars.length));
  app.chart.offset = 0;
  redrawCharts();
}

function zoomChart(event) {
  event.preventDefault();
  if (!app.bars.length) return;
  const direction = event.deltaY > 0 ? 10 : -10;
  app.chart.visibleCount = Math.max(30, Math.min(app.bars.length, app.chart.visibleCount + direction));
  app.chart.offset = Math.min(app.chart.offset, Math.max(0, app.bars.length - app.chart.visibleCount));
  redrawCharts();
}

function startChartDrag(event) {
  app.chart.dragging = true;
  app.chart.dragX = event.clientX;
  event.currentTarget.classList.add("dragging");
}

function moveChartDrag(event) {
  if (!app.chart.dragging) return showChartTooltip(event);
  const step = Math.max(3, app.chart.metrics?.step || 6);
  const delta = Math.trunc((event.clientX - app.chart.dragX) / step);
  if (!delta) return;
  app.chart.dragX = event.clientX;
  app.chart.offset = Math.max(0, Math.min(app.bars.length - app.chart.visibleCount, app.chart.offset + delta));
  $("#chartTooltip").classList.add("hidden");
  redrawCharts();
}

function stopChartDrag(event) {
  app.chart.dragging = false;
  event?.currentTarget?.classList.remove("dragging");
}

async function testAlert(id, button) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = "测试中…";
  try {
    const result = await api(`/api/alerts/${encodeURIComponent(id)}/test`, { method: "POST", body: "{}" });
    app.ruleTests.set(id, result);
    renderAlerts();
    toast(result.matched ? "当前真实数据满足规则，试算命中" : "当前真实数据未满足规则，试算完成");
  } catch (error) {
    showError(`规则测试失败：${error.message}`);
    button.disabled = false;
    button.textContent = previous;
  }
}

async function backtestAlert(id, button) {
  const rule = app.state.alerts.find((item) => item.id === id);
  $("#backtestTitle").textContent = rule ? `${rule.symbol} · ${ruleLabels[rule.type]} · 最多 750 根已完成日 K` : "";
  $("#backtestContent").innerHTML = '<div class="empty">正在逐日回放，请稍候…</div>';
  $("#backtestDialog").showModal();
  button.disabled = true;
  try {
    const result = await api(`/api/alerts/${encodeURIComponent(id)}/backtest`, { method: "POST", body: JSON.stringify({ limit: 750 }) });
    const horizons = [1, 5, 20];
    const stats = horizons.map((days) => {
      const value = result.stats?.[String(days)] || {};
      return `<div class="backtest-stat"><span>${days} 日后</span><strong>${value.average_pct == null ? "—" : `${value.average_pct >= 0 ? "+" : ""}${fmt(value.average_pct)}%`}</strong><small>样本 ${value.samples || 0} · 中位 ${value.median_pct == null ? "—" : `${fmt(value.median_pct)}%`} · 上涨占比 ${value.positive_pct == null ? "—" : `${fmt(value.positive_pct, 1)}%`}</small></div>`;
    }).join("");
    const events = (result.events || []).map((event) => `<tr><td>${escapeHtml(event.date)}</td><td>${fmt(event.close)}</td><td>${event.forward_returns?.["1"] == null ? "—" : `${fmt(event.forward_returns["1"])}%`}</td><td>${event.forward_returns?.["5"] == null ? "—" : `${fmt(event.forward_returns["5"])}%`}</td><td>${event.forward_returns?.["20"] == null ? "—" : `${fmt(event.forward_returns["20"])}%`}</td></tr>`).join("");
    $("#backtestContent").innerHTML = `<div class="backtest-summary"><div><span>数据区间</span><strong>${escapeHtml(result.period?.start || "—")} 至 ${escapeHtml(result.period?.end || "—")}</strong></div><div><span>日 K / 事件</span><strong>${result.bar_count} / ${result.event_count}</strong></div></div><div class="backtest-stats">${stats}</div><div class="table-scroll"><table><thead><tr><th>触发日</th><th>收盘</th><th>1日后</th><th>5日后</th><th>20日后</th></tr></thead><tbody>${events || '<tr><td colspan="5">该区间没有触发事件</td></tr>'}</tbody></table></div><p class="health-note">${escapeHtml(result.basis)} ${escapeHtml(result.caveat)}</p>`;
  } catch (error) {
    $("#backtestContent").innerHTML = `<div class="empty">回放失败：${escapeHtml(error.message)}</div>`;
  } finally { button.disabled = false; }
}

const auditLabels = {
  triggered: "已触发", triggered_silent: "静默触发", not_met: "未命中", condition_met: "条件持续成立",
  skipped_session: "交易时段跳过", blocked_daily: "每日一次拦截", blocked_cooldown: "冷却拦截", source_error: "数据源失败",
};

async function loadAudit() {
  const query = new URLSearchParams({ limit: "100" });
  if ($("#auditStockFilter").value) query.set("symbol", $("#auditStockFilter").value);
  if ($("#auditStatusFilter").value) query.set("status", $("#auditStatusFilter").value);
  const body = await api(`/api/audit?${query}`);
  app.audit = body;
  renderAudit();
}

function renderAudit() {
  const rules = app.state?.alerts || [];
  const stockSelect = $("#auditStockFilter");
  const current = stockSelect.value;
  stockSelect.innerHTML = '<option value="">全部股票</option>' + [...new Set(rules.map((rule) => rule.symbol))].map((symbol) => `<option value="${symbol}">${escapeHtml(app.state.watchlist.find((item) => item.symbol === symbol)?.name || symbol)}</option>`).join("");
  if ([...stockSelect.options].some((item) => item.value === current)) stockSelect.value = current;
  $("#auditSummary").textContent = `显示 ${app.audit.items?.length || 0} 条，保存 ${app.audit.total || 0} 条明细；清空明细不会重置累计计数`;
  $("#auditStats").innerHTML = (app.audit.summaries || []).map((item) => {
    const stats = item.stats || {};
    const label = ruleLabels[item.type] || item.type;
    return `<article class="audit-stat"><strong>${escapeHtml(item.symbol)} · ${escapeHtml(label)}</strong><span>检查 ${stats.checks || 0}　成立 ${stats.matched || 0}　触发 ${stats.triggered || 0}</span><small>跳过 ${stats.skipped || 0} · 拦截 ${stats.blocked || 0} · 错误 ${stats.errors || 0}</small></article>`;
  }).join("") || '<div class="empty">尚无规则统计</div>';
  $("#auditList").innerHTML = (app.audit.items || []).map((item) => `<article class="record-card"><div class="alert-title">${escapeHtml(item.symbol)} · ${escapeHtml(auditLabels[item.status] || item.status)}</div><div class="record-message">${escapeHtml(item.message)}</div><div class="record-meta">${localTime(item.checked_at)} · ${escapeHtml(sourceLabels[item.source] || item.source || "未知来源")}</div></article>`).join("") || '<div class="empty">暂无符合筛选条件的审计明细</div>';
}

function syncQuietHoursForm() {
  const enabled = $("#quietHoursEnabled").checked;
  $("#quietStart").disabled = !enabled;
  $("#quietEnd").disabled = !enabled;
  $("#quietHoursRow").classList.toggle("disabled", !enabled);
}

async function saveSettings(changes) {
  try { await api("/api/settings", { method: "PATCH", body: JSON.stringify(changes) }); await loadState(); await refreshAll({ notify: false, forceHistory: true }); }
  catch (error) { showError(error.message); }
}

function configureRefresh() {
  clearInterval(app.refreshTimer);
  if (app.state?.settings.auto_refresh) {
    app.refreshTimer = setInterval(() => refreshAll(), app.state.settings.refresh_seconds * 1000);
  }
}

function sendNotification(record, notify) {
  if (record.notification_suppressed) return;
  toast(`${record.name}：${record.message}`);
  if (notify && !app.nativeNotifications && "Notification" in window && Notification.permission === "granted") {
    new Notification(`自主看盘台 · ${record.name}`, { body: record.message, tag: record.rule_id });
  }
}

function exportRecords() {
  const records = app.state?.records || [];
  const stock = $("#recordStockFilter").value;
  const type = $("#recordRuleFilter").value;
  const date = $("#recordDateFilter").value;
  const filtered = records.filter((item) => (!stock || item.symbol === stock) && (!type || item.type === type) && (!date || String(item.triggered_at || "").slice(0, 10) === date));
  if (!filtered.length) return toast("当前筛选没有可导出的记录");
  const query = new URLSearchParams();
  if (stock) query.set("symbol", stock);
  if (type) query.set("type", type);
  if (date) query.set("date", date);
  const link = document.createElement("a");
  link.href = `/api/records.csv?${query}`;
  link.download = `自主看盘台-触发记录-${date || new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  toast(`已导出 ${filtered.length} 条记录`);
}

async function requestNotifications() {
  if (app.nativeNotifications) return toast("Windows 原生通知已由托盘程序接管");
  if (!("Notification" in window)) return toast("当前浏览器不支持桌面通知");
  const permission = await Notification.requestPermission();
  syncNotificationButton();
}

function syncNotificationButton() {
  const button = $("#notificationButton");
  if (app.nativeNotifications) {
    button.textContent = "Windows 通知已开启";
    button.disabled = true;
    button.title = "关闭浏览器后仍可由系统托盘发送原生通知";
    return;
  }
  if (!("Notification" in window)) {
    button.textContent = "浏览器不支持通知";
    button.disabled = true;
    button.title = "请改用支持桌面通知的普通 Chrome 或 Edge 浏览器";
    return;
  }
  if (Notification.permission === "denied") {
    button.textContent = "通知权限已被拒绝";
    button.disabled = true;
    button.title = "请在浏览器的网站权限中重新允许 127.0.0.1 的通知";
    return;
  }
  button.disabled = false;
  button.textContent = Notification.permission === "granted" ? "桌面通知已开启" : "开启桌面通知";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function bindEvents() {
  $("#addStockForm").addEventListener("submit", addStock);
  $("#stockCode").addEventListener("input", scheduleStockSearch);
  $("#stockCode").addEventListener("keydown", handleStockSearchKeydown);
  $("#stockCode").addEventListener("focus", () => { if (app.searchResults.length) renderStockSuggestions(); });
  document.addEventListener("click", (event) => { if (!event.target.closest("#addStockForm")) hideStockSuggestions(); });
  $("#watchGroupFilter").addEventListener("change", renderWatchlist);
  $("#watchSort").addEventListener("change", renderWatchlist);
  $("#bulkImportButton").addEventListener("click", () => $("#bulkImportDialog").showModal());
  $("#bulkImportForm").addEventListener("submit", importWatchlistBulk);
  $("#watchEditForm").addEventListener("submit", saveWatchEdit);
  $("#alertForm").addEventListener("submit", addAlert);
  $("#alertType").addEventListener("change", renderAlertParams);
  $("#cancelAlertEdit").addEventListener("click", cancelAlertEdit);
  $$('[data-rule-template]').forEach((button) => button.addEventListener("click", () => applyRuleTemplate(button.dataset.ruleTemplate)));
  $("#alertStockFilter").addEventListener("change", renderAlerts);
  $("#alertTypeFilter").addEventListener("change", renderAlerts);
  $("#refreshButton").addEventListener("click", () => refreshAll({ forceHistory: true }));
  $("#notificationButton").addEventListener("click", requestNotifications);
  $("#sourceTestButton").addEventListener("click", testSourceRoute);
  $("#sourceSelect").addEventListener("change", (event) => saveSettings({ source: event.target.value }));
  $("#intervalSelect").addEventListener("change", (event) => saveSettings({ refresh_seconds: Number(event.target.value) }));
  $("#autoRefresh").addEventListener("change", (event) => saveSettings({ auto_refresh: event.target.checked }));
  $("#quietHoursEnabled").addEventListener("change", (event) => { syncQuietHoursForm(); saveSettings({ quiet_hours_enabled: event.target.checked }); });
  $("#quietStart").addEventListener("change", (event) => saveSettings({ quiet_start: event.target.value }));
  $("#quietEnd").addEventListener("change", (event) => saveSettings({ quiet_end: event.target.value }));
  $("#clearRecords").addEventListener("click", async () => { await api("/api/records", { method: "DELETE" }); await loadState(); });
  $("#recordStockFilter").addEventListener("change", renderRecords);
  $("#recordRuleFilter").addEventListener("change", renderRecords);
  $("#recordDateFilter").addEventListener("change", renderRecords);
  $("#exportRecords").addEventListener("click", exportRecords);
  $("#refreshAudit").addEventListener("click", () => loadAudit().catch((error) => showError(error.message)));
  $("#auditStockFilter").addEventListener("change", () => loadAudit().catch((error) => showError(error.message)));
  $("#auditStatusFilter").addEventListener("change", () => loadAudit().catch((error) => showError(error.message)));
  $("#clearAudit").addEventListener("click", async () => { await api("/api/audit", { method: "DELETE" }); await loadAudit(); });
  $("#dataManagerButton").addEventListener("click", async () => { $("#dataManagerDialog").showModal(); await loadBackups(); });
  $("#createBackupButton").addEventListener("click", createBackup);
  $("#importStateButton").addEventListener("click", () => $("#importStateFile").click());
  $("#importStateFile").addEventListener("change", importStateFile);
  $$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.closeDialog}`).close()));
  $$(".tab").forEach((button) => button.addEventListener("click", () => {
    $$(".tab").forEach((item) => item.classList.toggle("active", item === button));
    $$(".tab-page").forEach((page) => page.classList.toggle("active", page.id === `${button.dataset.tab}Tab`));
    if (button.dataset.tab === "audit") loadAudit().catch((error) => showError(error.message));
  }));
  $("#chartRange").addEventListener("change", resetChartView);
  $("#showMa").addEventListener("change", redrawCharts);
  $("#showMarkers").addEventListener("change", redrawCharts);
  $("#resetChart").addEventListener("click", resetChartView);
  $("#chartCanvas").addEventListener("wheel", zoomChart, { passive: false });
  $("#chartCanvas").addEventListener("mousedown", startChartDrag);
  $("#chartCanvas").addEventListener("mousemove", moveChartDrag);
  $("#chartCanvas").addEventListener("mouseup", stopChartDrag);
  $("#chartCanvas").addEventListener("mouseleave", (event) => { stopChartDrag(event); $("#chartTooltip").classList.add("hidden"); drawChart(); });
  window.addEventListener("resize", () => { drawChart(); drawIndicatorCharts(); });
}

async function boot() {
  bindEvents();
  renderAlertParams();
  syncNotificationButton();
  try { await loadState(); await refreshAll({ notify: false, forceHistory: true }); }
  catch (error) { showError(error.message); }
}

boot();

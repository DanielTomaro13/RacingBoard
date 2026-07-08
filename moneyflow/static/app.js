// RacingBoard client. Works two ways from one codebase:
//   • LIVE   — WebSocket to the FastAPI backend (polls Betfair + TAB)
//   • REPLAY — steps through a captured JSON sequence (for GitHub Pages / offline)
(() => {
  const cfg = window.MF_CONFIG || {};
  const qs = new URLSearchParams(location.search);
  const state = {
    board: [], movers: [], selected: null, details: {},
    codeFilter: "ALL", mode: "connecting",
  };

  // ---------- helpers ----------
  const $ = (id) => document.getElementById(id);
  const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "%");
  const arrow = (d) => (d === "firming" ? "▲" : d === "drifting" ? "▼" : "▪");
  const money = (x) => (x == null ? null : "$" + Math.round(x).toLocaleString());
  const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function ttg(iso) {
    const ms = new Date(iso).getTime() - Date.now();
    if (isNaN(ms)) return "";
    const m = Math.round(ms / 60000);
    if (m <= 0) return "now";
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }
  const dirClass = (d) => (d === "firming" ? "firm" : d === "drifting" ? "drift" : "flatc");

  // ---------- dispatch (shared by both sources) ----------
  function apply(msg) {
    if (msg.type === "board") {
      state.board = msg.board || [];
      state.movers = msg.movers || [];
      renderHero(); renderMovers(); renderBoard();
      // auto-select the biggest mover's race on first data
      if (!state.selected && state.movers.length) select(state.movers[0].race_key);
    } else if (msg.type === "race") {
      state.details[msg.race_key] = msg.detail;
      if (msg.race_key === state.selected) renderDetail();
    }
  }

  // ---------- LIVE source ----------
  function liveConnect() {
    const base = cfg.apiBase || (location.protocol === "https:" ? "wss" : "ws") + "://" + location.host;
    let ws, opened = false;
    const url = base.replace(/^http/, "ws") + "/ws";
    try { ws = new WebSocket(url); } catch { return startReplay(); }
    const failTimer = setTimeout(() => { if (!opened) { try { ws.close(); } catch {} startReplay(); } }, 3500);
    ws.onopen = () => { opened = true; clearTimeout(failTimer); setMode("live"); };
    ws.onmessage = (e) => apply(JSON.parse(e.data));
    ws.onclose = () => { if (!opened) { startReplay(); } else { setMode("down"); setTimeout(liveConnect, 2500); } };
    ws.onerror = () => {};
    window.__subscribe = (k) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "subscribe", race_key: k })); };
  }

  // ---------- REPLAY source ----------
  async function startReplay() {
    if (state.mode === "replay") return;
    setMode("replay");
    let frames = [];
    try {
      const url = qs.get("replay") || cfg.replayUrl || "data/replay.json";
      frames = await (await fetch(url)).json();
    } catch { setMode("noreplay"); return; }
    if (!frames.length) { setMode("noreplay"); return; }
    let i = 0;
    window.__subscribe = (k) => {
      const fr = frames[i % frames.length];
      if (fr.races && fr.races[k]) apply({ type: "race", race_key: k, detail: fr.races[k] });
    };
    const tick = () => {
      const fr = frames[i % frames.length];
      apply({ type: "board", board: fr.board, movers: fr.movers });
      if (state.selected && fr.races && fr.races[state.selected])
        apply({ type: "race", race_key: state.selected, detail: fr.races[state.selected] });
      i++;
    };
    tick();
    setInterval(tick, 2600);
  }

  function setMode(m) {
    state.mode = m;
    const dot = $("conn"), lbl = $("conn-label"), banner = $("banner");
    dot.className = "dot";
    if (m === "live") { dot.classList.add("on"); lbl.textContent = "live"; }
    else if (m === "replay") { dot.classList.add("replay"); lbl.textContent = "replay"; banner.classList.add("show"); }
    else if (m === "down") { lbl.textContent = "reconnecting…"; }
    else if (m === "noreplay") { lbl.textContent = "no data"; }
    else lbl.textContent = "connecting…";
  }

  // ---------- HERO ----------
  function renderHero() {
    const b = state.board;
    $("s-races").textContent = b.length || "–";
    const byCode = { R: 0, G: 0, H: 0 };
    b.forEach((r) => (byCode[r.code] = (byCode[r.code] || 0) + 1));
    $("s-codes").innerHTML = Object.entries(byCode).filter(([, n]) => n)
      .map(([c, n]) => `<span class="chip-code"><i style="background:var(--code-${c})"></i>${n}</span>`).join("");

    const top = state.movers[0];
    if (top) {
      $("s-steam").innerHTML = `<span class="${dirClass(top.direction)}">${arrow(top.direction)} ${(top.share_delta * 100 > 0 ? "+" : "")}${(top.share_delta * 100).toFixed(1)}pt</span>`;
      $("s-steam-sub").textContent = `${top.runner} · ${top.venue} R${top.race_no}`;
    }
    const matched = b.reduce((s, r) => s + (r.bf_total_matched || 0), 0);
    $("s-matched").textContent = matched ? money(matched) : "–";

    const next = [...b].filter((r) => r.status === "OPEN").sort((a, z) => new Date(a.start_time) - new Date(z.start_time))[0] || b[0];
    if (next) { $("s-next").textContent = ttg(next.start_time); $("s-next-sub").textContent = `${next.venue} R${next.race_no}`; }
  }

  // ---------- MOVERS ----------
  function renderMovers() {
    const ul = $("movers");
    $("movers-count").textContent = state.movers.length ? state.movers.length : "";
    if (!state.movers.length) { ul.innerHTML = `<li class="flatc" style="padding:14px">No moves yet…</li>`; return; }
    ul.innerHTML = state.movers.map((m) => `
      <li data-key="${esc(m.race_key)}" data-tip='mover' data-json='${esc(JSON.stringify(m))}'>
        <span class="arrow ${dirClass(m.direction)}">${arrow(m.direction)}</span>
        <span class="who">
          <div class="rn">${esc(m.runner)}</div>
          <div class="ctx"><span class="code ${m.code}">${m.code}</span> ${esc(m.venue)} R${m.race_no}</div>
        </span>
        <span class="delta ${dirClass(m.direction)}">${m.share_delta > 0 ? "+" : ""}${(m.share_delta * 100).toFixed(1)}pt</span>
      </li>`).join("");
    wire(ul);
  }

  // ---------- BOARD ----------
  function renderBoard() {
    const ul = $("board");
    const rows = state.board.filter((r) => state.codeFilter === "ALL" || r.code === state.codeFilter);
    $("board-count").textContent = rows.length ? rows.length : "";
    if (!rows.length) { ul.innerHTML = `<li class="flatc" style="padding:14px">Waiting for races…</li>`; return; }
    ul.innerHTML = rows.map((r) => {
      const fav = r.favourite, mv = r.top_mover;
      const soon = (new Date(r.start_time) - Date.now()) < 5 * 60000;
      return `
      <li data-key="${esc(r.race_key)}" class="${r.race_key === state.selected ? "sel" : ""}">
        <span class="rhead"><span class="code ${r.code}">${r.code}</span></span>
        <span style="min-width:0">
          <div class="rhead"><span class="rvenue">${esc(r.venue)}</span><span class="rno">R${r.race_no}</span>${r.has_betfair ? '<span class="bfbadge">BF</span>' : ""}</div>
          <div class="rmeta">${fav ? `<b style="color:var(--ink-2)">${esc(fav.name)}</b> ${pct(fav.share)}` : "—"}
            ${mv && mv.direction !== "flat" ? `<span class="${dirClass(mv.direction)}">${arrow(mv.direction)} ${esc(mv.name)}</span>` : ""}</div>
        </span>
        <span class="rright">
          <div class="ttg ${soon ? "soon" : ""}">${ttg(r.start_time)}</div>
          <div class="rstatus">${r.status !== "OPEN" ? esc(r.status) : ""}</div>
        </span>
      </li>`;
    }).join("");
    wire(ul);
  }

  // ---------- DETAIL ----------
  function select(raceKey) {
    state.selected = raceKey;
    if (window.__subscribe) window.__subscribe(raceKey);
    if (state.details[raceKey]) renderDetail();
    else if (state.mode === "live" || state.mode === "down") {
      fetch(`/api/race/${encodeURIComponent(raceKey)}`).then((r) => r.ok ? r.json() : null)
        .then((d) => { if (d) { state.details[raceKey] = d; renderDetail(); } });
    }
    renderBoard();
  }

  function renderDetail() {
    const d = state.details[state.selected];
    const el = $("detail");
    if (!d) { el.innerHTML = `<div class="empty"><div class="big">⏳</div>No captured data for this race in replay mode.</div>`; return; }
    const ref = d.ref;
    const runners = d.runners.filter((r) => !r.scratched);
    const maxShare = Math.max(0.001, ...runners.map((r) => r.tote_pool_share || 0));
    el.innerHTML = `
      <div class="detail-head">
        <div class="detail-title">
          <span class="code ${ref.code}">${ref.code}</span>
          <h2>${esc(ref.venue)} — Race ${ref.race_no}</h2>
          <span class="status ${d.status === "OPEN" ? "open" : ""}">${esc(d.status)}</span>
        </div>
        <div class="metastrip">
          <div class="m"><div class="k">Jump</div><div class="v">${ttg(ref.start_time)}</div></div>
          <div class="m"><div class="k">Tote win pool</div><div class="v">${money(d.tote_win_pool) || "forming…"}</div></div>
          <div class="m"><div class="k">Betfair matched</div><div class="v">${money(d.bf_total_matched) || (ref.betfair_market_id ? "…" : "n/a")}</div></div>
          <div class="m"><div class="k">Runners</div><div class="v">${runners.length}</div></div>
        </div>
      </div>
      <div class="runners">${runners.map((r) => runnerRow(r, maxShare)).join("")}</div>
      <div class="legend">
        <span><span class="sw" style="background:linear-gradient(90deg,var(--bar-1),var(--bar-2))"></span> tote pool share</span>
        <span class="firm">▲ firming (money in)</span>
        <span class="drift">▼ drifting (money out)</span>
        <span><span class="sw" style="background:var(--firm)"></span> WoM = Betfair back vs lay pressure</span>
      </div>`;
    el.querySelectorAll("canvas.spark").forEach(drawSpark);
    wire(el.querySelector(".runners"), true);
  }

  function runnerRow(r, maxShare) {
    const share = r.tote_pool_share || 0;
    const w = Math.max(2, (share / maxShare) * 100);
    const move = r.price_move_pct;
    const moveTxt = move == null ? "" : `<span class="${dirClass(r.direction)}">${arrow(r.direction)} ${Math.abs(move).toFixed(0)}%</span>`;
    const wom = r.bf_wom;
    const tote = r.tote_win ? r.tote_win.toFixed(2) : "–";
    const fixed = r.fixed_win ? r.fixed_win.toFixed(2) : "–";
    return `
      <div class="runner" data-tip="runner" data-json='${esc(JSON.stringify(r))}'>
        <span class="num">${r.number}</span>
        <span class="rn">
          <div class="name">${esc(r.name)} <span class="dir ${dirClass(r.direction)}">${arrow(r.direction)}</span></div>
          <div class="barrow"><div class="barwrap"><div class="bar" style="width:${w}%"></div></div><span class="barval">${pct(share)}</span></div>
          ${wom != null ? `<div class="wom"><span class="lbl">WoM</span><div class="wombar"><b style="width:${(wom * 100).toFixed(0)}%"></b></div><span>${(wom * 100).toFixed(0)}%</span></div>` : ""}
        </span>
        <canvas class="spark" width="116" height="34" data-points='${esc(JSON.stringify(r.share_spark || []))}' data-dir="${r.direction}"></canvas>
        <span class="odds">
          <div class="move">${moveTxt || "<span class='flatc'>▪</span>"}</div>
          <div class="px">tote ${tote} · fix ${fixed}</div>
        </span>
      </div>`;
  }

  function drawSpark(c) {
    const pts = JSON.parse(c.dataset.points || "[]").filter((v) => v != null);
    const ctx = c.getContext("2d"), W = c.width, H = c.height, pad = 3;
    ctx.clearRect(0, 0, W, H);
    if (pts.length < 2) { ctx.fillStyle = "#8b8a84"; ctx.fillRect(pad, H / 2, W - 2 * pad, 1); return; }
    const min = Math.min(...pts), max = Math.max(...pts), rng = (max - min) || 1;
    const col = c.dataset.dir === "firming" ? "#16b364" : c.dataset.dir === "drifting" ? "#e5484d" : "#8b8a84";
    const X = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
    const Y = (v) => H - pad - ((v - min) / rng) * (H - 2 * pad);
    // area
    ctx.beginPath(); ctx.moveTo(X(0), H - pad);
    pts.forEach((v, i) => ctx.lineTo(X(i), Y(v)));
    ctx.lineTo(X(pts.length - 1), H - pad); ctx.closePath();
    ctx.fillStyle = col + "22"; ctx.fill();
    // line
    ctx.beginPath(); pts.forEach((v, i) => (i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))));
    ctx.strokeStyle = col; ctx.lineWidth = 1.75; ctx.lineJoin = "round"; ctx.stroke();
    // end dot
    ctx.beginPath(); ctx.arc(X(pts.length - 1), Y(pts[pts.length - 1]), 2.4, 0, 7); ctx.fillStyle = col; ctx.fill();
  }

  // ---------- interactions ----------
  function wire(container, isRunner) {
    container.querySelectorAll("[data-key]").forEach((li) => (li.onclick = () => select(li.dataset.key)));
    container.querySelectorAll("[data-tip]").forEach((el) => {
      el.onmousemove = (e) => showTip(e, el.dataset.tip, JSON.parse(el.dataset.json));
      el.onmouseleave = hideTip;
    });
  }
  const tt = $("tt");
  function showTip(e, kind, j) {
    let html = "";
    if (kind === "mover") {
      html = `<div class="tt-t">${esc(j.runner)}</div>
        <div class="tt-r"><span>Pool share</span><b>${pct(j.share)}</b></div>
        <div class="tt-r"><span>Move</span><b class="${dirClass(j.direction)}">${j.share_delta > 0 ? "+" : ""}${(j.share_delta * 100).toFixed(1)}pt</b></div>
        <div class="tt-r"><span>Price</span><b>${j.price_move_pct != null ? j.price_move_pct.toFixed(0) + "%" : "–"}</b></div>
        <div class="tt-r"><span>Race</span><b>${esc(j.venue)} R${j.race_no}</b></div>`;
    } else {
      html = `<div class="tt-t">${esc(j.name)}</div>
        <div class="tt-r"><span>Tote pool share</span><b>${pct(j.tote_pool_share)}</b></div>
        <div class="tt-r"><span>Tote / fixed</span><b>${j.tote_win ? j.tote_win.toFixed(2) : "–"} / ${j.fixed_win ? j.fixed_win.toFixed(2) : "–"}</b></div>
        ${j.bf_back != null ? `<div class="tt-r"><span>Betfair back / lay</span><b>${j.bf_back ?? "–"} / ${j.bf_lay ?? "–"}</b></div>` : ""}
        ${j.bf_wom != null ? `<div class="tt-r"><span>Weight of money</span><b>${(j.bf_wom * 100).toFixed(0)}% back</b></div>` : ""}
        <div class="tt-r"><span>Direction</span><b class="${dirClass(j.direction)}">${arrow(j.direction)} ${j.direction}</b></div>`;
    }
    tt.innerHTML = html; tt.classList.add("show");
    const pad = 14, w = tt.offsetWidth, h = tt.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > innerWidth) x = e.clientX - w - pad;
    if (y + h > innerHeight) y = e.clientY - h - pad;
    tt.style.left = x + "px"; tt.style.top = y + "px";
  }
  function hideTip() { tt.classList.remove("show"); }

  // filters
  $("code-filters").addEventListener("click", (e) => {
    const btn = e.target.closest("button"); if (!btn) return;
    state.codeFilter = btn.dataset.code;
    document.querySelectorAll("#code-filters button").forEach((b) => b.classList.toggle("active", b === btn));
    renderBoard();
  });

  // theme
  const savedTheme = localStorage.getItem("mf-theme");
  if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
  $("theme").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme") === "light" ? "" : "light";
    if (cur) document.documentElement.setAttribute("data-theme", cur); else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("mf-theme", cur);
    if (state.selected) renderDetail();
  };

  // clock + time-to-go refresh
  setInterval(() => {
    $("clock").textContent = new Date().toLocaleTimeString();
    renderBoard(); renderHero();
  }, 1000);

  // ---------- boot ----------
  if (cfg.forceReplay && !qs.get("api")) startReplay();
  else liveConnect();
})();

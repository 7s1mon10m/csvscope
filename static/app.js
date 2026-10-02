/* csvscope 前端。
   约定：这个文件只负责画图，不做任何统计 —— 所有数字都是后端算好的。
   也正因如此，原始数据从来不需要进浏览器。 */
"use strict";

const $ = (id) => document.getElementById(id);

/* -- 小工具 -- */

// 列名和取值都来自用户文件，必须转义。CSV 里塞一段 <script> 是常有的事。
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtInt(n) {
  return (n === null || n === undefined) ? "—" : Number(n).toLocaleString("en-US");
}

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  if (!isFinite(v)) return String(v);
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 1e9 || a < 1e-4) return v.toExponential(3);
  if (Number.isInteger(v)) return v.toLocaleString("en-US");
  return v.toLocaleString("en-US", { maximumFractionDigits: 5 });
}

function fmtBytes(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
}

const TYPE_LABEL = {
  number: "数值", id: "编号", date: "日期", text: "文本", empty: "全空", unknown: "无数据",
};

function chip(html, cls) {
  return `<span class="chip${cls ? " " + cls : ""}">${html}</span>`;
}

/* -- 进度：SSE -- */

function watchProgress() {
  const es = new EventSource("/api/events");

  es.onmessage = (ev) => {
    const s = JSON.parse(ev.data);
    $("phase").textContent = s.phase || "扫描中…";
    const pct = s.total_bytes ? (s.bytes_read / s.total_bytes) * 100 : 0;
    const fill = $("fill");
    if (s.total_bytes) {
      fill.classList.remove("indet");
      fill.style.width = Math.min(100, pct).toFixed(1) + "%";
      $("ptext").textContent =
        `${fmtBytes(s.bytes_read)} / ${fmtBytes(s.total_bytes)} · ` +
        `${fmtInt(s.rows)} 行 · ${s.elapsed}s`;
    } else {
      $("ptext").textContent = `${s.elapsed}s`;
    }
  };

  es.addEventListener("done", () => {
    es.close();                       // 主动关掉，否则浏览器会自动重连，白白刷请求
    load();
  });

  es.onerror = () => {
    es.close();
    fetch("/api/status").then((r) => r.json()).then((s) => {
      if (s.state === "error") return showError(s.error);
      if (s.state === "ready") return load();
      $("phase").textContent = "连接断开：" + (s.error || "进度流已结束");
    }).catch(() => { $("phase").textContent = "连不上本地服务，窗口可能已被关掉。"; });
  };
}

function showError(msg) {
  $("progress").classList.add("hidden");
  $("error").classList.remove("hidden");
  $("error").textContent = msg || "未知错误";
}

/* -- 加载报告 -- */

async function load() {
  const r = await fetch("/api/report");
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    return showError(e.error || "拿不到报告");
  }
  const rep = await r.json();
  $("progress").classList.add("hidden");
  $("report").classList.remove("hidden");
  renderMeta(rep);
  renderNotes(rep);
  renderColumns(rep);
  renderCorrelations(rep);
  document.title = "csvscope · " + rep.file.name;
}

function renderMeta(rep) {
  const f = rep.file, rows = rep.rows, scan = rep.scan;
  $("fname").textContent = "· " + f.name;

  const parts = [
    chip(`<b>${fmtInt(rows.data_rows)}</b> 行`),
    chip(`<b>${fmtInt(rows.columns)}</b> 列`),
    chip(fmtBytes(f.bytes)),
    chip(`编码 <b>${esc(f.encoding)}</b>` + (f.bom ? "（带 BOM）" : "")),
    chip(`分隔符 <b>${esc(f.delimiter_display)}</b>`),
    chip(`${scan.duration_s}s`),
  ];
  if (rows.ragged_rows) {
    parts.push(chip(`${fmtInt(rows.ragged_rows)} 行列数不齐`, "warn"));
  }
  if (f.decode_errors_in_sample) {
    parts.push(chip(`样本里有 ${fmtInt(f.decode_errors_in_sample)} 个解码错字`, "warn"));
  }
  $("meta").innerHTML = parts.join("");
}

function renderNotes(rep) {
  const out = [];
  const scan = rep.scan;

  if (scan.sampled) {
    out.push(`<div class="note">文件超过了抽样上限，<b>直方图、相关性、两端极值基于
      ${fmtInt(scan.reservoir_rows)} 行等概率抽样</b>。
      缺失数、均值、标准差、最小/最大仍然是<b>全量精确</b>的。</div>`);
  }

  // 把所有"我替你做了处理"的说明汇总到最上面。
  // 埋在每一列里没人会看到，而这些恰恰是最容易让人误会的地方。
  const cleaned = rep.columns.filter((c) => c.notes && c.notes.length);
  if (cleaned.length) {
    const lines = cleaned.map((c) =>
      `<div style="margin-top:5px"><code>${esc(c.name)}</code>：` +
      c.notes.map(([n, cnt]) => `${esc(n)}（${fmtInt(cnt)} 处）`).join("；") + "</div>"
    ).join("");
    out.push(`<div class="note">解析这些列时对取值做过归一化，原始内容未被修改：${lines}</div>`);
  }

  const ids = rep.columns.filter((c) => c.type === "id");
  if (ids.length) {
    out.push(`<div class="note">${ids.length} 列被判定为<b>编号</b>而不是数值，
      因此没有给它们算均值。给订单号、电话号算平均数是典型的错误结论。
      被这样处理的列：` +
      ids.map((c) => `<code>${esc(c.name)}</code>`).join("、") + "。</div>");
  }

  const capped = rep.columns.filter((c) => c.distinct_capped);
  if (capped.length) {
    out.push(`<div class="note">这些列的唯一值超过了 ${fmtInt(scan.distinct_limit)} 的统计上限，
      下方的"唯一值"和 top 榜是<b>截断的近似值</b>：` +
      capped.map((c) => `<code>${esc(c.name)}</code>`).join("、") + "。</div>");
  }

  $("notes").innerHTML = out.join("");
}

function renderColumns(rep) {
  const box = $("columns");
  box.innerHTML = "";
  const maxDistinct = Math.max(1, ...rep.columns.map((c) => c.distinct));

  rep.columns.forEach((c) => {
    const row = document.createElement("div");
    row.className = "colrow";
    row.innerHTML =
      `<div class="colname" title="${esc(c.name)}">${esc(c.name)}</div>` +
      `<div><span class="tag ${c.type}">${TYPE_LABEL[c.type] || c.type}</span></div>` +
      `<div><div class="num">${c.missing_pct}%</div>` +
        `<div class="missbar"><i style="width:${Math.min(100, c.missing_pct)}%"></i></div></div>` +
      `<div class="num">${fmtInt(c.distinct)}${c.distinct_capped ? "+" : ""}</div>` +
      `<div>${miniBar(c, maxDistinct)}</div>` +
      `<div class="caret">▸</div>`;

    const detail = document.createElement("div");
    detail.className = "detail hidden";

    let open = false, built = false;
    row.addEventListener("click", () => {
      open = !open;
      detail.classList.toggle("hidden", !open);
      row.querySelector(".caret").textContent = open ? "▾" : "▸";
      if (open && !built) { detail.innerHTML = columnDetail(c); built = true; }
    });

    box.appendChild(row);
    box.appendChild(detail);
  });
}

// 列表里那根小柱子：唯一值越多越满。一眼能看出哪几列是"自由的文本"、
// 哪几列是"重复出现的分类"，这决定了它们能不能用来分组。
function miniBar(c, max) {
  if (!c.distinct) return "";
  const w = Math.max(2, (Math.log10(c.distinct + 1) / Math.log10(max + 1)) * 100);
  const color = c.type === "number" ? "var(--bar-strong)" : "var(--bar)";
  return `<div class="topbar"><i style="width:${w.toFixed(1)}%;background:${color}"></i></div>`;
}

function columnDetail(c) {
  let html = `<div class="stats">`;
  const stats = [];

  if (c.type === "number") {
    stats.push(["均值", fmtNum(c.mean)], ["标准差", fmtNum(c.std)],
               ["最小", fmtNum(c.min)], ["最大", fmtNum(c.max)]);
  }
  stats.push(["缺失", `${fmtInt(c.missing)}（${c.missing_pct}%）`]);
  stats.push(["唯一值", fmtInt(c.distinct) + (c.distinct_capped ? "（截断）" : "")]);
  stats.push(["长度", c.len_min === c.len_max ? `${c.len_min}` : `${c.len_min}–${c.len_max}`]);
  if (c.numeric_fail) stats.push(["非数字值", fmtInt(c.numeric_fail)]);

  html += stats.map(([k, v]) =>
    `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
  html += `</div>`;

  html += `<div class="sub">判定依据：${esc(c.type_reason)}</div>`;

  if (c.histogram) html += histogramBlock(c);
  if (c.extremes && c.extremes.length) {
    html += `<div class="sub">抽到的两端极值</div>` +
      `<div class="topval">${c.extremes.map(fmtNum).join("　")}</div>`;
  }
  if (c.top && c.top.length) html += topBlock(c);

  // 这是唯一会把原始数据发到浏览器的入口，而且是用户主动点的，
  // 所以按钮旁边的说明得说清楚代价：会重新读一遍文件。
  html += `<div class="sub">想看这一列具体长什么样（会重新读一遍文件，只取前 20 行）</div>` +
    `<button class="act" data-col="${c.index}">查看样本</button>` +
    `<div class="samplebox"></div>`;

  return html;
}

function histogramBlock(c) {
  const h = c.histogram;
  const max = Math.max(1, ...h.counts);
  const W = 1000, H = 120;
  const bw = W / h.counts.length;
  const bars = h.counts.map((n, i) => {
    const bh = (n / max) * H;
    return `<rect x="${(i * bw + 0.5).toFixed(2)}" y="${(H - bh).toFixed(2)}" ` +
           `width="${Math.max(bw - 1, 0.5).toFixed(2)}" height="${bh.toFixed(2)}"></rect>`;
  }).join("");
  return `<div class="sub">分布（按真实极值分 ${h.counts.length} 个桶，
    计数来自 ${fmtInt(h.n)} 个抽样值）</div>` +
    `<svg class="hist" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" fill="var(--bar)">${bars}</svg>` +
    `<div class="axis"><span>${fmtNum(h.lo)}</span><span>${fmtNum(h.hi)}</span></div>`;
}

function topBlock(c) {
  // 每个值都只出现一次时，这张榜单不携带任何信息（一列 40 个订单号
  // 就是这样）。与其铺一屏“重复 1 次”，不如直接说清楚。
  if (c.top.length && c.top[0][1] === 1) {
    return `<div class="sub">没有重复出现的取值</div>` +
      `<div class="topval">这列有 ${fmtInt(c.distinct)} 个不同的值，` +
      `每一个都只出现一次${c.distinct_capped ? "（已达统计上限）" : ""}。</div>`;
  }
  const max = Math.max(1, ...c.top.map(([, n]) => n));
  const items = c.top.map(([v, n]) =>
    `<div class="topitem">` +
      `<div class="topval" title="${esc(v)}">${esc(v)}</div>` +
      `<div class="topbar"><i style="width:${((n / max) * 100).toFixed(1)}%"></i></div>` +
      `<div class="topcnt">${fmtInt(n)}</div>` +
    `</div>`).join("");
  return `<div class="sub">出现最多的取值${c.distinct_capped ? "（可能不完整）" : ""}</div>` +
    `<div class="toplist">${items}</div>`;
}

/* -- 按需取样本 -- */

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button.act[data-col]");
  if (!btn) return;
  const col = btn.getAttribute("data-col");
  const box = btn.parentElement.querySelector(".samplebox");
  btn.disabled = true;
  btn.textContent = "读取中…";
  try {
    const r = await fetch(`/api/sample?col=${col}&n=20`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    box.innerHTML = d.values.length
      ? `<table class="values"><tr><th>第几行数据</th><th>取值</th></tr>` +
        d.values.map(([i, v]) =>
          `<tr><td class="rn">${fmtInt(i)}</td><td class="vv">${esc(v)}</td></tr>`).join("") +
        `</table><div class="sub">${esc(d.note)}${d.capped ? "（已到达扫描上限）" : ""}</div>`
      : `<div class="sub">没读到非空的取值。</div>`;
  } catch (e) {
    box.innerHTML = `<div class="sub">读取失败：${esc(e.message)}</div>`;
  }
  btn.textContent = "重新读取";
  btn.disabled = false;
});

/* -- 相关性 -- */

function renderCorrelations(rep) {
  const box = $("correlations");
  if (!rep.correlations.length) {
    $("corrcard").classList.add("hidden");
    return;
  }
  box.innerHTML = rep.correlations.map((c) => {
    const cls = Math.abs(c.r) >= 0.8 ? "var(--accent)" : "var(--ink-soft)";
    return `<div class="corr" style="grid-template-columns:62px 1fr 92px">` +
      `<div class="r" style="color:${cls}">${c.r > 0 ? "+" : ""}${c.r.toFixed(3)}</div>` +
      `<div><span class="pair">${esc(c.a)} <em>↔</em> ${esc(c.b)}</span></div>` +
      `<div class="n">${fmtInt(c.n)} 对</div>` +
    `</div>`;
  }).join("");
}

/* -- 启动 -- */

fetch("/api/status").then((r) => r.json()).then((s) => {
  if (s.state === "ready") return load();
  if (s.state === "error") return showError(s.error);
  watchProgress();
}).catch(() => showError("连不上本地服务。"));

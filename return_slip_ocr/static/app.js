// 返品伝票OCR（精度記録）画面の処理
const MAX_MB = Number(document.body.dataset.maxMb);
const LOW_CONF = Number(document.body.dataset.lowConf);
const FIELDS = JSON.parse(document.body.dataset.fields);

const $ = (id) => document.getElementById(id);
const canvas = $("canvas");
const ctx = canvas.getContext("2d");

const state = {
  doc: null, // /api/docs の応答（ファイル情報・集計）
  page: 1,
  pageData: null, // /api/docs/<id>/pages/<n> の応答
  items: [], // このページのOCR結果
  image: null,
  activeNo: null, // OCR一覧でマウスを乗せている枠
  rows: [], // 明細（編集中）
  dirty: false,
  assign: null, // 枠を選択中の欄 {row, field}
  hoverCell: null,
  ocrRunning: false,
  cancelOcr: false,
};

// ---------------------------------------------------------------- 共通

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v; // textContent なので OCR 結果に記号が含まれても安全
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c);
  return e;
}

function showError(message, hint) {
  $("error-msg").textContent = message;
  $("error-hint").textContent = hint ? "対処: " + hint : "";
  $("error").hidden = false;
  $("error").scrollIntoView({ block: "nearest" });
}

function clearError() {
  $("error").hidden = true;
}

async function api(method, url, body) {
  const opts = { method };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw { message: "サーバーに接続できません。", hint: "app.py を起動したウィンドウが閉じていないか確認してください。" };
  }
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw { message: `サーバーから想定外の応答がありました（HTTP ${res.status}）。`, hint: "起動したウィンドウのログを確認してください。" };
  }
  if (!res.ok || !data.ok) throw { message: data.error || `エラー（HTTP ${res.status}）`, hint: data.hint, code: data.code };
  return data;
}

function pct(v) {
  return v === null || v === undefined ? "-" : (v * 100).toFixed(1) + "%";
}

function resultClass(result) {
  return (
    { "一致": "ok", "一致（表記差）": "ok-notation", "不一致": "ng", "不一致（読めず）": "ng-unread", "未入力": "pending" }[result] || "pending"
  );
}

function setDirty(v) {
  state.dirty = v;
  $("save-status").textContent = v ? "未保存の変更があります" : $("save-status").textContent;
  $("save-status").classList.toggle("warn", v);
}

window.addEventListener("beforeunload", (ev) => {
  if (state.dirty) ev.preventDefault();
});

function confirmDiscard() {
  return !state.dirty || confirm("このページに保存していない変更があります。破棄してよいですか？");
}

// ---------------------------------------------------------------- アップロード

$("upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  clearError();
  const file = $("pdf").files[0];
  if (!file) return showError("PDFファイルが選択されていません。", "ファイルを選んでください。");
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    return showError("PDF以外のファイルです: " + file.name, "拡張子が .pdf のファイルを選んでください。");
  }
  if (file.size > MAX_MB * 1024 * 1024) {
    return showError(`ファイルサイズが上限（${MAX_MB}MB）を超えています。`, "小さいPDFを使ってください。");
  }
  if (!confirmDiscard()) return;

  const body = new FormData();
  body.append("pdf", file);
  $("upload-btn").disabled = true;
  $("status").textContent = "アップロード中…";
  try {
    state.doc = await api("POST", "/api/docs", body);
    setDirty(false);
    $("status").textContent = "";
    $("doc").hidden = false;
    $("doc-name").textContent = `${state.doc.filename}（全${state.doc.page_count}ページ）`;
    $("records-folder").textContent = state.doc.records_folder;
    renderDocSummary();
    await openPage(1, true);
  } catch (e) {
    $("status").textContent = "";
    showError(e.message || String(e), e.hint);
  } finally {
    $("upload-btn").disabled = false;
  }
});

async function refreshDoc() {
  state.doc = await api("GET", `/api/docs/${state.doc.doc_id}`);
  renderDocSummary();
}

// ---------------------------------------------------------------- ページ

function renderTabs() {
  const tabs = $("page-tabs");
  tabs.replaceChildren(
    ...state.doc.summary.by_page.map((p) => {
      const status = p.saved ? `記録済 ${p.total.correct}/${p.total.target}` : p.ocr_done ? "OCR済" : "未OCR";
      return el(
        "button",
        {
          type: "button",
          class: "tab " + (p.saved ? "saved" : p.ocr_done ? "ocr-done" : "todo") + (p.page === state.page ? " current" : ""),
          onclick: () => openPage(p.page),
        },
        el("b", { text: `${p.page}` }),
        el("small", { text: status })
      );
    })
  );
}

async function openPage(page, force = false) {
  if (!force && page === state.page && state.pageData) return;
  if (!force && !confirmDiscard()) return;
  clearError();
  $("status").textContent = `${page}ページ目を読み込み中…`;
  try {
    const data = await api("GET", `/api/docs/${state.doc.doc_id}/pages/${page}`);
    state.page = page;
    await renderPage(data);
    $("status").textContent = "";
  } catch (e) {
    $("status").textContent = "";
    showError(e.message, e.hint);
  }
  renderTabs();
}

async function renderPage(data) {
  state.pageData = data;
  state.items = data.ocr ? data.ocr.items : [];
  state.activeNo = null;
  state.assign = null;
  state.hoverCell = null;
  state.rows = data.table.rows.map((r) => structuredClone(r));
  state.image = await loadImage(data.image.data_url);
  canvas.width = state.image.width;
  canvas.height = state.image.height;
  setDirty(false);
  $("save-status").textContent = data.table.saved_at ? `保存日時: ${data.table.saved_at}` : "";
  $("save-status").classList.remove("warn");
  if (data.table.source === "reocr") {
    $("save-status").textContent = "OCRし直したため、枠の割り当てを自動に戻して保存しました（入力済みの正解は残しています）。割り当てを原本と見比べてください。";
  }

  $("viewer-title").textContent = `${data.page}ページ目（枠の番号 = OCR一覧のNo）`;
  $("ocr-page").textContent = data.ocr ? "このページをOCRし直す" : "このページをOCR";
  renderPageSummary();
  renderRows();
  renderReview();
  draw();
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject({ message: "ページ画像の表示に失敗しました。" });
    img.src = src;
  });
}

function renderPageSummary() {
  const d = state.pageData;
  const parts = [["ページ", `${d.page} / ${d.page_count}`]];
  if (d.ocr) {
    const s = d.ocr.summary;
    const t = d.ocr.timing_sec;
    parts.push(
      ["検出数", s.count],
      ["平均信頼度", s.avg_confidence == null ? "-" : s.avg_confidence.toFixed(3)],
      [`低信頼度（< ${s.low_confidence_threshold}）`, s.low_confidence_count],
      ["画像", `${d.image.width}×${d.image.height}px / ${d.image.dpi}dpi`],
      ["OCR日時", d.ocr.ocr_at],
      ["処理時間", `画像化 ${t.render}s / モデル ${t.model_load}s / OCR ${t.ocr}s`]
    );
  } else {
    parts.push(["OCR", "まだ実行していません（「このページをOCR」を押してください）"]);
  }
  $("page-summary").replaceChildren(...parts.map(([k, v]) => el("div", {}, k + ": ", el("b", { text: String(v) }))));
}

// ---------------------------------------------------------------- OCR 実行

async function ocrPage(page) {
  return api("POST", `/api/docs/${state.doc.doc_id}/pages/${page}/ocr`);
}

function setOcrRunning(running) {
  state.ocrRunning = running;
  $("ocr-page").disabled = running;
  $("ocr-all").disabled = running;
  $("ocr-cancel").hidden = !running;
}

$("ocr-page").addEventListener("click", async () => {
  clearError();
  const saved = state.pageData?.table.source === "saved";
  if (state.pageData?.ocr) {
    const msg = saved
      ? "このページは記録済みです。OCRし直すと枠の番号が変わるため、枠の割り当ては自動に戻ります（入力した正解は残ります）。続けますか？"
      : "このページをOCRし直しますか？";
    if (!confirm(msg)) return;
  }
  if (!confirmDiscard()) return;
  setOcrRunning(true);
  const started = Date.now();
  const timer = setInterval(() => {
    $("ocr-progress").textContent = `${state.page}ページ目をOCR中… ${Math.floor((Date.now() - started) / 1000)}秒（初回はモデル読み込みで時間がかかります）`;
  }, 500);
  try {
    const data = await ocrPage(state.page);
    await renderPage(data);
    await refreshDoc();
    $("ocr-progress").textContent = `完了（${((Date.now() - started) / 1000).toFixed(1)}秒）`;
  } catch (e) {
    $("ocr-progress").textContent = "";
    showError(e.message, e.hint);
  } finally {
    clearInterval(timer);
    setOcrRunning(false);
  }
});

$("ocr-all").addEventListener("click", async () => {
  clearError();
  const todo = state.doc.summary.by_page.filter((p) => !p.ocr_done).map((p) => p.page);
  if (!todo.length) {
    $("ocr-progress").textContent = "すべてのページがOCR済みです。";
    return;
  }
  setOcrRunning(true);
  state.cancelOcr = false;
  let done = 0;
  try {
    for (const p of todo) {
      if (state.cancelOcr) break;
      $("ocr-progress").textContent = `${p}ページ目をOCR中…（${done + 1} / ${todo.length}）`;
      const data = await ocrPage(p);
      done++;
      if (p === state.page && !state.dirty) await renderPage(data);
      await refreshDoc();
    }
    $("ocr-progress").textContent = state.cancelOcr ? `中止しました（${done}ページ完了）` : `${done}ページのOCRが完了しました。`;
  } catch (e) {
    $("ocr-progress").textContent = `${done}ページ完了後にエラーで止まりました。`;
    showError(e.message, e.hint);
  } finally {
    setOcrRunning(false);
  }
});

$("ocr-cancel").addEventListener("click", () => {
  state.cancelOcr = true;
  $("ocr-progress").textContent = "今のページが終わったら中止します…";
});

// ---------------------------------------------------------------- OCR 一覧（表）

function visibleItems() {
  const q = $("filter-text").value.trim();
  return state.items.filter((it) => (!q || it.text.includes(q)) && (!$("filter-low").checked || it.confidence < LOW_CONF));
}

// 枠No → 割り当て先（"1行目 品番" など）
function assignedMap() {
  const m = new Map();
  state.rows.forEach((r, i) => {
    for (const f of FIELDS) for (const no of r.cells[f].ocr_nos) m.set(no, { row: i, field: f });
  });
  return m;
}

function renderRows() {
  const amap = assignedMap();
  const frag = document.createDocumentFragment();
  for (const it of visibleItems()) {
    const a = amap.get(it.no);
    const tr = el(
      "tr",
      {
        "data-no": it.no,
        class: [it.confidence < LOW_CONF ? "low" : "", it.no === state.activeNo ? "active" : "", a ? "assigned" : ""].join(" "),
        title: "4点座標: " + it.points.map((p) => `(${p[0]}, ${p[1]})`).join(" "),
        onmouseenter: () => setActive(it.no, false),
        onclick: () => (state.assign ? toggleAssign(it.no) : setActive(it.no, false)),
      },
      el("td", { text: it.no }),
      el("td", { class: "text", text: it.text }),
      el("td", { class: "conf", text: it.confidence.toFixed(3) }),
      el("td", { text: Math.round(it.box.x) }),
      el("td", { text: Math.round(it.box.y) }),
      el("td", { text: Math.round(it.box.w) }),
      el("td", { text: Math.round(it.box.h) }),
      el("td", { class: "text", text: a ? `${a.row + 1}行目 ${a.field}` : "" })
    );
    frag.append(tr);
  }
  if (!state.items.length) {
    frag.append(el("tr", {}, el("td", { colspan: 8, class: "text muted", text: "OCR結果はまだありません。" })));
  }
  $("rows").replaceChildren(frag);
}

function setActive(no, scrollRow) {
  state.activeNo = no;
  for (const tr of $("rows").children) tr.classList.toggle("active", Number(tr.dataset.no) === no);
  if (scrollRow) $("rows").querySelector(`tr[data-no="${no}"]`)?.scrollIntoView({ block: "center" });
  draw();
}

// ---------------------------------------------------------------- 画像と枠

function draw() {
  if (!state.image) return;
  ctx.drawImage(state.image, 0, 0);
  if (!$("show-boxes").checked) return;

  const amap = assignedMap();
  const target = state.assign || state.hoverCell;
  const fontSize = Math.max(14, Math.round(canvas.width / 90));
  ctx.font = `${fontSize}px sans-serif`;
  for (const it of visibleItems()) {
    const a = amap.get(it.no);
    const inTarget = target && a && a.row === target.row && a.field === target.field;
    const active = it.no === state.activeNo;
    const low = it.confidence < LOW_CONF;
    ctx.setLineDash(a && !inTarget ? [8, 4] : []);
    ctx.lineWidth = active || inTarget ? 4 : 2;
    ctx.strokeStyle = inTarget ? "#8a2be2" : active ? "#1f6feb" : low ? "#e08600" : "#1a9e4b";
    ctx.beginPath();
    it.points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.stroke();
    if (active || inTarget) {
      ctx.fillStyle = inTarget ? "rgba(138,43,226,0.18)" : "rgba(31,111,235,0.15)";
      ctx.fill();
    }
    ctx.setLineDash([]);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(String(it.no), it.box.x, Math.max(fontSize, it.box.y - 3));
  }
}

canvas.addEventListener("click", (ev) => {
  const rect = canvas.getBoundingClientRect();
  const x = ((ev.clientX - rect.left) * canvas.width) / rect.width;
  const y = ((ev.clientY - rect.top) * canvas.height) / rect.height;
  // 重なっている場合は小さい枠を優先
  const hits = visibleItems()
    .filter((it) => x >= it.box.x && x <= it.box.x + it.box.w && y >= it.box.y && y <= it.box.y + it.box.h)
    .sort((a, b) => a.box.w * a.box.h - b.box.w * b.box.h);
  if (!hits.length) return;
  if (state.assign) toggleAssign(hits[0].no);
  else setActive(hits[0].no, true);
});

$("show-boxes").addEventListener("change", draw);
$("actual-size").addEventListener("change", () => $("canvas-wrap").classList.toggle("actual", $("actual-size").checked));
$("filter-text").addEventListener("input", () => { renderRows(); draw(); });
$("filter-low").addEventListener("change", () => { renderRows(); draw(); });

// ---------------------------------------------------------------- 明細の確認

function cellOcr(cell) {
  const byNo = new Map(state.items.map((it) => [it.no, it]));
  const picked = cell.ocr_nos.map((n) => byNo.get(n)).filter(Boolean).sort((a, b) => a.box.x - b.box.x);
  return {
    text: picked.map((it) => it.text).join(""),
    minConf: picked.length ? Math.min(...picked.map((it) => it.confidence)) : null,
  };
}

function markStale(cell) {
  cell.stale = true;
  setDirty(true);
}

function toggleAssign(no) {
  const { row, field } = state.assign;
  const cell = state.rows[row].cells[field];
  if (cell.ocr_nos.includes(no)) {
    cell.ocr_nos = cell.ocr_nos.filter((n) => n !== no);
  } else {
    // 1つの枠は1つの欄にだけ割り当てる
    state.rows.forEach((r) =>
      FIELDS.forEach((f) => {
        const c = r.cells[f];
        if (c !== cell && c.ocr_nos.includes(no)) {
          c.ocr_nos = c.ocr_nos.filter((n) => n !== no);
          c.assign = "manual";
          markStale(c);
        }
      })
    );
    cell.ocr_nos.push(no);
  }
  cell.assign = "manual";
  markStale(cell);
  renderReview();
  renderRows();
  draw();
}

function startAssign(row, field) {
  state.assign = state.assign && state.assign.row === row && state.assign.field === field ? null : { row, field };
  renderReview();
  draw();
}

$("assign-done").addEventListener("click", () => {
  state.assign = null;
  renderReview();
  draw();
});

function renderReview() {
  const d = state.pageData;
  $("review-page").textContent = `${d.page}ページ目`;

  let note = "";
  if (!d.ocr) note = "このページはまだOCRしていません。OCRすると、明細の欄に文字枠を割り当てられます。";
  else if (d.table.source === "saved") note = `保存済みの記録を表示しています。${d.table.note ? "（参考）" + d.table.note : ""}`;
  else note = d.table.note;
  $("review-note").textContent = note;

  $("assign-banner").hidden = !state.assign;
  if (state.assign) $("assign-target").textContent = `${state.assign.row + 1}行目「${state.assign.field}」`;

  // 列の境界をまたいで自動割り当てしなかった枠
  const amap = assignedMap();
  const byNo = new Map(state.items.map((it) => [it.no, it]));
  const un = (d.table.unassigned || []).filter((n) => !amap.has(n) && byNo.has(n));
  $("unassigned").replaceChildren(
    ...(un.length
      ? [
          el("span", { text: "自動で割り当てなかった枠（列の境界をまたいでいます）: " }),
          ...un.map((n) =>
            el("button", {
              type: "button",
              class: "chip",
              text: `No.${n}「${byNo.get(n).text}」`,
              title: state.assign ? "選択中の欄に追加" : "欄の「枠を選ぶ」を押してからクリックすると追加できます",
              onclick: () => (state.assign ? toggleAssign(n) : setActive(n, true)),
            })
          ),
        ]
      : [])
  );

  $("review-head").replaceChildren(
    el("th", { text: "行" }),
    el("th", { text: "No（OCR）" }),
    ...FIELDS.map((f) => el("th", { text: f })),
    el("th", { text: "" })
  );

  const tbody = $("review-rows");
  if (!state.rows.length) {
    tbody.replaceChildren(
      el("tr", {}, el("td", { colspan: FIELDS.length + 3, class: "muted", text: d.ocr ? "明細の行がありません。「行を追加」してください。" : "" }))
    );
  } else {
    tbody.replaceChildren(...state.rows.map((r, i) => reviewRow(r, i)));
  }
  $("add-row").disabled = !d.ocr;
  $("save-review").disabled = !d.ocr;
}

function reviewRow(r, i) {
  return el(
    "tr",
    {},
    el("td", { class: "center", text: i + 1 }),
    el("td", { class: "center", text: r.no_text || "" }),
    ...FIELDS.map((f) => reviewCell(r, i, f)),
    el("td", {}, el("button", { type: "button", class: "small danger", text: "行を削除", onclick: () => deleteRow(i) }))
  );
}

function reviewCell(r, i, f) {
  const c = r.cells[f];
  const ocr = cellOcr(c);
  const isTarget = state.assign && state.assign.row === i && state.assign.field === f;
  const result = c.stale ? null : c.result;

  const truthInput = el("input", {
    type: "text",
    class: "truth",
    value: c.truth,
    placeholder: c.truth_blank ? "（原本も空欄）" : "原本の値",
    disabled: c.truth_blank,
    oninput: (ev) => {
      c.truth = ev.target.value;
      markStale(c);
      td.querySelector(".judge").replaceChildren(judgeView(c, null));
      td.className = cellClass(null, isTarget);
    },
  });

  const td = el(
    "td",
    {
      class: cellClass(result, isTarget),
      onmouseenter: () => { state.hoverCell = { row: i, field: f }; draw(); },
      onmouseleave: () => { state.hoverCell = null; draw(); },
    },
    el(
      "div",
      { class: "ocr-line" },
      el("span", { class: "tag " + (c.assign === "auto" ? "auto" : "manual"), text: c.assign === "auto" ? "自動" : "手動" }),
      el("span", { class: "ocr-text", text: ocr.text || "（枠なし）" }),
      ocr.minConf !== null ? el("span", { class: "muted", text: ` ${ocr.minConf.toFixed(2)}` }) : null
    ),
    el(
      "div",
      { class: "nos" },
      el("span", { class: "muted", text: c.ocr_nos.length ? `枠 ${c.ocr_nos.join(", ")}` : "枠 なし" }),
      el("button", { type: "button", class: "small" + (isTarget ? " on" : ""), text: isTarget ? "選択中" : "枠を選ぶ", onclick: () => startAssign(i, f) }),
      c.ocr_nos.length
        ? el("button", {
            type: "button",
            class: "small",
            text: "外す",
            title: "この欄の枠をすべて外す（読めなかった扱い）",
            onclick: () => { c.ocr_nos = []; c.assign = "manual"; markStale(c); renderReview(); renderRows(); draw(); },
          })
        : null
    ),
    el(
      "div",
      { class: "truth-line" },
      truthInput,
      el("button", {
        type: "button",
        class: "small",
        text: "写す",
        title: "OCR結果を正解欄にコピー（原本と見比べて同じときに使う）",
        disabled: !ocr.text || c.truth_blank,
        onclick: () => { c.truth = ocr.text; markStale(c); renderReview(); },
      })
    ),
    el(
      "label",
      { class: "small-label" },
      el("input", {
        type: "checkbox",
        checked: c.truth_blank,
        onchange: (ev) => { c.truth_blank = ev.target.checked; if (c.truth_blank) c.truth = ""; markStale(c); renderReview(); },
      }),
      " 原本も空欄"
    ),
    el("div", { class: "judge" }, judgeView(c, result))
  );
  return td;
}

function cellClass(result, isTarget) {
  return "rc " + (result ? resultClass(result) : "stale") + (isTarget ? " target" : "");
}

function judgeView(c, result) {
  if (!result) return el("span", { class: "muted", text: "未判定（保存すると判定します）" });
  return el(
    "span",
    {},
    el("b", { text: result }),
    c.rules ? el("div", { class: "rules-text", text: c.rules }) : null,
    result === "不一致" ? el("div", { class: "rules-text", text: `比較: 「${c.ocr_norm}」≠「${c.truth_norm}」` }) : null
  );
}

function newRow() {
  const cells = {};
  for (const f of FIELDS) cells[f] = { ocr_nos: [], assign: "manual", truth: "", truth_blank: false, stale: true };
  return { no_text: "", cells };
}

$("add-row").addEventListener("click", () => {
  state.rows.push(newRow());
  setDirty(true);
  renderReview();
});

function deleteRow(i) {
  if (!confirm(`${i + 1}行目を削除しますか？（保存するまで記録には反映されません）`)) return;
  state.rows.splice(i, 1);
  state.assign = null;
  setDirty(true);
  renderReview();
  renderRows();
  draw();
}

$("save-review").addEventListener("click", async () => {
  clearError();
  const empty = [];
  state.rows.forEach((r, i) => FIELDS.forEach((f) => {
    const c = r.cells[f];
    if (!c.truth_blank && !c.truth.trim()) empty.push(`${i + 1}行目 ${f}`);
  }));
  if (empty.length && !confirm(`正解が未入力の欄が ${empty.length} 個あります（集計の対象外になります）。\n${empty.slice(0, 10).join("、")}${empty.length > 10 ? " ほか" : ""}\n\nこのまま保存しますか？`)) return;

  $("save-review").disabled = true;
  $("save-status").textContent = "保存中…";
  try {
    const body = {
      rows: state.rows.map((r) => ({
        no_text: r.no_text,
        cells: Object.fromEntries(
          FIELDS.map((f) => [f, { ocr_nos: r.cells[f].ocr_nos, assign: r.cells[f].assign, truth: r.cells[f].truth, truth_blank: r.cells[f].truth_blank }])
        ),
      })),
    };
    const res = await api("PUT", `/api/docs/${state.doc.doc_id}/pages/${state.page}/review`, body);
    state.rows = res.rows;
    state.pageData.table.source = "saved";
    state.pageData.table.saved_at = res.saved_at;
    state.pageData.table.rows = structuredClone(res.rows);
    state.doc.summary = res.summary;
    setDirty(false);
    $("save-status").textContent = `保存しました（${res.saved_at}）。CSVも更新しました。`;
    renderReview();
    renderRows();
    renderDocSummary();
    draw();
  } catch (e) {
    $("save-status").textContent = "保存できませんでした。";
    showError(e.message, e.hint);
  } finally {
    $("save-review").disabled = false;
  }
});

// ---------------------------------------------------------------- 集計

function renderDocSummary() {
  const s = state.doc.summary;
  $("by-field").replaceChildren(
    el(
      "thead",
      {},
      el("tr", {}, ...["項目", "正しく読めた / 対象", "正解率", "完全一致", "表記差のみ", "不一致", "読めず", "未入力"].map((h) => el("th", { text: h })))
    ),
    el(
      "tbody",
      {},
      ...[...s.by_field.map((c) => [c.field, c]), ["全項目", s.total]].map(([name, c]) =>
        el(
          "tr",
          { class: name === "全項目" ? "total" : "" },
          el("td", { class: "text", text: name }),
          el("td", { text: `${c.correct} / ${c.target}` }),
          el("td", { text: pct(c.rate) }),
          el("td", { text: c.exact }),
          el("td", { text: c.notation }),
          el("td", { text: c.mismatch }),
          el("td", { text: c.unread }),
          el("td", { class: "muted", text: c.pending })
        )
      )
    )
  );

  $("by-page").replaceChildren(
    el("thead", {}, el("tr", {}, el("th", { text: "ページ" }), el("th", { text: "状態" }), ...s.fields.map((f) => el("th", { text: f })), el("th", { text: "ページ計" }))),
    el(
      "tbody",
      {},
      ...s.by_page.map((p) =>
        el(
          "tr",
          { class: "clickable" + (p.page === state.page ? " current" : ""), onclick: () => openPage(p.page), title: "クリックでこのページを開く" },
          el("td", { text: p.page }),
          el("td", { class: "text", text: p.saved ? `記録済（${p.rows}行）` : p.ocr_done ? "OCR済" : "未OCR" }),
          ...s.fields.map((f) => {
            const c = p.fields[f];
            return el("td", { class: c.target && c.correct < c.target ? "has-ng" : "", text: p.saved ? `${c.correct}/${c.target}` : "-" });
          }),
          el("td", { text: p.saved ? `${p.total.correct}/${p.total.target}（${pct(p.total.rate)}）` : "-" })
        )
      )
    )
  );
  renderTabs();
}

// ---------------------------------------------------------------- OCR文字一覧のエクスポート（ブラウザ内でファイルを作るだけ）

function exportItems() {
  return $("export-filtered").checked ? visibleItems() : state.items;
}

function exportBaseName() {
  const name = state.doc.filename.replace(/\.pdf$/i, "");
  return `${name}_p${state.page}_ocr`;
}

function download(filename, content, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function csvCell(v) {
  let s = String(v ?? "");
  // Excelで数式として解釈されないようにする（=, +, -, @ で始まる文字列）
  if (typeof v === "string" && /^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

$("export-csv").addEventListener("click", () => {
  if (!state.items.length) return showError("このページはまだOCRしていません。");
  const header = ["ページ", "No", "文字", "信頼度", "低信頼度", "x", "y", "幅", "高さ", "左上x", "左上y", "右上x", "右上y", "右下x", "右下y", "左下x", "左下y"];
  const lines = [header.map(csvCell).join(",")];
  for (const it of exportItems()) {
    lines.push(
      [state.page, it.no, it.text, it.confidence, it.confidence < LOW_CONF ? "○" : "", it.box.x, it.box.y, it.box.w, it.box.h, ...it.points.flat()]
        .map(csvCell)
        .join(",")
    );
  }
  // 先頭の BOM で、Excelでダブルクリックしても文字化けしない
  download(exportBaseName() + ".csv", "﻿" + lines.join("\r\n") + "\r\n", "text/csv;charset=utf-8");
});

$("export-json").addEventListener("click", () => {
  if (!state.items.length) return showError("このページはまだOCRしていません。");
  const d = state.pageData;
  const out = {
    filename: d.filename,
    exported_at: new Date().toISOString(),
    page: d.page,
    page_count: d.page_count,
    image: { width: d.image.width, height: d.image.height, dpi: d.image.dpi }, // 画像本体は含めない
    summary: d.ocr.summary,
    timing_sec: d.ocr.timing_sec,
    engine: d.ocr.engine,
    ocr_at: d.ocr.ocr_at,
    items: exportItems(),
  };
  download(exportBaseName() + ".json", JSON.stringify(out, null, 2), "application/json");
});

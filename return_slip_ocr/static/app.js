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
  check: null, // /api/docs/<id>/check の応答（照合とチェックの結果）
  reasons: [], // 返品理由マスタの選択肢
  status: null, // 伝票の状態（確認中／確定済み／出力済み）
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
  // 直したのに古いエラーが出たままだと迷うので、照合結果が「保存前の古いもの」だとわかるようにする
  document.body.classList.toggle("unsaved", v);
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
  $("upload-form").classList.add("busy");
  $("file-name").textContent = `${file.name} を読み込み中…`;
  $("status").textContent = "アップロード中…";
  try {
    state.doc = await api("POST", "/api/docs", body);
    state.headerDraft = null; // 新しい伝票なので、伝票情報の入力中の値は作り直す
    state.hover = null;
    setDirty(false);
    $("status").textContent = "";
    $("file-name").textContent = `読み込みました：${file.name}`;
    $("doc").hidden = false;
    $("doc-name").textContent = `${state.doc.filename}（全${state.doc.page_count}ページ）`;
    $("records-folder").textContent = state.doc.records_folder;
    renderDocSummary();
    await openPage(1, true);
    renderHeader();
    await loadCheck();
  } catch (e) {
    $("status").textContent = "";
    $("file-name").textContent = "読み込めませんでした。別のPDFを選んでください";
    showError(e.message || String(e), e.hint);
  } finally {
    $("upload-form").classList.remove("busy");
    $("pdf").value = ""; // 同じファイルをもう一度選んでも読み込めるように
  }
});

async function refreshDoc() {
  state.doc = await api("GET", `/api/docs/${state.doc.doc_id}`);
  renderDocSummary();
  renderHeader();
  await loadCheck();
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

  // 人が決める項目は、横にスクロールしなくても見えるように左側に置く
  $("review-head").replaceChildren(
    el("th", { text: "行" }),
    el("th", { text: "① 人が決める項目・元の売上" }),
    ...FIELDS.map((f, k) => el("th", { text: k === 0 ? `② OCRの確認：${f}` : f })),
    el("th", { text: "" })
  );

  const tbody = $("review-rows");
  if (!$("accuracy").open) {
    tbody.replaceChildren();
  } else if (!state.rows.length) {
    tbody.replaceChildren(
      el("tr", {}, el("td", { colspan: FIELDS.length + 4, class: "muted", text: d.ocr ? "明細の行がありません。「行を追加」してください。" : "" }))
    );
  } else {
    tbody.replaceChildren(...state.rows.map((r, i) => reviewRow(r, i)));
  }
  renderWork();
  $("add-row").disabled = !d.ocr || isLocked();
  $("save-review").disabled = !d.ocr || isLocked();
}

function reviewRow(r, i) {
  return el(
    "tr",
    {},
    el(
      "td",
      { class: "center row-head" },
      el("b", { class: "row-no", text: i + 1 }),
      r.no_text ? el("div", { class: "muted", text: `No ${r.no_text}` }) : null,
      el("button", {
        type: "button",
        class: "small adopt-all",
        text: "OCRを全部採用",
        title: "この行で、正解が空でOCRが読めている欄を、すべて「OCRを採用」にします（原本と見比べて正しいときだけ）",
        disabled: isLocked() || !FIELDS.some((f) => canAdopt(r.cells[f])),
        onclick: () => adoptAll(r),
      })
    ),
    extraCell(r, i),
    ...FIELDS.map((f) => reviewCell(r, i, f)),
    el("td", {}, el("button", { type: "button", class: "small danger", text: "行を削除", disabled: isLocked(), onclick: () => deleteRow(i) }))
  );
}

function canAdopt(c) {
  return !c.truth_blank && !(c.truth || "").trim() && !c.adopt_ocr && !!cellOcr(c).text;
}

function adoptAll(r) {
  for (const f of FIELDS) {
    const c = r.cells[f];
    if (canAdopt(c)) {
      c.adopt_ocr = true;
      markStale(c);
    }
  }
  renderReview();
}

function reviewCell(r, i, f, tag = "td") {
  const c = r.cells[f];
  const ocr = cellOcr(c);
  const isTarget = state.assign && state.assign.row === i && state.assign.field === f;
  const result = c.stale ? null : c.result;
  const extraClass = tag === "td" ? "" : " field-card";

  const truthInput = el("input", {
    type: "text",
    class: "truth",
    "aria-label": `${i + 1}行目 ${f} の正解（原本の値）`,
    value: c.truth,
    placeholder: c.truth_blank ? "（原本も空欄）" : "原本の値",
    disabled: c.truth_blank || isLocked(),
    oninput: (ev) => {
      c.truth = ev.target.value;
      markStale(c);
      td.querySelector(".judge").replaceChildren(judgeView(c, null));
      td.className = cellClass(null, isTarget) + extraClass;
    },
  });

  const td = el(
    tag,
    {
      class: cellClass(result, isTarget) + extraClass,
      onmouseenter: () => { state.hoverCell = { row: i, field: f }; draw(); drawWork(); },
      onmouseleave: () => { if (!td.contains(document.activeElement)) { state.hoverCell = null; draw(); drawWork(); } },
      onfocusin: () => { state.hoverCell = { row: i, field: f }; draw(); drawWork(); },
      onfocusout: () => { state.hoverCell = null; draw(); drawWork(); },
    },
    tag === "td" ? null : el("div", { class: "field-name", text: f }),
    el(
      "div",
      { class: "ocr-line" },
      el("span", { class: "tag " + (c.assign === "auto" ? "auto" : "manual"), text: c.assign === "auto" ? "自動" : "手動" }),
      el("span", { class: "ocr-text", text: ocr.text || "（枠なし）" }),
      ocr.minConf !== null ? el("span", { class: "muted", text: ` ${ocr.minConf.toFixed(2)}` }) : null,
      ocr.minConf !== null && ocr.minConf < LOW_CONF ? el("span", { class: "tag low", text: "要確認", title: "OCRの自信が低い文字です。原本と必ず見比べてください" }) : null
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
    el(
      "label",
      { class: "small-label adopt", title: "原本と見比べてOCRの読み取りが正しいときに選びます。正解を入力した欄は、正解が優先されます" },
      el("input", {
        type: "checkbox",
        checked: c.adopt_ocr,
        disabled: !ocr.text || c.truth_blank || isLocked(),
        onchange: (ev) => { c.adopt_ocr = ev.target.checked; markStale(c); renderReview(); },
      }),
      " OCRを採用"
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
  for (const f of FIELDS) cells[f] = { ocr_nos: [], assign: "manual", truth: "", truth_blank: false, adopt_ocr: false, stale: true };
  return { no_text: "", cells, extra: { 明細区分: "通常", 良品不良: "", 税率: "", 元売上: null } };
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
          FIELDS.map((f) => [
            f,
            { ocr_nos: r.cells[f].ocr_nos, assign: r.cells[f].assign, truth: r.cells[f].truth, truth_blank: r.cells[f].truth_blank, adopt_ocr: !!r.cells[f].adopt_ocr },
          ])
        ),
        extra: r.extra || {},
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
    await loadCheck();
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

// ================================================================ 1.4版：見出し・照合・確定・マスタ（要件定義書 F3・F7・F8・F9）

const HEADER_FIELDS = ["会社名", "会社コード", "伝票番号", "返品日", "元売上番号"];
const COUNT_KEYS = { partner: ["m_partner"], item: ["m_item"], reason: ["m_return_reason"], sales: ["t_sales", "t_sales_line"] };

function isLocked() {
  return !!(state.doc && state.doc.status && state.doc.status.state !== "確認中");
}

// ---------------------------------------------------------------- 照合とチェック

async function loadCheck() {
  if (!state.doc) return;
  try {
    const data = await api("GET", `/api/docs/${state.doc.doc_id}/check`);
    state.check = data.check;
    // 伝票情報は、人が入力中でなければ、照合に使った値（保存した値、なければOCRの値）で作り直す
    const typing = document.activeElement && (document.activeElement.id || "").startsWith("hdr-");
    if (typeof headerDirty !== "undefined" && !headerDirty && !saveAgain && !typing) state.headerDraft = null;
    state.reasons = data.reasons || [];
    state.unreviewedPages = data.unreviewed_pages || [];
  } catch (e) {
    state.check = null;
    showError(e.message, e.hint);
  }
  renderCheck();
  if (state.pageData) renderReview();
}

function checkLine(page, row) {
  return state.check ? state.check.lines.find((l) => l.page === page && l.row === row) : null;
}

function rowIssues(page, row) {
  return state.check ? state.check.issues.filter((i) => i.page === page && i.row === row) : [];
}

function yen(v) {
  return v === null || v === undefined ? "-" : Number(v).toLocaleString("ja-JP");
}

function renderCheck() {
  const c = state.check;
  const st = (state.doc && state.doc.status && state.doc.status.state) || "確認中";
  $("confirm-state").textContent = st;
  $("confirm-state").className = "pill state-" + ({ 確認中: "review", 確定済み: "confirmed", 出力済み: "exported" }[st] || "review");
  if (!c) {
    $("check-summary").replaceChildren();
    $("issue-list").replaceChildren();
    return;
  }
  const h = c.header;
  const chips = [
    ["エラー", c.errors, c.errors ? "chip-error" : "chip-ok"],
    ["警告", c.warnings, c.warnings ? "chip-warn" : "chip-ok"],
    ["会社（マスタ）", c.partner_found ? `${h["会社コード"]} ${h["会社名"]}` : "未特定", c.partner_found ? "" : "chip-error"],
    ["計上月", h.fiscal_month ? `${h.fiscal_month.replace("-", "/")}（締め ${h.closing_date.slice(5).replace("-", "/")}）` : "-", h.late ? "chip-warn" : ""],
    ["返品合計（税込）", yen(c.tax ? c.tax.total : c.lines.reduce((s, l) => s + (l.金額 || 0), 0)) + " 円", ""],
  ];
  $("check-summary").replaceChildren(
    ...chips.map(([k, v, cls]) => el("div", { class: "sum-chip " + cls }, el("span", { text: k }), el("b", { text: String(v) })))
  );
  renderTax();
  const pending = state.unreviewedPages || [];
  const items = [
    ...pending.map((p) => ({ level: "エラー", check: "P", message: `${p}ページ目は、まだOCRまたは「保存して判定」が済んでいません。`, page: p, row: null })),
    ...c.issues,
  ];
  $("issue-list").replaceChildren(
    ...(items.length
      ? items.map((i) =>
          // 押せるものは button にする（キーボードでも押せるように）
          el(
            "li",
            { class: i.level === "エラー" ? "lv-error" : "lv-warn" },
            el(
              "button",
              { type: "button", class: "issue-btn", onclick: () => goToRow(i.page, i.row, i.field) },
              el("span", { class: "issue-badge", text: `${i.level} ${i.check}` }),
              el("span", { class: "issue-where", text: i.field ? `伝票情報「${i.field}」` : i.row ? `${i.page}ページ ${i.row}行目` : i.page ? `${i.page}ページ` : "伝票" }),
              el("span", { class: "issue-msg", text: i.message })
            )
          )
        )
      : [el("li", { class: "lv-ok" }, el("span", { class: "issue-badge", text: "OK" }), el("span", { text: "エラー・警告はありません。確定できます。" }))])
  );
  const locked = isLocked();
  $("confirm-btn").disabled = locked || c.errors > 0 || pending.length > 0;
  $("confirm-btn").hidden = st !== "確認中";
  $("unconfirm-btn").hidden = st !== "確定済み";
}

async function goToRow(page, row, field) {
  // 伝票情報の項目なら、その入力欄へ
  if (field) {
    const inp = document.querySelector(`#work-header [data-field="${field}"]`);
    if (inp) { inp.scrollIntoView({ block: "center", behavior: "smooth" }); inp.focus({ preventScroll: true }); }
    return;
  }
  if (!page) return;
  if (page !== state.page) await openPage(page);
  if (!row) return;
  const tr = $("work-rows").children[row - 1];
  if (tr) {
    tr.scrollIntoView({ block: "center", behavior: "smooth" });
    tr.classList.add("flash");
    setTimeout(() => tr.classList.remove("flash"), 1600);
    const first = tr.querySelector("input:not([disabled]), select:not([disabled])");
    if (first) first.focus({ preventScroll: true });
  }
}

$("check-refresh").addEventListener("click", () => loadCheck());

$("confirm-btn").addEventListener("click", async () => {
  clearError();
  if (state.dirty) return showError("保存していない変更があります。", "先に「保存して判定」を押してください。");
  $("confirm-btn").disabled = true;
  try {
    const res = await api("POST", `/api/docs/${state.doc.doc_id}/confirm`);
    state.doc.status = res.status;
    $("confirm-status").textContent = `確定しました（${res.status.confirmed_at}）。返品の記録に登録しました。`;
  } catch (e) {
    showError(e.message, e.hint);
  }
  renderHeader();
  await loadCheck();
});

$("unconfirm-btn").addEventListener("click", async () => {
  if (!confirm("確定を取り消して、編集できる状態に戻しますか？（返品の記録からも外します）")) return;
  clearError();
  try {
    const res = await api("DELETE", `/api/docs/${state.doc.doc_id}/confirm`);
    state.doc.status = res.status;
    $("confirm-status").textContent = "確定を取り消しました。";
  } catch (e) {
    showError(e.message, e.hint);
  }
  renderHeader();
  await loadCheck();
});

// ---------------------------------------------------------------- 明細の「人が決める項目」と元の売上

function select(options, value, onchange, disabled, label) {
  return el(
    "select",
    { disabled, "aria-label": label, onchange: (ev) => onchange(ev.target.value) },
    ...options.map(([v, label]) => el("option", { value: v, selected: v === value, text: label }))
  );
}

function candidateLabel(c) {
  return `${c.sales_no}（${c.sales_date.slice(5).replace("-", "/")}）${yen(c.unit_price)}円・残り${c.remaining}`;
}

// 表の中で使う短い表示（くわしくはポインタを乗せると出る）
function candidateShort(c) {
  return `${c.sales_no} 残${c.remaining}`;
}

function extraCell(r, i, tag = "td") {
  r.extra = r.extra || { 明細区分: "通常", 良品不良: "", 税率: "", 元売上: null };
  const ex = r.extra;
  const locked = isLocked();
  const change = (key, v) => {
    ex[key] = v;
    setDirty(true);
    renderReview();
  };
  const discount = ex.明細区分 === "値引";
  const line = checkLine(state.page, i + 1);
  const parts = [
    el("div", { class: "ex-line" }, el("span", { class: "ex-label", text: "区分" }), select([["通常", "通常"], ["値引", "値引"]], ex.明細区分, (v) => change("明細区分", v), locked, `${i + 1}行目 区分`)),
  ];
  if (discount) {
    parts.push(
      el(
        "div",
        { class: "ex-line" },
        el("span", { class: "ex-label", text: "税率" }),
        select([["", "選んでください"], ["10", "10%"], ["8", "8%（軽減）"], ["按分", "按分（金額の比率で分ける）"]], ex.税率 || "", (v) => change("税率", v), locked, `${i + 1}行目 値引の税率`)
      )
    );
  } else {
    parts.push(
      el(
        "div",
        { class: "ex-line" },
        el("span", { class: "ex-label", text: "良品/不良" }),
        select([["", "選んでください"], ["良品", "良品（在庫に戻す）"], ["不良", "不良（廃棄・返品）"]], ex.良品不良 || "", (v) => change("良品不良", v), locked, `${i + 1}行目 良品か不良か`)
      )
    );
    const cell = r.cells["返品理由"];
    if (state.reasons.length && cell) {
      // 今の返品理由（入力した正解、なければ採用したOCRの文字）を、選択肢の中で選ばれた状態で見せる
      const current = ((cell.truth || "").trim() || (cell.adopt_ocr ? cellOcr(cell).text : "")).replace(/\s+/g, "");
      const known = state.reasons.find((x) => x.name.replace(/\s+/g, "") === current);
      const opts = [["", "（マスタから選ぶ）"], ...state.reasons.map((x) => [x.name, `${x.reason_code} ${x.name}`])];
      if (current && !known) opts.splice(1, 0, ["__ocr__", `伝票の文字「${current}」（マスタに無い）`]);
      parts.push(
        el(
          "div",
          { class: "ex-line" },
          el("span", { class: "ex-label", text: "返品理由" }),
          select(
            opts,
            known ? known.name : current ? "__ocr__" : "",
            (v) => {
              if (!v || v === "__ocr__") return;
              cell.truth = v; // 選んだ理由を、返品理由の欄の正解として入れる
              cell.truth_blank = false;
              markStale(cell);
              renderReview();
            },
            locked,
            `${i + 1}行目 返品理由`
          )
        )
      );
    }
    // 元の売上
    if (line) {
      const cands = line.候補 || [];
      const chosen = ex.元売上;
      let body;
      if (!cands.length) body = el("span", { class: "muted", text: "候補なし（品番・返品日・元売上番号を確認）" });
      else if (cands.length === 1 && !chosen)
        body = el("span", { class: "cand-one", text: candidateLabel(cands[0]), title: "候補が1件だけなので、照合先として表示しています" });
      else
        body = select(
          [["", `選んでください（候補${cands.length}件）`], ...cands.map((c) => [`${c.sales_no}|${c.line_no}`, candidateLabel(c)])],
          chosen ? `${chosen.sales_no}|${chosen.line_no}` : "",
          (v) => {
            const [no, ln] = v.split("|");
            change("元売上", v ? { sales_no: no, line_no: Number(ln) } : null);
          },
          locked,
          `${i + 1}行目 元の売上`
        );
      parts.push(el("div", { class: "ex-line cand" }, el("span", { class: "ex-label", text: "元の売上" }), body));
      if (line.商品名) parts.push(el("div", { class: "ex-line muted", text: `商品：${line.商品名}（${line.税率}%）` }));
    }
  }
  const iss = rowIssues(state.page, i + 1);
  if (iss.length)
    parts.push(el("ul", { class: "row-issues" }, ...iss.map((x) => el("li", { class: x.level === "エラー" ? "lv-error" : "lv-warn", text: x.message }))));
  return el(tag, { class: "extra-cell" }, ...parts);
}

// ---------------------------------------------------------------- マスタ・売上データの取り込み

async function loadMasters() {
  try {
    const data = await api("GET", "/api/import/kinds");
    renderMasters(data);
  } catch (e) {
    $("master-list").replaceChildren(el("p", { class: "muted", text: "マスタの情報を読めませんでした: " + e.message }));
  }
}

function renderMasters(data) {
  $("master-list").replaceChildren(
    ...data.kinds.map((k) => {
      const count = COUNT_KEYS[k.kind].map((t) => data.counts[t]).join(" / ");
      const file = el("input", { type: "file", id: `imp-${k.kind}`, accept: ".xlsx,.csv" });
      const result = el("div", { class: "imp-result" });
      const btn = el("button", {
        type: "button",
        class: "small",
        text: "取り込む",
        onclick: async () => {
          if (!file.files[0]) return result.replaceChildren(el("span", { class: "imp-ng", text: "ファイルを選んでください。" }));
          const body = new FormData();
          body.append("file", file.files[0]);
          btn.disabled = true;
          result.replaceChildren(el("span", { class: "muted", text: "取り込み中…" }));
          try {
            const res = await fetch(`/api/import/${k.kind}`, { method: "POST", body }).then((r) => r.json());
            if (res.imported) {
              result.replaceChildren(el("span", { class: "imp-ok", text: `${res.rows}行を取り込みました。` }), ...(res.notices || []).map((n) => el("div", { class: "muted", text: n })));
              await loadMasters();
              if (state.doc) await loadCheck();
            } else if (res.errors) {
              result.replaceChildren(
                el("div", { class: "imp-ng", text: res.error || "取り込めませんでした。" }),
                el(
                  "table",
                  { class: "stat-table imp-errors" },
                  el("thead", {}, el("tr", {}, ...["行", "列", "理由"].map((h) => el("th", { text: h })))),
                  el("tbody", {}, ...res.errors.map((e) => el("tr", {}, el("td", { text: e.row }), el("td", { class: "text", text: e.column }), el("td", { class: "text", text: e.message }))))
                )
              );
            } else {
              result.replaceChildren(el("div", { class: "imp-ng", text: res.error || "取り込めませんでした。" }), el("div", { class: "muted", text: res.hint || "" }));
            }
          } catch (e) {
            result.replaceChildren(el("div", { class: "imp-ng", text: "サーバーに接続できません。" }));
          } finally {
            btn.disabled = false;
          }
        },
      });
      return el(
        "div",
        { class: "master-row" },
        el("div", { class: "master-name" }, el("b", { text: k.label }), el("span", { class: "muted", text: `${count} 件` })),
        el("a", { class: "small-link", href: `/api/import/${k.kind}/template`, text: "見本Excel" }),
        el("label", { class: "file" }, file),
        btn,
        result
      );
    })
  );
}

loadMasters();

// ================================================================ 計算式（税率ごとの合計・消費税）

function renderTax() {
  const t = state.check && state.check.tax;
  const box = $("tax-box");
  if (!t || !t.rates.length) return box.replaceChildren();
  const rows = [];
  for (const r of t.rates) {
    const parts = [...r.items.map((a) => yen(a)), ...r.discounts.map((d) => yen(d.amount))];
    rows.push(
      el(
        "div",
        { class: "tax-rate" },
        el("div", { class: "tax-title", text: `${r.rate}%対象` }),
        el("div", { class: "formula" }, el("span", { text: "合計（税込）" }), el("code", { text: `${parts.join(" + ").replace(/\+ -/g, "− ")} = ${yen(r.total)} 円` })),
        ...r.discounts.filter((d) => d.note !== "値引").map((d) => el("div", { class: "formula sub" }, el("span", { text: "値引の按分" }), el("code", { text: `${d.note.replace("値引の按分", "").replace(/[（）]/g, "")} → ${yen(d.amount)} 円` }))),
        el(
          "div",
          { class: "formula" },
          el("span", { text: "うち消費税" }),
          el("code", { text: `${yen(r.total)} × ${r.rate} ÷ ${100 + r.rate} = ${r.tax_exact} → ${t.rounding} → ${yen(r.tax)} 円` })
        ),
        el("div", { class: "formula" }, el("span", { text: "税抜金額" }), el("code", { text: `${yen(r.total)} − ${yen(r.tax)} = ${yen(r.base)} 円` }))
      )
    );
  }
  box.replaceChildren(
    el("div", { class: "tax-head" }, el("b", { text: "計算式" }), el("span", { class: "muted", text: "単価は税込。消費税は合計の中に入っているので、足しません" })),
    ...rows,
    el("div", { class: "formula total" }, el("span", { text: "返品合計（税込）" }), el("code", { text: `${yen(t.total)} 円` }))
  );
}

// ================================================================ ドラッグ＆ドロップで読み込む（要件定義書 F1）

(function setupDrop() {
  const zone = $("upload-form");
  let depth = 0;
  // ページのほかの場所に落としても、ブラウザがPDFを開いて画面が切り替わらないようにする
  window.addEventListener("dragover", (ev) => ev.preventDefault());
  window.addEventListener("drop", (ev) => ev.preventDefault());
  zone.addEventListener("dragenter", (ev) => { ev.preventDefault(); depth++; zone.classList.add("dragging"); });
  zone.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) zone.classList.remove("dragging"); });
  zone.addEventListener("dragover", (ev) => { ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
  zone.addEventListener("drop", (ev) => {
    ev.preventDefault();
    depth = 0;
    zone.classList.remove("dragging");
    const files = [...ev.dataTransfer.files];
    if (!files.length) return;
    const pdfs = files.filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    if (!pdfs.length) return showError("PDF以外のファイルです: " + files[0].name, "拡張子が .pdf のファイルを落としてください。");
    if (files.length > 1) $("status").textContent = `今は1つずつ読み込みます（${pdfs[0].name} を読み込みます）。複数まとめての読み込みは次の周で作ります。`;
    const dt = new DataTransfer();
    dt.items.add(pdfs[0]);
    $("pdf").files = dt.files;
    zone.requestSubmit();
  });
})();


// ファイルを選んだら、すぐに読み込む（アップロードボタンは使わない）
$("pdf").addEventListener("change", () => {
  const f = $("pdf").files[0];
  if (!f) return;
  $("file-name").textContent = f.name;
  $("upload-form").requestSubmit();
});

// ================================================================ 作業画面：左に伝票の画像、右に入力欄（Issue #13）
//
// 入力欄の値の決め方（要件定義書 F6 と同じ）
//   正解（人が書き換えた値）があればそれ → なければOCRの値（空白などをそろえたもの）
//   OCRの自信が低い欄は「要確認」。人が ✓ を押すか書き換えるまで、確定できない
// 書き換えたら少し待って自動で保存し、照合し直す（「保存して判定」ボタンは使わない）

state.zoom = null; // null = 幅に合わせる
state.hover = null; // 枠を出す対象 { kind: "cell", row, field } / { kind: "header", field }
state.headerDraft = null; // 伝票情報の入力中の値 { values: {...}, confirmed: [...] }
let saveTimer = null;
let saving = false;
let saveAgain = false;
let headerDirty = false;

const LINE_INPUTS = ["品番", "枝番", "数量", "単価"];

function normText(t) {
  return (t || "").normalize("NFKC").replace(/\s+/g, "");
}

function cellValue(c) {
  if (c.truth_blank) return "";
  if ((c.truth || "").trim()) return normText(c.truth);
  return normText(cellOcr(c).text);
}

function needsConfirm(c) {
  return !c.truth_blank && !(c.truth || "").trim() && !c.adopt_ocr && !!cellOcr(c).text;
}

// 必ず入れる欄（通常の行は品番・数量・単価、値引の行は単価）
function isRequired(f, discount) {
  return discount ? f === "単価" : ["品番", "数量", "単価"].includes(f);
}

// 黄色にする欄：要確認（OCRの自信が低い）か、必ず入れる欄が空
function cellNeedsWork(c, f, discount) {
  return needsConfirm(c) || (isRequired(f, discount) && cellValue(c) === "");
}

function confText(conf) {
  return conf === null || conf === undefined ? "" : `OCRの自信 ${conf.toFixed(2)}`;
}

// ---------------------------------------------------------------- 伝票情報（見出し）

function headerProposal(f) {
  return (state.doc && state.doc.header_proposal && state.doc.header_proposal[f]) || null;
}

function headerNeedsConfirm(f) {
  const p = headerProposal(f);
  const d = state.headerDraft;
  if (!p || !d || p.confidence === null || p.confidence >= LOW_CONF) return false;
  if (d.confirmed.includes(f)) return false;
  return normText(d.values[f]) === normText(p.value); // 人が書き換えていない
}

function renderHeader() {
  if (!state.doc) return;
  const box = $("work-header");
  if (!state.headerDraft && state.check && state.check.header_values) {
    state.headerDraft = { values: { ...state.check.header_values }, confirmed: [...(state.check.header_confirmed || [])] };
  }
  if (!state.headerDraft) return box.replaceChildren(el("p", { class: "muted", text: "OCRすると、ここに伝票の見出しが入ります。" }));
  const d = state.headerDraft;
  box.replaceChildren(
    ...HEADER_FIELDS.map((f) => {
      const p = headerProposal(f);
      const need = headerNeedsConfirm(f);
      const id = `hdr-${HEADER_FIELDS.indexOf(f)}`;
      const input = el("input", {
        type: "text",
        id,
        "data-field": f,
        class: need ? "need" : "",
        value: d.values[f] || "",
        disabled: isLocked(),
        "aria-describedby": `${id}-hint`,
        oninput: (ev) => {
          d.values[f] = ev.target.value;
          headerDirty = true;
          scheduleSave();
        },
        onfocus: () => setHover({ kind: "header", field: f }),
        onblur: () => setHover(null),
      });
      return el(
        "div",
        { class: "fg" + (need ? " need" : ""), onmouseenter: () => setHover({ kind: "header", field: f }), onmouseleave: () => { if (document.activeElement !== input) setHover(null); } },
        el("label", { for: id, text: f }),
        el(
          "div",
          { class: "inp-wrap" },
          input,
          need ? confirmButton(`伝票情報「${f}」を確認しました`, () => { d.confirmed.push(f); headerDirty = true; scheduleSave(); renderWork(); }) : null
        ),
        el("small", { id: `${id}-hint`, class: "hint", text: need ? `要確認（${confText(p.confidence)}）原本と見比べて ✓` : p ? confText(p.confidence) : "OCRでは見つかりませんでした" })
      );
    })
  );
}

function confirmButton(label, onclick) {
  return el("button", { type: "button", class: "ok-btn", title: label, "aria-label": label, disabled: isLocked(), onclick }, "✓");
}

// ---------------------------------------------------------------- 明細

function lineInput(r, i, f, discount) {
  const c = r.cells[f];
  const ocr = cellOcr(c);
  const id = `w-${i}-${f}`;
  if (discount && f !== "単価") {
    return el("td", { class: "na" }, el("span", { "aria-hidden": "true", text: "－" }), el("span", { class: "visually-hidden", text: `${i + 1}行目 ${f}：値引の行では使いません` }));
  }
  const need = cellNeedsWork(c, f, discount);
  const low = ocr.minConf !== null && ocr.minConf < LOW_CONF;
  const input = el("input", {
    type: "text",
    id,
    class: "cell-input" + (need ? " need" : ""),
    value: cellValue(c),
    inputmode: f === "数量" || f === "単価" ? "numeric" : null,
    "aria-label": `${i + 1}行目 ${f}`,
    title: ocr.text ? `OCR：${ocr.text}（${confText(ocr.minConf)}）` : "OCRでは読めませんでした",
    disabled: isLocked(),
    oninput: (ev) => {
      const v = ev.target.value;
      if (!v.trim()) {
        c.truth = "";
        c.truth_blank = true;
      } else {
        c.truth = v;
        c.truth_blank = false;
      }
      c.adopt_ocr = false;
      markStale(c);
      scheduleSave();
    },
    onfocus: () => setHover({ kind: "cell", row: i, field: f }),
    onblur: () => setHover(null),
  });
  return el(
    "td",
    {
      class: need ? "need" : "",
      onmouseenter: () => setHover({ kind: "cell", row: i, field: f }),
      onmouseleave: () => { if (document.activeElement !== input) setHover(null); },
    },
    el(
      "div",
      { class: "inp-wrap" },
      input,
      needsConfirm(c) ? confirmButton(`${i + 1}行目 ${f} を確認しました（${ocr.text}）`, () => { c.truth = normText(ocr.text); markStale(c); scheduleSave(); renderWork(); }) : null
    ),
    needsConfirm(c) && low ? el("small", { class: "hint", text: "要確認" }) : need && !cellValue(c) ? el("small", { class: "hint", text: "入力してください" }) : null
  );
}

function reasonSelect(r, i, line, discount) {
  const c = r.cells["返品理由"];
  const current = normText(cellValue(c));
  const known = state.reasons.find((x) => line && line.理由コード ? x.reason_code === line.理由コード : normText(x.name) === current);
  const opts = [["", discount ? "（なし）" : "選ぶ"], ...state.reasons.map((x) => [x.name, x.name])];
  let value = known ? known.name : "";
  if (current && !known) {
    opts.splice(1, 0, ["__ocr__", `「${current}」（マスタに無い）`]);
    value = "__ocr__";
  }
  const need = needsConfirm(c) && !known;
  return el(
    "td",
    { class: need ? "need" : "", onmouseenter: () => setHover({ kind: "cell", row: i, field: "返品理由" }), onmouseleave: () => setHover(null) },
    el(
      "select",
      {
        "aria-label": `${i + 1}行目 返品理由`,
        title: known ? `理由コード ${known.reason_code}：${known.name}` : "",
        disabled: isLocked(),
        onfocus: () => setHover({ kind: "cell", row: i, field: "返品理由" }),
        onblur: () => setHover(null),
        onchange: (ev) => {
          const v = ev.target.value;
          if (v === "__ocr__") return;
          c.truth = v;
          c.truth_blank = !v;
          c.adopt_ocr = false;
          markStale(c);
          scheduleSave();
        },
      },
      ...opts.map(([v, label]) => el("option", { value: v, selected: v === value, text: label }))
    )
  );
}

function decisionSelect(r, i, discount) {
  const ex = r.extra;
  if (discount) {
    return el(
      "td",
      { class: ex.税率 ? "" : "need" },
      select(
        [["", "税率を選ぶ"], ["10", "10%"], ["8", "8%（軽減）"], ["按分", "按分"]],
        ex.税率 || "",
        (v) => { ex.税率 = v; scheduleSave(); },
        isLocked(),
        `${i + 1}行目 値引の税率`
      )
    );
  }
  const s = select(
    [["", "選ぶ"], ["良品", "良品"], ["不良", "不良"]],
    ex.良品不良 || "",
    (v) => { ex.良品不良 = v; scheduleSave(); },
    isLocked(),
    `${i + 1}行目 良品か不良か`
  );
  return el("td", { class: ex.良品不良 ? "" : "need" }, s);
}

function salesCell(r, i, line, discount) {
  if (discount) return el("td", { class: "na" }, el("span", { "aria-hidden": "true", text: "－" }));
  if (!line) return el("td", { class: "muted", text: "…" });
  const cands = line.候補 || [];
  const chosen = r.extra.元売上;
  if (!cands.length) return el("td", { class: "cand-none", text: "候補なし" });
  if (cands.length === 1 && !chosen) return el("td", { class: "cand-one", title: candidateLabel(cands[0]), text: candidateShort(cands[0]) });
  return el(
    "td",
    { class: chosen ? "" : "need" },
    select(
      [["", `選ぶ（候補${cands.length}件）`], ...cands.map((c) => [`${c.sales_no}|${c.line_no}`, candidateLabel(c)])],
      chosen ? `${chosen.sales_no}|${chosen.line_no}` : "",
      (v) => {
        const [no, ln] = v.split("|");
        r.extra.元売上 = v ? { sales_no: no, line_no: Number(ln) } : null;
        scheduleSave();
      },
      isLocked(),
      `${i + 1}行目 元の売上`
    )
  );
}

function statusCell(i) {
  const iss = rowIssues(state.page, i + 1);
  const ng = iss.some((x) => x.level === "エラー");
  const label = ng ? "エラーあり" : iss.length ? "警告あり" : "問題なし";
  const msg = iss.map((x) => x.message).join("\n");
  return el(
    "span",
    { class: "row-state " + (ng ? "ng" : iss.length ? "warn" : "ok"), title: msg || label },
    el("span", { "aria-hidden": "true", text: ng ? "!" : iss.length ? "?" : "✓" }),
    el("span", { class: "visually-hidden", text: `${i + 1}行目：${label}。${msg}` })
  );
}

function renderWork() {
  if (!state.doc || !state.pageData) return;
  $("work-page-label").textContent = `${state.page}ページ目（全${state.doc.page_count}ページ）`;
  renderHeader();

  const keep = rememberFocus();
  const tbody = $("work-rows");
  $("work-line-count").textContent = `${state.rows.length}行`;
  if (!state.rows.length) {
    tbody.replaceChildren(el("tr", {}, el("td", { colspan: 9, class: "muted", text: state.pageData.ocr ? "明細が読み取れませんでした。「＋ 行を追加」で入力できます。" : "まだOCRしていません。左の「このページをOCR」を押してください。" })));
  } else {
    tbody.replaceChildren(
      ...state.rows.map((r, i) => {
        r.extra = r.extra || { 明細区分: "通常", 良品不良: "", 税率: "", 元売上: null };
        const discount = r.extra.明細区分 === "値引";
        const line = checkLine(state.page, i + 1);
        return el(
          "tr",
          { class: discount ? "discount" : "" },
          el(
            "th",
            { scope: "row", class: "row-no" },
            el("b", { class: "row-num", text: i + 1 }),
            statusCell(i),
            select(
              [["通常", "通常"], ["値引", "値引"]],
              r.extra.明細区分,
              (v) => {
                r.extra.明細区分 = v;
                for (const f of ["品番", "枝番", "数量"]) {
                  const c = r.cells[f];
                  if (v === "値引") { c.truth = ""; c.truth_blank = true; c.adopt_ocr = false; } else if (c.truth_blank) { c.truth_blank = false; }
                }
                scheduleSave();
                renderWork();
              },
              isLocked(),
              `${i + 1}行目 区分`
            )
          ),
          ...LINE_INPUTS.map((f) => lineInput(r, i, f, discount)),
          reasonSelect(r, i, line, discount),
          decisionSelect(r, i, discount),
          salesCell(r, i, line, discount),
          el(
            "td",
            { class: "row-ops" },
            el("button", {
              type: "button",
              class: "icon-btn",
              "aria-label": `${i + 1}行目を削除`,
              title: "この行を削除",
              disabled: isLocked(),
              onclick: () => {
                if (!confirm(`${i + 1}行目を削除しますか？`)) return;
                state.rows.splice(i, 1);
                scheduleSave();
                renderWork();
              },
            }, "✕")
          )
        );
      })
    );
  }
  // 選択肢を作り直したあとも、見た目の選択とずれないようにする
  tbody.querySelectorAll("select").forEach((s) => { const o = s.querySelector("option[selected]"); if (o) s.value = o.value; });
  $("work-add-row").disabled = isLocked() || !state.pageData.ocr;
  restoreFocus(keep);
  renderProgress();
  drawWork();
}

$("work-add-row").addEventListener("click", () => {
  state.rows.push(newRow());
  scheduleSave();
  renderWork();
  const last = $("work-rows").lastElementChild;
  if (last) { const inp = last.querySelector("input"); if (inp) inp.focus(); }
});

// 書き換えのたびに表を作り直しても、入力中の欄とカーソルの位置がずれないようにする
function rememberFocus() {
  const a = document.activeElement;
  if (!a || !a.id || !(a.id.startsWith("w-") || a.id.startsWith("hdr-"))) return null;
  return { id: a.id, start: a.selectionStart, end: a.selectionEnd };
}
function restoreFocus(k) {
  if (!k) return;
  const e = document.getElementById(k.id);
  if (!e) return;
  e.focus({ preventScroll: true });
  if (typeof k.start === "number" && e.setSelectionRange) e.setSelectionRange(k.start, k.end);
}

// ---------------------------------------------------------------- あと何か所

function renderProgress() {
  if (!state.check) return;
  let need = HEADER_FIELDS.filter(headerNeedsConfirm).length;
  state.rows.forEach((r, i) => {
    const ex = r.extra || {};
    const discount = ex.明細区分 === "値引";
    for (const f of discount ? ["単価"] : LINE_INPUTS) if (cellNeedsWork(r.cells[f], f, discount)) need++;
    if (!discount && (needsConfirm(r.cells["返品理由"]) || !cellValue(r.cells["返品理由"]))) need++;
    if (!discount && !ex.良品不良) need++;
    if (discount && !ex.税率) need++;
  });
  const errors = state.check.errors;
  const st = (state.doc.status && state.doc.status.state) || "確認中";
  let text;
  if (st !== "確認中") text = `この伝票は「${st}」です。`;
  else if (need) text = `あと ${need} か所、確認・入力してください（黄色の欄）。`;
  else if (errors) text = `入力はそろいました。照合のエラーが ${errors} 件あります（上の「合計と照合」を見てください）。`;
  else text = "確認はすべて終わりました。確定できます。";
  $("work-progress").textContent = text;
  $("work-progress").className = "work-progress " + (st !== "確認中" ? "done" : need || errors ? "todo" : "ready");
}

// ---------------------------------------------------------------- 自動保存

function scheduleSave() {
  setDirty(true);
  renderProgress();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 700);
}

async function doSave() {
  if (!state.doc || isLocked()) return;
  if (saving) { saveAgain = true; return; }
  saving = true;
  $("confirm-status").textContent = "保存中…";
  try {
    if (headerDirty && state.headerDraft) {
      headerDirty = false;
      await api("PUT", `/api/docs/${state.doc.doc_id}/header`, { ...state.headerDraft.values, 確認済み: state.headerDraft.confirmed });
    }
    const body = {
      rows: state.rows.map((r) => ({
        no_text: r.no_text,
        cells: Object.fromEntries(
          FIELDS.map((f) => [f, { ocr_nos: r.cells[f].ocr_nos, assign: r.cells[f].assign, truth: r.cells[f].truth, truth_blank: r.cells[f].truth_blank, adopt_ocr: !!r.cells[f].adopt_ocr }])
        ),
        extra: r.extra || {},
      })),
    };
    const res = await api("PUT", `/api/docs/${state.doc.doc_id}/pages/${state.page}/review`, body);
    // 入力中に次の書き換えが来ていたら、画面の値を優先する（保存の結果で上書きしない）
    if (!saveAgain) {
      state.rows = res.rows;
      state.pageData.table.source = "saved";
      state.pageData.table.rows = structuredClone(res.rows);
    }
    state.doc.summary = res.summary;
    setDirty(saveAgain);
    $("confirm-status").textContent = `自動で保存しました（${res.saved_at.slice(11)}）`;
    await loadCheck();
  } catch (e) {
    $("confirm-status").textContent = "保存できませんでした。";
    showError(e.message, e.hint);
  } finally {
    saving = false;
    if (saveAgain) { saveAgain = false; doSave(); }
  }
}

// ---------------------------------------------------------------- 伝票の画像と枠

function setHover(h) {
  state.hover = h;
  drawWork(true);
}

function hoverBoxes() {
  const h = state.hover;
  if (!h) return [];
  const byNo = new Map(state.items.map((it) => [it.no, it]));
  let nos = [];
  if (h.kind === "cell" && state.rows[h.row]) nos = state.rows[h.row].cells[h.field] ? state.rows[h.row].cells[h.field].ocr_nos : [];
  if (h.kind === "header") { const p = headerProposal(h.field); nos = p ? p.ocr_nos : []; }
  return nos.map((n) => byNo.get(n)).filter(Boolean);
}

function drawWork(scrollToBox) {
  const cv = $("work-canvas");
  const img = state.image;
  if (!cv || !img) return;
  const wrap = $("work-image");
  const fit = (wrap.clientWidth || 600) / img.width;
  const scale = state.zoom ? fit * state.zoom : fit;
  const w = Math.round(img.width * scale);
  const h = Math.round(img.height * scale);
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const g = cv.getContext("2d");
  g.drawImage(img, 0, 0, w, h);
  const boxes = hoverBoxes();
  if (!boxes.length) return;
  g.lineWidth = 3;
  g.strokeStyle = "#4353ff";
  g.fillStyle = "rgba(67, 83, 255, 0.12)";
  for (const it of boxes) {
    const x = it.box.x * scale - 3, y = it.box.y * scale - 3, bw = it.box.w * scale + 6, bh = it.box.h * scale + 6;
    g.fillRect(x, y, bw, bh);
    g.strokeRect(x, y, bw, bh);
  }
  // 枠が画面の外なら、見える位置までスクロールする
  if (scrollToBox) {
    const b = boxes[0];
    const top = b.box.y * scale, left = b.box.x * scale;
    if (top < wrap.scrollTop || top > wrap.scrollTop + wrap.clientHeight - 40) wrap.scrollTop = Math.max(0, top - wrap.clientHeight / 3);
    if (left < wrap.scrollLeft || left > wrap.scrollLeft + wrap.clientWidth - 40) wrap.scrollLeft = Math.max(0, left - wrap.clientWidth / 3);
  }
}
window.addEventListener("resize", () => requestAnimationFrame(() => drawWork()));
$("zoom-in").addEventListener("click", () => { state.zoom = Math.min(4, (state.zoom || 1) * 1.25); drawWork(); });
$("zoom-out").addEventListener("click", () => { state.zoom = Math.max(0.5, (state.zoom || 1) / 1.25); drawWork(); });
$("zoom-fit").addEventListener("click", () => { state.zoom = null; drawWork(); });

$("accuracy").addEventListener("toggle", () => { if (state.pageData) { renderReview(); renderRows(); draw(); } });

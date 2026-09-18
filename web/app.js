"use strict";

const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="api-token"]').content;
const active = new Set(["queued", "downloading", "tagging"]);
const labels = { queued: "대기 중", downloading: "다운로드 중", tagging: "변환 · 태그 기록", saved: "완료", review: "태그 확인 필요", skipped: "이미 저장됨", error: "실패", cancelled: "취소됨" };
let connected = false;
let submitting = false;
let initialized = false;
let latest = [];
const rows = new Map();
let editing = null;
let editorSerial = 0;
let savingTags = false;

async function api(path, data) {
  const response = await fetch(`/api/${path}`, {
    method: data === undefined ? "GET" : "POST",
    headers: { "X-Minsmix-Token": token, "Content-Type": "application/json" },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "요청을 처리하지 못했어요.");
  return result;
}

function message(text, error = false) {
  const box = $("#form-message");
  box.hidden = !text;
  box.textContent = text;
  box.dataset.error = String(error);
}

function updateSubmit() {
  const count = $("#links").value.split(/\r?\n/).filter((line) => line.trim()).length;
  $("#link-count").textContent = `${count} / 50`;
  $("#link-count").dataset.over = String(count > 50);
  $("#submit").disabled = submitting || !connected || !count || count > 50 || !$("#folder").value.trim();
  $("#submit-label").textContent = submitting ? "목록에 추가하는 중…" : count ? `${count}곡 다운로드 시작` : "다운로드 시작";
}

async function openFolder(root) {
  try { await api("open", { root }); }
  catch (error) { message(error.message, true); }
}

function render(state) {
  latest = state.jobs;
  if (!initialized) {
    let folder;
    try { folder = localStorage.getItem("minsmix-folder"); } catch { /* Storage can be disabled. */ }
    $("#folder").value = folder || state.root;
    $("#choose-folder").hidden = !state.picker;
    initialized = true;
    queueMicrotask(() => loadLibrary(true));
  }
  const ids = new Set(latest.map((job) => job.id));
  for (const [id, row] of rows) {
    if (!ids.has(id)) { row.remove(); rows.delete(id); }
  }
  latest.forEach((job, index) => {
    let row = rows.get(job.id);
    if (!row) {
      row = $("#job-template").content.firstElementChild.cloneNode(true);
      row.dataset.id = job.id;
      rows.set(job.id, row);
      $("#jobs").append(row);
    }
    row.dataset.state = job.status;
    row.querySelector(".job-number").textContent = String(index + 1).padStart(2, "0");
    row.querySelector(".job-title").textContent = job.title || "가져온 오디오";
    const percent = job.status === "downloading" && job.progress !== null ? ` ${job.progress}%` : "";
    row.querySelector(".job-status").textContent = labels[job.status] + percent;
    const editable = Boolean(job.path) && !active.has(job.status);
    row.querySelector(".job-status").disabled = !editable;
    row.querySelector(".job-edit").hidden = !editable;
    row.querySelector(".job-edit").textContent = job.status === "review" ? "태그 확인하기" : "태그 수정";
    const service = job.source_platform === "soundcloud" ? "SoundCloud" : "YouTube";
    const detail = job.status === "queued" ? "앞의 곡이 끝나면 자동으로 시작해요" : job.status === "tagging" ? "MP3로 변환하고 메타데이터를 기록하고 있어요" : job.status === "review" ? "원본을 확인하고 태그를 저장해 주세요" : job.status === "skipped" ? "같은 곡의 파일이 있어 건너뛰었어요" : job.status === "cancelled" ? "다운로드 전 대기 목록에서 취소했어요" : job.artist || (job.status === "downloading" ? `${service}에서 최상의 오디오 소스를 가져오는 중` : "");
    row.querySelector(".job-detail").textContent = detail;
    const progress = row.querySelector(".job-progress");
    progress.hidden = !["downloading", "tagging"].includes(job.status);
    if (job.progress === null) progress.removeAttribute("value");
    else progress.value = job.progress;
    const error = row.querySelector(".job-error");
    error.textContent = job.error;
    error.hidden = !job.error;
    const link = row.querySelector(".source-link");
    link.href = job.url;
    link.hidden = !job.url;
    link.setAttribute("aria-label", `${job.title} 원본 링크 열기`);
    row.querySelector(".job-open").hidden = !job.path;
    row.querySelector(".job-retry").hidden = !["error", "cancelled"].includes(job.status);
  });
  const running = latest.filter((job) => active.has(job.status)).length;
  const saved = latest.filter((job) => ["saved", "review", "skipped"].includes(job.status)).length;
  const errors = latest.filter((job) => job.status === "error").length;
  const review = latest.filter((job) => job.status === "review").length;
  $("#job-count").textContent = latest.length;
  $("#empty-state").hidden = latest.length > 0;
  $("#queue-summary").textContent = !latest.length ? "아직 추가한 곡이 없어요" : `${saved}곡 준비됨${running ? ` · ${running}곡 진행 예정 / 진행 중` : ""}${errors ? ` · ${errors}곡 실패` : ""}${review ? ` · ${review}곡 태그 확인` : ""}`;
  $("#cancel-waiting").hidden = !latest.some((job) => job.status === "queued");
  $("#clear-finished").hidden = !latest.some((job) => !active.has(job.status));
  updateSubmit();
}

async function refresh() {
  try {
    const state = await api("state");
    connected = true;
    render(state);
    $("#connection-text").textContent = "로컬 연결됨";
  } catch {
    connected = false;
    $("#connection-text").textContent = "연결 끊김 · 실행 창을 확인하세요";
  }
  $("#connection").dataset.connected = String(connected);
  updateSubmit();
}

async function poll() {
  await refresh();
  setTimeout(poll, latest.some((job) => active.has(job.status)) ? 800 : 2500);
}

$("#links").addEventListener("input", updateSubmit);
$("#folder").addEventListener("input", () => { updateSubmit(); rememberFolder(); });
function rememberFolder() {
  try { localStorage.setItem("minsmix-folder", $("#folder").value); } catch { /* Optional preference. */ }
}
$("#folder").addEventListener("change", rememberFolder);
$("#import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("#submit").disabled) return;
  submitting = true;
  updateSubmit();
  const input = $("#links").value;
  try {
    const result = await api("add", { links: input, root: $("#folder").value, browser_cookies: $("#browser-cookies").value || null });
    if ($("#links").value === input) $("#links").value = result.invalid.join("\n");
    rememberFolder();
    const notices = [`${result.accepted}곡을 다운로드 목록에 추가했어요.`];
    if (result.duplicates) notices.push(`중복 링크 ${result.duplicates}개는 건너뛰었어요.`);
    if (result.invalid.length) notices.push(`확인이 필요한 링크 ${result.invalid.length}개는 입력창에 남겨뒀어요. YouTube 영상 또는 SoundCloud 단일 트랙 링크인지 확인해 주세요.`);
    message(notices.join("\n"), result.invalid.length > 0);
    await refresh();
  } catch (error) { message(error.message, true); }
  finally { submitting = false; updateSubmit(); }
});

$("#choose-folder").addEventListener("click", async () => {
  const button = $("#choose-folder");
  button.disabled = true;
  button.textContent = "폴더 선택 중…";
  try {
    const result = await api("choose-folder", {});
    if (result.root) { $("#folder").value = result.root; rememberFolder(); updateSubmit(); }
  } catch (error) { message(error.message, true); }
  finally { button.disabled = false; button.textContent = "폴더 선택"; }
});
$("#open-folder").addEventListener("click", () => openFolder($("#folder").value));
$("#cancel-waiting").addEventListener("click", async () => {
  try { await api("cancel", {}); message("대기 중인 항목을 취소했어요. 진행 중인 곡은 끝까지 처리해요."); await refresh(); }
  catch (error) { message(error.message, true); }
});
$("#clear-finished").addEventListener("click", async () => {
  try { await api("clear", {}); message("완료 목록을 비웠어요. 저장한 파일은 그대로예요."); await refresh(); }
  catch (error) { message(error.message, true); }
});
$("#jobs").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const job = latest.find((item) => item.id === button.closest(".job").dataset.id);
  if (!job) return;
  if (button.classList.contains("job-edit") || button.classList.contains("job-status")) {
    await openEditor(job);
  } else if (button.classList.contains("job-open")) {
    await openFolder(job.path.slice(0, job.path.lastIndexOf("/")));
  } else if (button.classList.contains("job-retry")) {
    button.disabled = true;
    try { await api("retry", { id: job.id, browser_cookies: $("#browser-cookies").value || null }); await refresh(); }
    catch (error) { message(error.message, true); }
    finally { button.disabled = false; }
  }
});

async function loadLibrary(quiet = false) {
  const button = $("#load-library");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "불러오는 중…";
  try {
    const result = await api("library", {root: $("#folder").value});
    if (!quiet) message(`라이브러리 ${result.count}곡을 불러왔어요. ‘태그 확인하기’ 또는 ‘태그 수정’을 눌러보세요.`);
    await refresh();
  } catch (error) { if (!quiet) message(error.message, true); }
  finally { button.disabled = false; button.textContent = "라이브러리 불러오기"; }
}
$("#load-library").addEventListener("click", () => loadLibrary());

function editorError(text) {
  $("#tag-error").textContent = text;
  $("#tag-error").hidden = !text;
}

async function openEditor(job) {
  const request = ++editorSerial;
  editing = null;
  $("#tag-form").reset();
  editorError("");
  $("#tag-fields").disabled = true;
  $("#tag-save").disabled = true;
  $("#tag-loading").hidden = false;
  $("#tag-original").textContent = "";
  $("#tag-filename").textContent = "";
  $("#tag-source").hidden = true;
  $("#tag-csv-note").hidden = true;
  $("#tag-dialog").showModal();
  try {
    const sourceId = job.source_id || (job.url ? new URL(job.url).searchParams.get("v") || "" : "");
    const result = await api("tag-read", {root: job.root, path: job.path, source_id: sourceId, source_platform: job.source_platform || ""});
    if (request !== editorSerial || !$("#tag-dialog").open) return;
    editing = result;
    for (const field of ["artist", "title", "album", "genre"]) $(`#tag-${field}`).value = result.tags[field] || "";
    $("#tag-original").textContent = result.video_title || "저장된 원본 제목이 없어요. 파일명과 보유한 곡 정보를 확인해 주세요.";
    $("#tag-filename").textContent = result.path.split("/").pop();
    $("#tag-source").hidden = !result.source_url;
    $("#tag-source").href = result.source_url;
    $("#tag-csv-note").hidden = !result.csv_pending;
    $("#tag-fields").disabled = false;
    $("#tag-save").disabled = false;
    $("#tag-artist").focus();
    await refresh();
  } catch (error) { if (request === editorSerial) editorError(error.message); }
  finally { if (request === editorSerial) $("#tag-loading").hidden = true; }
}

$("#tag-close").addEventListener("click", () => { if (!savingTags) $("#tag-dialog").close(); });
$("#tag-dialog").addEventListener("cancel", (event) => { if (savingTags) event.preventDefault(); });
$("#tag-dialog").addEventListener("close", () => { ++editorSerial; editing = null; });
$("#tag-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editing || savingTags) return;
  const tags = Object.fromEntries(["artist", "title", "album", "genre"].map((field) => [field, $(`#tag-${field}`).value.trim()]));
  if (!tags.artist || !tags.title) { editorError("아티스트와 제목을 입력해 주세요."); return; }
  savingTags = true;
  $("#tag-save").disabled = true;
  $("#tag-close").disabled = true;
  $("#tag-fields").disabled = true;
  $("#tag-save").textContent = "저장 중…";
  editorError("");
  try {
    const result = await api("tag-save", {root: editing.root, path: editing.path, source_id: editing.source_id, source_platform: editing.source_platform, revision: editing.revision, tags});
    $("#tag-dialog").close();
    message(`‘${result.tags.title}’ 태그를 MP3와 CSV에 저장했어요.${result.path !== result.previous_path ? " tracks 폴더로 옮겼어요." : ""} 수정 전 파일은 .tag-backups에 보관했어요.`);
    await refresh();
  } catch (error) { editorError(error.message); }
  finally {
    savingTags = false;
    $("#tag-save").disabled = false;
    $("#tag-close").disabled = false;
    $("#tag-fields").disabled = false;
    $("#tag-save").textContent = "확인하고 저장";
  }
});
poll();

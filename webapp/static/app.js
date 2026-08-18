const state = {
  dashboard: null,
  filter: "all",
  selectedClipId: null,
  uploadFile: null,
  busy: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  $("#toasts").append(item);
  setTimeout(() => item.remove(), 4500);
}

function fmtDate(value, includeDate = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    ...(includeDate ? { day: "2-digit", month: "short" } : {}),
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function titleFromCaption(caption) {
  return String(caption || "Без названия").split("\n").find(Boolean) || "Без названия";
}

function renderDashboard(data) {
  state.dashboard = data;
  $("#pendingMetric").textContent = data.stats.pending;
  $("#postedMetric").textContent = data.stats.posted;
  $("#nextMetric").textContent = data.stats.next_publish ? fmtDate(data.stats.next_publish, false) : "Выкл";
  $("#automationMetric").textContent = data.system.scheduler ? "Активна" : "Пауза";
  $("#queryInput").value ||= data.pipeline.query;
  $("#hashtags").innerHTML = data.pipeline.hashtags.map(tag => `<span>${escapeHtml(tag)}</span>`).join("");

  const systemOk = data.system.ollama && data.system.scheduler && data.platforms.youtube.connected;
  $("#systemLabel").textContent = systemOk ? "Система готова" : "Нужна проверка";
  $("#systemLabel").closest(".live-pill").classList.toggle("off", !systemOk);

  const yt = data.platforms.youtube;
  $("#youtubeDetail").textContent = yt.connected
    ? `OAuth активен · ${yt.post_times.join(", ")} · ${privacyLabel(yt.privacy)}`
    : "OAuth не подключён";
  $("#youtubeCard .switch").classList.toggle("on", yt.enabled && yt.connected);

  const tg = data.platforms.telegram;
  $("#telegramDetail").textContent = tg.connected ? "Бот и канал подключены" : "Добавьте токен бота и ID канала";
  $("#telegramSwitch").classList.toggle("on", tg.enabled && tg.connected);

  $("#scheduleEnabled").checked = yt.enabled;
  $("#scheduleTimes").value = yt.post_times.join(", ");
  $("#schedulePrivacy").value = yt.privacy;
  $("#lastUpdated").textContent = `Обновлено ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;

  renderJobs(data.jobs);
  renderQueue(data.clips);
}

function privacyLabel(value) {
  return { private: "приватно", unlisted: "по ссылке", public: "публично" }[value] || value;
}

function renderJobs(jobs) {
  $("#jobCount").textContent = jobs.length;
  if (!jobs.length) {
    $("#jobsList").innerHTML = `<div class="empty-state compact"><span>◎</span><p>Новых задач пока нет</p></div>`;
    return;
  }
  $("#jobsList").innerHTML = jobs.slice(0, 6).map(job => `
    <article class="job-item ${escapeHtml(job.status)}">
      <div class="job-top"><b title="${escapeHtml(job.title)}">${escapeHtml(job.title)}</b><span>${jobStatus(job.status)}</span></div>
      <div class="job-progress"><i style="width:${Math.max(3, job.progress)}%"></i></div>
      <div class="job-bottom">
        <small>${escapeHtml(job.message || "В очереди")}</small>
        ${["queued", "running"].includes(job.status) ? `<button type="button" data-job-cancel="${escapeHtml(job.id)}" title="Отменить только эту задачу">Отменить</button>` : ""}
      </div>
    </article>
  `).join("");
}

function jobStatus(status) {
  return { queued: "очередь", running: "в работе", cancelling: "останавливаю", cancelled: "отменено", succeeded: "готово", failed: "ошибка" }[status] || status;
}

$("#jobsList").addEventListener("click", async event => {
  const button = event.target.closest("button[data-job-cancel]");
  if (!button) return;
  const job = state.dashboard?.jobs.find(item => item.id === button.dataset.jobCancel);
  if (!job || !window.confirm(`Отменить только задачу «${job.title}»?`)) return;
  button.disabled = true;
  try {
    await api(`/api/jobs/${job.id}/cancel`, {method: "POST"});
    toast("Останавливаю выбранный процесс");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
});

function renderQueue(clips) {
  const filtered = clips.filter(clip => state.filter === "all" || clip.status === state.filter);
  if (!filtered.length) {
    $("#queueList").innerHTML = `<div class="empty-state"><span>◌</span><h3>В этом разделе пока пусто</h3></div>`;
    return;
  }
  $("#queueList").innerHTML = filtered.map(clip => {
    const title = titleFromCaption(clip.caption);
    const actions = clip.status === "pending" ? `
      <button data-action="preview" data-id="${clip.id}">Просмотр</button>
      <button data-action="edit" data-id="${clip.id}">Текст</button>
      <button class="publish" data-action="publish" data-id="${clip.id}">YouTube ↗</button>
      <button data-action="reject" data-id="${clip.id}">Отклонить</button>
    ` : clip.file_exists ? `<button data-action="preview" data-id="${clip.id}">Просмотр</button>` : "";
    return `
      <article class="queue-row" data-status="${escapeHtml(clip.status)}">
        <div class="video-info">
          <button class="video-thumb" data-action="preview" data-id="${clip.id}" aria-label="Открыть ${escapeHtml(title)}"></button>
          <div class="video-copy"><b title="${escapeHtml(title)}">${escapeHtml(title)}</b><small>#${clip.id} · ${escapeHtml(clip.filename)} · ${clip.size_mb} МБ</small></div>
        </div>
        <span class="status-badge ${escapeHtml(clip.status)}">${clipStatus(clip.status)}</span>
        <span class="queue-date">${fmtDate(clip.created_at)}</span>
        <div class="row-actions">${actions}</div>
      </article>`;
  }).join("");
}

function clipStatus(status) {
  return { pending: "На проверке", posted: "Опубликовано", rejected: "Отклонено" }[status] || status;
}

async function refresh({ silent = false } = {}) {
  if (state.busy) return;
  state.busy = true;
  $("#refreshButton").style.transform = "rotate(120deg)";
  try {
    const data = await api("/api/dashboard");
    renderDashboard(data);
  } catch (error) {
    if (!silent) toast(error.message, "error");
    $("#systemLabel").textContent = "Панель недоступна";
    $("#systemLabel").closest(".live-pill").classList.add("off");
  } finally {
    state.busy = false;
    $("#refreshButton").style.transform = "";
  }
}

$("#doramaForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const sourceMode = new FormData(event.currentTarget).get("sourceMode");
  button.disabled = true;
  button.firstChild.textContent = "Добавляю в очередь ";
  try {
    const endpoint = sourceMode === "licensed" ? "/api/jobs/licensed-dorama" : "/api/jobs/dorama";
    await api(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: $("#queryInput").value.trim(),
        focus: "динамичный последовательный пересказ без раскрытия финала",
        limit: Number($("#limitInput").value),
      }),
    });
    toast(sourceMode === "licensed"
      ? "Расширенный поиск Creative Commons и производство выпуска запущены."
      : "Мультиплатформенный радар запущен.");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.firstChild.textContent = sourceMode === "licensed" ? "Найти и собрать выпуск " : "Создать обзор без скачивания ";
  }
});

$$('input[name="sourceMode"]').forEach(input => input.addEventListener("change", () => {
  const licensed = $('input[name="sourceMode"]:checked').value === "licensed";
  $("#licenseHint").textContent = licensed
    ? "Поиск идёт в YouTube, Wikimedia Commons и Internet Archive. Скачивание — только CC BY/CC BY-SA/CC0, public domain или разрешённый канал."
    : "Ищет параллельно в YouTube, Bilibili, Dailymotion, Internet Archive и Wikimedia Commons; использует только названия, счётчики и ссылки.";
  $("#doramaForm button[type=submit]").firstChild.textContent = licensed ? "Найти и собрать выпуск " : "Создать обзор без скачивания ";
}));

function setUploadFile(file) {
  state.uploadFile = file || null;
  $("#uploadTitle").textContent = file ? file.name : "Перетащите серию или трейлер";
  $("#uploadHint").textContent = file ? `${(file.size / 1024 / 1024).toFixed(1)} МБ · готово к анализу` : "ваш файл с правом использования · MP4, MOV, MKV, WEBM";
  $("#clipButton").disabled = !file || !$("#rightsConfirm").checked;
}

$("#videoFile").addEventListener("change", event => setUploadFile(event.target.files[0]));
$("#rightsConfirm").addEventListener("change", () => setUploadFile(state.uploadFile));
["dragenter", "dragover"].forEach(name => $("#dropzone").addEventListener(name, event => { event.preventDefault(); $("#dropzone").classList.add("dragging"); }));
["dragleave", "drop"].forEach(name => $("#dropzone").addEventListener(name, event => { event.preventDefault(); $("#dropzone").classList.remove("dragging"); }));
$("#dropzone").addEventListener("drop", event => setUploadFile(event.dataTransfer.files[0]));

$("#uploadForm").addEventListener("submit", async event => {
  event.preventDefault();
  if (!state.uploadFile) return;
  const button = $("#clipButton");
  button.disabled = true;
  button.firstChild.textContent = "Загружаю файл ";
  try {
    const form = new FormData();
    form.append("file", state.uploadFile);
    const uploaded = await api("/api/uploads", { method: "POST", body: form });
    await api("/api/jobs/episode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: uploaded.filename,
        focus: $("#episodeFocus").value.trim(),
        rights_confirmed: $("#rightsConfirm").checked,
      }),
    });
    toast("Файл загружен. Пятиминутный пересказ запущен.");
    setUploadFile(null);
    $("#videoFile").value = "";
    $("#rightsConfirm").checked = false;
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = !state.uploadFile || !$("#rightsConfirm").checked;
    button.firstChild.textContent = "Сделать пересказ на 5 минут ";
  }
});

$("#queueList").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id);
  const clip = state.dashboard.clips.find(item => item.id === id);
  if (!clip) return;
  const action = button.dataset.action;
  if (action === "preview") {
    $("#previewTitle").textContent = titleFromCaption(clip.caption);
    $("#previewVideo").src = `/api/clips/${id}/video`;
    $("#previewDialog").showModal();
  }
  if (action === "publish") {
    state.selectedClipId = id;
    $("#publishTitle").textContent = titleFromCaption(clip.caption);
    $("#publishDialog").showModal();
  }
  if (action === "edit") {
    const caption = window.prompt("Заголовок, описание и хештеги:", clip.caption);
    if (caption && caption.trim() !== clip.caption) {
      try {
        await api(`/api/clips/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ caption: caption.trim() }) });
        toast("Текст и хештеги обновлены");
        await refresh();
      } catch (error) { toast(error.message, "error"); }
    }
  }
  if (action === "reject" && window.confirm(`Отклонить выпуск «${titleFromCaption(clip.caption)}»? Файл будет перенесён в rejected.`)) {
    try {
      await api(`/api/clips/${id}/reject`, { method: "POST" });
      toast("Выпуск отклонён и убран из очереди");
      await refresh();
    } catch (error) { toast(error.message, "error"); }
  }
});

$("#publishForm").addEventListener("submit", async event => {
  event.preventDefault();
  const privacy = new FormData(event.currentTarget).get("privacy");
  if (privacy === "public" && !window.confirm("Ролик сразу станет публичным и может попасть к подписчикам. Продолжить?")) return;
  try {
    await api(`/api/clips/${state.selectedClipId}/publish/youtube`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ privacy }) });
    $("#publishDialog").close();
    toast(`Загрузка на YouTube запущена: ${privacyLabel(privacy)}`);
    await refresh();
  } catch (error) { toast(error.message, "error"); }
});

$("#closePreview").addEventListener("click", () => $("#previewDialog").close());
$("#previewDialog").addEventListener("close", () => { $("#previewVideo").pause(); $("#previewVideo").removeAttribute("src"); });
$$('[data-close-dialog]').forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));

$$('[data-filter]').forEach(button => button.addEventListener("click", () => {
  state.filter = button.dataset.filter;
  $$('[data-filter]').forEach(item => item.classList.toggle("active", item === button));
  renderQueue(state.dashboard?.clips || []);
}));

$("#settingsButton").addEventListener("click", () => $("#settingsDialog").showModal());
$("#settingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const postTimes = $("#scheduleTimes").value.split(",").map(item => item.trim()).filter(Boolean);
  const privacy = $("#schedulePrivacy").value;
  if (privacy === "public" && !window.confirm("Расписание будет публиковать одобренные ролики публично. Вы уверены?")) return;
  try {
    await api("/api/settings/youtube", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: $("#scheduleEnabled").checked, post_times: postTimes, privacy_status: privacy }),
    });
    $("#settingsDialog").close();
    toast("Расписание YouTube сохранено");
    await refresh();
  } catch (error) { toast(error.message, "error"); }
});

$("#doctorButton").addEventListener("click", async () => {
  try { await api("/api/jobs/doctor", { method: "POST" }); toast("Проверка системы запущена"); await refresh(); }
  catch (error) { toast(error.message, "error"); }
});
$("#runSchedulerButton").addEventListener("click", async () => {
  try { await api("/api/jobs/scheduler", { method: "POST" }); toast("Расписание проверяется"); await refresh(); }
  catch (error) { toast(error.message, "error"); }
});
$("#refreshButton").addEventListener("click", () => refresh());

refresh();
setInterval(() => refresh({ silent: true }), 5000);

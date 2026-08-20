const state = {
  dashboard: null,
  filter: "all",
  selectedClipId: null,
  uploadFile: null,
  pendingUpload: null,
  editorDuration: 0,
  busy: false,
  csrfToken: null,
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
  const method = String(options.method || "GET").toUpperCase();
  const requestOptions = { ...options };
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    if (!state.csrfToken) {
      const session = await fetch("/api/session", { cache: "no-store" });
      if (!session.ok) throw new Error("Не удалось открыть защищённую API-сессию");
      state.csrfToken = (await session.json()).csrf_token;
    }
    const headers = new Headers(requestOptions.headers || {});
    headers.set("X-Dorama-CSRF", state.csrfToken);
    requestOptions.headers = headers;
  }
  const response = await fetch(path, requestOptions);
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
  $("#telegramDetail").textContent = tg.running
    ? "Бот запущен вместе с сервером"
    : (tg.connected ? "Настройки найдены, бот не запущен" : "Добавьте токен бота и ID канала");
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
  $("#clearJobsButton").disabled = !jobs.some(job =>
    ["succeeded", "failed", "attention", "cancelled"].includes(job.status)
  );
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
  return {
    queued: "очередь",
    running: "в работе",
    cancelling: "останавливаю",
    paused: "приостановлено",
    attention: "нужна проверка",
    cancelled: "отменено",
    succeeded: "готово",
    failed: "ошибка",
  }[status] || status;
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

$("#clearJobsButton").addEventListener("click", () => {
  $("#clearJobsDialog").showModal();
});

$("#clearJobsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#clearJobsButton");
  button.disabled = true;
  try {
    const result = await api("/api/jobs/history", {method: "DELETE"});
    $("#clearJobsDialog").close();
    toast(result.cleared
      ? `Удалено завершённых процессов: ${result.cleared}`
      : "Завершённых процессов для очистки нет");
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
      <button class="delete" data-action="delete" data-id="${clip.id}">Удалить</button>
    ` : clip.status === "publishing" ? `
      ${clip.file_exists ? `<button data-action="preview" data-id="${clip.id}">Просмотр</button>` : ""}
    ` : `
      ${clip.file_exists ? `<button data-action="preview" data-id="${clip.id}">Просмотр</button>` : ""}
      <button class="delete" data-action="delete" data-id="${clip.id}">Удалить</button>
    `;
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
  return {
    pending: "На проверке",
    publishing: "Публикуется",
    posted: "Опубликовано",
    rejected: "Отклонено",
  }[status] || status;
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

function episodeMode() {
  return $('input[name="episodeMode"]:checked')?.value || "recap";
}

function formatClock(value) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function updateUploadMode() {
  const translate = episodeMode() === "translate";
  const focusField = $("#episodeFocus").closest("label");
  focusField.hidden = translate;
  $("#episodeFocus").disabled = translate;
  $("#uploadButtonLabel").textContent = translate ? "Перевести выбранный фрагмент" : "Сделать пересказ на 5 минут";
  $("#episodeFeatures").innerHTML = translate
    ? `<li><span>01</span><div><b>Точная хронология</b><small>без перестановки и пересказа сцен</small></div></li>
       <li><span>02</span><div><b>Фрагмент до 5 минут</b><small>для длинного видео откроется таймлайн</small></div></li>
       <li><span>03</span><div><b>Русская озвучка</b><small>перевод речи, QA и субтитры</small></div></li>`
    : `<li><span>01</span><div><b>Сюжетный таймлайн</b><small>распознаёт речь на любом языке</small></div></li>
       <li><span>02</span><div><b>Ключевые сцены</b><small>монтаж ровно на пять минут</small></div></li>
       <li><span>03</span><div><b>Русский сторителлинг</b><small>озвучка, QA и субтитры</small></div></li>`;
}

function setUploadFile(file) {
  if (
    state.pendingUpload
    && (!file || state.pendingUpload.source_name !== file.name || state.pendingUpload.source_size !== file.size)
  ) {
    state.pendingUpload = null;
  }
  state.uploadFile = file || null;
  $("#uploadTitle").textContent = file ? file.name : "Перетащите серию или трейлер";
  $("#uploadHint").textContent = file ? `${(file.size / 1024 / 1024).toFixed(1)} МБ · готово к анализу` : "ваш файл с правом использования · MP4, MOV, MKV, WEBM";
  $("#clipButton").disabled = !file || !$("#rightsConfirm").checked;
}

$("#videoFile").addEventListener("change", event => setUploadFile(event.target.files[0]));
$("#rightsConfirm").addEventListener("change", () => setUploadFile(state.uploadFile));
$$('input[name="episodeMode"]').forEach(input => input.addEventListener("change", updateUploadMode));
["dragenter", "dragover"].forEach(name => $("#dropzone").addEventListener(name, event => { event.preventDefault(); $("#dropzone").classList.add("dragging"); }));
["dragleave", "drop"].forEach(name => $("#dropzone").addEventListener(name, event => { event.preventDefault(); $("#dropzone").classList.remove("dragging"); }));
$("#dropzone").addEventListener("drop", event => setUploadFile(event.dataTransfer.files[0]));

function clearUploadForm() {
  state.pendingUpload = null;
  setUploadFile(null);
  $("#videoFile").value = "";
  $("#rightsConfirm").checked = false;
}

async function submitEpisodeJob(uploaded, startSeconds = null, endSeconds = null) {
  const mode = uploaded.mode;
  const payload = {
    filename: uploaded.filename,
    focus: mode === "recap" ? uploaded.focus : "",
    rights_confirmed: true,
    mode,
  };
  if (mode === "translate") {
    payload.start_seconds = startSeconds ?? 0;
    payload.end_seconds = endSeconds ?? uploaded.duration_seconds;
  }
  await api("/api/jobs/episode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function updateEditorSelection(changed = "start") {
  const duration = state.editorDuration;
  let start = Number($("#editorStart").value);
  let end = Number($("#editorEnd").value);
  if (!Number.isFinite(start)) start = 0;
  if (!Number.isFinite(end)) end = Math.min(300, duration);
  start = Math.max(0, Math.min(start, Math.max(0, duration - 10)));
  end = Math.max(10, Math.min(end, duration));
  if (changed === "start") {
    start = Math.min(start, end - 10);
    if (end - start > 300) end = Math.min(duration, start + 300);
  } else {
    end = Math.max(end, start + 10);
    if (end - start > 300) start = Math.max(0, end - 300);
  }
  start = Math.max(0, start);
  end = Math.min(duration, end);

  $("#editorStart").value = start.toFixed(1);
  $("#editorEnd").value = end.toFixed(1);
  $("#editorStartRange").value = String(start);
  $("#editorEndRange").value = String(end);
  const left = duration ? (start / duration) * 100 : 0;
  const width = duration ? ((end - start) / duration) * 100 : 0;
  $("#editorSelection").style.left = `${left}%`;
  $("#editorSelection").style.width = `${width}%`;
  $("#editorSelectedDuration").textContent = formatClock(end - start);
  $("#startTranslationButton").disabled = end - start < 10 || end - start > 300.01;
}

function openTranslationEditor(uploaded) {
  state.pendingUpload = uploaded;
  state.editorDuration = Number(uploaded.duration_seconds);
  const end = Math.min(300, state.editorDuration);
  $("#editorFilename").textContent = `${uploaded.display_name || uploaded.filename} · ${formatClock(state.editorDuration)}. Выберите непрерывный фрагмент до пяти минут.`;
  $("#editorDurationLabel").textContent = formatClock(state.editorDuration);
  ["#editorStartRange", "#editorEndRange"].forEach(selector => {
    $(selector).max = String(state.editorDuration);
  });
  $("#editorStart").max = String(Math.max(0, state.editorDuration - 10));
  $("#editorEnd").max = String(state.editorDuration);
  $("#editorStart").value = "0.0";
  $("#editorEnd").value = end.toFixed(1);
  $("#editorVideo").src = `/api/uploads/${encodeURIComponent(uploaded.filename)}/video`;
  $("#editorPlayhead").textContent = "00:00";
  updateEditorSelection("start");
  $("#translationEditorDialog").showModal();
}

$("#uploadForm").addEventListener("submit", async event => {
  event.preventDefault();
  if (!state.uploadFile) return;
  const button = $("#clipButton");
  button.disabled = true;
  $("#uploadButtonLabel").textContent = "Загружаю и проверяю файл";
  try {
    if (
      episodeMode() === "translate"
      && state.pendingUpload?.editor_required
      && state.pendingUpload.source_name === state.uploadFile.name
      && state.pendingUpload.source_size === state.uploadFile.size
    ) {
      openTranslationEditor(state.pendingUpload);
      return;
    }
    const form = new FormData();
    form.append("file", state.uploadFile);
    const uploaded = await api("/api/uploads", { method: "POST", body: form });
    const prepared = {
      ...uploaded,
      mode: episodeMode(),
      focus: $("#episodeFocus").value.trim(),
      source_name: state.uploadFile.name,
      source_size: state.uploadFile.size,
    };
    if (prepared.mode === "translate" && prepared.editor_required) {
      openTranslationEditor(prepared);
      toast("Видео длиннее пяти минут — выберите фрагмент на таймлайне.");
      return;
    }
    await submitEpisodeJob(prepared);
    toast(prepared.mode === "translate"
      ? "Файл загружен. Прямой перевод всего фрагмента запущен."
      : "Файл загружен. Пятиминутный пересказ запущен.");
    clearUploadForm();
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = !state.uploadFile || !$("#rightsConfirm").checked;
    updateUploadMode();
  }
});

$("#editorStartRange").addEventListener("input", event => {
  $("#editorStart").value = event.target.value;
  updateEditorSelection("start");
  $("#editorVideo").currentTime = Number($("#editorStart").value);
});
$("#editorEndRange").addEventListener("input", event => {
  $("#editorEnd").value = event.target.value;
  updateEditorSelection("end");
  $("#editorVideo").currentTime = Number($("#editorEnd").value);
});
$("#editorStart").addEventListener("change", () => updateEditorSelection("start"));
$("#editorEnd").addEventListener("change", () => updateEditorSelection("end"));
$("#setEditorStart").addEventListener("click", () => {
  $("#editorStart").value = String($("#editorVideo").currentTime);
  updateEditorSelection("start");
});
$("#setEditorEnd").addEventListener("click", () => {
  $("#editorEnd").value = String($("#editorVideo").currentTime);
  updateEditorSelection("end");
});
$("#editorVideo").addEventListener("timeupdate", event => {
  $("#editorPlayhead").textContent = formatClock(event.target.currentTime);
});
$("#translationEditorForm").addEventListener("submit", async event => {
  event.preventDefault();
  if (!state.pendingUpload) return;
  const button = $("#startTranslationButton");
  button.disabled = true;
  button.textContent = "Добавляю в очередь…";
  try {
    await submitEpisodeJob(
      state.pendingUpload,
      Number($("#editorStart").value),
      Number($("#editorEnd").value),
    );
    $("#translationEditorDialog").close();
    clearUploadForm();
    toast("Прямой перевод выбранного фрагмента запущен.");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.textContent = "Перевести фрагмент";
    button.disabled = false;
  }
});
$("#translationEditorDialog").addEventListener("close", () => {
  $("#editorVideo").pause();
  $("#editorVideo").removeAttribute("src");
  $("#editorVideo").load();
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
  if (action === "delete" && window.confirm(`Удалить выпуск «${titleFromCaption(clip.caption)}» и его локальные файлы без возможности восстановления?`)) {
    button.disabled = true;
    try {
      const result = await api(`/api/clips/${id}`, { method: "DELETE" });
      toast(result.cleanup_pending
        ? "Выпуск удалён; временная копия осталась в скрытой папке .trash"
        : "Выпуск и локальные файлы удалены");
      await refresh();
    } catch (error) {
      button.disabled = false;
      toast(error.message, "error");
    }
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

async function loadPipelineSettings() {
  try {
    const s = await api("/api/settings/pipeline");
    $("#setWhisperModel").value = s.whisper_model;
    $("#setWhisperDevice").value = s.whisper_device;
    $("#setOllamaModel").value = s.ollama_model;
    $("#setVoice").value = s.voice;
    $("#setRate").value = s.rate;
    $("#setPitch").value = s.pitch;
    $("#setDuration").value = String(s.target_duration_seconds);
    $("#setSceneCount").value = String(s.scene_count);
    $("#setVolume").value = s.original_audio_volume;
    $("#setVolumeLabel").textContent = String(s.original_audio_volume);
    $("#setScriptWords").value = s.target_script_words;
    $("#setRequireReview").checked = s.require_review;
    const sources = new Set(s.search_sources);
    $$('input[name="source"]', $("#setSourcesGroup")).forEach(cb => { cb.checked = sources.has(cb.value); });
  } catch (_) {}
}

$("#setVolume").addEventListener("input", () => { $("#setVolumeLabel").textContent = $("#setVolume").value; });

$$('[data-settings-tab]').forEach(tab => tab.addEventListener("click", () => {
  $$('[data-settings-tab]').forEach(t => t.classList.toggle("active", t === tab));
  $$('[data-settings-section]').forEach(s => s.classList.toggle("active", s.dataset.settingsSection === tab.dataset.settingsTab));
}));

$("#openSettingsButton").addEventListener("click", async () => {
  await loadPipelineSettings();
  $("#settingsDialog").showModal();
});

$("#settingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const sources = [...$$('input[name="source"]:checked', $("#setSourcesGroup"))].map(cb => cb.value);
  if (!sources.length) { toast("Выберите хотя бы один источник поиска", "error"); return; }
  try {
    await api("/api/settings/pipeline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        whisper_model: $("#setWhisperModel").value,
        whisper_device: $("#setWhisperDevice").value,
        ollama_model: $("#setOllamaModel").value,
        search_sources: sources,
        voice: $("#setVoice").value,
        rate: $("#setRate").value,
        pitch: $("#setPitch").value,
        target_duration_seconds: Number($("#setDuration").value),
        scene_count: Number($("#setSceneCount").value),
        original_audio_volume: Number($("#setVolume").value),
        target_script_words: Number($("#setScriptWords").value),
        require_review: $("#setRequireReview").checked,
      }),
    });
    $("#settingsDialog").close();
    toast("Настройки пайплайна сохранены");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  }
});

$("#ytSettingsButton").addEventListener("click", () => $("#ytScheduleDialog").showModal());
$("#ytScheduleForm").addEventListener("submit", async event => {
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
    $("#ytScheduleDialog").close();
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

updateUploadMode();
refresh();
setInterval(() => refresh({ silent: true }), 5000);

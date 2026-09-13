"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const profileFields = ["name", "company", "role", "resume", "jd", "company_notes", "materials", "source_notes", "custom_prompt"];
  const settingInputs = {
    deepseek_model: "deepseek-model", asr_provider: "asr-provider", whisper_model: "whisper-model",
    asr_language: "asr-language", asr_quality: "asr-quality",
    cloud_asr_url: "cloud-asr-url", cloud_asr_model: "cloud-asr-model",
    silence_ms: "silence-ms", min_speech_ms: "min-speech-ms", max_segment_s: "max-segment-s",
    energy_threshold: "energy-threshold", answer_language: "answer-language", answer_style: "answer-style",
  };
  const defaults = { deepseek_model: "deepseek-flash", asr_provider: "local", whisper_model: "base", asr_language: "auto", asr_quality: "balanced", cloud_asr_url: "", cloud_asr_model: "", silence_ms: 700, min_speech_ms: 300, max_segment_s: 12, energy_threshold: .008, answer_language: "auto", answer_style: "concise" };
  const viewNames = { live: "实时面试", prepare: "面试准备", history: "会话记录", settings: "连接与设置" };
  let state = { settings: {}, profiles: [], active_profile_id: null, session: { listening: false, transcripts: [], answers: [] }, capabilities: {} };
  let currentView = "live";
  let editorId = null;
  let profileDirty = false;
  let profileInitialized = false;
  let settingsDirty = false;
  let settingsInitialized = false;
  let selectedAnswerId = null;
  let generatingId = null;
  let feedbackAnswerId = null;
  let archive = null;
  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = 1500;
  let deviceList = [];
  let answerRenderQueued = false;
  let isClosing = false;
  let pendingGeneration = false;
  let overlayOpened = false;
  let overlayOpening = false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function setText(id, text) { $(id).textContent = text == null ? "" : String(text); }
  function cleanSession(session = {}) {
    return { ...session, transcripts: Array.isArray(session.transcripts) ? session.transcripts : [], answers: Array.isArray(session.answers) ? session.answers : [] };
  }
  function activeProfile() { return state.profiles.find((profile) => profile.id === state.active_profile_id); }
  function currentAnswer() { return state.session.answers.find((answer) => answer.id === selectedAnswerId); }
  function dateValue(value) {
    if (!value) return null;
    const result = new Date(typeof value === "number" && value < 100000000000 ? value * 1000 : value);
    return Number.isNaN(result.getTime()) ? null : result;
  }
  function formatTime(value, full = false) {
    const date = dateValue(value);
    if (!date) return full ? "时间未记录" : "刚刚";
    return date.toLocaleString("zh-CN", full ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function latency(ms) {
    const value = Number(ms);
    if (ms === null || ms === undefined || !Number.isFinite(value)) return "";
    return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
  }
  function toast(message, type = "success") {
    if (!message) return;
    const item = el("div", `toast ${type}`, message);
    if (type === "error") item.setAttribute("role", "alert");
    $("toast-region").append(item);
    setTimeout(() => item.remove(), type === "error" ? 9000 : 5200);
  }
  async function api(path, options = {}) {
    const { body, ...rest } = options;
    const request = { credentials: "same-origin", cache: "no-store", ...rest };
    if (body instanceof FormData) request.body = body;
    else if (body !== undefined) { request.body = JSON.stringify(body); request.headers = { "Content-Type": "application/json", ...(request.headers || {}) }; }
    let response;
    try { response = await fetch(path, request); }
    catch { throw new Error("暂时连接不到本机服务，请确认面试伴航仍在运行。"); }
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      let message = data.detail || data.error || `操作未完成（${response.status}）`;
      if (Array.isArray(message)) message = message.map((item) => item.msg || "请检查输入内容").join("；");
      if (response.status === 401 || response.status === 403) message = "本机连接已失效，请通过桌面启动器重新打开面试伴航。";
      const error = new Error(typeof message === "string" ? message : "操作未完成，请检查输入内容。");
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function post(path, body = {}) { return api(path, { method: "POST", body }); }
  async function busy(button, action) {
    if (button.disabled) return;
    button.disabled = true;
    try { return await action(); }
    catch (error) { toast(error.message, "error"); }
    finally { button.disabled = isClosing; }
  }
  async function refreshState() { applyState(await api("/api/state")); }
  function applyState(data) {
    if (data.state && !data.session) data = data.state;
    const priorSessionId = state.session.id;
    state = { ...state, ...data, settings: { ...state.settings, ...(data.settings || {}) }, profiles: Array.isArray(data.profiles) ? data.profiles : state.profiles, session: data.session ? cleanSession(data.session) : state.session };
    if (state.session.id !== priorSessionId) setPartialTranscript("");
    const activeGeneration = state.session.answers.findLast((answer) => ["streaming", "generating", "pending"].includes(answer.status));
    generatingId = activeGeneration?.id || null;
    if (!state.session.answers.some((answer) => answer.id === selectedAnswerId)) selectedAnswerId = state.session.answers.at(-1)?.id || null;
    renderProfiles();
    renderLiveContext();
    renderTranscripts();
    renderAnswer();
    renderAudio();
    renderPlatformHelp();
    if (!profileInitialized) { if (!profileDirty) loadProfile(activeProfile() || null); profileInitialized = true; }
    if (!settingsDirty) fillSettings();
    renderSecretStatus();
    settingsInitialized = true;
    $("auto-answer").checked = Boolean(state.settings.auto_answer);
    setText("auto-answer-note", state.settings.auto_answer ? "检测到完整问题后，自动整理回答思路" : "也可手动选择问题后生成思路");
    if (deviceList.length) $("audio-device").value = state.settings.device_id == null ? "" : String(state.settings.device_id);
    if (state.components?.asr?.message) setText("warmup-status", state.components.asr.message);
    if (state.components?.audio?.message && !generatingId) setText("live-status-message", state.components.audio.message);
  }
  function navigate(view) {
    if (!(view in viewNames)) view = "live";
    currentView = view;
    $$(".view").forEach((node) => { const active = node.id === `view-${view}`; node.hidden = !active; node.classList.toggle("active", active); });
    $$(".nav-item").forEach((node) => { const active = node.dataset.view === view; node.classList.toggle("active", active); if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
    setText("breadcrumb-current", viewNames[view]);
    document.title = `${viewNames[view]} · 面试伴航`;
    if (window.location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view === "history") loadHistory().catch((error) => toast(error.message, "error"));
  }
  function renderLiveContext() {
    const profile = activeProfile();
    setText("live-context-title", profile ? [profile.company, profile.role].filter(Boolean).join(" · ") || profile.name : "先准备一份属于你的面试资料");
    const parts = profile ? [profile.resume && "简历已添加", profile.jd && "岗位已添加", profile.company_notes && "公司背景已添加"].filter(Boolean) : [];
    setText("context-completeness", parts.length ? parts.join(" · ") : "简历 · 岗位 · 公司背景");
  }
  function renderProfiles() {
    const select = $("active-profile");
    select.replaceChildren();
    if (!state.profiles.length) { const option = el("option", "", "暂无面试资料"); option.value = ""; select.append(option); }
    for (const profile of state.profiles) {
      const option = el("option", "", profile.name || "未命名资料"); option.value = profile.id; select.append(option);
    }
    select.value = state.active_profile_id || "";
    setText("profile-count", state.profiles.length);
    const list = $("profile-list"); list.replaceChildren();
    for (const profile of state.profiles) {
      const button = el("button", `profile-entry${profile.id === editorId ? " active" : ""}`); button.type = "button";
      button.append(el("strong", "", profile.name || "未命名资料"), el("span", "", [profile.company, profile.role].filter(Boolean).join(" · ") || "补充公司与岗位"));
      button.addEventListener("click", () => switchProfile(profile.id)); list.append(button);
    }
  }
  function profileInput(name) { return $("profile-form").elements.namedItem(name); }
  function loadProfile(profile) {
    editorId = profile?.id || null;
    for (const field of profileFields) profileInput(field).value = profile?.[field] || "";
    profileDirty = false;
    setText("profile-editor-title", profile ? "编辑面试资料" : "新建面试资料");
    setText("profile-save-status", profile ? "已保存到本机" : "尚未保存");
    $("delete-profile").hidden = !editorId;
    renderProfiles();
  }
  function mayDiscardProfile() { return !profileDirty || window.confirm("这份资料有未保存的修改。放弃修改并切换吗？"); }
  async function switchProfile(id) {
    if (id === editorId) return;
    if (!mayDiscardProfile()) { $("active-profile").value = state.active_profile_id || ""; return; }
    try {
      await post("/api/profiles/active", { id });
      await refreshState();
      loadProfile(state.profiles.find((profile) => profile.id === id));
    } catch (error) { toast(error.message, "error"); $("active-profile").value = state.active_profile_id || ""; }
  }
  function markProfileDirty() { profileDirty = true; setText("profile-save-status", "有未保存的修改"); }
  async function saveProfile() {
    if (!$("profile-form").reportValidity()) return null;
    if (state.session.listening && (!editorId || editorId !== state.active_profile_id)) throw new Error("请先停止监听，再保存并切换到新的面试资料。");
    const profile = {};
    for (const field of profileFields) profile[field] = profileInput(field).value.trim();
    if (!profile.name) throw new Error("请先填写资料名称。");
    if (editorId) profile.id = editorId;
    const result = await post("/api/profiles", profile);
    const saved = result.profile || result;
    const id = saved.id || editorId;
    if (id && id !== state.active_profile_id) await post("/api/profiles/active", { id });
    await refreshState();
    loadProfile(state.profiles.find((item) => item.id === id) || activeProfile());
    toast("资料已保存，并用于当前面试。");
    return id || state.active_profile_id;
  }
  function activateMaterial(name) {
    $$("[data-material]").forEach((button) => { const active = button.dataset.material === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; });
    $$("[data-pane]").forEach((node) => { node.hidden = node.dataset.pane !== name; });
    $("import-target").value = name;
  }
  async function importFile() {
    const file = $("import-file").files[0];
    if (!file) return;
    const target = $("import-target").value;
    const button = $("import-file-button");
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "正在导入…";
    const help = $("import-help");
    help.className = "field-help import-help";
    help.setAttribute("role", "status");
    setText("import-help", `正在读取 ${file.name}，请稍候…`);
    const timeout = new AbortController();
    const timeoutId = setTimeout(() => timeout.abort(), 60000);
    try {
      if (!/\.(pdf|docx|txt|md)$/i.test(file.name)) throw new Error("暂不支持这个文件格式。请使用带文字的 PDF、Word (.docx)、TXT 或 Markdown；旧版 .doc 请另存为 .docx。");
      if (!file.size) throw new Error("所选文件为空，请重新选择。");
      if (file.size > 8 * 1024 * 1024) throw new Error("文件超过 8 MB，请压缩 PDF 或只保留简历正文后重试。");
      const body = new FormData(); body.append("file", file);
      const result = await api("/api/import", { method: "POST", body, signal: timeout.signal });
      const input = profileInput(target);
      if (!result.text?.trim()) throw new Error("没有提取到文字。扫描版简历需要先转成文字，或直接粘贴内容。");
      input.value = [input.value.trim(), result.text].filter(Boolean).join("\n\n");
      markProfileDirty(); activateMaterial(target);
      setText("import-help", `已导入 ${result.filename || file.name}（${result.text.length} 字符），请检查内容后点击“保存并用于面试”。${result.warning || ""}`);
      help.classList.add("success-text");
      toast(result.warning || "文件内容已导入，请检查后保存。", result.warning ? "warning" : "success");
    } catch (error) {
      const message = timeout.signal.aborted ? "导入超过 60 秒，请重试或改用文字版简历。" : error.message;
      help.className = "field-help import-help error-text";
      help.setAttribute("role", "alert");
      setText("import-help", `导入失败：${message}`);
      toast(message, "error");
    } finally {
      clearTimeout(timeoutId);
      button.disabled = isClosing;
      button.textContent = "选择文件";
      $("import-file").value = "";
    }
  }
  function setPartialTranscript(text) {
    const value = typeof text === "string" ? text.trim() : "";
    setText("transcript-partial", value ? `正在识别：${value}` : "");
    $("transcript-partial").hidden = !value;
  }
  function renderTranscripts() {
    setText("transcript-count", `${state.session.transcripts.length} 条`);
    const list = $("transcript-list");
    if (!state.session.transcripts.length) {
      if (!list.querySelector(".empty-transcript")) {
        const empty = el("div", "empty-transcript");
        const wave = el("div", "wave-illustration"); for (let i = 0; i < 7; i++) wave.append(el("i"));
        empty.append(wave, el("strong", "", "等待对话开始"), el("p", "", "开启监听后，面试官的话会实时整理在这里。"), el("span", "", "建议佩戴耳机，减少回声")); list.replaceChildren(empty);
      }
      return;
    }
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    list.replaceChildren();
    for (const transcript of state.session.transcripts) {
      const item = el("div", "transcript-item");
      const time = el("time", "", `${formatTime(transcript.at)}${transcript.asr_ms != null ? ` · 识别 ${latency(transcript.asr_ms)}` : ""}`);
      if (transcript.asr_ms != null) time.title = "本段声音转为文字的耗时，不包含等待问题结束的停顿。";
      if (dateValue(transcript.at)) time.dateTime = dateValue(transcript.at).toISOString();
      const add = el("button", "", "加入问题，编辑后回答 ↗"); add.type = "button";
      add.addEventListener("click", () => {
        const input = $("manual-question");
        input.value = [input.value.trim(), transcript.text].filter(Boolean).join("\n");
        input.focus();
        setText("live-status-message", "已加入问题框。可以合并其他片段、纠正识别文字，再生成回答。");
      });
      item.append(time, el("p", "", transcript.text), add); list.append(item);
    }
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }
  function renderAudio() {
    const listening = Boolean(state.session.listening);
    $("toggle-listening").classList.toggle("listening", listening);
    $("toggle-listening").querySelector("span").textContent = listening ? "停止监听" : "开始监听";
    $("toggle-listening").querySelector("use").setAttribute("href", listening ? "#i-stop" : "#i-wave");
    $("listening-dot").classList.toggle("on", listening);
    $("audio-device").disabled = listening;
    $("warmup").hidden = state.settings.asr_provider !== "local";
    if (!listening) { setPartialTranscript(""); $("level-fill").style.width = "0%"; setText("audio-status-label", "尚未监听"); }
    else setText("audio-status-label", "正在倾听");
  }
  function renderAnswer() {
    const answer = currentAnswer();
    const streaming = Boolean(generatingId || pendingGeneration);
    $("stop-answer").hidden = !streaming;
    $("generate-answer").disabled = streaming;
    $("answer-empty").hidden = Boolean(answer);
    $("answer-article").hidden = !answer;
    $("answer-tools").hidden = !answer?.text;
    $("generation-state").classList.toggle("generating", streaming);
    setText("generation-state", streaming ? "正在生成" : answer?.status === "error" ? "生成未完成" : answer ? answer.cancelled || answer.status === "cancelled" ? "已停止" : "已完成" : "等待问题");
    setText("answer-latency", answer?.first_token_ms != null ? `首字 ${latency(answer.first_token_ms)}` : "");
    if (answer) {
      const modeLabels = { answer: "当前问题", prepare: "面试准备", review: "面试复盘" };
      $("answer-article").querySelector(".question-label").textContent = modeLabels[answer.mode] || "当前问题";
      setText("answer-question", answer.question || (answer.mode === "prepare" ? "面试准备清单" : answer.mode === "review" ? "本场面试复盘" : "回答思路"));
      setText("answer-body", answer.text || (answer.status === "error" ? answer.error || "生成未完成，请检查连接后重试。" : "正在结合你的资料整理思路……"));
      $("answer-body").classList.toggle("streaming", answer.id === generatingId);
      const sources = (answer.sources || []).map((source) => typeof source === "string" ? source : source.label || source.name || source.title || "面试资料");
      setText("answer-sources", sources.length ? `参考资料：${sources.join(" · ")}` : "");
      const rating = typeof answer.feedback === "string" ? answer.feedback : answer.feedback?.rating;
      $("feedback-useful").classList.toggle("selected", rating === "useful");
      $("feedback-improve").classList.toggle("selected", rating === "improve");
      $("feedback-useful").setAttribute("aria-pressed", String(rating === "useful"));
      $("feedback-improve").setAttribute("aria-pressed", String(rating === "improve"));
      setText("answer-footnote", answer.total_ms != null ? `本次生成 ${latency(answer.total_ms)} · 以真实经历为依据` : "以真实经历为依据 · 由你决定如何表达");
    } else setText("answer-footnote", "以真实经历为依据 · 由你决定如何表达");
    const select = $("answer-select");
    const existing = [...select.options].map((option) => option.value).join(",");
    const wanted = state.session.answers.map((item) => item.id).join(",");
    if (existing !== wanted) {
      select.replaceChildren();
      state.session.answers.forEach((item, index) => { const option = el("option", "", `${String(index + 1).padStart(2, "0")} · ${(item.question || (item.mode === "prepare" ? "面试准备清单" : "面试复盘")).slice(0, 65)}`); option.value = item.id; select.append(option); });
    }
    select.value = selectedAnswerId || "";
    $("previous-answers").hidden = state.session.answers.length < 2;
  }
  function queueAnswerRender() {
    if (answerRenderQueued) return;
    answerRenderQueued = true;
    requestAnimationFrame(() => { answerRenderQueued = false; renderAnswer(); });
  }
  async function generateAnswer(mode = "answer") {
    if (generatingId || pendingGeneration) { toast("当前回答仍在生成，可以先停止生成。", "warning"); return; }
    let question = $("manual-question").value.trim();
    if (mode === "answer" && !question) { toast("先输入问题，或从左侧选择面试官的话。", "warning"); $("manual-question").focus(); return; }
    if (mode !== "answer") {
      if (profileDirty || !editorId) { const saved = await saveProfile(); if (!saved) return; }
      else if (editorId !== state.active_profile_id) { await post("/api/profiles/active", { id: editorId }); await refreshState(); }
      question = mode === "prepare" ? "请结合我的简历、岗位要求和公司资料，整理一份重点明确、可以直接练习的面试准备清单。" : "请结合本场面试的问题、我的资料和回答反馈，复盘表达质量、知识缺口以及下一步练习建议。";
    }
    navigate("live"); pendingGeneration = true; renderAnswer();
    setText("live-status-message", "正在结合当前资料生成回答，文字会逐步显示。");
    try {
      const result = await post("/api/answer", { question, mode });
      if (result.id && !state.session.answers.some((answer) => answer.id === result.id)) {
        state.session.answers.push({ id: result.id, question, mode, text: "", status: "streaming", at: new Date().toISOString() });
        generatingId = result.id; selectedAnswerId = result.id;
      }
      if (mode === "answer") $("manual-question").value = "";
    } catch (error) { setText("live-status-message", error.message); throw error; }
    finally { pendingGeneration = false; renderAnswer(); }
  }
  async function loadDevices() {
    const result = await api("/api/audio/devices");
    deviceList = result.devices || [];
    const select = $("audio-device"); const prior = select.value;
    const option = el("option", "", state.capabilities?.platform === "macOS" ? "系统播放声音（所有应用）" : "系统默认播放设备"); option.value = ""; select.replaceChildren(option);
    for (const device of deviceList) { const item = el("option", "", `${device.name}${device.is_default ? " · 默认" : ""}`); item.value = String(device.id); select.append(item); }
    const selected = state.settings.device_id == null ? prior : String(state.settings.device_id);
    select.value = [...select.options].some((item) => item.value === selected) ? selected : "";
    if (result.error) { toast(result.error, "warning"); setText("live-status-message", result.error); }
  }
  function fillSettings() {
    const settings = { ...defaults, ...state.settings };
    for (const [name, id] of Object.entries(settingInputs)) $(id).value = settings[name] ?? defaults[name] ?? "";
    $("deepseek-key").value = ""; $("cloud-asr-key").value = "";
    $("delete-deepseek-key").checked = false; $("delete-cloud-key").checked = false;
    settingsDirty = false; setText("settings-save-status", "设置保存在本机"); toggleAsrFields();
  }
  function renderPlatformHelp() {
    let note = $("audio-platform-help");
    if (!note) {
      note = el("p", "field-help", "");
      note.id = "audio-platform-help";
      $("audio-device").closest(".audio-bar").after(note);
    }
    note.textContent = state.capabilities?.audio_note || "";
    note.hidden = !note.textContent;
  }
  function renderSecretStatus() {
    const failed = Boolean(state.capabilities?.secret_error);
    const notice = $("secret-read-error");
    if (notice && state.capabilities?.platform === "macOS") {
      notice.textContent = "已保存的 API 密钥暂时无法读取。请确认当前 Mac 的登录钥匙串已解锁，并允许面试伴航访问；简历和资料仍保存在本机。";
    }
    if (notice) notice.hidden = !failed;
    for (const [name, id] of [["deepseek_key_set", "deepseek-key-status"], ["cloud_asr_key_set", "cloud-key-status"]]) {
      const unreadable = failed && !state.settings[name];
      setText(id, state.settings[name] ? "已保存" : unreadable ? "读取失败" : "尚未配置");
      $(id).classList.toggle("error-text", unreadable);
    }
    $("deepseek-key").placeholder = state.settings.deepseek_key_set ? "已保存密钥；留空保持不变" : failed ? "已保存密钥暂时无法读取" : "输入你的 DeepSeek API Key";
    $("cloud-asr-key").placeholder = state.settings.cloud_asr_key_set ? "已保存语音密钥；切换平台时请填写新密钥" : failed ? "已保存密钥暂时无法读取" : $("asr-provider").value === "qwen" ? "输入阿里云百炼 API Key" : "输入语音服务密钥";
  }
  function toggleAsrFields() {
    const localOption = $("asr-provider").querySelector('option[value="local"]');
    if (localOption) {
      localOption.hidden = Boolean(state.capabilities?.cloud_edition);
      localOption.disabled = Boolean(state.capabilities?.cloud_edition);
    }
    const provider = $("asr-provider").value;
    const local = provider === "local";
    const qwen = provider === "qwen";
    $("local-asr-fields").hidden = !local;
    $("cloud-asr-fields").hidden = provider !== "cloud";
    $("qwen-asr-fields").hidden = !qwen;
    $("remote-asr-fields").hidden = local;
    $("qwen-key-help").hidden = !qwen;
    $("asr-advanced-settings").hidden = qwen;
    setText("cloud-key-label", qwen ? "阿里云百炼 API Key" : "语音服务 API Key");
    renderSecretStatus();
    setText("remote-audio-note", qwen ? "开启监听后，电脑声音会实时发送给阿里云进行识别，不采集你的麦克风。费用以服务商为准。" : "开启监听后，电脑声音片段会发送给这里配置的语音服务。费用以服务商为准。");
  }
  async function saveSettings(quiet = false) {
    for (const input of $$("#settings-form input[type='number']")) if (!input.reportValidity()) throw new Error("请检查停顿与延迟调节中的数值。");
    if ($("asr-provider").value === "cloud" && !$("cloud-asr-url").reportValidity()) throw new Error("请填写有效的云端识别接口地址。");
    const settings = {};
    for (const [name, id] of Object.entries(settingInputs)) {
      const value = $(id).type === "number" ? Number($(id).value) : $(id).value.trim();
      if (value !== (state.settings[name] ?? defaults[name] ?? "")) settings[name] = value;
    }
    if ($("deepseek-key").value.trim()) settings.deepseek_key = $("deepseek-key").value.trim();
    if ($("cloud-asr-key").value.trim()) settings.cloud_asr_key = $("cloud-asr-key").value.trim();
    if ($("delete-deepseek-key").checked) settings.delete_deepseek_key = true;
    if ($("delete-cloud-key").checked) settings.delete_cloud_asr_key = true;
    await post("/api/settings", settings);
    settingsDirty = false;
    await refreshState(); fillSettings();
    if (!quiet) toast("设置已保存。");
  }
  async function applyChinesePreset() {
    const status = $("asr-preset-status");
    const preset = { asr_language: "zh", asr_quality: "accurate", silence_ms: 900, min_speech_ms: 200, energy_threshold: .003 };
    status.className = "field-help";
    status.setAttribute("role", "status");
    if (state.session.listening) {
      status.className = "field-help error-text";
      status.setAttribute("role", "alert");
      setText("asr-preset-status", "请先停止监听，再应用中文面试优化。");
      return;
    }
    setText("asr-preset-status", "正在应用中文面试优化…");
    const priorValues = Object.fromEntries(Object.keys(preset).map((name) => [name, $(settingInputs[name]).value]));
    settingsDirty = true; // Keep other settings and API key drafts intact during state broadcasts.
    try {
      await post("/api/settings", preset);
      state.settings = { ...state.settings, ...preset };
      for (const [name, value] of Object.entries(preset)) {
        const input = $(settingInputs[name]);
        if (input.value === priorValues[name]) input.value = String(value);
      }
      setText("asr-preset-status", "中文面试优化已保存。英文或中英混合面试，请将对话语言切回“自动（中英）”并保存。");
      status.className = "field-help success-text";
      toast("中文面试优化已保存。准确模式会增加一些识别等待时间。");
    } catch (error) {
      setText("asr-preset-status", error.message);
      status.className = "field-help error-text";
      status.setAttribute("role", "alert");
      throw error;
    } finally {
      settingsDirty = Object.entries(settingInputs).some(([name, id]) => {
        const input = $(id);
        const value = input.type === "number" ? Number(input.value) : input.value.trim();
        return value !== (state.settings[name] ?? defaults[name] ?? "");
      }) || Boolean($("deepseek-key").value.trim() || $("cloud-asr-key").value.trim() || $("delete-deepseek-key").checked || $("delete-cloud-key").checked);
      setText("settings-save-status", settingsDirty ? "有未保存的修改" : "设置保存在本机");
    }
  }
  async function openOverlay() {
    if (overlayOpening) return;
    overlayOpening = true;
    $$(".overlay-open").forEach((button) => { button.disabled = true; });
    const status = $("overlay-status");
    status.className = "overlay-status field-help";
    status.setAttribute("role", "status");
    setText("overlay-status", "正在打开回答悬浮窗…");
    try {
      const result = await post("/api/overlay/open", { answer_id: selectedAnswerId || null });
      if (!result.opened) throw new Error("悬浮窗未能打开，请稍后重试。");
      overlayOpened = true;
      setText("overlay-status", "悬浮窗会同步最新回答。拖动顶部移动，在窗内调整透明度或置顶。");
      toast("回答悬浮窗已打开：拖动顶部移动，可调整透明度和置顶。");
    } catch (error) {
      setText("overlay-status", error.message);
      status.className = "overlay-status field-help error-text";
      status.setAttribute("role", "alert");
      throw error;
    } finally {
      overlayOpening = false;
      $$(".overlay-open").forEach((button) => { button.disabled = false; });
    }
  }
  async function warmup(saveFirst = false) {
    if (saveFirst) await saveSettings(true);
    const local = state.settings.asr_provider === "local";
    const message = local ? "正在预热识别，首次使用可能需要下载模型。" : "云端识别无需本地模型预热。保存密钥后即可开始监听。";
    setText("warmup-status", message);
    setText("live-status-message", message);
    if (local) await post("/api/audio/warmup");
    toast(local ? "已开始预热识别，状态会显示在这里。" : message);
  }
  async function loadHistory() {
    const result = await api("/api/sessions");
    const sessions = Array.isArray(result) ? result : result.sessions || result.archives || [];
    const list = $("history-list"); list.replaceChildren();
    setText("history-count", `${sessions.length} 场`);
    if (!sessions.length) { list.append(el("div", "history-empty", "还没有归档会话。\n新建会话后，可在这里回看。")); return; }
    for (const session of sessions) {
      const button = el("button", `history-entry${session.id === archive?.id ? " active" : ""}`); button.type = "button"; button.dataset.sessionId = session.id;
      button.append(el("strong", "", session.title || session.profile_name || session.name || `面试会话 · ${formatTime(session.created_at || session.at || session.started_at, true)}`));
      const answers = session.answer_count ?? session.answers_count ?? session.answers?.length;
      button.append(el("span", "", [formatTime(session.created_at || session.at || session.started_at, true), answers != null ? `${answers} 次回答` : null].filter(Boolean).join(" · ")));
      button.addEventListener("click", () => busy(button, async () => {
        const result = await api(`/api/sessions/${encodeURIComponent(session.id)}`); archive = cleanSession(result.session || result);
        $$(".history-entry").forEach((item) => item.classList.toggle("active", item.dataset.sessionId === session.id)); renderArchive();
      })); list.append(button);
    }
  }
  function renderArchive() {
    if (!archive) return;
    const detail = $("history-detail"); detail.replaceChildren();
    setText("history-detail-title", archive.title || archive.profile_name || archive.name || "面试会话详情");
    $("export-history").hidden = false;
    if (!archive.answers.length) detail.append(el("p", "field-help", "本场暂无生成的回答。"));
    for (const answer of archive.answers) {
      const item = el("article", "history-answer");
      const metadata = [formatTime(answer.at), answer.first_token_ms != null ? `首字 ${latency(answer.first_token_ms)}` : null].filter(Boolean).join(" · ");
      item.append(el("div", "history-meta", metadata), el("h3", "", answer.question || "回答思路"), el("div", "answer-body", answer.text || "未生成回答内容"));
      if (answer.feedback?.rating) item.append(el("p", "field-help", `反馈：${answer.feedback.rating === "useful" ? "有帮助" : "待改进"}${answer.feedback.note ? ` · ${answer.feedback.note}` : ""}`));
      detail.append(item);
    }
    if (archive.transcripts.length) {
      const transcripts = el("details", "history-transcripts"); transcripts.append(el("summary", "", `查看原始问题记录 · ${archive.transcripts.length} 条`));
      archive.transcripts.forEach((item) => transcripts.append(el("p", "", `${formatTime(item.at)}\n${item.text}`))); detail.append(transcripts);
    }
  }
  function exportArchive() {
    if (!archive) return;
    const lines = ["# 面试伴航 · 会话记录", "", `会话：${archive.title || archive.profile_name || archive.name || archive.id || "面试会话"}`, ""];
    for (const answer of archive.answers) { lines.push(`## ${answer.question || "回答思路"}`, "", answer.text || "", ""); if (answer.feedback?.rating) lines.push(`反馈：${answer.feedback.rating === "useful" ? "有帮助" : "待改进"} ${answer.feedback.note || ""}`, ""); }
    if (archive.transcripts.length) { lines.push("## 问题原始记录", ""); archive.transcripts.forEach((item) => lines.push(`- ${formatTime(item.at)} ${item.text}`)); }
    const url = URL.createObjectURL(new Blob(["\ufeff", lines.join("\n")], { type: "text/markdown;charset=utf-8" }));
    const anchor = el("a"); anchor.href = url; anchor.download = `面试记录-${String(archive.id || "archive").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40)}.md`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  async function feedback(rating, note = "", id = selectedAnswerId) {
    if (!id) return;
    await post("/api/feedback", { answer_id: id, rating, note });
    const answer = state.session.answers.find((item) => item.id === id);
    if (answer) answer.feedback = { rating, note };
    renderAnswer(); toast("反馈已保存，面试复盘时会参考。");
  }
  function handleMessage(message) {
    const type = message.type;
    if (type === "snapshot") { applyState(message); return; }
    if (type === "level") { $("level-fill").style.width = `${Math.min(100, Math.max(0, Number(message.value) || 0) * 100)}%`; return; }
    if (type === "status") {
      if (message.component === "audio") {
        if (message.state === "listening") state.session.listening = true;
        if (["idle", "error"].includes(message.state)) state.session.listening = false;
        renderAudio();
      }
      if (message.component === "asr") {
        if (["idle", "error"].includes(message.state)) setPartialTranscript("");
        setText("warmup-status", message.message || ({ ready: "识别已就绪", loading: "正在加载识别模型", transcribing: "正在识别电脑声音" })[message.state] || "");
        if (message.state === "transcribing") setText("audio-status-label", "正在识别");
        if (message.state === "ready" && state.session.listening) setText("audio-status-label", "正在倾听");
      }
      if (message.message) setText("live-status-message", message.message);
      if (message.state === "error") toast(message.message || "服务暂时不可用，请检查设置。", "error");
      return;
    }
    if (type === "transcript_partial") {
      if (state.session.listening) setPartialTranscript(message.text);
      return;
    }
    if (type === "transcript") {
      setPartialTranscript("");
      if (!state.session.transcripts.some((item) => item.id === message.id)) state.session.transcripts.push(message);
      renderTranscripts(); if (state.session.listening) setText("audio-status-label", "正在倾听"); return;
    }
    if (type === "answer_start") {
      let answer = state.session.answers.find((item) => item.id === message.id);
      if (!answer) { answer = { ...message, text: "", status: "streaming" }; state.session.answers.push(answer); }
      else Object.assign(answer, message, { status: "streaming" });
      selectedAnswerId = message.id; generatingId = message.id; pendingGeneration = false;
      $("answer-content").scrollTop = 0; renderAnswer(); return;
    }
    if (type === "answer_delta") {
      let answer = state.session.answers.find((item) => item.id === message.id);
      if (!answer) { answer = { id: message.id, text: "", status: "streaming" }; state.session.answers.push(answer); selectedAnswerId = message.id; }
      answer.text = (answer.text || "") + (message.text || "");
      if (message.first_token_ms != null) answer.first_token_ms = message.first_token_ms;
      generatingId = message.id; queueAnswerRender(); return;
    }
    if (type === "answer_done") {
      const answer = state.session.answers.find((item) => item.id === message.id);
      if (answer) Object.assign(answer, message, { status: message.status === "error" ? "error" : message.cancelled ? "cancelled" : "done" });
      if (generatingId === message.id) generatingId = null;
      pendingGeneration = false; renderAnswer();
      setText("live-status-message", message.status === "error" ? message.error || "生成未完成，请检查连接后重试。" : message.cancelled ? "已停止生成。可以调整问题后重新生成。" : "回答已整理完成。结合你的真实经历，选择适合自己的表达。"); return;
    }
    if (type === "answer_error") {
      const answer = state.session.answers.find((item) => item.id === message.id);
      if (answer) { answer.status = "error"; answer.error = message.message; }
      if (!message.id || generatingId === message.id) generatingId = null;
      pendingGeneration = false; renderAnswer(); toast(message.message || "回答生成失败，请检查服务连接。", "error"); return;
    }
    if (type === "session_reset") { setPartialTranscript(""); state.session = cleanSession(message.session); selectedAnswerId = null; generatingId = null; pendingGeneration = false; renderTranscripts(); renderAnswer(); renderAudio(); return; }
    if (type === "auto_pending") { setText("live-status-message", `正在等待问题完整：${message.text || ""}`); return; }
    if (type === "error" || type === "warning") { if (type === "error") setPartialTranscript(""); toast(message.message, type); if (message.message) setText("live-status-message", message.message); }
  }
  function connectSocket() {
    clearTimeout(reconnectTimer);
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${location.host}/ws`);
    socket.addEventListener("open", () => {
      reconnectDelay = 1500; $("connection-status").className = "connection-status connected"; setText("connection-text", "本机已连接");
      refreshState().catch((error) => toast(error.message, "error"));
    });
    socket.addEventListener("message", (event) => { try { handleMessage(JSON.parse(event.data)); } catch (error) { console.warn("无法处理本机状态更新", error); } });
    socket.addEventListener("close", () => {
      setPartialTranscript("");
      if (isClosing) return;
      $("connection-status").className = "connection-status disconnected"; setText("connection-text", "正在重新连接");
      const closedSocket = socket;
      api("/api/state").catch((error) => {
        if (isClosing || socket !== closedSocket || socket.readyState === WebSocket.OPEN) return;
        setText("connection-text", error.status === 401 || error.status === 403 ? "请从启动器重新打开" : "本机服务已断开");
        $("connection-status").title = error.message;
      });
      reconnectTimer = setTimeout(connectSocket, reconnectDelay); reconnectDelay = Math.min(reconnectDelay * 1.7, 15000);
    });
    socket.addEventListener("error", () => socket.close());
  }

  $$(".nav-item").forEach((button) => { button.setAttribute("aria-label", viewNames[button.dataset.view]); button.title = viewNames[button.dataset.view]; button.addEventListener("click", () => navigate(button.dataset.view)); });
  $$("[data-go]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.go)));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
  $("edit-context").addEventListener("click", () => navigate("prepare"));
  $("active-profile").addEventListener("change", (event) => { if (event.target.value) switchProfile(event.target.value); });
  $("profile-form").addEventListener("input", markProfileDirty);
  $("profile-form").addEventListener("submit", (event) => { event.preventDefault(); busy($("save-profile"), saveProfile); });
  $("create-profile").addEventListener("click", () => { if (mayDiscardProfile()) { loadProfile(null); activateMaterial("resume"); $("profile-name").focus(); } });
  $("delete-profile").addEventListener("click", () => busy($("delete-profile"), async () => {
    if (!editorId || !window.confirm("删除这份面试资料？已归档的会话记录会保留。")) return;
    await api(`/api/profiles/${encodeURIComponent(editorId)}`, { method: "DELETE" });
    profileDirty = false; await refreshState(); loadProfile(activeProfile() || null); toast("面试资料已删除。");
  }));
  $$("[data-material]").forEach((button, index, buttons) => {
    button.id = `tab-${button.dataset.material}`;
    button.setAttribute("aria-controls", `pane-${button.dataset.material}`);
    button.tabIndex = button.classList.contains("active") ? 0 : -1;
    const pane = document.querySelector(`[data-pane="${button.dataset.material}"]`);
    pane.id = `pane-${button.dataset.material}`; pane.setAttribute("role", "tabpanel"); pane.setAttribute("aria-labelledby", button.id);
    button.addEventListener("click", () => activateMaterial(button.dataset.material));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      activateMaterial(buttons[next].dataset.material); buttons[next].focus();
    });
  });
  $("import-file-button").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", importFile);
  $("generate-prepare").addEventListener("click", () => busy($("generate-prepare"), () => generateAnswer("prepare")));
  $("generate-review").addEventListener("click", () => busy($("generate-review"), () => generateAnswer("review")));
  $("generate-answer").addEventListener("click", async () => { try { await generateAnswer(); } catch (error) { toast(error.message, "error"); } });
  $("manual-question").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); $("generate-answer").click(); } });
  $("stop-answer").addEventListener("click", () => busy($("stop-answer"), async () => { await post("/api/answer/stop"); generatingId = null; pendingGeneration = false; await refreshState(); setText("live-status-message", "已停止生成，声音监听可独立继续。"); }));
  $("answer-select").addEventListener("change", (event) => {
    selectedAnswerId = event.target.value; renderAnswer(); $("answer-content").scrollTop = 0;
    if (overlayOpened) post("/api/overlay/select", { id: selectedAnswerId }).catch((error) => {
      setText("overlay-status", error.message);
      $("overlay-status").className = "overlay-status field-help error-text";
      $("overlay-status").setAttribute("role", "alert");
    });
  });
  $$(".overlay-open").forEach((button) => {
    button.addEventListener("click", () => busy(button, openOverlay));
  });
  $("copy-answer").addEventListener("click", () => busy($("copy-answer"), async () => { const answer = currentAnswer(); if (answer?.text) { await navigator.clipboard.writeText(answer.text); toast("回答已复制。"); } }));
  $("feedback-useful").addEventListener("click", () => busy($("feedback-useful"), () => feedback("useful")));
  $("feedback-improve").addEventListener("click", () => { feedbackAnswerId = selectedAnswerId; $("feedback-note").value = currentAnswer()?.feedback?.note || ""; $("feedback-dialog").showModal(); });
  $("close-feedback").addEventListener("click", () => $("feedback-dialog").close());
  $("feedback-form").addEventListener("submit", (event) => { event.preventDefault(); busy(event.submitter, async () => { await feedback("improve", $("feedback-note").value.trim(), feedbackAnswerId); $("feedback-dialog").close(); }); });
  $("refresh-devices").addEventListener("click", () => busy($("refresh-devices"), loadDevices));
  $("audio-device").addEventListener("change", () => busy($("refresh-devices"), async () => { const value = $("audio-device").value; await post("/api/settings", { device_id: value === "" ? null : Number(value) }); state.settings.device_id = value === "" ? null : Number(value); }));
  $("toggle-listening").addEventListener("click", () => busy($("toggle-listening"), async () => {
    setText("live-status-message", state.session.listening ? "正在停止监听…" : "正在连接电脑声音，请稍候…");
    try {
      if (state.session.listening) await post("/api/audio/stop");
      else { const value = $("audio-device").value; await post("/api/audio/start", { device_id: value === "" ? null : Number(value) }); }
      await refreshState();
    } catch (error) { setText("live-status-message", error.message); throw error; }
  }));
  $("auto-answer").addEventListener("change", async () => {
    const input = $("auto-answer"); const value = input.checked; input.disabled = true;
    try { await post("/api/settings", { auto_answer: value }); state.settings.auto_answer = value; setText("auto-answer-note", value ? "检测到完整问题后，自动整理回答思路" : "也可手动选择问题后生成思路"); }
    catch (error) { input.checked = !value; toast(error.message, "error"); }
    finally { input.disabled = false; }
  });
  $("warmup").addEventListener("click", () => busy($("warmup"), () => warmup()));
  $("settings-warmup").addEventListener("click", () => busy($("settings-warmup"), () => warmup(true)));
  $("chinese-asr-preset").addEventListener("click", () => busy($("chinese-asr-preset"), applyChinesePreset));
  $("settings-form").addEventListener("input", () => { settingsDirty = true; setText("settings-save-status", "有未保存的修改"); });
  $("asr-provider").addEventListener("change", toggleAsrFields);
  $("sensevoice-preset").addEventListener("click", () => {
    $("cloud-asr-url").value = "https://api.siliconflow.cn/v1/audio/transcriptions";
    $("cloud-asr-model").value = "FunAudioLLM/SenseVoiceSmall";
    settingsDirty = true;
    setText("settings-save-status", "有未保存的修改");
    setText("cloud-preset-status", "已填入 SenseVoice 地址和模型，该服务会自动判断语种。请填写硅基流动的语音 API Key，再点击“保存设置”；费用以服务商为准。");
  });
  $("settings-form").addEventListener("submit", (event) => { event.preventDefault(); busy($("save-settings"), () => saveSettings()); });
  $("test-connection").addEventListener("click", () => busy($("test-connection"), async () => {
    setText("connection-test-result", "正在保存设置并测试连接……"); $("connection-test-result").className = "field-help";
    try { await saveSettings(true); const result = await post("/api/settings/test"); setText("connection-test-result", result.message || `连接成功${result.latency_ms != null ? ` · ${latency(result.latency_ms)}` : ""}`); $("connection-test-result").className = "field-help success-text"; toast("DeepSeek 连接测试成功。"); }
    catch (error) { setText("connection-test-result", error.message); $("connection-test-result").className = "field-help error-text"; throw error; }
  }));
  $("new-session").addEventListener("click", () => busy($("new-session"), async () => {
    if (generatingId || pendingGeneration) { toast("请先停止当前回答生成，再新建会话。", "warning"); return; }
    if ((state.session.answers.length || state.session.transcripts.length) && !window.confirm("新建会话并将当前记录归档？")) return;
    await post("/api/session/new"); await refreshState(); $("manual-question").value = ""; toast("新会话已就绪，原有记录可在会话记录中查看。");
  }));
  $("refresh-history").addEventListener("click", () => busy($("refresh-history"), loadHistory));
  $("export-history").addEventListener("click", exportArchive);
  $("shutdown-app").addEventListener("click", () => busy($("shutdown-app"), async () => {
    if ((profileDirty || settingsDirty) && !window.confirm("还有未保存的修改。退出应用并放弃这些修改吗？")) return;
    await post("/api/shutdown");
    isClosing = true; clearTimeout(reconnectTimer); socket?.close();
    state.session.listening = false; generatingId = null; pendingGeneration = false;
    profileDirty = false; settingsDirty = false;
    renderAudio(); renderAnswer();
    $("connection-status").className = "connection-status disconnected";
    setText("connection-text", "应用已退出");
    setText("live-status-message", "声音监听和回答服务已停止，可关闭此窗口。下次请通过启动器重新打开。");
    setText("settings-save-status", "应用已退出，可以关闭此窗口");
    $$("main button, main input, main select, main textarea").forEach((input) => { input.disabled = true; });
    toast("应用已退出，声音监听和回答已停止。可以关闭此窗口。");
  }));
  window.addEventListener("beforeunload", (event) => { if (profileDirty || settingsDirty) { event.preventDefault(); event.returnValue = ""; } });
  window.addEventListener("pagehide", () => { isClosing = true; clearTimeout(reconnectTimer); socket?.close(); });
  navigate(location.hash.slice(1) || "live");
  fillSettings();
  refreshState().then(loadDevices).catch((error) => { toast(error.message, "error"); setText("live-status-message", error.message); });
  connectSocket();
})();

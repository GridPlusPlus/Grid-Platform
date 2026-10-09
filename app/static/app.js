"use strict";

const oauthResult = new URLSearchParams(window.location.hash.slice(1));
const oauthToken = oauthResult.get("auth") === "gitea" ? oauthResult.get("access_token") : null;
const oauthError = oauthResult.get("auth_error");
if (oauthToken) {
  const segment = oauthToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  const payload = JSON.parse(atob(segment.padEnd(Math.ceil(segment.length / 4) * 4, "=")));
  sessionStorage.setItem("grid_token", oauthToken);
  sessionStorage.setItem("grid_user_id", String(Number(payload.sub)));
  sessionStorage.removeItem("grid_email");
  history.replaceState(null, "", `${location.pathname}${location.search}`);
} else if (oauthError) {
  history.replaceState(null, "", `${location.pathname}${location.search}`);
}

const state = {
  token: sessionStorage.getItem("grid_token"),
  userId: Number(sessionStorage.getItem("grid_user_id")) || null,
  email: sessionStorage.getItem("grid_email"),
  spritePage: 1,
  mySpritePage: 1,
  packPage: 1,
  selectedSprites: [],
  editorSource: "all",
  editorLibrary: [],
  editorFavoriteFolders: [],
  editorFavoriteFolderId: null,
  theme: document.documentElement.dataset.theme || "light",
  me: null,
  favoriteFolders: [],
  activeFavoriteFolderId: null,
  previewSprite: null,
  previewFolderIds: [],
  uploadFocusX: 0.5,
  uploadFocusY: 0.5,
  uploadMaxCropX: 0,
  uploadMaxCropY: 0,
  uploadPreviewSequence: 0,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

// Small DOM helpers keep user content out of innerHTML.
function element(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.type) node.type = options.type;
  if (options.title) node.title = options.title;
  return node;
}

function notify(message, error = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.classList.toggle("error", error);
  notice.classList.remove("hidden");
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => notice.classList.add("hidden"), 4500);
}

function setSession(token, userId, email) {
  state.token = token;
  state.userId = userId;
  state.email = email;
  if (token) {
    sessionStorage.setItem("grid_token", token);
    sessionStorage.setItem("grid_user_id", String(userId));
    sessionStorage.setItem("grid_email", email);
  } else {
    sessionStorage.removeItem("grid_token");
    sessionStorage.removeItem("grid_user_id");
    sessionStorage.removeItem("grid_email");
  }
  renderSession();
  renderAccountAuthState();
}

function renderSession() {
  $("#session-label").textContent = state.token ? state.email || "已登入" : "未登入";
  $("#logout-button").classList.toggle("hidden", !state.token);
}

function renderAccountAuthState() {
  $("#account-login").classList.toggle("hidden", Boolean(state.token));
  $("#account-dashboard").classList.toggle("hidden", !state.token);
}

function renderTheme() {
  document.documentElement.dataset.theme = state.theme;
  const dark = state.theme === "dark";
  $(".theme-icon").textContent = dark ? "☀" : "☾";
  $("#theme-toggle").setAttribute("aria-label", dark ? "切換明亮模式" : "切換黑夜模式");
  $("#theme-toggle").title = dark ? "切換明亮模式" : "切換黑夜模式";
}

function toggleTheme() {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem("grid_theme", state.theme);
  renderTheme();
}

async function api(path, options = {}, protectedRequest = false) {
  const headers = new Headers(options.headers || {});
  if (protectedRequest || (state.token && path.startsWith("/packs?"))) {
    if (!state.token) throw new Error("請先登入");
    headers.set("Authorization", `Bearer ${state.token}`);
  }
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {...options, headers});
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.arrayBuffer();
  if (!response.ok) {
    const error = data?.error;
    if (response.status === 401 && ["AUTH_TOKEN_INVALID", "AUTH_TOKEN_EXPIRED"].includes(error?.code)) {
      setSession(null, null, null);
    }
    const detailText = Array.isArray(error?.details)
      ? error.details.map((item) => item.message).join("；")
      : "";
    throw new Error(detailText || error?.message || `HTTP ${response.status}`);
  }
  return data;
}

function switchView(viewName) {
  const primaryView = viewName === "register" ? "account" : viewName;
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === primaryView));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${viewName}-view`));
  if (viewName === "sprites") loadSprites();
  if (viewName === "packs") loadPacks();
  if (viewName === "account") {
    renderAccountAuthState();
    if (state.token) loadAccountDashboard();
  }
}

// Render raw RGBA sprites safely through canvas.
function addTags(container, tags) {
  if (!tags) return;
  tags.split(",").forEach((tag) => container.append(element("span", {className: "tag", text: tag})));
}

async function paintSprite(canvas, spriteId) {
  try {
    const bytes = new Uint8ClampedArray(await api(`/sprites/${spriteId}/image`));
    if (bytes.byteLength !== 4096) throw new Error("圖片資料長度錯誤");
    const context = canvas.getContext("2d");
    context.putImageData(new ImageData(bytes, 32, 32), 0, 0);
    return true;
  } catch (error) {
    canvas.setAttribute("aria-label", error.message);
    return false;
  }
}

function clearUploadPreview(message = "選擇圖片後顯示最終結果") {
  state.uploadPreviewSequence += 1;
  const canvas = $("#upload-preview-canvas");
  canvas.getContext("2d").clearRect(0, 0, 32, 32);
  canvas.classList.remove("can-pan", "dragging");
  $("#upload-preview-status").textContent = message;
  $("#upload-pan-controls").classList.add("hidden");
  state.uploadMaxCropX = 0;
  state.uploadMaxCropY = 0;
}

function uploadPreviewPayload() {
  const form = $("#upload-form");
  const file = form.elements.file.files[0];
  if (!file) return null;
  const payload = new FormData();
  payload.set("file", file);
  payload.set("image_mode", form.elements.image_mode.value);
  payload.set(
    "trim_transparent",
    form.elements.trim_transparent.checked ? "true" : "false",
  );
  payload.set("focus_x", String(state.uploadFocusX));
  payload.set("focus_y", String(state.uploadFocusY));
  return payload;
}

async function loadUploadPreview() {
  const payload = uploadPreviewPayload();
  if (!payload) {
    clearUploadPreview();
    return;
  }
  const sequence = ++state.uploadPreviewSequence;
  $("#upload-preview-status").textContent = "正在產生預覽…";
  try {
    const response = await fetch("/sprites/preview", {
      method: "POST",
      headers: {Authorization: `Bearer ${state.token}`},
      body: payload,
    });
    if (!response.ok) {
      const data = await response.json();
      if (
        response.status === 401
        && ["AUTH_TOKEN_INVALID", "AUTH_TOKEN_EXPIRED"].includes(data?.error?.code)
      ) {
        setSession(null, null, null);
      }
      const details = Array.isArray(data?.error?.details)
        ? data.error.details.map((item) => item.message).join("；")
        : "";
      throw new Error(details || data?.error?.message || `HTTP ${response.status}`);
    }
    const bytes = new Uint8ClampedArray(await response.arrayBuffer());
    if (sequence !== state.uploadPreviewSequence) return;
    if (bytes.byteLength !== 4096) throw new Error("圖片資料長度錯誤");

    const canvas = $("#upload-preview-canvas");
    canvas.getContext("2d").putImageData(new ImageData(bytes, 32, 32), 0, 0);
    const logicalWidth = Number(response.headers.get("X-Logical-Width"));
    const logicalHeight = Number(response.headers.get("X-Logical-Height"));
    const contentWidth = Number(response.headers.get("X-Content-Width"));
    const contentHeight = Number(response.headers.get("X-Content-Height"));
    state.uploadMaxCropX = Number(response.headers.get("X-Max-Crop-X")) || 0;
    state.uploadMaxCropY = Number(response.headers.get("X-Max-Crop-Y")) || 0;
    const detected = response.headers.get("X-Pixel-Grid-Detected") === "true";
    const canPan = state.uploadMaxCropX > 0 || state.uploadMaxCropY > 0;
    canvas.classList.toggle("can-pan", canPan);
    $("#upload-pan-controls").classList.toggle("hidden", !canPan);

    const mode = $("#upload-form").elements.image_mode.value;
    if (mode === "pixel" && detected) {
      $("#upload-preview-status").textContent =
        `已還原 ${logicalWidth}×${logicalHeight} 像素網格；內容 ${contentWidth}×${contentHeight}`;
    } else if (mode === "pixel" && canPan) {
      $("#upload-preview-status").textContent =
        `以 ${contentWidth}×${contentHeight} 原始像素裁切，拖曳可調整位置`;
    } else if (mode === "pixel" && logicalWidth > 64) {
      $("#upload-preview-status").textContent = "未偵測到可靠像素網格，已改用完整顯示";
    } else {
      $("#upload-preview-status").textContent =
        `最終內容來源：${contentWidth}×${contentHeight}`;
    }
  } catch (error) {
    if (sequence !== state.uploadPreviewSequence) return;
    clearUploadPreview(error.message);
  }
}

function resetUploadFocus(refresh = true) {
  state.uploadFocusX = 0.5;
  state.uploadFocusY = 0.5;
  if (refresh) loadUploadPreview();
}

function shiftUploadFocus(axis, amount) {
  const maximum = axis === "x" ? state.uploadMaxCropX : state.uploadMaxCropY;
  if (!maximum) return;
  const key = axis === "x" ? "uploadFocusX" : "uploadFocusY";
  const currentCrop = Math.floor(maximum * state[key] + 0.5);
  state[key] = Math.max(0, Math.min(maximum, currentCrop + amount)) / maximum;
  loadUploadPreview();
}

function uploaderText(sprite) {
  return sprite.owner_name ? `上傳者：${sprite.owner_name}` : "上傳者：已刪除的使用者";
}

async function openSpritePreview(sprite) {
  state.previewSprite = sprite;
  $("#sprite-preview-name").textContent = sprite.name;
  $("#sprite-preview-owner").textContent = uploaderText(sprite);
  const tags = $("#sprite-preview-tags");
  tags.replaceChildren();
  addTags(tags, sprite.tags);
  if (!sprite.tags) tags.append(element("span", {className: "meta", text: "無標籤"}));
  const canvas = $("#sprite-preview-canvas");
  canvas.getContext("2d").clearRect(0, 0, 32, 32);
  await paintSprite(canvas, sprite.id);
  await refreshPreviewFavoriteState();
  $("#sprite-preview-dialog").showModal();
}

function spriteCard(sprite) {
  const card = element("article", {className: "sprite-card"});
  const preview = element("button", {
    className: "preview preview-trigger",
    type: "button",
    title: `放大預覽 ${sprite.name}`,
  });
  preview.setAttribute("aria-label", `放大預覽 ${sprite.name}`);
  const canvas = element("canvas");
  canvas.width = 32;
  canvas.height = 32;
  preview.append(canvas);
  paintSprite(canvas, sprite.id);
  preview.addEventListener("click", async () => {
    preview.disabled = true;
    try {
      await openSpritePreview(sprite);
    } finally {
      preview.disabled = false;
    }
  });

  const body = element("div", {className: "card-body"});
  const title = element("div", {className: "card-title"});
  title.append(element("h3", {text: sprite.name}), element("span", {className: "meta", text: `#${sprite.id}`}));
  const tags = element("div", {className: "tags"});
  addTags(tags, sprite.tags);
  body.append(title, element("p", {className: "uploader", text: uploaderText(sprite)}), tags);
  if (sprite.owner_id === state.userId) {
    const actions = element("div", {className: "card-actions"});
    const remove = element("button", {className: "button danger", text: "刪除", type: "button"});
    remove.addEventListener("click", async () => {
      if (!confirm(`確定刪除「${sprite.name}」？素材包內的順序會自動重排。`)) return;
      try {
        await api(`/sprites/${sprite.id}`, {method: "DELETE"}, true);
        notify("素材已刪除");
        loadSprites();
        if (state.token) loadMySprites();
        if (state.activeFavoriteFolderId) loadFavoriteFolder(state.activeFavoriteFolderId);
      } catch (error) { notify(error.message, true); }
    });
    actions.append(remove);
    body.append(actions);
  }
  card.append(preview, body);
  return card;
}

function pagination(container, pagination, onPage) {
  container.replaceChildren();
  const previous = element("button", {className: "button ghost", text: "上一頁", type: "button"});
  previous.disabled = !pagination.has_previous;
  previous.addEventListener("click", () => onPage(pagination.page - 1));
  const label = element("span", {text: pagination.total_pages ? `${pagination.page} / ${pagination.total_pages}` : "0 / 0"});
  const next = element("button", {className: "button ghost", text: "下一頁", type: "button"});
  next.disabled = !pagination.has_next;
  next.addEventListener("click", () => onPage(pagination.page + 1));
  container.append(previous, label, next);
}

async function loadSprites() {
  const form = new FormData($("#sprite-filter"));
  const params = new URLSearchParams({
    page: String(state.spritePage),
    page_size: "24",
    tag_mode: form.get("tag_mode"),
    sort: form.get("sort"),
  });
  if (form.get("name").trim()) params.set("name", form.get("name"));
  if (form.get("tags").trim()) params.set("tags", form.get("tags"));
  const grid = $("#sprite-grid");
  grid.replaceChildren(element("div", {className: "empty", text: "載入中…"}));
  try {
    const data = await api(`/sprites?${params}`);
    grid.replaceChildren();
    if (!data.items.length) grid.append(element("div", {className: "empty", text: "沒有符合條件的素材"}));
    data.items.forEach((sprite) => grid.append(spriteCard(sprite)));
    pagination($("#sprite-pagination"), data.pagination, (page) => {
      state.spritePage = page; loadSprites(); window.scrollTo({top: 0, behavior: "smooth"});
    });
  } catch (error) {
    grid.replaceChildren(element("div", {className: "empty", text: error.message}));
  }
}

// Account views reuse the public sprite cards and pagination.
async function loadAccountDashboard() {
  if (!state.token) return;
  try {
    state.me = await api("/users/me", {}, true);
    $("#profile-email").value = state.me.email;
    const usernameInput = $("#profile-username");
    if (usernameInput.dataset.dirty !== "true") {
      usernameInput.value = state.me.username;
    }
    await Promise.all([loadMySprites(), loadFavoriteFolders()]);
  } catch (error) {
    notify(error.message, true);
  }
}

function switchAccountSection(sectionName) {
  $$(".account-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.accountView === sectionName);
  });
  $$(".account-section").forEach((section) => {
    section.classList.toggle("active", section.id === `account-${sectionName}`);
  });
  if (sectionName === "uploads") loadMySprites();
  if (sectionName === "favorites") loadFavoriteFolders();
}

async function loadMySprites() {
  if (!state.token) return;
  const form = new FormData($("#my-sprite-filter"));
  const params = new URLSearchParams({
    page: String(state.mySpritePage),
    page_size: "24",
    sort: form.get("sort"),
    mine: "true",
  });
  if (form.get("name").trim()) params.set("name", form.get("name"));
  const grid = $("#my-sprite-grid");
  grid.replaceChildren(element("div", {className: "empty", text: "載入中…"}));
  try {
    const data = await api(`/sprites?${params}`, {}, true);
    grid.replaceChildren();
    if (!data.items.length) {
      grid.append(element("div", {className: "empty", text: "尚未上傳任何素材"}));
    }
    data.items.forEach((sprite) => grid.append(spriteCard(sprite)));
    pagination($("#my-sprite-pagination"), data.pagination, (page) => {
      state.mySpritePage = page;
      loadMySprites();
    });
  } catch (error) {
    grid.replaceChildren(element("div", {className: "empty", text: error.message}));
  }
}

function favoriteFolderButton(folder) {
  const button = element("button", {
    className: "favorite-folder-button",
    type: "button",
  });
  button.classList.toggle("active", folder.id === state.activeFavoriteFolderId);
  button.append(
    element("span", {className: "item-name", text: folder.name}),
    element("span", {className: "meta", text: `${folder.sprite_count} / 100`}),
  );
  button.addEventListener("click", () => loadFavoriteFolder(folder.id));
  return button;
}

function renderFavoriteFolders() {
  const list = $("#favorite-folder-list");
  list.replaceChildren();
  $("#favorite-folder-count").textContent = `${state.favoriteFolders.length} / 5`;
  $("#favorite-folder-form button").disabled = state.favoriteFolders.length >= 5;
  $("#favorite-folder-form input").disabled = state.favoriteFolders.length >= 5;
  if (!state.favoriteFolders.length) {
    list.append(element("div", {className: "empty", text: "尚未建立收藏夾"}));
    $("#favorite-folder-heading").classList.add("hidden");
    $("#favorite-sprite-grid").replaceChildren(
      element("div", {className: "empty", text: "建立收藏夾後，即可從素材預覽加入收藏。"}),
    );
    state.activeFavoriteFolderId = null;
    return;
  }
  state.favoriteFolders.forEach((folder) => list.append(favoriteFolderButton(folder)));
}

// Favorite membership is edited as one atomic folder selection.
async function loadFavoriteFolders(options = {}) {
  if (!state.token) return;
  try {
    const data = await api("/favorites/folders", {}, true);
    state.favoriteFolders = data.items;
    if (
      state.activeFavoriteFolderId &&
      !state.favoriteFolders.some((folder) => folder.id === state.activeFavoriteFolderId)
    ) {
      state.activeFavoriteFolderId = null;
    }
    renderFavoriteFolders();
    const preferredId = options.selectId || state.activeFavoriteFolderId;
    if (preferredId) {
      await loadFavoriteFolder(preferredId);
    } else if (state.favoriteFolders.length && options.openFirst !== false) {
      await loadFavoriteFolder(state.favoriteFolders[0].id);
    }
    return data;
  } catch (error) {
    notify(error.message, true);
    return null;
  }
}

async function loadFavoriteFolder(folderId) {
  try {
    const folder = await api(`/favorites/folders/${folderId}`, {}, true);
    state.activeFavoriteFolderId = folder.id;
    $("#favorite-folder-heading").classList.remove("hidden");
    $("#favorite-folder-name").textContent = folder.name;
    $("#favorite-folder-meta").textContent = `${folder.sprite_count} / 100 個素材`;
    const grid = $("#favorite-sprite-grid");
    grid.replaceChildren();
    if (!folder.sprites.length) {
      grid.append(element("div", {className: "empty", text: "這個收藏夾目前沒有素材"}));
    }
    folder.sprites.forEach((sprite) => grid.append(spriteCard(sprite)));
    renderFavoriteFolders();
  } catch (error) {
    notify(error.message, true);
    await loadFavoriteFolders({openFirst: true});
  }
}

async function createFavoriteFolder(name) {
  const folder = await api(
    "/favorites/folders",
    {method: "POST", body: JSON.stringify({name})},
    true,
  );
  await loadFavoriteFolders({selectId: folder.id});
  return folder;
}

async function refreshPreviewFavoriteState() {
  const heart = $("#favorite-heart");
  heart.classList.remove("active");
  heart.textContent = "♡";
  heart.title = state.token ? "管理收藏" : "登入後收藏";
  heart.setAttribute("aria-label", heart.title);
  state.previewFolderIds = [];
  if (!state.token || !state.previewSprite) return;
  try {
    const data = await api(
      `/favorites/sprites/${state.previewSprite.id}`,
      {},
      true,
    );
    state.previewFolderIds = data.folder_ids;
    const active = data.folder_ids.length > 0;
    heart.classList.toggle("active", active);
    heart.textContent = active ? "♥" : "♡";
  } catch (error) {
    notify(error.message, true);
  }
}

function renderFavoritePicker() {
  const list = $("#favorite-picker-list");
  list.replaceChildren();
  const selectedIds = new Set(state.previewFolderIds);
  if (!state.favoriteFolders.length) {
    list.append(element("div", {className: "empty", text: "尚未建立收藏夾"}));
  }
  state.favoriteFolders.forEach((folder) => {
    const row = element("div", {className: "favorite-picker-item"});
    const label = element("label");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.value = String(folder.id);
    checkbox.checked = selectedIds.has(folder.id);
    checkbox.disabled = folder.sprite_count >= 100 && !checkbox.checked;
    label.append(checkbox, element("span", {text: folder.name}));
    row.append(
      label,
      element("span", {className: "meta", text: `${folder.sprite_count} / 100`}),
    );
    list.append(row);
  });
  const atLimit = state.favoriteFolders.length >= 5;
  $("#favorite-picker-create input").disabled = atLimit;
  $("#favorite-picker-create button").disabled = atLimit;
}

async function openFavoritePicker() {
  if (!state.token) {
    $("#sprite-preview-dialog").close();
    notify("請先登入後使用收藏功能", true);
    switchView("account");
    return;
  }
  if (!state.previewSprite) return;
  await loadFavoriteFolders({openFirst: false});
  const membership = await api(
    `/favorites/sprites/${state.previewSprite.id}`,
    {},
    true,
  );
  state.previewFolderIds = membership.folder_ids;
  renderFavoritePicker();
  $("#favorite-picker-dialog").showModal();
}

function packCard(pack) {
  const card = element("article", {className: "pack-card"});
  const info = element("div");
  info.append(
    element("h3", {text: pack.name}),
    element("span", {className: "meta", text: `#${pack.id} · ${pack.sprite_count} 個素材`}),
  );
  const actions = element("div", {className: "card-actions"});
  const view = element("button", {className: "button ghost", text: "查看", type: "button"});
  view.addEventListener("click", () => openPackEditor(pack.id));
  const exportButton = element("button", {className: "button dark", text: "匯出 .db", type: "button"});
  exportButton.addEventListener("click", () => exportPack(pack.id, pack.name));
  actions.append(view, exportButton);
  if (pack.owner_id === state.userId) {
    const remove = element("button", {className: "button danger", text: "刪除", type: "button"});
    remove.addEventListener("click", async () => {
      if (!confirm(`確定刪除素材包「${pack.name}」？`)) return;
      try {
        await api(`/packs/${pack.id}`, {method: "DELETE"}, true);
        notify("素材包已刪除");
        loadPacks();
      } catch (error) { notify(error.message, true); }
    });
    actions.append(remove);
  }
  card.append(info, actions);
  return card;
}

// Pack editing keeps sprite order in one client-side list.
async function loadPacks() {
  const form = new FormData($("#pack-filter"));
  const mine = form.get("mine") === "on";
  if (mine && !state.token) {
    notify("請先登入後查看自己的素材包", true);
    $("#pack-filter").elements.mine.checked = false;
  }
  const params = new URLSearchParams({
    page: String(state.packPage),
    page_size: "20",
    mine: String(mine && Boolean(state.token)),
    sort: form.get("sort"),
  });
  if (form.get("name").trim()) params.set("name", form.get("name"));
  const grid = $("#pack-grid");
  grid.replaceChildren(element("div", {className: "empty", text: "載入中…"}));
  try {
    const data = await api(`/packs?${params}`);
    grid.replaceChildren();
    if (!data.items.length) grid.append(element("div", {className: "empty", text: "沒有符合條件的素材包"}));
    data.items.forEach((pack) => grid.append(packCard(pack)));
    pagination($("#pack-pagination"), data.pagination, (page) => { state.packPage = page; loadPacks(); });
  } catch (error) {
    grid.replaceChildren(element("div", {className: "empty", text: error.message}));
  }
}

async function exportPack(id, name) {
  try {
    const response = await fetch(`/packs/${id}/export`);
    if (!response.ok) {
      const errorBody = await response.json().catch(() => null);
      const error = errorBody?.error;
      const detailText = Array.isArray(error?.details)
        ? error.details.map((item) => item.message).join("；")
        : "";
      throw new Error(detailText || error?.message || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${name.replace(/[\\/:*?"<>|]/g, "_") || `pack-${id}`}.db`;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) { notify(error.message, true); }
}

function renderSelectedSprites(editable) {
  const list = $("#selected-sprites");
  list.replaceChildren();
  $("#selected-count").textContent = `${state.selectedSprites.length} 個`;
  state.selectedSprites.forEach((sprite, index) => {
    const item = element("li", {className: "selected-item"});
    item.draggable = editable;
    item.dataset.index = String(index);

    const preview = element("div", {className: "selected-preview"});
    const canvas = element("canvas");
    canvas.width = 32;
    canvas.height = 32;
    canvas.setAttribute("aria-label", `${sprite.name} 預覽`);
    preview.append(canvas);
    paintSprite(canvas, sprite.id);

    const details = element("div", {className: "selected-details"});
    details.append(
      element("span", {className: "item-name", text: `${index + 1}. ${sprite.name}`}),
      element("span", {className: "meta", text: `#${sprite.id}`}),
    );
    item.append(preview, details);
    if (editable) {
      const actions = element("div", {className: "mini-actions"});
      const up = element("button", {text: "↑", type: "button", title: "上移"});
      const down = element("button", {text: "↓", type: "button", title: "下移"});
      const remove = element("button", {text: "×", type: "button", title: "移除"});
      up.disabled = index === 0;
      down.disabled = index === state.selectedSprites.length - 1;
      up.addEventListener("click", () => moveSelected(index, index - 1));
      down.addEventListener("click", () => moveSelected(index, index + 1));
      remove.addEventListener("click", () => { state.selectedSprites.splice(index, 1); renderSelectedSprites(true); renderLibrary(); });
      actions.append(up, down, remove);
      item.append(actions);
      item.addEventListener("dragstart", () => item.classList.add("dragging"));
      item.addEventListener("dragend", () => item.classList.remove("dragging"));
      item.addEventListener("dragover", (event) => event.preventDefault());
      item.addEventListener("drop", (event) => {
        event.preventDefault();
        const source = Number(list.querySelector(".dragging")?.dataset.index);
        if (Number.isInteger(source)) moveSelected(source, index);
      });
    }
    list.append(item);
  });
}

function moveSelected(from, to) {
  if (to < 0 || to >= state.selectedSprites.length || from === to) return;
  const [item] = state.selectedSprites.splice(from, 1);
  state.selectedSprites.splice(to, 0, item);
  renderSelectedSprites(true);
}

function renderLibrary() {
  const container = $("#editor-library");
  container.replaceChildren();
  const selectedIds = new Set(state.selectedSprites.map((sprite) => sprite.id));
  const search = $("#editor-search").value.trim();
  const selectedFolder = state.editorFavoriteFolders.find(
    (folder) => folder.id === state.editorFavoriteFolderId,
  );
  state.editorLibrary.forEach((sprite) => {
    const row = element("div", {className: "library-item"});
    const preview = element("div", {className: "library-preview"});
    const canvas = element("canvas");
    canvas.width = 32;
    canvas.height = 32;
    canvas.setAttribute("aria-label", `${sprite.name} 預覽`);
    preview.append(canvas);
    paintSprite(canvas, sprite.id);

    const details = element("div", {className: "library-details"});
    details.append(
      element("span", {className: "item-name", text: sprite.name}),
      element("span", {className: "meta", text: `#${sprite.id}`}),
    );
    const tags = element("div", {className: "tags library-tags"});
    addTags(tags, sprite.tags);
    if (!sprite.tags) tags.append(element("span", {className: "meta", text: "無標籤"}));
    details.append(tags);

    const add = element("button", {className: "button ghost", text: selectedIds.has(sprite.id) ? "已加入" : "加入", type: "button"});
    add.disabled = selectedIds.has(sprite.id);
    add.addEventListener("click", () => {
      state.selectedSprites.push(sprite);
      renderSelectedSprites(true);
      renderLibrary();
    });
    row.append(preview, details, add);
    container.append(row);
  });
  if (!state.editorLibrary.length) {
    let message = search ? "沒有符合的素材" : "輸入 ID、名稱或標籤後搜尋素材";
    if (
      state.editorSource === "favorites"
      && !state.editorFavoriteFolders.length
    ) {
      message = "尚未建立收藏夾";
    } else if (
      state.editorSource === "favorites"
      && !state.editorFavoriteFolderId
    ) {
      message = "請先選擇收藏夾";
    } else if (
      state.editorSource === "favorites"
      && selectedFolder?.sprite_count === 0
    ) {
      message = "這個收藏夾目前沒有素材";
    } else if (state.editorSource === "favorites" && !search) {
      message = "輸入 ID、名稱或標籤後搜尋收藏夾素材";
    }
    container.append(element("div", {className: "empty", text: message}));
  }
}

async function loadEditorLibrary() {
  const search = $("#editor-search").value.trim();
  const container = $("#editor-library");
  if (!search) {
    state.editorLibrary = [];
    renderLibrary();
    return;
  }
  container.replaceChildren(element("div", {className: "empty", text: "載入中…"}));
  try {
    if (state.editorSource === "favorites") {
      if (!state.editorFavoriteFolderId) {
        state.editorLibrary = [];
        renderLibrary();
        return;
      }
      const folder = await api(
        `/favorites/folders/${state.editorFavoriteFolderId}`,
        {},
        true,
      );
      const normalizedSearch = search.normalize("NFC").toLowerCase();
      state.editorLibrary = /^\d+$/.test(search)
        ? folder.sprites.filter((sprite) => sprite.id === Number(search))
        : folder.sprites.filter((sprite) =>
          sprite.name.normalize("NFC").toLowerCase().includes(normalizedSearch)
          || sprite.tags.normalize("NFC").toLowerCase().includes(normalizedSearch));
    } else {
      const params = new URLSearchParams({
        page: "1",
        page_size: "100",
        sort: "name_asc",
      });
      if (/^\d+$/.test(search)) {
        params.set("id", search);
        const data = await api(`/sprites?${params}`);
        state.editorLibrary = data.items;
      } else {
        const byName = new URLSearchParams(params);
        byName.set("name", search);
        const byTag = new URLSearchParams(params);
        byTag.set("tags", search);
        byTag.set("tag_mode", "or");
        const [nameData, tagData] = await Promise.all([
          api(`/sprites?${byName}`),
          api(`/sprites?${byTag}`),
        ]);
        const merged = new Map();
        [...nameData.items, ...tagData.items].forEach((sprite) => merged.set(sprite.id, sprite));
        state.editorLibrary = [...merged.values()].sort((left, right) =>
          left.name.localeCompare(right.name, "zh-Hant", {sensitivity: "base"})
          || left.id - right.id);
      }
    }
    renderLibrary();
  } catch (error) {
    state.editorLibrary = [];
    renderLibrary();
    notify(error.message, true);
    if (state.editorSource === "favorites") {
      await loadEditorFavoriteFolders();
      state.editorLibrary = [];
      renderLibrary();
    }
  }
}

function renderEditorFavoriteFolders() {
  const select = $("#editor-folder-select");
  select.replaceChildren();
  state.editorFavoriteFolders.forEach((folder) => {
    const option = element("option", {
      text: `${folder.name}（${folder.sprite_count}）`,
    });
    option.value = String(folder.id);
    option.selected = folder.id === state.editorFavoriteFolderId;
    select.append(option);
  });
  select.disabled = !state.editorFavoriteFolders.length;
}

async function loadEditorFavoriteFolders() {
  try {
    const data = await api("/favorites/folders", {}, true);
    state.editorFavoriteFolders = data.items;
    if (
      !state.editorFavoriteFolders.some(
        (folder) => folder.id === state.editorFavoriteFolderId,
      )
    ) {
      state.editorFavoriteFolderId = state.editorFavoriteFolders[0]?.id || null;
    }
    renderEditorFavoriteFolders();
    state.editorLibrary = [];
    renderLibrary();
  } catch (error) {
    state.editorFavoriteFolders = [];
    state.editorFavoriteFolderId = null;
    state.editorLibrary = [];
    renderEditorFavoriteFolders();
    renderLibrary();
    notify(error.message, true);
  }
}

async function switchEditorSource(source) {
  state.editorSource = source;
  $("#editor-search").value = "";
  state.editorLibrary = [];
  $$(".editor-source-tab").forEach((tab) => {
    const active = tab.dataset.editorSource === source;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  $("#editor-folder-field").classList.toggle("hidden", source !== "favorites");
  if (source === "favorites") {
    await loadEditorFavoriteFolders();
  } else {
    renderLibrary();
  }
}

async function openPackEditor(packId = null) {
  if (!state.token && packId === null) {
    notify("請先登入後建立素材包", true);
    switchView("account");
    return;
  }
  $("#pack-id").value = packId || "";
  state.selectedSprites = [];
  state.editorSource = "all";
  state.editorLibrary = [];
  state.editorFavoriteFolders = [];
  state.editorFavoriteFolderId = null;
  $("#editor-search").value = "";
  renderEditorFavoriteFolders();
  $("#editor-folder-field").classList.add("hidden");
  $$(".editor-source-tab").forEach((tab) => {
    const active = tab.dataset.editorSource === "all";
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  let editable = true;
  if (packId !== null) {
    try {
      const pack = await api(`/packs/${packId}`);
      $("#pack-name").value = pack.name;
      state.selectedSprites = pack.sprites.map((sprite) => ({id: sprite.id, name: sprite.name, tags: sprite.tags}));
      editable = pack.owner_id === state.userId;
      $("#pack-dialog-title").textContent = editable ? "編輯素材包" : "查看素材包";
    } catch (error) { notify(error.message, true); return; }
  } else {
    $("#pack-name").value = "";
    $("#pack-dialog-title").textContent = "建立素材包";
  }
  $("#pack-name").disabled = !editable;
  $("#pack-form").querySelector('button[type="submit"]').classList.toggle("hidden", !editable);
  $("#pack-library-section").classList.toggle("hidden", !editable);
  renderSelectedSprites(editable);
  if (editable) renderLibrary();
  $("#pack-dialog").showModal();
}

// Wire page navigation and dialog controls.
$$(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
$$(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$$(".account-tab").forEach((tab) => {
  tab.addEventListener("click", () => switchAccountSection(tab.dataset.accountView));
});
$("#theme-toggle").addEventListener("click", toggleTheme);
$("#favorite-heart").addEventListener("click", openFavoritePicker);

$("#sprite-filter").addEventListener("submit", (event) => { event.preventDefault(); state.spritePage = 1; loadSprites(); });
$("#pack-filter").addEventListener("submit", (event) => { event.preventDefault(); state.packPage = 1; loadPacks(); });
$("#my-sprite-filter").addEventListener("submit", (event) => {
  event.preventDefault();
  state.mySpritePage = 1;
  loadMySprites();
});
$("#open-upload").addEventListener("click", () => {
  if (!state.token) { notify("請先登入後上傳素材", true); switchView("account"); return; }
  $("#upload-dialog").showModal();
});
$("#upload-form input[name=\"file\"]").addEventListener("change", () => {
  resetUploadFocus(false);
  loadUploadPreview();
});
$("#upload-form select[name=\"image_mode\"]").addEventListener("change", () => {
  resetUploadFocus();
});
$("#upload-form input[name=\"trim_transparent\"]").addEventListener("change", () => {
  resetUploadFocus();
});
$("#reset-upload-focus").addEventListener("click", () => resetUploadFocus());
$$("[data-pan-x]").forEach((button) => {
  button.addEventListener("click", () => {
    shiftUploadFocus("x", Number(button.dataset.panX));
  });
});
$$("[data-pan-y]").forEach((button) => {
  button.addEventListener("click", () => {
    shiftUploadFocus("y", Number(button.dataset.panY));
  });
});
{
  const canvas = $("#upload-preview-canvas");
  let drag = null;
  canvas.addEventListener("pointerdown", (event) => {
    if (!state.uploadMaxCropX && !state.uploadMaxCropY) return;
    drag = {
      x: event.clientX,
      y: event.clientY,
      focusX: state.uploadFocusX,
      focusY: state.uploadFocusY,
    };
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointerup", (event) => {
    if (!drag) return;
    const pixels = canvas.clientWidth / 32;
    if (state.uploadMaxCropX) {
      state.uploadFocusX = Math.max(
        0,
        Math.min(
          1,
          drag.focusX
            - (event.clientX - drag.x) / (pixels * state.uploadMaxCropX),
        ),
      );
    }
    if (state.uploadMaxCropY) {
      state.uploadFocusY = Math.max(
        0,
        Math.min(
          1,
          drag.focusY
            - (event.clientY - drag.y) / (pixels * state.uploadMaxCropY),
        ),
      );
    }
    drag = null;
    canvas.classList.remove("dragging");
    loadUploadPreview();
  });
  canvas.addEventListener("pointercancel", () => {
    drag = null;
    canvas.classList.remove("dragging");
  });
}
$("#create-pack").addEventListener("click", () => openPackEditor());
$("#editor-search-button").addEventListener("click", loadEditorLibrary);
$("#editor-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadEditorLibrary(); } });
$$(".editor-source-tab").forEach((tab) => {
  tab.addEventListener("click", () => switchEditorSource(tab.dataset.editorSource));
});
$("#editor-folder-select").addEventListener("change", (event) => {
  state.editorFavoriteFolderId = Number(event.currentTarget.value) || null;
  $("#editor-search").value = "";
  state.editorLibrary = [];
  renderLibrary();
});

$("#logout-button").addEventListener("click", async () => {
  try { await api("/auth/logout", {method: "POST"}, true); }
  catch (error) { notify(error.message, true); }
  finally {
    state.me = null;
    state.favoriteFolders = [];
    state.activeFavoriteFolderId = null;
    setSession(null, null, null);
  }
});

$("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  try {
    state.me = await api(
      "/users/me",
      {
        method: "PATCH",
        body: JSON.stringify({username: $("#profile-username").value}),
      },
      true,
    );
    $("#profile-username").value = state.me.username;
    $("#profile-username").dataset.dirty = "false";
    notify("暱稱已更新");
    loadSprites();
    loadMySprites();
    if (state.activeFavoriteFolderId) loadFavoriteFolder(state.activeFavoriteFolderId);
  } catch (error) {
    notify(error.message, true);
    formElement.reportValidity();
  }
});
$("#profile-username").addEventListener("input", (event) => {
  event.currentTarget.dataset.dirty = "true";
});

$("#favorite-folder-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const name = new FormData(formElement).get("name");
  try {
    await createFavoriteFolder(name);
    formElement.reset();
    notify("收藏夾已建立");
  } catch (error) {
    notify(error.message, true);
  }
});

$("#rename-favorite-folder").addEventListener("click", async () => {
  const folder = state.favoriteFolders.find(
    (item) => item.id === state.activeFavoriteFolderId,
  );
  if (!folder) return;
  const name = prompt("新的收藏夾名稱", folder.name);
  if (name === null) return;
  try {
    await api(
      `/favorites/folders/${folder.id}`,
      {method: "PATCH", body: JSON.stringify({name})},
      true,
    );
    await loadFavoriteFolders({selectId: folder.id});
    notify("收藏夾已重新命名");
  } catch (error) {
    notify(error.message, true);
  }
});

$("#delete-favorite-folder").addEventListener("click", async () => {
  const folder = state.favoriteFolders.find(
    (item) => item.id === state.activeFavoriteFolderId,
  );
  if (!folder || !confirm(`確定刪除收藏夾「${folder.name}」？素材本身不會被刪除。`)) {
    return;
  }
  try {
    await api(`/favorites/folders/${folder.id}`, {method: "DELETE"}, true);
    state.activeFavoriteFolderId = null;
    await loadFavoriteFolders({openFirst: true});
    await refreshPreviewFavoriteState();
    notify("收藏夾已刪除");
  } catch (error) {
    notify(error.message, true);
  }
});

$("#favorite-picker-create").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const name = new FormData(formElement).get("name");
  try {
    const folder = await createFavoriteFolder(name);
    state.previewFolderIds = [...new Set([...state.previewFolderIds, folder.id])];
    formElement.reset();
    renderFavoritePicker();
  } catch (error) {
    notify(error.message, true);
  }
});

$("#save-favorite-membership").addEventListener("click", async () => {
  if (!state.previewSprite) return;
  const folderIds = $$("#favorite-picker-list input[type='checkbox']:checked").map(
    (checkbox) => Number(checkbox.value),
  );
  try {
    const result = await api(
      `/favorites/sprites/${state.previewSprite.id}`,
      {method: "PUT", body: JSON.stringify({folder_ids: folderIds})},
      true,
    );
    state.previewFolderIds = result.folder_ids;
    $("#favorite-picker-dialog").close();
    await refreshPreviewFavoriteState();
    await loadFavoriteFolders({
      selectId: state.activeFavoriteFolderId,
      openFirst: false,
    });
    notify(folderIds.length ? "收藏已更新" : "已移出所有收藏夾");
  } catch (error) {
    notify(error.message, true);
  }
});

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  form.set("image_mode", formElement.elements.image_mode.value);
  form.set(
    "trim_transparent",
    formElement.elements.trim_transparent.checked ? "true" : "false",
  );
  form.set("focus_x", String(state.uploadFocusX));
  form.set("focus_y", String(state.uploadFocusY));
  try {
    await api("/sprites", {method: "POST", body: form}, true);
    formElement.reset();
    resetUploadFocus(false);
    clearUploadPreview();
    $("#upload-dialog").close();
    notify("素材已上傳");
    state.spritePage = 1;
    loadSprites();
  } catch (error) { notify(error.message, true); }
});

$("#pack-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("#pack-id").value;
  const body = {name: $("#pack-name").value, sprite_ids: state.selectedSprites.map((sprite) => sprite.id)};
  try {
    await api(id ? `/packs/${id}` : "/packs", {method: id ? "PATCH" : "POST", body: JSON.stringify(body)}, true);
    $("#pack-dialog").close();
    notify(id ? "素材包已更新" : "素材包已建立");
    state.packPage = 1;
    loadPacks();
  } catch (error) { notify(error.message, true); }
});

renderSession();
renderAccountAuthState();
renderTheme();
loadSprites();

if (oauthToken) {
  api("/users/me", {}, true)
    .then((me) => {
      state.me = me;
      setSession(oauthToken, me.id, me.email);
      notify("已透過 Gitea 登入");
      switchView("sprites");
    })
    .catch((error) => {
      setSession(null, null, null);
      notify(error.message, true);
    });
} else if (oauthError) {
  notify("Gitea 登入未完成，請再試一次", true);
}

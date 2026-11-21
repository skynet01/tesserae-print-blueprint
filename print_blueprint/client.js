// print_blueprint: the sheet server.py drew, full bleed, or a small empty / error state.
// The sheet is a finished picture at the cell's size, footnote included; nothing here draws on it.

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

const HEX = /^#[0-9a-f]{6}$/i;

function colour(value, fallback) {
  return HEX.test(String(value || "")) ? value : fallback;
}

function cellLook(shadow, dashboard) {
  const cell = shadow.host?.closest(".cell");
  if (!cell || !(dashboard || cell.getAttribute("data-theme") || cell.getAttribute("data-style") ||
                 cell.style.getPropertyValue("--font-family"))) return null;
  const css = getComputedStyle(shadow.host);
  // No resolved dashboard colours: keep the server's blue-paper fallback.
  if (dashboard && (!css.getPropertyValue("--surface").trim() ||
                    !css.getPropertyValue("--text-primary").trim())) return null;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const pen = canvas.getContext("2d");
  const hex = (token, fallback) => {
    pen.fillStyle = fallback;
    pen.fillStyle = css.getPropertyValue(token).trim() || fallback;
    pen.fillRect(0, 0, 1, 1);
    return "#" + Array.from(pen.getImageData(0, 0, 1, 1).data).slice(0, 3)
      .map(v => v.toString(16).padStart(2, "0")).join("");
  };
  return {bg: hex("--surface", "#ffffff"), ink: hex("--text-primary", "#000000"),
          rule: hex("--text-secondary", "#555555"), accent: hex("--accent-4", "#000000"),
          font: css.getPropertyValue("--font-family").trim()};
}

function sameLook(a, b) {
  if (!a || !b) return !a && !b;
  const family = s => String(s).split(",")[0].trim().replace(/["']/g, "").toLowerCase();
  return ["bg", "ink", "rule", "accent"].every(k => a[k] === b[k]) && family(a.font) === family(b.font);
}

export default async function render(shadow, ctx) {
  const t = ctx?.t || ((key, fallback) => fallback ?? key);
  let data = ctx?.data ?? {};
  if (data.ai_wait && !ctx?.preview) {
    // Awaited by Tesserae's compose signal (25 s), outside its six-second fetch budget.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15_000);
    try {
      const prefix = window.TESSERAE_URL_PREFIX || "";
      const response = await fetch(prefix + data.ai_wait, {signal: controller.signal, cache: "no-store"});
      if (response.ok && response.status === 200) {
        const ready = await response.json();
        if (ready.image) data = {...data, image: ready.image, note: ""};
      }
    } catch {
      // Keep the quick sheet on a timeout or failed request.
    } finally {
      clearTimeout(timer);
    }
  }
  const look = cellLook(shadow, ctx?.cell?.options?.background === "dashboard");
  if (!sameLook(look, data.cell_look)) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 9_000);
    try {
      const query = new URLSearchParams({look: JSON.stringify(look), options: JSON.stringify(ctx?.cell?.options || {}),
        w: data.w || ctx?.cell?.w || 1200, h: data.h || ctx?.cell?.h || 800,
        preview: data.look_preview ? "1" : "0", panel: data.look_panel || "colour",
        target: data.look_device || ""});
      const response = await fetch((window.TESSERAE_URL_PREFIX || "") + "/plugins/print_blueprint/look?" + query,
                                   {signal: controller.signal, cache: "no-store"});
      if (response.ok) {
        const styled = await response.json();
        if (!styled.error) data = {...data, ...styled};
      }
    } catch {
      // Keep the last usable sheet if the live editor's look cannot be resolved in time.
    } finally {
      clearTimeout(timer);
    }
  }
  const size = ctx?.cell?.size || "md";
  const bg = colour(data.colors?.bg, "#1c4688");
  const ink = colour(data.colors?.ink, "#ecf2ff");
  const head = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <link rel="stylesheet" href="/static/icons/phosphor/bold/style.css">
    <link rel="stylesheet" href="/plugins/print_blueprint/client.css">`;

  if (data.error) {
    shadow.innerHTML = `${head}
      <div class="w" data-widget="print_blueprint">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(t("widget_title", "Print Blueprint"))}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  const style = `--pb-bg:${bg}; --pb-ink:${ink}`;
  if (data.image) {
    const printer = String(data.printer || t("a_printer", "a printer"));
    const title = String(data.title || t("a_print", "a print"));
    let alt = t("sheet_alt", "Blueprint of {title} on {printer}")
      .replace("{title}", title).replace("{printer}", printer);
    if (data.note) alt += `. ${data.note}`;
    shadow.innerHTML = `${head}
      <div class="w is-bleed pb size-${escapeHtml(size)}" data-widget="print_blueprint" style="${escapeHtml(style)}">
        <img class="sheet" src="${escapeHtml(data.image)}" alt="${escapeHtml(alt)}" title="${escapeHtml(data.note || "")}">
      </div>`;
    return;
  }

  const empty = data.empty || {};
  const kind = String(empty.kind || "nothing");
  const printer = String(empty.printer || "");
  const title = t(kind, empty.title || "Nothing to draw yet");
  const detail = t(`${kind}_detail`, empty.detail || "").replace("{printer}", printer);
  shadow.innerHTML = `${head}
    <div class="w is-bleed pb pb-empty size-${escapeHtml(size)}" data-widget="print_blueprint" style="${escapeHtml(style)}">
      <div class="empty">
        <i class="ph-bold ph-printer" aria-hidden="true"></i>
        <strong>${escapeHtml(title)}</strong>
        ${detail ? `<span>${escapeHtml(detail)}</span>` : ""}
      </div>
    </div>`;
}

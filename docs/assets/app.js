(function () {
  const KEY = "arxiv-digest-saved";
  const load = () => { try { return new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); } catch (e) { return new Set(); } };
  const store = (s) => { try { localStorage.setItem(KEY, JSON.stringify([...s])); } catch (e) {} };
  let saved = load();

  function paintStars(root) {
    root.querySelectorAll(".star").forEach((b) => {
      const on = saved.has(b.dataset.id);
      b.classList.toggle("on", on);
      b.textContent = on ? "★" : "☆";
    });
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest(".star");
    if (!b) return;
    saved.has(b.dataset.id) ? saved.delete(b.dataset.id) : saved.add(b.dataset.id);
    store(saved);
    paintStars(document);
    if (window.__rerender) window.__rerender();
  });

  function math(el) {
    if (window.renderMathInElement) {
      try {
        renderMathInElement(el, { delimiters: [{ left: "$$", right: "$$", display: true }, { left: "$", right: "$", display: false }], throwOnError: false });
      } catch (e) {}
    }
  }

  async function search() {
    const q = document.getElementById("q");
    if (!q) return;
    const only = document.getElementById("savedonly");
    if (location.hash === "#saved") { only.checked = true; document.getElementById("sh").textContent = "Saved"; }
    const root = document.body.dataset.root || "";
    let rows = [];
    try { rows = await fetch(root + "data/all.json", { cache: "no-store" }).then((r) => r.json()); } catch (e) {}
    const esc = (s) => String(s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const out = document.getElementById("results"), cnt = document.getElementById("count");
    function render() {
      const terms = q.value.toLowerCase().split(/\s+/).filter(Boolean);
      let list = rows.filter((r) => (!only.checked || saved.has(r.id)) &&
        terms.every((t) => (r.t + " " + r.a + " " + r.k + " " + r.x + " " + r.id).toLowerCase().includes(t)));
      if (!terms.length && !only.checked) list = list.slice(0, 60);
      cnt.textContent = list.length + (list.length === 1 ? " paper" : " papers");
      out.innerHTML = list.slice(0, 300).map((r) =>
        `<li class="row"><span class="score ${r.s >= 8 ? "hi" : r.s >= 6 ? "mid" : "lo"}">${Math.round(r.s)}</span><div>` +
        `<a href="https://arxiv.org/abs/${esc(r.id)}">${esc(r.t)}</a><span class="sub">${esc(r.a)} · <a class="date" href="${root}days/${r.d}.html#p${esc(r.id)}">${r.d}</a> · ${esc(r.k)}</span>` +
        `<span class="reason">${esc(r.x)}</span></div><button class="star" data-id="${esc(r.id)}">☆</button></li>`).join("");
      paintStars(out); math(out);
    }
    window.__rerender = render;
    q.addEventListener("input", render);
    only.addEventListener("change", render);
    window.addEventListener("hashchange", () => { only.checked = location.hash === "#saved"; render(); });
    render();
  }

  document.addEventListener("DOMContentLoaded", () => {
    paintStars(document);
    search();
  });
  window.addEventListener("load", () => math(document.body));
})();

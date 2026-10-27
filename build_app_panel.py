"""Gera o panel.html do app (painel com login) a partir do index.html.
Uso: python build_app_panel.py index.html caminho/do/app/panel.html"""
import sys

CSS = """
  .actions .who{font-size:12px;color:var(--mut);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .actions .btn.ghost{background:transparent;border:1px solid var(--line2);box-shadow:none;color:var(--ink);font-weight:500}
  .actions .btn.ghost:hover{background:var(--glass2)}
  @media (max-width:640px){ .actions{flex-wrap:wrap;margin-left:0} }
"""

BOOT = r"""
const DADOS_URL = {{DADOS_URL}}, LOGIN_URL = {{LOGIN_URL}}, MARCA = {{MARCA}};
(function marcaDagua(){
  const t = String(MARCA).replace(/[<&>"]/g, c => ({"<":"&lt;","&":"&amp;",">":"&gt;",'"':"&quot;"}[c]));
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="420" height="220"><text x="20" y="130" transform="rotate(-22 210 110)" font-family="Inter,Arial,sans-serif" font-size="15" fill="white" fill-opacity=".055">${t}</text></svg>`;
  const el = document.createElement("div");
  el.setAttribute("aria-hidden","true");
  el.style.cssText = "position:fixed;inset:0;z-index:50;pointer-events:none;background-image:url('data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg) + "')";
  document.body.appendChild(el);
})();
(async function carregar(){
  const fail = msg => { document.querySelector("main").innerHTML = '<div class="card">Não foi possível carregar os dados do painel (' + msg + ').</div>'; };
  try {
    const r = await fetch(DADOS_URL, {credentials: "same-origin", cache: "no-store"});
    if (r.status === 401) { location.href = LOGIN_URL; return; }   // sessão encerrada: volta ao login
    if (!r.ok) {
      let m = "HTTP " + r.status;
      try { const j = await r.json(); if (j && j.message) m = j.message; } catch (e) {}
      return fail(m);
    }
    let d;
    if ((r.headers.get("Content-Type") || "").includes("gzip")) {
      if (!("DecompressionStream" in window)) return fail("navegador desatualizado; atualize o Chrome, Edge, Firefox ou Safari");
      d = await new Response(r.body.pipeThrough(new DecompressionStream("gzip"))).json();
    } else {
      d = await r.json();
    }
    window.ANS_DATA = d;
    start();
  } catch (e) { fail(e.message); }
})();
"""

idx, out = sys.argv[1:3]
h = open(idx, encoding="utf-8").read()
h = h.replace('<script src="data.js"></script>\n', "")
h = h.replace('href="modelo-comissoes.xlsx" download', 'href="{{MODELO_URL}}"')
h = h.replace('<div class="actions">', '<div class="actions">\n    {{USUARIO}}', 1)
h = h.replace("DADOS DE DEMONSTRAÇÃO (fictícios). Rode ans_update.py sem --demo para carregar os dados reais da ANS.",
              "DADOS DE DEMONSTRAÇÃO (fictícios).")
h = h.replace("</style>", CSS + "</style>", 1)
a = h.index("<script>\n") + len("<script>\n")
b = h.index("</script>", a)
h = h[:a] + "function start(){\n" + h[a:b] + "}\n" + BOOT + h[b:]
for ph in ("{{DADOS_URL}}", "{{LOGIN_URL}}", "{{MARCA}}", "{{MODELO_URL}}", "{{USUARIO}}"):
    assert ph in h, ph
open(out, "w", encoding="utf-8").write(h)
print("panel.html gerado")

"""Gera as partes do Painel de Mercado usadas pelo app Maestro a partir do index.html.

Uso: python build_app_panel.py index.html caminho/do/app/app/painel
Saída: panel.css, panel.html e panel.js (o app encaixa na moldura do Maestro).
O index.html continua abrindo sozinho no navegador (com data.js) para testes locais.
"""
import os
import sys

BOOT = r"""
const DADOS_URL = {{DADOS_URL}}, LOGIN_URL = {{LOGIN_URL}};
(async function carregar(){
  const alvo = document.getElementById("painel");
  const fail = msg => { alvo.innerHTML = '<div class="card">Não foi possível carregar os dados do painel (' + msg + ').</div>'; };
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


def entre(texto, ini, fim):
    a = texto.index(ini) + len(ini)
    return texto[a:texto.index(fim, a)]


idx, saida = sys.argv[1:3]
h = open(idx, encoding="utf-8").read()
css = entre(h, "/*PAINEL-CSS*/", "/*/PAINEL-CSS*/").strip()
html = entre(h, "<!--PAINEL-HTML-->", "<!--/PAINEL-HTML-->").strip()
html = html.replace("DADOS DE DEMONSTRAÇÃO (fictícios). Rode ans_update.py sem --demo para carregar os dados reais da ANS.",
                    "DADOS DE DEMONSTRAÇÃO (fictícios).")
a = h.index('<script src="data.js"></script>')
corpo = entre(h[a:], "<script>\n", "</script>")
js = "function start(){\n" + corpo + "}\n" + BOOT
assert 'id="painel"' in html and "{{DADOS_URL}}" in js
os.makedirs(saida, exist_ok=True)
for nome, conteudo in (("panel.css", css), ("panel.html", html), ("panel.js", js)):
    open(os.path.join(saida, nome), "w", encoding="utf-8").write(conteudo + "\n")
print(f"partes do painel geradas em {saida}: css {len(css)//1024} KB, html {len(html)//1024} KB, js {len(js)//1024} KB")

#!/usr/bin/env python3
"""
Painel ANS - Ramo Hub
Baixa os dados abertos da ANS (beneficiarios, reclamacoes NIP e demonstracoes
contabeis), agrega e gera o arquivo data.js que o painel (index.html) le.

Uso:
  pip install pandas
  python ans_update.py                      # Brasil inteiro, ultimos 13 meses
  python ans_update.py --ufs GO,MT,MS,DF,SP,RJ,MG,ES   # so Centro-Oeste e Sudeste
  python ans_update.py --inspect            # mostra pastas/colunas encontradas na ANS
  python ans_update.py --demo               # gera dados FICTICIOS para testar o painel

Atualize 1x por mes (a ANS publica os beneficiarios mensalmente).
"""
import argparse
import io
import json
import random
import re
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

BASE = "https://dadosabertos.ans.gov.br/FTP/PDA/"
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
UA = {"User-Agent": "ramo-painel-ans/1.0"}

UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
       "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

# Nomes de coluna candidatos (a ANS ja mudou nomes no passado; ajuste aqui se preciso)
COLS = {
    "uf": ["SG_UF"],
    "cd_op": ["CD_OPERADORA", "REGISTRO_ANS", "REG_ANS", "CD_REGISTRO_ANS"],
    "nome": ["NM_RAZAO_SOCIAL", "RAZAO_SOCIAL"],
    "modalidade": ["MODALIDADE_OPERADORA"],
    "contr": ["DE_CONTRATACAO_PLANO", "CONTRATACAO_PLANO"],
    "cobertura": ["COBERTURA_ASSIST_PLAN", "COBERTURA_ASSISTENCIAL"],
    "cd_mun": ["CD_MUNICIPIO"],
    "nm_mun": ["NM_MUNICIPIO"],
    "ativos": ["QT_BENEFICIARIO_ATIVO"],
}


# ---------------------------------------------------------------- utilidades
def http_get(url, tries=3):
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=UA), timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def list_links(url):
    html = http_get(url).decode("utf-8", "ignore")
    links = re.findall(r'href="([^"?#][^"]*)"', html)
    return [l for l in links if not l.startswith(("/", "..", "http"))]


def find_col(cols, key):
    up = {c.upper(): c for c in cols}
    for cand in COLS[key]:
        if cand in up:
            return up[cand]
    return None


def month_shift(ym, k):
    t = int(ym[:4]) * 12 + int(ym[4:]) - 1 + k
    return f"{t // 12}{t % 12 + 1:02d}"


def norm_contr(v):
    v = str(v).upper()
    if "INDIV" in v:
        return "Individual/familiar"
    if "EMPRES" in v:
        return "Coletivo empresarial"
    if "ADES" in v:
        return "Coletivo por adesão"
    return "Outros"


CONTRS = ["Individual/familiar", "Coletivo empresarial", "Coletivo por adesão", "Outros"]


def read_csv_chunks(zbytes, **kw):
    """Le o primeiro CSV do zip em pedacos, tentando utf-8 e depois latin1."""
    for enc in ("utf-8", "latin1"):
        try:
            with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
                name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
                with z.open(name) as f:
                    for chunk in pd.read_csv(f, sep=";", encoding=enc, dtype=str,
                                             chunksize=400_000, **kw):
                        yield chunk
            return
        except UnicodeDecodeError:
            continue


def cached_download(url, dest):
    dest.parent.mkdir(exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes()
    data = http_get(url)
    dest.write_bytes(data)
    return data


# ---------------------------------------------------------------- beneficiarios
def find_dataset_dir(keyword):
    for l in list_links(BASE):
        if keyword in l.lower():
            return BASE + l
    return None


def process_benef_zip(zbytes):
    agg, mun = defaultdict(int), defaultdict(int)
    for ch in read_csv_chunks(zbytes):
        c = {k: find_col(ch.columns, k) for k in COLS}
        if not (c["uf"] and c["cd_op"] and c["ativos"]):
            raise RuntimeError(f"Colunas inesperadas: {list(ch.columns)}")
        if c["cobertura"]:  # so planos medico-hospitalares (exclui odontologico)
            ch = ch[ch[c["cobertura"]].str.upper().str.contains("HOSPITALAR", na=False)]
        ch["_q"] = pd.to_numeric(ch[c["ativos"]], errors="coerce").fillna(0)
        ch["_c"] = ch[c["contr"]].map(norm_contr) if c["contr"] else "Outros"
        g = ch.groupby([c["uf"], c["cd_op"], c["nome"] or c["cd_op"],
                        c["modalidade"] or c["cd_op"], "_c"])["_q"].sum()
        for (uf, cd, nm, md, ct), q in g.items():
            agg[(uf, cd, nm, md, ct)] += int(q)
        if c["cd_mun"]:
            g2 = ch.groupby([c["uf"], c["cd_mun"], c["nm_mun"] or c["cd_mun"], c["cd_op"]])["_q"].sum()
            for k, q in g2.items():
                mun[k] += int(q)
    return agg, mun


def find_benef_root():
    """Procura, entre todas as pastas com 'benefici' no nome, a que tem subpastas AAAAMM."""
    top = list_links(BASE)
    cands = [BASE + l for l in top if "benefici" in l.lower()]
    seen = []
    for c in cands:
        try:
            links = list_links(c)
        except Exception as e:  # noqa
            seen.append((c, f"erro: {e}"))
            continue
        months = [l.strip("/") for l in links if re.fullmatch(r"\d{6}/?", l)]
        if months:
            return c, sorted(months)
        seen.append((c, links[:20]))
    print("=== DIAGNOSTICO: pasta de beneficiarios nao encontrada ===")
    print("Raiz (primeiros 80 itens):", top[:80])
    for c, info in seen:
        print("Candidata:", c, "->", info)
    sys.exit("Pasta de beneficiarios com subpastas AAAAMM nao encontrada. Envie o diagnostico acima.")


def build_benef(months, ufs):
    root, avail = find_benef_root()
    latest = avail[-1]
    wanted = [m for m in (month_shift(latest, -i) for i in range(months)) if m in avail][::-1]
    print(f"Competencias: {wanted[0]} a {wanted[-1]}")

    jobs = []
    for m in wanted:
        folder = root + m + "/"
        files = {re.search(r"-([A-Z]{2})-", f).group(1): f for f in list_links(folder)
                 if f.endswith(".zip") and re.search(r"-([A-Z]{2})-", f)}
        for uf in ufs:
            if uf in files:
                jobs.append((m, uf, folder + files[uf]))

    def dl(j):
        m, uf, url = j
        return j, cached_download(url, CACHE / f"benef_{m}_{uf}.zip")

    series, ops, muni = [], {}, []
    with ThreadPoolExecutor(4) as ex:
        for n, (j, zb) in enumerate(ex.map(dl, jobs), 1):
            m, uf, _ = j
            print(f"[{n}/{len(jobs)}] {m} {uf}", flush=True)
            agg, mun = process_benef_zip(zb)
            for (u, cd, nm, md, ct), q in agg.items():
                series.append([wanted.index(m), u, cd, CONTRS.index(ct), q])
                ops[cd] = {"n": nm, "m": md}
            if m == wanted[-1]:
                for (u, cm, nmun, cd), q in mun.items():
                    muni.append([u, cm, nmun, cd, q])
    return wanted, series, ops, muni


# ---------------------------------------------------------------- NIP e financeiro
def build_nip():
    """Conta reclamacoes (NIP) por operadora no ano mais recente disponivel."""
    d = find_dataset_dir("nip") or find_dataset_dir("demandas")
    if not d:
        print("AVISO: dataset NIP nao encontrado; reputacao fica fora do score.")
        return {}
    files = sorted(l for l in list_links(d) if l.lower().endswith((".zip", ".csv")))
    if not files:
        return {}
    out, used = defaultdict(int), files[-1]
    data = cached_download(d + used, CACHE / f"nip_{used}")
    try:
        for ch in read_csv_chunks(data) if used.endswith(".zip") else [pd.read_csv(io.BytesIO(data), sep=";", dtype=str)]:
            col = find_col(ch.columns, "cd_op")
            if not col:
                print("AVISO: coluna de operadora nao achada no NIP:", list(ch.columns))
                return {}
            for cd, q in ch[col].value_counts().items():
                out[str(cd)] += int(q)
    except Exception as e:  # noqa
        print("AVISO: falha lendo NIP:", e)
        return {}
    print(f"NIP: {used} ({len(out)} operadoras)")
    return dict(out)


def build_fin():
    """PL (conta 25) e sinistralidade (conta 41 / conta 31) da ultima demonstracao."""
    d = find_dataset_dir("demonstracoes_contabeis") or find_dataset_dir("contabeis")
    if not d:
        print("AVISO: demonstracoes contabeis nao encontradas; solidez fica fora do score.")
        return {}
    years = sorted(l.strip("/") for l in list_links(d) if re.fullmatch(r"\d{4}/?", l))
    if not years:
        return {}
    folder = d + years[-1] + "/"
    files = sorted(l for l in list_links(folder) if l.lower().endswith(".zip"))
    if not files:
        return {}
    data = cached_download(folder + files[-1], CACHE / f"fin_{years[-1]}_{files[-1]}")
    acc = defaultdict(dict)
    try:
        for ch in read_csv_chunks(data):
            ch.columns = [c.upper() for c in ch.columns]
            reg = find_col(ch.columns, "cd_op")
            if not reg or "CD_CONTA_CONTABIL" not in ch.columns:
                print("AVISO: colunas inesperadas em contabeis:", list(ch.columns))
                return {}
            ch = ch[ch["CD_CONTA_CONTABIL"].isin(["25", "31", "41"])]
            for r, c, v in zip(ch[reg], ch["CD_CONTA_CONTABIL"], ch["VL_SALDO_FINAL"]):
                acc[str(r)][c] = float(str(v).replace(".", "").replace(",", "."))
    except Exception as e:  # noqa
        print("AVISO: falha lendo contabeis:", e)
        return {}
    out = {}
    for r, a in acc.items():
        s = (a.get("41", 0) / a["31"]) if a.get("31") else None
        out[r] = {"pl": a.get("25"), "sin": s}
    print(f"Financeiro: {files[-1]} ({len(out)} operadoras)")
    return out


# ---------------------------------------------------------------- demo
def build_demo(months):
    random.seed(7)
    latest = "202609"
    wanted = [month_shift(latest, -i) for i in range(months)][::-1]
    ops, series, nip, fin = {}, [], {}, {}
    mods = ["Medicina de grupo", "Cooperativa médica", "Seguradora especializada em saúde", "Autogestão"]
    for i in range(1, 41):
        cd = str(300000 + i)
        ops[cd] = {"n": f"OPERADORA DEMO {i:02d} (FICTÍCIA)", "m": random.choice(mods)}
        size, trend = random.randint(5_000, 900_000), random.uniform(-0.08, 0.18)
        nip[cd] = int(size / 10_000 * random.uniform(1, 25))
        fin[cd] = {"pl": random.choice([1, 1, 1, 1, -1]) * random.uniform(1e6, 3e8),
                   "sin": random.uniform(0.6, 0.95)}
        for uf in random.sample(UFS, random.randint(2, 12)):
            share = random.random()
            for mi in range(months):
                f = 1 + trend * (mi - months + 1) / (months - 1)
                for ci in range(3):
                    q = int(size * share * f * [0.2, 0.55, 0.25][ci] / 3)
                    series.append([mi, uf, cd, ci, max(q, 0)])
    muni = []
    for uf in UFS:
        for k in range(8):
            for cd in random.sample(list(ops), 6):
                muni.append([uf, f"{uf}{k:03d}", f"Município Demo {uf}-{k + 1}", cd, random.randint(200, 40_000)])
    return wanted, series, ops, muni, nip, fin


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ufs", default=",".join(UFS))
    ap.add_argument("--months", type=int, default=13)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    a = ap.parse_args()

    if a.inspect:
        for l in list_links(BASE):
            print(l)
        return

    if a.demo:
        wanted, series, ops, muni, nip, fin = build_demo(a.months)
    else:
        ufs = [u.strip().upper() for u in a.ufs.split(",") if u.strip()]
        wanted, series, ops, muni = build_benef(a.months, ufs)
        nip, fin = build_nip(), build_fin()

    payload = {
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "demo": a.demo,
        "meses": wanted,
        "ops": ops,
        "series": series,
        "muni": muni,
        "nip": nip,
        "fin": fin,
    }
    txt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out = HERE / "data.js"
    out.write_text("window.ANS_DATA = " + txt + ";", encoding="utf-8")
    (HERE / "data.json").write_text(txt, encoding="utf-8")  # usado pelo plugin do WordPress
    print(f"OK -> {out} e data.json ({out.stat().st_size / 1e6:.1f} MB). Abra index.html no navegador.")


if __name__ == "__main__":
    main()

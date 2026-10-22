#!/usr/bin/env python3
"""
Painel ANS - Ramo Hub
Baixa os dados abertos da ANS (beneficiarios, reclamacoes NIP, demonstracoes
contabeis e os indices oficiais IGR, IDSS e Classificacao Prudencial), agrega e
gera os arquivos data.js e data.json que o painel (index.html / plugin WP) le.

Uso:
  pip install pandas
  python ans_update.py                      # Brasil inteiro, ultimos 13 meses
  python ans_update.py --ufs GO,MT,MS,DF,SP,RJ,MG,ES   # so Centro-Oeste e Sudeste
  python ans_update.py --inspect            # mostra pastas/colunas encontradas na ANS
  python ans_update.py --demo               # gera dados FICTICIOS para testar o painel

Arquivos auxiliares: ibge_renda.json (renda do IBGE, atualizar 1x por ano em fevereiro).

Atualize 1x por mes (a ANS publica os beneficiarios mensalmente).
Linhas que comecam com "AVISO:" indicam um conjunto que nao foi lido; o painel
continua funcionando e apenas tira aquele criterio do score.
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
UA = {"User-Agent": "ramo-painel-ans/1.1"}

UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
       "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]

# Nomes de coluna candidatos (a ANS ja mudou nomes no passado; ajuste aqui se preciso)
COLS = {
    "uf": ["SG_UF"],
    "cd_op": ["CD_OPERADORA", "REGISTRO_ANS", "REG_ANS", "CD_REGISTRO_ANS", "REGISTRO_OPERADORA"],
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
            with urlopen(Request(url, headers=UA), timeout=180) as r:
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


def norm_reg(v):
    """Registro ANS sem aspas nem zeros a esquerda ('000477' -> '477')."""
    s = str(v).strip().strip('"').strip()
    s = s.split(".")[0] if re.fullmatch(r"\d+\.0+", s) else s
    return s.lstrip("0") or "0"


def to_float(v):
    s = str(v).strip().strip('"')
    if not s or s.upper() in ("ND", "NA", "NAN", "-"):
        return None
    try:
        return float(s.replace(".", "").replace(",", ".") if "," in s else s)
    except ValueError:
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


def read_plain_csv(data):
    """CSV solto (nao zipado), separador ';'. Tenta utf-8 (com/sem BOM) e latin1."""
    for enc in ("utf-8-sig", "latin1"):
        try:
            df = pd.read_csv(io.BytesIO(data), sep=";", encoding=enc, dtype=str)
            df.columns = [str(c).strip().strip('"').upper() for c in df.columns]
            return df
        except UnicodeDecodeError:
            continue
    raise RuntimeError("nao consegui decodificar o CSV")


def cached_download(url, dest):
    dest.parent.mkdir(exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes()
    data = http_get(url)
    dest.write_bytes(data)
    return data


def fresh_download(url, dest):
    """Para arquivos pequenos que a ANS substitui no mesmo nome: sempre baixa de novo."""
    dest.parent.mkdir(exist_ok=True)
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
    """Conta reclamacoes (NIP) por operadora no ano mais recente disponivel.
    Usado so como reserva quando a operadora nao tem IGR."""
    d = find_dataset_dir("demandas_dos_consumidores_nip") or find_dataset_dir("nip")
    if not d:
        print("AVISO: dataset NIP nao encontrado.")
        return {}
    files = sorted(l for l in list_links(d) if l.lower().endswith((".zip", ".csv")))
    if not files:
        print("AVISO: pasta NIP sem arquivos .zip/.csv:", d)
        return {}
    out, used = defaultdict(int), files[-1]
    data = cached_download(d + used, CACHE / f"nip_{used}")
    try:
        chunks = read_csv_chunks(data) if used.endswith(".zip") else [read_plain_csv(data)]
        for ch in chunks:
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
        print("AVISO: pasta de contabeis sem subpastas AAAA:", d)
        return {}
    folder = d + years[-1] + "/"
    files = sorted(l for l in list_links(folder) if l.lower().endswith(".zip"))
    if not files:
        print("AVISO: sem .zip em", folder)
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


# ---------------------------------------------------------------- indices oficiais
def build_igr():
    """IGR (metodologia 2023): reclamacoes NIP por 100 mil beneficiarios, mensal.
    Guarda o IGR do ultimo mes e o IGR medio dos ultimos 12 meses (usado no score),
    so para cobertura 'Assistencia medica'."""
    try:
        root = find_dataset_dir("igr")
        if not root:
            print("AVISO: pasta IGR nao encontrada; reclamacoes usam a contagem NIP.")
            return {}, None
        sub = sorted(l for l in list_links(root) if l.endswith("/") and "versao" in l.lower())
        folder = root + sub[-1] if sub else root
        csvs = sorted(l for l in list_links(folder) if l.lower().endswith(".csv") and "igr" in l.lower())
        if not csvs:
            print("AVISO: CSV do IGR nao encontrado em", folder)
            return {}, None
        df = read_plain_csv(fresh_download(folder + csvs[-1], CACHE / "igr.csv"))
        need = {"REGISTRO_OPERADORA", "IGR", "QTD_RECLAMACOES", "QTD_BENEFICIARIOS", "COMPETENCIA"}
        if not need <= set(df.columns):
            print("AVISO: colunas inesperadas no IGR:", list(df.columns))
            return {}, None
        if "COBERTURA" in df.columns:
            df = df[df["COBERTURA"].str.upper().str.contains("DICA", na=False)
                    & ~df["COBERTURA"].str.upper().str.contains("ODONTO", na=False)]
        df["_r"] = df["REGISTRO_OPERADORA"].map(norm_reg)
        df["_comp"] = df["COMPETENCIA"].str.strip().str[:7]
        comps = sorted(df["_comp"].dropna().unique())
        last12 = comps[-12:]
        df = df[df["_comp"].isin(last12)]
        df["_rec"] = pd.to_numeric(df["QTD_RECLAMACOES"], errors="coerce").fillna(0)
        df["_ben"] = pd.to_numeric(df["QTD_BENEFICIARIOS"], errors="coerce").fillna(0)
        out = {}
        for r, g in df.groupby("_r"):
            ben = g["_ben"].sum()
            g_last = g[g["_comp"] == g["_comp"].max()]
            out[r] = {
                "i12": round(float(g["_rec"].sum()) * 100_000 / float(ben), 2) if ben > 0 else None,
                "i": to_float(g_last["IGR"].iloc[0]),
                "rec": int(g["_rec"].sum()),
                "porte": str(g_last["PORTE_OPERADORA"].iloc[0]) if "PORTE_OPERADORA" in g else "",
            }
        ref = f"{last12[0]} a {last12[-1]}" if last12 else None
        print(f"IGR: {csvs[-1]} ({len(out)} operadoras, {ref})")
        return out, ref
    except Exception as e:  # noqa
        print("AVISO: falha lendo IGR:", e)
        return {}, None


def build_idss():
    """IDSS (0 a 1, 1 = melhor) mais recente disponivel por operadora, com as 4 dimensoes."""
    try:
        root = find_dataset_dir("idss")
        if not root:
            print("AVISO: pasta do IDSS nao encontrada; qualidade fica fora do score.")
            return {}, None
        csvs = [l for l in list_links(root) if l.lower().endswith(".csv")]
        if not csvs:
            print("AVISO: CSV do IDSS nao encontrado em", root)
            return {}, None
        # arquivo cujo nome termina no ano mais recente (ex.: ...-2016_2025.csv)
        csvs.sort(key=lambda n: max([int(x) for x in re.findall(r"(20\d{2})", n)] or [0]))
        df = read_plain_csv(fresh_download(root + csvs[-1], CACHE / "idss.csv"))
        reg = find_col(df.columns, "cd_op")
        pairs = sorted({m.group(1) for c in df.columns
                        if (m := re.fullmatch(r"IDSS_(\d{4})_\d{4}", c))}, reverse=True)
        if not reg or not pairs:
            print("AVISO: colunas inesperadas no IDSS:", list(df.columns)[:12])
            return {}, None
        out = {}
        for _, row in df.iterrows():
            r = norm_reg(row[reg])
            for ano in pairs:  # do mais recente para o mais antigo
                col = next(c for c in df.columns if c.startswith(f"IDSS_{ano}_"))
                v = to_float(row[col])
                if v is None:
                    continue
                suf = col[len("IDSS_"):]
                dims = {k.lower(): to_float(row.get(f"{k}_{suf}")) for k in ("IDQS", "IDGA", "IDSM", "IDGR")}
                out[r] = {"v": v, "ano": int(ano), **{k: d for k, d in dims.items() if d is not None}}
                break
        anos = sorted({o["ano"] for o in out.values()})
        ref = str(anos[-1]) if anos else None
        print(f"IDSS: {csvs[-1]} ({len(out)} operadoras, programa mais recente {ref})")
        return out, ref
    except Exception as e:  # noqa
        print("AVISO: falha lendo IDSS:", e)
        return {}, None


def build_prud():
    """Classificacao Prudencial (RN 475): S1 a S4 conforme porte/receita. Informativo, nao entra no score."""
    try:
        root = find_dataset_dir("classificacao_prudencial")
        if not root:
            print("AVISO: pasta da Classificacao Prudencial nao encontrada.")
            return {}, None
        csvs = sorted(l for l in list_links(root) if l.lower().endswith(".csv"))
        if not csvs:
            print("AVISO: CSV da Classificacao Prudencial nao encontrado em", root)
            return {}, None
        df = read_plain_csv(fresh_download(root + csvs[-1], CACHE / "prud.csv"))
        need = {"REGISTRO_OPERADORA", "DT_TRIMESTRE_REFERENCIA", "TP_CLASSIFICACAO"}
        if not need <= set(df.columns):
            print("AVISO: colunas inesperadas na Classificacao Prudencial:", list(df.columns))
            return {}, None
        df["_r"] = df["REGISTRO_OPERADORA"].map(norm_reg)
        df = df.sort_values("DT_TRIMESTRE_REFERENCIA").drop_duplicates("_r", keep="last")
        out = {r: str(s).strip().strip('"') for r, s in zip(df["_r"], df["TP_CLASSIFICACAO"])}
        ref = df["DT_TRIMESTRE_REFERENCIA"].max()
        print(f"Classificacao Prudencial: {csvs[-1]} ({len(out)} operadoras, ate {ref})")
        return out, ref
    except Exception as e:  # noqa
        print("AVISO: falha lendo Classificacao Prudencial:", e)
        return {}, None


def rekey(d, ops, label):
    """Converte as chaves de um dicionario para o mesmo codigo usado em ops e informa o cruzamento."""
    if not d:
        return {}
    lut = {norm_reg(k): k for k in ops}
    out = {lut[norm_reg(k)]: v for k, v in d.items() if norm_reg(k) in lut}
    print(f"Cruzamento {label}: {len(out)} de {len(ops)} operadoras do painel")
    if ops and len(out) < 0.2 * len(ops):
        print(f"AVISO: poucas operadoras de {label} cruzaram com os beneficiarios. "
              f"Exemplos de codigo: beneficiarios={list(ops)[:3]} {label}={list(d)[:3]}")
    return out


# ---------------------------------------------------------------- potencial por regiao
UF_IBGE = {"11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP", "17": "TO",
           "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB", "26": "PE", "27": "AL",
           "28": "SE", "29": "BA", "31": "MG", "32": "ES", "33": "RJ", "35": "SP", "41": "PR",
           "42": "SC", "43": "RS", "50": "MS", "51": "MT", "52": "GO", "53": "DF"}
SIDRA_POP = "https://apisidra.ibge.gov.br/values/t/6579/n3/all/v/9324/p/all"


def build_pop_ibge():
    """Populacao residente estimada por UF (IBGE, SIDRA tabela 6579). {ano: {UF: pessoas}}."""
    try:
        rows = json.loads(http_get(SIDRA_POP).decode("utf-8"))[1:]
        out = defaultdict(dict)
        for r in rows:
            uf, v = UF_IBGE.get(str(r.get("D1C"))), str(r.get("V", ""))
            if uf and v.isdigit():
                out[str(r["D3C"])][uf] = int(v)
        print(f"Populacao IBGE: anos {sorted(out)}")
        return dict(out)
    except Exception as e:  # noqa
        print("AVISO: falha lendo populacao do IBGE (SIDRA 6579):", e)
        return {}


def build_cobertura():
    """Beneficiarios por UF (assistencia medica e exclusivamente odontologico) do conjunto
    'Taxa de cobertura' da ANS, todas as UFs, no periodo mais recente. A populacao vem do
    proprio arquivo quando preenchida; se vier zerada, da estimativa do IBGE do mesmo ano."""
    try:
        root = find_dataset_dir("taxa_de_cobertura")
        if not root:
            print("AVISO: conjunto 'taxa de cobertura' nao encontrado; aba de potencial sem penetracao.")
            return None
        csvs = sorted(l for l in list_links(root) if l.lower().endswith(".csv"))
        if not csvs:
            print("AVISO: CSV da taxa de cobertura nao encontrado em", root)
            return None
        df = read_plain_csv(fresh_download(root + csvs[-1], CACHE / "cobertura.csv"))
        need = {"PERIODO", "SG_UF", "BENEF_ASSISTENCIA_MEDICA", "BENEF_EXCLUS_ODONTOLOGICO", "POPULACAO"}
        if not need <= set(df.columns):
            print("AVISO: colunas inesperadas na taxa de cobertura:", list(df.columns))
            return None
        for c in ("BENEF_ASSISTENCIA_MEDICA", "BENEF_EXCLUS_ODONTOLOGICO", "POPULACAO"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        df["PERIODO"] = df["PERIODO"].astype(str).str.strip()
        df["SG_UF"] = df["SG_UF"].astype(str).str.strip().str.upper()
        per = sorted(p for p in df["PERIODO"].unique() if p and p.lower() != "nan")[-1]
        g = df[df["PERIODO"] == per].groupby("SG_UF")[
            ["BENEF_ASSISTENCIA_MEDICA", "BENEF_EXCLUS_ODONTOLOGICO", "POPULACAO"]].sum()
        pop_ibge = build_pop_ibge()
        ano = re.match(r"\d{4}", per)
        ibge_ano = ano.group(0) if ano and ano.group(0) in pop_ibge else None
        ufs, faltas = {}, []
        for uf in UFS:
            if uf not in g.index:
                faltas.append(uf)
                continue
            r = g.loc[uf]
            pop, fonte = int(r["POPULACAO"]), "ANS"
            if pop <= 0:
                pop = pop_ibge.get(ibge_ano, {}).get(uf) if ibge_ano else None
                fonte = f"IBGE {ibge_ano}" if pop else None
            ufs[uf] = {"med": int(r["BENEF_ASSISTENCIA_MEDICA"]), "odo": int(r["BENEF_EXCLUS_ODONTOLOGICO"]),
                       "pop": pop, "pop_fonte": fonte}
        if faltas:
            print("AVISO: UFs sem linhas na taxa de cobertura:", faltas)
        sem_pop = [u for u, v in ufs.items() if not v["pop"]]
        if sem_pop:
            print(f"AVISO: sem populacao para {sem_pop} no periodo {per} (nem na ANS nem no IBGE do mesmo ano).")
        print(f"Taxa de cobertura: {csvs[-1]} periodo {per} ({len(ufs)} UFs)")
        return {"periodo": per, "arquivo": csvs[-1], "ufs": ufs}
    except Exception as e:  # noqa
        print("AVISO: falha lendo a taxa de cobertura:", e)
        return None


def load_renda():
    f = HERE / "ibge_renda.json"
    if not f.exists():
        print("AVISO: ibge_renda.json nao encontrado; aba de potencial sem renda.")
        return None
    r = json.loads(f.read_text(encoding="utf-8"))
    if datetime.now().year - int(r.get("ano", 0)) > 1:
        print(f"AVISO: renda do IBGE e de {r.get('ano')}; atualize ibge_renda.json com a divulgacao mais recente.")
    return r


# ---------------------------------------------------------------- demo
def build_demo(months):
    random.seed(7)
    latest = "202609"
    wanted = [month_shift(latest, -i) for i in range(months)][::-1]
    ops, series, nip, fin, igr, idss, prud = {}, [], {}, {}, {}, {}, {}
    mods = ["Medicina de grupo", "Cooperativa médica", "Seguradora especializada em saúde", "Autogestão"]
    for i in range(1, 41):
        cd = str(300000 + i)
        ops[cd] = {"n": f"OPERADORA DEMO {i:02d} (FICTÍCIA)", "m": random.choice(mods)}
        size, trend = random.randint(5_000, 900_000), random.uniform(-0.08, 0.18)
        nip[cd] = int(size / 10_000 * random.uniform(1, 25))
        fin[cd] = {"pl": random.choice([1, 1, 1, 1, -1]) * random.uniform(1e6, 3e8),
                   "sin": random.uniform(0.6, 0.95)}
        if random.random() < 0.9:
            igr[cd] = {"i12": round(random.uniform(2, 120), 2), "i": round(random.uniform(1, 150), 2),
                       "rec": random.randint(5, 2000), "porte": "Grande" if size > 100_000 else "Médio"}
        if random.random() < 0.85:
            idss[cd] = {"v": round(random.uniform(0.3, 0.98), 4), "ano": 2025}
        prud[cd] = "S1" if size > 600_000 else "S2" if size > 150_000 else "S3"
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
    refs = {"igr": "2025-10 a 2026-09", "idss": "2025", "prud": "2026-06-30"}
    cob = {"periodo": "2026", "arquivo": "demo", "ufs": {}}
    for uf in UFS:
        if uf == "AP":  # simula UF sem populacao, para testar o aviso de dado faltante
            cob["ufs"][uf] = {"med": 90_000, "odo": 40_000, "pop": None, "pop_fonte": None}
            continue
        pop = random.randint(600_000, 46_000_000)
        cob["ufs"][uf] = {"med": int(pop * random.uniform(0.05, 0.38)), "odo": int(pop * random.uniform(0.03, 0.2)),
                          "pop": pop, "pop_fonte": "IBGE 2026"}
    return wanted, series, ops, muni, nip, fin, igr, idss, prud, refs, cob


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
        wanted, series, ops, muni, nip, fin, igr, idss, prud, refs, cob = build_demo(a.months)
    else:
        ufs = [u.strip().upper() for u in a.ufs.split(",") if u.strip()]
        wanted, series, ops, muni = build_benef(a.months, ufs)
        nip, fin = build_nip(), build_fin()
        igr, r_igr = build_igr()
        idss, r_idss = build_idss()
        prud, r_prud = build_prud()
        refs = {"igr": r_igr, "idss": r_idss, "prud": r_prud}
        cob = build_cobertura()
        nip, fin = rekey(nip, ops, "NIP"), rekey(fin, ops, "contabeis")
        igr, idss, prud = rekey(igr, ops, "IGR"), rekey(idss, ops, "IDSS"), rekey(prud, ops, "Prudencial")

    payload = {
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "demo": a.demo,
        "meses": wanted,
        "ops": ops,
        "series": series,
        "muni": muni,
        "nip": nip,
        "fin": fin,
        "igr": igr,
        "idss": idss,
        "prud": prud,
        "refs": refs,
        "cob": cob,
        "renda": load_renda(),
    }
    txt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out = HERE / "data.js"
    out.write_text("window.ANS_DATA = " + txt + ";", encoding="utf-8")
    (HERE / "data.json").write_text(txt, encoding="utf-8")  # usado pelo plugin do WordPress
    print(f"OK -> {out} e data.json ({out.stat().st_size / 1e6:.1f} MB). Abra index.html no navegador.")


if __name__ == "__main__":
    main()

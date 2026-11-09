#!/usr/bin/env python3
"""
otrs-faq: recebe o webhook do Znuny (chamado novo) e sugere artigos da FAQ e chamados
parecidos ja resolvidos. SEM LLM: tudo local (BM25 sobre a FAQ em memoria + banco do OTRS
em modo somente leitura via otrs_core).

  POST /hook            webhook do Znuny (JSON: TicketNumber, Title, Queue, Service, Body)
  GET  /health          estado do servico
  GET  /sugerir?titulo=...&fila=...   debug: devolve as sugestoes SEM postar no Teams
  GET  /last            ultimos cartoes gerados (modo teste: sem TEAMS_WEBHOOK_URL)

Variaveis de ambiente:
  OTRS_DB_*              ver otrs_core.py
  HOOK_TOKEN             segredo esperado no header X-Hook-Token (recomendado)
  TEAMS_URL_<FILA>       URL do fluxo do Power Automate de cada canal, pelo prefixo da fila
                         normalizado (maiusculas, :: e espacos viram _). Ex.:
                         TEAMS_URL_TI_INFRAESTRUTURA, TEAMS_URL_TI_BI, TEAMS_URL_TI_SISTEMAS.
                         Vale o prefixo mais longo que casar; TEAMS_URL_TI e o padrao das
                         demais filas TI::*. Sem URL para a fila = modo teste (nao posta).
  OTRS_BASE_URL          ex.: https://servidor/otrs (links da FAQ); vazio = sem link
  QUEUE_REGEX            filas atendidas (padrao ^TI::)
  MIN_SCORE              relevancia minima BM25 para sugerir (padrao 3.0)
  MIN_TIT_MASS           peso minimo (idf) dos termos que casam no TITULO/palavras-chave (3.0)
  MIN_COBERTURA          fracao minima do peso da consulta coberta pelo artigo (0.35)
  POST_EMPTY             "1" posta mesmo sem sugestao (padrao 0)
  PORT                   padrao 8095
"""

import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import urllib.request
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import otrs_core as core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("otrs-faq")

PORT = int(os.environ.get("PORT", "8095"))
HOOK_TOKEN = os.environ.get("HOOK_TOKEN", "")
BASE_URL = os.environ.get("OTRS_BASE_URL", "").rstrip("/")
QUEUE_RE = re.compile(os.environ.get("QUEUE_REGEX", r"^TI::"))
MIN_SCORE = float(os.environ.get("MIN_SCORE", "3.0"))
MIN_TIT_MASS = float(os.environ.get("MIN_TIT_MASS", "3.0"))
MIN_COBERTURA = float(os.environ.get("MIN_COBERTURA", "0.35"))
POST_EMPTY = os.environ.get("POST_EMPTY", "0") == "1"
TOP_FAQ, TOP_SIM = 3, 3
K1, B = 1.5, 0.75
DEDUP_SEGUNDOS = 6 * 3600

STOP = set("""
a o as os um uma uns umas de do da dos das em no na nos nas por para com sem sob sobre e ou
que se ao aos ja nao mais mas como foi ser esta estao esse essa isso este isto ter tem tenho
favor bom boa dia tarde noite ola obrigado obrigada gostaria solicito solicitacao solicitar
preciso precisamos urgente chamado ticket abrir abertura pedido por favor segue conforme
atenciosamente att cordialmente voce voces vez fazer feito esta estou foram seria
""".split())


def norm(s: str) -> str:
    return unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()


def palavras(s: str):
    """Palavras normalizadas (sem acento, sem stopword), tamanho >= 3."""
    return [w for w in re.findall(r"[a-z0-9]{3,}", norm(s)) if w not in STOP]


def radical(w: str) -> str:
    if len(w) > 3 and w.endswith("s"):
        w = w[:-1]
    return w[:5]


def toks(s: str):
    return [radical(w) for w in palavras(s)]


def sem_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))


# ------------------------------------------------------------------- indice --

class Indice:
    def __init__(self, itens):
        self.docs = []
        self.df = Counter()
        for it in itens:
            tf = Counter()
            for t in toks(sem_html(it.get("f_subject"))):
                tf[t] += 3
            for t in toks(sem_html(it.get("f_keywords"))):
                tf[t] += 3
            for c in ("f_field1", "f_field2", "f_field3", "f_field4", "f_field5", "f_field6"):
                for t in toks(sem_html(it.get(c))):
                    tf[t] += 1
            if not tf:
                continue
            destaque = set(toks(sem_html(it.get("f_subject")))) | set(toks(sem_html(it.get("f_keywords"))))
            self.docs.append({"id": it["id"], "num": it.get("f_number"),
                              "titulo": sem_html(it.get("f_subject")).strip(),
                              "tf": tf, "dl": sum(tf.values()), "destaque": destaque})
            for t in tf:
                self.df[t] += 1
        self.n = len(self.docs)
        self.avgdl = (sum(d["dl"] for d in self.docs) / self.n) if self.n else 1.0
        self.criado = time.time()

    def idf(self, t):
        return math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))

    def buscar(self, consulta: Counter, n: int):
        # massa de relevancia dos termos da consulta que existem na FAQ
        massa_total = sum(self.idf(t) for t in consulta if self.df.get(t))
        res = []
        for d in self.docs:
            s, hits, massa_tit, massa_hit = 0.0, 0, 0.0, 0.0
            for t, qtf in consulta.items():
                tf = d["tf"].get(t, 0)
                if not tf:
                    continue
                hits += 1
                idf = self.idf(t)
                massa_hit += idf
                if t in d["destaque"]:
                    massa_tit += idf
                s += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * d["dl"] / self.avgdl)) * min(qtf, 2)
            # exige termo relevante no titulo/palavras-chave e boa cobertura da consulta
            if hits and massa_tit >= MIN_TIT_MASS and massa_total and massa_hit / massa_total >= MIN_COBERTURA:
                res.append((s, hits, d))
        res.sort(key=lambda x: x[0], reverse=True)
        minimo_hits = 1 if len(consulta) <= 2 else 2
        res = [(s, h, d) for s, h, d in res if s >= MIN_SCORE and h >= minimo_hits]
        if res:  # corta o que e bem mais fraco que o melhor
            res = [(s, h, d) for s, h, d in res if s >= 0.6 * res[0][0]]
        return [(s, d) for s, h, d in res][:n]


INDICE = None
ULTIMOS = deque(maxlen=20)
VISTOS = {}
LOCK = threading.Lock()


def carregar_indice():
    global INDICE
    itens = core.faq_todos()
    INDICE = Indice(itens)
    log.info("indice da FAQ carregado: %d artigos", INDICE.n)


def recarregar_periodicamente():
    while True:
        time.sleep(1800)
        try:
            carregar_indice()
        except Exception as e:
            log.warning("falha ao recarregar indice (mantendo o anterior): %s", e)


# ---------------------------------------------------------------- sugestoes --

def consulta_do_chamado(t: dict) -> Counter:
    c = Counter()
    for tk in toks(t.get("Title", "")):
        c[tk] += 2
    for tk in toks(t.get("Service", "")):
        c[tk] += 1
    for tk in toks((t.get("Body") or "")[:1500]):
        c[tk] += 1
    return c


def sugerir_faq(t: dict):
    if INDICE is None:
        return []
    return [{"id": d["id"], "num": d["num"], "titulo": d["titulo"], "score": round(s, 2)}
            for s, d in INDICE.buscar(consulta_do_chamado(t), TOP_FAQ)]


_ASSINATURA = re.compile(r"\b(?:atenciosamente|cordialmente|att\.?|abra[cç]os|obrigad[oa])\b", re.I)
_SAUDACAO = re.compile(r"^(?:ol[aá]|oi|bom dia|boa tarde|boa noite)\b[^,.!:]{0,60}[,!.:]\s*", re.I)


_ENCAMINHAMENTO = re.compile(
    r"favor proceder|proceder com o atendimento|encaminhad|repassad|respondid[oa] (no|pelo) teams|"
    r"chamado (finalizado|encerrado|fechado)", re.I)


def limpar_resposta(txt: str) -> str:
    """Tira saudacao (com o nome da pessoa) e assinatura, deixando so o conteudo da resposta."""
    txt = re.sub(r"\s+", " ", txt or "").strip()
    txt = _ASSINATURA.split(txt)[0]
    txt = _SAUDACAO.sub("", txt)
    return txt.strip(" ,;-")


def sugerir_similares(t: dict):
    pal = list(dict.fromkeys(w for w in palavras(t.get("Title", "")) if len(w) >= 4))[:4]
    if not pal:
        return []
    q = {radical(w) for w in pal}
    fila = (t.get("Queue") or "").split("::")[0] or None
    cands = core.titulos_fechados(pal, fila, dias=730, n=60, minimo=2)
    pont = []
    for c in cands:
        if str(c["tn"]) == str(t.get("TicketNumber")):
            continue
        ov = len(q & set(toks(c["title"])))
        if ov >= min(2, len(q)):
            pont.append((ov, c))
    pont.sort(key=lambda x: (x[0], x[1]["create_time"]), reverse=True)
    saida = []
    for ov, c in pont[:TOP_SIM]:
        resp = [limpar_resposta(sem_html(r["a_body"])) for r in core.respostas_agente(c["id"], 3)]
        uteis = [r for r in resp if r and not _ENCAMINHAMENTO.search(r)]  # tira "favor proceder", etc.
        melhor = max(uteis or resp, key=len) if (uteis or resp) else ""
        melhor = core.mascarar(melhor)[:220]
        saida.append({"tn": c["tn"], "titulo": core.mascarar(c["title"]),
                      "fila": c["fila"], "resposta": melhor})
    return saida


def montar_cartao(t: dict, faqs, sims):
    body = [
        {"type": "TextBlock", "text": f"Sugestão da FAQ · #{t.get('TicketNumber', '?')}",
         "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": t.get("Title", ""), "isSubtle": True, "wrap": True},
    ]
    if faqs:
        body.append({"type": "TextBlock", "text": "Artigos da FAQ que podem ajudar",
                     "weight": "Bolder", "spacing": "Medium"})
        for f in faqs:
            rotulo = f"FAQ {f['num']} – {f['titulo']}" if f.get("num") else f["titulo"]
            texto = f"• [{rotulo}]({BASE_URL}/index.pl?Action=AgentFAQZoom;ItemID={f['id']})" \
                if BASE_URL else f"• {rotulo}"
            body.append({"type": "TextBlock", "text": texto, "wrap": True})
    if sims:
        body.append({"type": "TextBlock", "text": "Chamados parecidos já resolvidos",
                     "weight": "Bolder", "spacing": "Medium"})
        for s in sims:
            body.append({"type": "TextBlock", "text": f"• #{s['tn']} – {s['titulo']} ({s['fila']})",
                         "wrap": True})
            if s["resposta"]:
                body.append({"type": "TextBlock", "text": f"Resposta: {s['resposta']}",
                             "isSubtle": True, "size": "Small", "wrap": True})
    body.append({"type": "TextBlock", "spacing": "Medium", "isSubtle": True, "size": "Small",
                 "wrap": True,
                 "text": "Sugestão automática por palavras-chave (sem IA). Confira antes de usar."})
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body}}]}


def rotas_teams():
    """{sufixo_normalizado: url} a partir das variaveis TEAMS_URL_*."""
    return {k[len("TEAMS_URL_"):]: v for k, v in os.environ.items()
            if k.startswith("TEAMS_URL_") and v}


def url_para_fila(fila: str):
    """Devolve (nome_da_rota, url) do prefixo mais longo que casa com a fila, ou (None, None)."""
    f = re.sub(r"[^A-Z0-9]+", "_", norm(fila).upper()).strip("_")
    melhor = None
    for sufixo, url in rotas_teams().items():
        if (f == sufixo or f.startswith(sufixo + "_")) and (melhor is None or len(sufixo) > len(melhor[0])):
            melhor = (sufixo, url)
    return melhor if melhor else (None, None)


def postar(url: str, cartao: dict):
    req = urllib.request.Request(url, data=json.dumps(cartao).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def processar(t: dict):
    tn = str(t.get("TicketNumber", "?"))
    try:
        if not QUEUE_RE.search(t.get("Queue") or ""):
            log.info("tn=%s ignorado (fila fora do filtro)", tn)
            return
        agora = time.time()
        with LOCK:
            if agora - VISTOS.get(tn, 0) < DEDUP_SEGUNDOS:
                log.info("tn=%s ignorado (duplicado)", tn)
                return
            VISTOS[tn] = agora
            for k in [k for k, v in VISTOS.items() if agora - v > DEDUP_SEGUNDOS]:
                VISTOS.pop(k, None)
        if not t.get("Body") and tn.isdigit():
            # TicketCreate pode vir sem corpo: tenta o 1o artigo no banco (se o chamado existir la)
            try:
                _, arts = core.chamado_por_tn(tn)
                if arts:
                    t["Body"] = sem_html(arts[0]["a_body"])[:1500]
            except Exception as e:
                log.warning("tn=%s nao li o corpo no banco: %s", tn, e)
        faqs = sugerir_faq(t)
        try:
            sims = sugerir_similares(t)
        except Exception as e:
            log.warning("tn=%s similares falhou: %s", tn, e)
            sims = []
        if not faqs and not sims and not POST_EMPTY:
            log.info("tn=%s sem sugestao; nada postado", tn)
            return
        cartao = montar_cartao(t, faqs, sims)
        rota, url = url_para_fila(t.get("Queue") or "")
        ULTIMOS.append({"tn": tn, "fila": t.get("Queue"), "destino": rota, "cartao": cartao})
        if url:
            status = postar(url, cartao)
            log.info("tn=%s fila=%s faq=%d similares=%d postado em %s http=%s",
                     tn, t.get("Queue"), len(faqs), len(sims), rota, status)
        else:
            log.info("tn=%s fila=%s faq=%d similares=%d (sem URL do Teams p/ a fila: modo teste, nao postado)",
                     tn, t.get("Queue"), len(faqs), len(sims))
    except Exception as e:
        log.exception("tn=%s falha ao processar: %s", tn, e)


# --------------------------------------------------------------------- http --

def achar(d, *nomes):
    """Busca (sem diferenciar maiuscula) a primeira chave em dicts/listas aninhados."""
    alvo = {n.lower() for n in nomes}
    pilha = [d]
    while pilha:
        x = pilha.pop(0)
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in alvo and isinstance(v, (str, int)):
                    return str(v)
            pilha.extend(v for v in x.values() if isinstance(v, (dict, list)))
        elif isinstance(x, list):
            pilha.extend(x)
    return ""


class Handler(BaseHTTPRequestHandler):
    def _json(self, codigo, obj):
        dados = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _autorizado(self, query=None):
        if not HOOK_TOKEN:
            return True
        token = self.headers.get("X-Hook-Token", "") or (query or {}).get("token", [""])[0]
        return token == HOOK_TOKEN

    def log_message(self, fmt, *args):  # silencia o log padrao (pode ter dados)
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._json(200, {"status": "ok", "faq_indexadas": INDICE.n if INDICE else 0,
                                    "rotas_teams": sorted(rotas_teams()), "filas": QUEUE_RE.pattern})
        if not self._autorizado(q):
            return self._json(401, {"erro": "token invalido"})
        if u.path == "/last":
            return self._json(200, list(ULTIMOS))
        if u.path == "/sugerir":
            t = {"Title": q.get("titulo", [""])[0], "Queue": q.get("fila", ["TI::"])[0],
                 "Service": q.get("servico", [""])[0], "Body": q.get("corpo", [""])[0]}
            saida = {"faq": sugerir_faq(t), "similares": []}
            try:
                saida["similares"] = sugerir_similares(t)
            except Exception as e:
                saida["erro_similares"] = str(e)
            return self._json(200, saida)
        self._json(404, {"erro": "nao encontrado"})

    def do_POST(self):
        if urlparse(self.path).path != "/hook":
            return self._json(404, {"erro": "nao encontrado"})
        if not self._autorizado():
            return self._json(401, {"erro": "token invalido"})
        try:
            tam = min(int(self.headers.get("Content-Length", "0")), 262144)
            dados = json.loads(self.rfile.read(tam) or b"{}")
        except Exception:
            return self._json(400, {"erro": "json invalido"})
        t = {"TicketNumber": achar(dados, "TicketNumber", "TN"),
             "TicketID": achar(dados, "TicketID"),
             "Title": achar(dados, "Title", "Subject"),
             "Queue": achar(dados, "Queue"),
             "Service": achar(dados, "Service"),
             "Body": achar(dados, "Body")}
        t = {k: ("" if v.strip() == "-" else v) for k, v in t.items()}  # XSLT usa "-" p/ vazio
        threading.Thread(target=processar, args=(t,), daemon=True).start()
        self._json(202, {"status": "recebido"})


def main():
    try:
        carregar_indice()
    except Exception as e:
        log.error("indice nao carregou (vou tentar de novo em 60s): %s", e)

        def tentar():
            while INDICE is None:
                time.sleep(60)
                try:
                    carregar_indice()
                except Exception as e2:
                    log.error("indice ainda indisponivel: %s", e2)
        threading.Thread(target=tentar, daemon=True).start()
    threading.Thread(target=recarregar_periodicamente, daemon=True).start()
    log.info("otrs-faq na porta %d | rotas Teams=%s | filas=%s", PORT, sorted(rotas_teams()), QUEUE_RE.pattern)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

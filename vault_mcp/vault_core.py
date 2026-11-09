"""
Nucleo SOMENTE-LEITURA do MCP do vault Obsidian (sem dependencias externas).

Garantias:
- so le arquivos .md dentro de VAULT_DIR (caminho resolvido; '..' e links simbolicos fora da raiz sao recusados);
- so enxerga as pastas de VAULT_INCLUDE (padrao: tudo) e nunca as de VAULT_EXCLUDE;
- ignora pastas de sistema do Obsidian (.obsidian, .trash, .git);
- mascara segredos (senha/token/chave) em todo texto devolvido.

Variaveis de ambiente:
  VAULT_DIR      raiz do vault (padrao /vault)
  VAULT_INCLUDE  pastas de 1o nivel permitidas, separadas por virgula (vazio = todas)
  VAULT_EXCLUDE  pastas/prefixos de caminho bloqueados, separados por virgula
  VAULT_MAX_CHARS limite de caracteres por nota devolvida (padrao 12000)
"""

import os
import re
import threading
import unicodedata

RAIZ = os.path.realpath(os.environ.get("VAULT_DIR", "/vault"))
INCLUDE = [p.strip().strip("/\\") for p in os.environ.get("VAULT_INCLUDE", "").split(",") if p.strip()]
EXCLUDE = [p.strip().strip("/\\") for p in os.environ.get("VAULT_EXCLUDE", "").split(",") if p.strip()]
MAX_CHARS = int(os.environ.get("VAULT_MAX_CHARS", "12000"))
IGNORAR_DIRS = {".obsidian", ".trash", ".git", ".stfolder"}

_WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
_PALAVRA = re.compile(r"\w{2,}", re.UNICODE)

# ---- mascara de segredos ---------------------------------------------------
_SEGREDOS = [
    # chave: valor  /  chave=valor  (senha, password, token, secret, api key, chave ...)
    # so com ':' ou '=' logo apos a palavra (aceita "senha do painel: x"); evita texto corrido
    (re.compile(r"(?i)\b(senha|password|passwd|pwd|secret|token|api[_ -]?key|apikey|chave|credencial|authorization)"
                r"((?:\s+(?:d[aoe]s?|do)\s+[\w\-\.]{1,25})?)(\s*[:=]\s*)([`'\"]?)([^\s`'\"|,;\[\]]{4,})"),
     lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer ***"),
    (re.compile(r"(?i)(://[^/\s:@]+:)([^@\s/]{3,})(@)"), r"\1***\3"),  # user:senha@host
    # blobs longos alfanumericos (tokens/hashes): precisam de letra E digito e nao fazem parte de
    # caminho, nome de nota ou palavra composta (sem '/', '\', '-', '_', '[' vizinhos)
    (re.compile(r"(?<![\w/\\\[\-\.])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{32,}(?![\w/\\\]\-\.])"), "***"),
]


def mascarar(texto: str) -> str:
    for padrao, subst in _SEGREDOS:
        texto = padrao.sub(subst, texto)
    return texto


# ---- indice ---------------------------------------------------------------
def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def _permitido(rel: str) -> bool:
    """rel = caminho relativo com '/' ; aplica INCLUDE/EXCLUDE."""
    partes = rel.split("/")
    if any(p in IGNORAR_DIRS for p in partes):
        return False
    if INCLUDE:
        # nota solta na raiz so entra se a raiz estiver em INCLUDE como '.'
        topo = partes[0] if len(partes) > 1 else "."
        if topo not in INCLUDE:
            return False
    for ex in EXCLUDE:
        if rel == ex or rel.startswith(ex + "/") or rel.startswith(ex):
            return False
    return True


class Indice:
    def __init__(self):
        self._lock = threading.Lock()
        self._notas = {}      # rel -> dict(mtime, texto, titulo)
        self._por_nome = {}   # nome sem extensao (lower) -> rel
        self._backlinks = {}  # rel -> set(rel)
        self._assinatura = None

    def _varrer(self):
        achados = {}
        for dirpath, dirnames, filenames in os.walk(RAIZ):
            dirnames[:] = [d for d in dirnames if d not in IGNORAR_DIRS]
            for f in filenames:
                if not f.lower().endswith(".md"):
                    continue
                completo = os.path.join(dirpath, f)
                rel = os.path.relpath(completo, RAIZ).replace("\\", "/")
                if not _permitido(rel):
                    continue
                real = os.path.realpath(completo)
                if os.path.commonpath([real, RAIZ]) != RAIZ:  # link simbolico para fora
                    continue
                achados[rel] = (completo, os.path.getmtime(completo))
        return achados

    def atualizar(self, forcar=False):
        with self._lock:
            achados = self._varrer()
            assin = tuple(sorted((r, m) for r, (_, m) in achados.items()))
            if not forcar and assin == self._assinatura:
                return
            notas = {}
            for rel, (completo, mtime) in achados.items():
                antigo = self._notas.get(rel)
                if antigo and antigo["mtime"] == mtime:
                    notas[rel] = antigo
                    continue
                try:
                    with open(completo, "r", encoding="utf-8", errors="replace") as fh:
                        texto = fh.read()
                except OSError:
                    continue
                notas[rel] = {"mtime": mtime, "texto": texto,
                              "titulo": os.path.splitext(os.path.basename(rel))[0]}
            self._notas = notas
            self._por_nome = {}
            for rel, n in notas.items():
                self._por_nome.setdefault(n["titulo"].lower(), rel)
            back = {rel: set() for rel in notas}
            for rel, n in notas.items():
                for alvo in self._links_de(n["texto"]):
                    destino = self._por_nome.get(alvo.lower())
                    if destino and destino != rel:
                        back[destino].add(rel)
            self._backlinks = back
            self._assinatura = assin

    @staticmethod
    def _links_de(texto):
        return list(dict.fromkeys(m.strip() for m in _WIKILINK.findall(texto)))

    def resolver(self, entrada: str):
        """Aceita caminho relativo (com ou sem .md) ou so o nome da nota."""
        self.atualizar()
        e = (entrada or "").strip().replace("\\", "/").lstrip("/")
        if not e:
            return None
        cand = e if e.lower().endswith(".md") else e + ".md"
        if cand in self._notas:
            return cand
        base = os.path.splitext(os.path.basename(e))[0].lower()
        return self._por_nome.get(base)


IDX = Indice()


# ---- operacoes ------------------------------------------------------------
def buscar(termo: str, pasta: str = "", limite: int = 8):
    IDX.atualizar()
    palavras = [_sem_acento(p) for p in _PALAVRA.findall(termo or "")][:8]
    if not palavras:
        raise ValueError("informe um termo com pelo menos 2 letras")
    pasta = (pasta or "").strip().strip("/\\")
    limite = max(1, min(int(limite), 20))
    pontuados = []
    for rel, n in IDX._notas.items():
        if pasta and not rel.startswith(pasta.rstrip("/") + "/"):
            continue
        titulo = _sem_acento(n["titulo"] + " " + rel)
        corpo = _sem_acento(n["texto"])
        score = 0
        for p in palavras:
            score += 5 * titulo.count(p) + min(corpo.count(p), 20)
        if all((p in titulo or p in corpo) for p in palavras):
            score += 10  # bonus se todas as palavras aparecem
        if score:
            pontuados.append((score, rel, n))
    pontuados.sort(key=lambda x: x[0], reverse=True)
    saida = []
    for score, rel, n in pontuados[:limite]:
        saida.append({"caminho": rel, "titulo": n["titulo"], "score": score,
                      "trecho": mascarar(_trecho(n["texto"], palavras))})
    return {"palavras": palavras, "resultados": saida, "total_notas": len(IDX._notas),
            "sincronizado_em": sincronizado_em()}


def sincronizado_em():
    """Quando o PC enviou a copia (marcador .sync_ok gravado por vault_receive.sh); None se nao houver."""
    try:
        with open(os.path.join(RAIZ, ".sync_ok"), "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _trecho(texto: str, palavras, tam: int = 280) -> str:
    sem = _sem_acento(texto)
    pos = -1
    for p in palavras:
        pos = sem.find(p)
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    ini = max(0, pos - tam // 3)
    return re.sub(r"\s+", " ", texto[ini:ini + tam]).strip()


def ler_nota(entrada: str, inicio: int = 0):
    rel = IDX.resolver(entrada)
    if not rel:
        return {"erro": "nota nao encontrada (use vault_buscar ou vault_listar)"}
    n = IDX._notas[rel]
    inicio = max(0, int(inicio))
    texto = n["texto"]
    fatia = texto[inicio:inicio + MAX_CHARS]
    return {"caminho": rel, "titulo": n["titulo"], "conteudo": mascarar(fatia),
            "truncado": inicio + MAX_CHARS < len(texto), "proximo_inicio": inicio + MAX_CHARS,
            "total_chars": len(texto), "links": IDX._links_de(texto)[:60]}


def vizinhos(entrada: str):
    rel = IDX.resolver(entrada)
    if not rel:
        return {"erro": "nota nao encontrada"}
    n = IDX._notas[rel]
    saida_links, quebrados = [], []
    for alvo in IDX._links_de(n["texto"]):
        destino = IDX._por_nome.get(alvo.lower())
        (saida_links if destino else quebrados).append(destino or alvo)
    return {"caminho": rel, "links_de_saida": saida_links, "links_sem_nota": quebrados[:30],
            "backlinks": sorted(IDX._backlinks.get(rel, []))[:60]}


def listar(pasta: str = ""):
    IDX.atualizar()
    pasta = (pasta or "").strip().strip("/\\")
    prefixo = pasta + "/" if pasta else ""
    subpastas, notas = set(), []
    for rel in sorted(IDX._notas):
        if not rel.startswith(prefixo):
            continue
        resto = rel[len(prefixo):]
        if "/" in resto:
            subpastas.add(resto.split("/")[0])
        else:
            notas.append(rel)
    return {"pasta": pasta or "/", "subpastas": sorted(subpastas), "notas": notas[:200],
            "total_na_pasta": len(notas), "sincronizado_em": sincronizado_em()}

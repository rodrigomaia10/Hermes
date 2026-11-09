"""
Nucleo SOMENTE-LEITURA de acesso ao banco do OTRS/Znuny (MySQL/MariaDB).
Compartilhado pelo MCP (otrs_mcp.py) e pelo servico otrs-faq (faq_service.py).
Nao depende da biblioteca `mcp`; so de pymysql.

Defesa em profundidade (nenhuma camada sozinha e suficiente):
  1. O usuario do banco deve ter so SELECT. No primeiro acesso roda SHOW GRANTS e
     RECUSA operar se houver qualquer privilegio de escrita.
  2. A sessao e aberta com SET SESSION TRANSACTION READ ONLY.
  3. SQL livre aceita UM unico SELECT/SHOW/DESCRIBE/EXPLAIN; o resto e rejeitado.
  4. Limite de linhas, de tempo e de tamanho de texto; PII mascarada em texto livre.

Configuracao SO por variaveis de ambiente (nunca no codigo):
  OTRS_DB_HOST, OTRS_DB_PORT (3306), OTRS_DB_NAME (otrs),
  OTRS_DB_USER, OTRS_DB_PASSWORD, OTRS_MASK_PII (1)
"""

import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal

import pymysql

logger = logging.getLogger("otrs-core")

MAX_ROWS = 50
MAX_TEXT = 2000
ALLOWED_PRIVS = {"SELECT", "USAGE", "SHOW VIEW"}
MASK_PII = os.environ.get("OTRS_MASK_PII", "1") != "0"

# ---------------------------------------------------------------- seguranca --

_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"")
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|replace|drop|alter|create|truncate|grant|revoke|"
    r"call|do|handler|load|lock|unlock|set|rename|optimize|repair|analyze|"
    r"kill|shutdown|install|sleep|benchmark|get_lock|release_lock)\b"
    r"|\binto\s+(outfile|dumpfile|@)|\bfor\s+(update|share)\b|\block\s+in\s+share\b",
    re.I,
)
_START = re.compile(r"^\s*(select|show|describe|desc|explain)\b", re.I)


def validar_sql(sql: str) -> str:
    """Aceita um unico statement de leitura; levanta ValueError caso contrario."""
    if not sql or not sql.strip():
        raise ValueError("SQL vazio")
    if "/*" in sql or "--" in sql or "#" in _STRING_LITERAL.sub("''", sql):
        raise ValueError("comentarios SQL nao sao permitidos")
    sem_literais = _STRING_LITERAL.sub("''", sql)
    corpo = sem_literais.strip().rstrip(";").strip()
    if ";" in corpo:
        raise ValueError("somente um statement por chamada")
    if not _START.match(corpo):
        raise ValueError("somente SELECT/SHOW/DESCRIBE/EXPLAIN")
    m = _FORBIDDEN.search(corpo)
    if m:
        raise ValueError(f"palavra/construcao nao permitida: {m.group(0)!r}")
    return sql.strip().rstrip(";")


# Ordem importa: CNPJ antes de CPF.
_PII = [
    (re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), "[CNPJ]"),
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[CPF]"),
    (re.compile(r"\b[A-Za-z]{3}-?\d[A-Za-z0-9]\d{2}\b"), "[PLACA]"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"(\(?\d{2}\)?\s?)?9?\d{4}-?\d{4}\b"), "[TEL]"),
]

# Identificadores e metadados nunca sao mascarados (so texto livre).
_SEM_MASCARA = {"id", "tn", "f_number", "fila", "estado", "remetente", "create_time",
                "created", "changed", "Field", "Type", "Null", "Key", "Default", "Extra"}


def mascarar(texto: str) -> str:
    for rx, rotulo in _PII:
        texto = rx.sub(rotulo, texto)
    return texto


def _limpar(valor, chave=None):
    if isinstance(valor, (bytes, bytearray)):
        valor = valor.decode("utf-8", "replace")
    if isinstance(valor, (datetime, date)):
        return valor.isoformat(sep=" ")
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, str):
        if MASK_PII and chave not in _SEM_MASCARA:
            valor = mascarar(valor)
        if len(valor) > MAX_TEXT:
            valor = valor[:MAX_TEXT] + f"... [truncado, {len(valor)} chars]"
    return valor


def linhas(rows):
    return [{k: _limpar(v, k) for k, v in r.items()} for r in rows]


# -------------------------------------------------------------------- banco --

_conn = None
_grants_ok = False


def _verificar_grants(cur):
    """Recusa operar se o usuario tiver qualquer privilegio alem de leitura."""
    global _grants_ok
    cur.execute("SHOW GRANTS FOR CURRENT_USER()")
    for row in cur.fetchall():
        linha = next(iter(row.values()))
        m = re.match(r"GRANT\s+(.*?)\s+ON\s", linha, re.I | re.S)
        if not m:
            continue
        privs = {p.strip().upper() for p in m.group(1).split(",")}
        extras = privs - ALLOWED_PRIVS
        if extras:
            raise RuntimeError(
                "usuario do banco tem privilegios alem de leitura "
                f"({sorted(extras)}); recuso operar. Use um usuario so com SELECT."
            )
    _grants_ok = True


def conectar():
    global _conn
    if _conn is not None:
        try:
            _conn.ping(reconnect=True)
            return _conn
        except Exception:
            _conn = None
    faltando = [v for v in ("OTRS_DB_HOST", "OTRS_DB_USER", "OTRS_DB_PASSWORD") if not os.environ.get(v)]
    if faltando:
        raise RuntimeError(f"variaveis de ambiente nao definidas: {', '.join(faltando)}")
    host = os.environ["OTRS_DB_HOST"]
    _conn = pymysql.connect(
        host=host,
        port=int(os.environ.get("OTRS_DB_PORT", "3306")),
        user=os.environ["OTRS_DB_USER"],
        password=os.environ["OTRS_DB_PASSWORD"],
        database=os.environ.get("OTRS_DB_NAME", "otrs"),
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=20,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    with _conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        for stmt in ("SET SESSION max_statement_time=15",      # MariaDB
                     "SET SESSION max_execution_time=15000"):  # MySQL
            try:
                cur.execute(stmt)
            except Exception:
                pass
        if not _grants_ok:
            _verificar_grants(cur)
    logger.info("conectado a %s (somente leitura)", host)
    return _conn


def consultar(sql: str, params=(), limite: int = MAX_ROWS):
    conn = conectar()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchmany(limite)


# ------------------------------------------------------------------ consultas --

def tabelas():
    return [next(iter(r.values())) for r in consultar("SHOW TABLES", limite=1000)]


def colunas(tabela: str):
    if not re.fullmatch(r"[A-Za-z0-9_]+", tabela):
        raise ValueError("nome de tabela invalido")
    return consultar(f"DESCRIBE `{tabela}`")


def faq_todos():
    """Todos os artigos de FAQ aprovados e validos, SEM mascara (uso interno, indexacao)."""
    return consultar(
        "SELECT id, f_number, f_subject, f_keywords, f_field1, f_field2, f_field3, "
        "f_field4, f_field5, f_field6 FROM faq_item WHERE valid_id = 1 AND approved = 1",
        limite=10000,
    )


def faq_like(palavras, limite=100):
    campos = ["i.f_subject", "i.f_keywords", "i.f_field1", "i.f_field2", "i.f_field3"]
    cond, params = [], []
    for p in palavras:
        cond.append("(" + " OR ".join(f"{c} LIKE %s" for c in campos) + ")")
        params += [f"%{p}%"] * len(campos)
    sql = ("SELECT i.* FROM faq_item i WHERE i.valid_id = 1 AND i.approved = 1 AND ("
           + " OR ".join(cond) + ") LIMIT %s")
    return consultar(sql, params + [limite], limite=limite)


def titulos_fechados(palavras, fila_prefix=None, dias=730, n=60, minimo=2):
    """Chamados FECHADOS cujo TITULO contem pelo menos `minimo` das palavras. Rapido (so a
    tabela ticket); use candidatos_fechados para buscar tambem no texto dos artigos."""
    minimo = max(1, min(minimo, len(palavras)))
    params = [dias]
    filtro_fila = ""
    if fila_prefix:
        filtro_fila = " AND q.name LIKE %s"
        params.append(f"{fila_prefix}%")
    soma = " + ".join(["(t.title LIKE %s)"] * len(palavras))
    params += [f"%{p}%" for p in palavras]
    params += [minimo, n]
    sql = (
        "SELECT t.id, t.tn, t.title, q.name AS fila, s.name AS estado, t.create_time "
        "FROM ticket t "
        "JOIN queue q ON q.id = t.queue_id "
        "JOIN ticket_state s ON s.id = t.ticket_state_id "
        "JOIN ticket_state_type st ON st.id = s.type_id AND st.name = 'closed' "
        "WHERE t.create_time >= NOW() - INTERVAL %s DAY" + filtro_fila +
        " AND (" + soma + ") >= %s ORDER BY t.create_time DESC LIMIT %s"
    )
    return consultar(sql, params, limite=n)


def candidatos_fechados(palavras, fila_prefix=None, dias=365, n=40):
    """Chamados FECHADOS cujo titulo/artigos citam alguma das palavras (sem artigos).
    LENTO (varre o texto de centenas de milhares de artigos); preferir titulos_fechados."""
    params = [dias]
    filtro_fila = ""
    if fila_prefix:
        filtro_fila = " AND q.name LIKE %s"
        params.append(f"{fila_prefix}%")
    cond = []
    for p in palavras:
        cond.append("(t.title LIKE %s OR m.a_subject LIKE %s OR m.a_body LIKE %s)")
        params += [f"%{p}%"] * 3
    sql = (
        "SELECT DISTINCT t.id, t.tn, t.title, q.name AS fila, s.name AS estado, t.create_time "
        "FROM ticket t "
        "JOIN queue q ON q.id = t.queue_id "
        "JOIN ticket_state s ON s.id = t.ticket_state_id "
        "JOIN ticket_state_type st ON st.id = s.type_id AND st.name = 'closed' "
        "JOIN article a ON a.ticket_id = t.id "
        "JOIN article_data_mime m ON m.article_id = a.id "
        "WHERE t.create_time >= NOW() - INTERVAL %s DAY" + filtro_fila +
        " AND (" + " OR ".join(cond) + ") ORDER BY t.create_time DESC LIMIT %s"
    )
    return consultar(sql, params + [n], limite=n)


def respostas_agente(ticket_id, n=2):
    """Ultimas respostas de agente de um chamado (como foi resolvido)."""
    return consultar(
        "SELECT m.a_subject, m.a_body, a.create_time "
        "FROM article a "
        "JOIN article_data_mime m ON m.article_id = a.id "
        "JOIN article_sender_type ast ON ast.id = a.article_sender_type_id AND ast.name = 'agent' "
        "WHERE a.ticket_id = %s ORDER BY a.create_time DESC LIMIT %s",
        (ticket_id, n),
    )


def chamado_por_tn(tn):
    rows = consultar(
        "SELECT t.id, t.tn, t.title, q.name AS fila, s.name AS estado, t.create_time "
        "FROM ticket t JOIN queue q ON q.id = t.queue_id "
        "JOIN ticket_state s ON s.id = t.ticket_state_id WHERE t.tn = %s",
        (tn,),
    )
    if not rows:
        return None, []
    arts = consultar(
        "SELECT ast.name AS remetente, m.a_subject, m.a_body, a.create_time "
        "FROM article a JOIN article_data_mime m ON m.article_id = a.id "
        "JOIN article_sender_type ast ON ast.id = a.article_sender_type_id "
        "WHERE a.ticket_id = %s ORDER BY a.create_time ASC LIMIT 20",
        (rows[0]["id"],),
    )
    return rows[0], arts

#!/usr/bin/env python3
"""
MCP SOMENTE-LEITURA para o banco do OTRS/Znuny. A logica de banco e as garantias de
seguranca (grants so SELECT, sessao READ ONLY, SQL validado, mascara de PII) ficam em
otrs_core.py; este arquivo so expoe as tools via MCP.

Variaveis de ambiente: ver otrs_core.py. Dependencias: pymysql (em /app/vendor) e mcp.
"""

import asyncio
import json
import logging
import re

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

import otrs_core as core

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("otrs-mcp")

server = Server("otrs-leitura-mcp")


def _palavras(termo: str):
    return [p for p in re.findall(r"\w{3,}", termo.lower())][:8]


def t_schema(args):
    tabela = (args.get("tabela") or "").strip()
    if not tabela:
        return {"tabelas": core.tabelas()[:500]}
    return {"tabela": tabela, "colunas": core.linhas(core.colunas(tabela))}


def t_select(args):
    sql = core.validar_sql(args.get("sql", ""))
    return {"linhas": core.linhas(core.consultar(sql))}


def t_faq_buscar(args):
    palavras = _palavras(args.get("termo", ""))
    if not palavras:
        raise ValueError("informe um termo com pelo menos 3 letras")
    limite = min(int(args.get("limite", 5)), 20)
    campos = ["f_subject", "f_keywords", "f_field1", "f_field2", "f_field3"]
    itens = core.faq_like(palavras)

    def score(r):
        texto = " ".join(str(r.get(c) or "") for c in campos).lower()
        return sum(texto.count(p) for p in palavras)

    itens.sort(key=score, reverse=True)
    return {"itens": core.linhas(itens[:limite]), "palavras": palavras}


def t_chamados_similares(args):
    palavras = _palavras(args.get("termo", ""))
    if not palavras:
        raise ValueError("informe um termo com pelo menos 3 letras")
    limite = min(int(args.get("limite", 5)), 20)
    dias = min(int(args.get("dias", 365)), 1825)
    if args.get("buscar_no_texto"):
        tickets = core.candidatos_fechados(palavras, args.get("fila"), dias, n=30)  # lento
    else:
        tickets = core.titulos_fechados(palavras, args.get("fila"), dias, n=30)
    saida = []
    for t in tickets[:limite]:
        saida.append({
            "chamado": core.linhas([t])[0],
            "respostas_agente": core.linhas(core.respostas_agente(t["id"])),
        })
    return {"similares": saida, "palavras": palavras}


def t_chamado(args):
    tn = str(args.get("numero", "")).strip()
    if not re.fullmatch(r"\d{6,20}", tn):
        raise ValueError("numero de chamado invalido")
    chamado, arts = core.chamado_por_tn(tn)
    if not chamado:
        return {"erro": "chamado nao encontrado"}
    return {"chamado": core.linhas([chamado])[0], "artigos": core.linhas(arts)}


TOOLS = {
    "otrs_schema": (
        t_schema,
        "Lista as tabelas do banco do OTRS ou descreve as colunas de uma tabela.",
        {"tabela": {"type": "string", "description": "opcional; vazio lista as tabelas"}},
        [],
    ),
    "otrs_select": (
        t_select,
        "Executa UM SELECT/SHOW/DESCRIBE/EXPLAIN somente leitura (maximo 50 linhas).",
        {"sql": {"type": "string"}},
        ["sql"],
    ),
    "faq_buscar": (
        t_faq_buscar,
        "Busca artigos aprovados e validos da FAQ do OTRS por palavras do problema.",
        {"termo": {"type": "string"}, "limite": {"type": "integer", "default": 5}},
        ["termo"],
    ),
    "chamados_similares": (
        t_chamados_similares,
        "Acha chamados FECHADOS parecidos e devolve as ultimas respostas de agente "
        "(como foram resolvidos).",
        {
            "termo": {"type": "string"},
            "fila": {"type": "string", "description": "prefixo da fila, ex.: 'TI'"},
            "dias": {"type": "integer", "default": 365},
            "limite": {"type": "integer", "default": 5},
            "buscar_no_texto": {"type": "boolean", "default": False,
                                "description": "tambem busca no corpo dos artigos (lento, ~10s+)"},
        },
        ["termo"],
    ),
    "chamado_obter": (
        t_chamado,
        "Le um chamado pelo numero (tn) com seus artigos (ate 20).",
        {"numero": {"type": "string"}},
        ["numero"],
    ),
}


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=nome,
            description=desc,
            inputSchema={"type": "object", "properties": props, "required": req},
        )
        for nome, (_, desc, props, req) in TOOLS.items()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    if name not in TOOLS:
        return [types.TextContent(type="text", text=f"ferramenta desconhecida: {name}")]
    try:
        resultado = await asyncio.to_thread(TOOLS[name][0], arguments or {})
    except Exception as e:  # erro vira resposta, nao derruba o servidor
        logger.warning("%s falhou: %s", name, e)
        resultado = {"erro": str(e)}
    return [types.TextContent(type="text", text=json.dumps(resultado, ensure_ascii=False, default=str))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="otrs-leitura-mcp",
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())

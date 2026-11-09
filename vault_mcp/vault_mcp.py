#!/usr/bin/env python3
"""
MCP SOMENTE-LEITURA para o vault Obsidian (notas .md). Toda a logica e as garantias de
seguranca (raiz fixa, pastas permitidas, mascara de segredos) ficam em vault_core.py;
este arquivo so expoe as tools via MCP.

Variaveis de ambiente: ver vault_core.py. Dependencia: mcp.
"""

import asyncio
import json
import logging

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

import vault_core as core

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vault-mcp")

server = Server("vault-leitura-mcp")

TOOLS = {
    "vault_buscar": (
        lambda a: core.buscar(a.get("termo", ""), a.get("pasta", ""), a.get("limite", 8)),
        "Busca notas do vault (documentacao interna: projetos, servidores, processos) por palavras. "
        "Devolve caminho, titulo e um trecho. Use antes de responder sobre infraestrutura/projetos da Cootravale.",
        {"termo": {"type": "string"},
         "pasta": {"type": "string", "description": "opcional, ex.: 'Projetos'"},
         "limite": {"type": "integer", "default": 8}},
        ["termo"],
    ),
    "vault_ler_nota": (
        lambda a: core.ler_nota(a.get("caminho", ""), a.get("inicio", 0)),
        "Le uma nota pelo caminho relativo (ex.: 'Projetos/otrs.md') ou so pelo nome ('otrs'). "
        "Segredos sao mascarados. Se truncado, repita com 'inicio' = proximo_inicio.",
        {"caminho": {"type": "string"}, "inicio": {"type": "integer", "default": 0}},
        ["caminho"],
    ),
    "vault_vizinhos": (
        lambda a: core.vizinhos(a.get("caminho", "")),
        "Mostra as notas ligadas por [[links]]: para onde a nota aponta e quem aponta para ela.",
        {"caminho": {"type": "string"}},
        ["caminho"],
    ),
    "vault_listar": (
        lambda a: core.listar(a.get("pasta", "")),
        "Lista subpastas e notas de uma pasta do vault (vazio = raiz).",
        {"pasta": {"type": "string", "description": "opcional, ex.: 'Projetos'"}},
        [],
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
                server_name="vault-leitura-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())

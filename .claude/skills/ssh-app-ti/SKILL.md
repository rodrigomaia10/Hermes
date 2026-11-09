---
name: ssh-app-ti
description: Acessa o servidor app-ti (192.168.1.251, user cootrati) por SSH, onde roda o Hermes em Docker. Use ao pedir para ver/alterar o Hermes, logs do container hermes-agent, ou qualquer coisa no app-ti.
---

# ssh-app-ti

Servidor: `cootrati@192.168.1.251` (hostname `appti`). Hermes = container `hermes-agent`, porta 8090, código em `~/hermes-docker`.

## Login (não interativo, PowerShell)
A senha está na env var Windows `app-ti`, **escopo User** (não aparece em `$env:` na sessão do Claude).

```powershell
$d = "<scratchpad>"
Set-Content "$d\askpass.ps1" -Encoding ascii 'Write-Output ([Environment]::GetEnvironmentVariable("app-ti","User"))'
Set-Content "$d\askpass.cmd" -Encoding ascii "@powershell -NoProfile -ExecutionPolicy Bypass -File `"$d\askpass.ps1`""
$env:SSH_ASKPASS="$d\askpass.cmd"; $env:SSH_ASKPASS_REQUIRE="force"; $env:DISPLAY="x"
ssh -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password cootrati@192.168.1.251 '<comando>'
Remove-Item "$d\askpass.*" -Force
```

## Armadilhas
- Lendo a var sem `,"User"` vem vazio → `Permission denied (publickey,password)`.
- Nunca imprimir/gravar o valor da senha; apagar o askpass ao final.
- `BatchMode=yes` falha (não há chave); não usar.
- PowerShell 5.1 remove aspas duplas dos argumentos do ssh e põe BOM no pipe: para comandos com aspas/sed/pipe, gravar o script com `[IO.File]::WriteAllText` (UTF-8 sem BOM, sem `\r`) e enviar com `cmd /c "type script.sh | ssh ... bash -s"`.
- Config do Hermes: `~/hermes-docker/data/config.yaml`; ao exibir, mascarar `key|token|secret|password`.
- Provedor do Hermes: `provider: openai-api` é sobreposto pelo `OPENAI_BASE_URL` do compose (Ollama); para outro endpoint use `provider: custom:<nome>` de `custom_providers`. Teste rápido: `docker exec hermes-agent hermes -z "prompt"` (aceita `--provider`/`-m`).
- 9router no servidor: container `9router` (compose), painel `:20128`, senha em `~/hermes-docker/.9router.env`, API interna `http://9router:20128/v1`; `docker exec -i` é necessário com heredoc; imagem sem curl.
- Testar código Python no app-ti: container descartável `docker run --rm -i --network hermes-docker_default -v ~/hermes-docker/app:/app:ro python:3.11-slim` com `pip install pymysql mcp==1.28.1` (mcp do Hermes = 1.28.1). Segredos via `export` no script (stdin), nunca em arquivo; apagar `remote.sh`/askpass do scratchpad no fim.
- No PowerShell, `Remove-Item` na mesma chamada do ssh às vezes é bloqueado e some a saída: apagar temporários em chamada separada com `-LiteralPath`.
- Chaves de provedores (ex.: `ANTHROPIC_API_KEY`) vão em `~/hermes-docker/data/.env` (= `/root/.hermes/.env`); o compose do `hermes-agent` não tem env_file. Para ler o config sem vazar segredos: `sed -E 's/((key|token|secret|password)[a-z_]*:).*/\1 ***/I'` dentro do script `remote.sh` (nunca inline no ssh).
- **Recriar o container:** serviço do compose = `hermes` (container `hermes-agent`): `docker compose up -d --no-deps hermes`; sem `--build`. A imagem é de jul/2026, então correções feitas dentro do container somem; o que precisa persistir fica em `~/hermes-docker/patches/` + volumes/env no compose (ui-tui, patch anthropic_adapter, HERMES_NODE, vault-data). "Chat unavailable: 1" = falta `site-packages/ui-tui/` e/ou `HERMES_NODE`. Ver memory `hermes-recriar-container-persistencia`.
- Só leitura por padrão (`docker ps`, `docker logs`, `ls`); alterar só se o usuário pedir.

## Mapa
- `~/hermes-docker` (compose + código), `~/docker-compose.yml`, `~/.pm2` (não investigado).
- `docker ps -a | grep hermes` → `hermes-agent`, `0.0.0.0:8090->8090`.

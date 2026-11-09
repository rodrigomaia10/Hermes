#!/bin/bash
# Roda NO SERVIDOR (app-ti). Recebe um tar do vault pelo stdin (enviado pelo PC via ssh), grava numa
# pasta nova e troca o symlink `current` de forma atomica. O container monta ~/hermes-docker/vault-data
# em /vault-root (somente leitura) e o MCP le /vault-root/current. Troca por symlink (e nao por mv de
# pasta) porque um bind mount de pasta substituida continuaria enxergando a antiga.
#
# A extracao e feita em Python (e nao com tar) porque o tar do Windows grava pastas com permissao 000,
# que o tar do Linux respeita; aqui as permissoes sao normalizadas e caminhos suspeitos sao recusados.
#
# Uso: authorized_keys com  command="/home/cootrati/hermes-docker/bin/vault_receive.sh",restrict  <chave>
set -euo pipefail
BASE="$HOME/hermes-docker/vault-data"
mkdir -p "$BASE"
TS="$(date +%Y%m%d-%H%M%S)"
NOVO="$BASE/v-$TS"
mkdir -p "$NOVO"
trap 'rm -rf "$NOVO"' ERR

# o script vai por -c (e nao "python3 - <<EOF") para o stdin continuar sendo o tar enviado pelo PC
PYEXTRAI=$(cat <<'PY'
import os, sys, tarfile
destino = os.path.realpath(sys.argv[1])
n = 0
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tf:
    for m in tf:
        partes = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".")]
        partes = partes[1:]                      # remove o diretorio raiz "claude_memory/"
        if not partes or ".." in partes:
            continue
        if not (m.isdir() or m.isfile()):        # ignora links e arquivos especiais
            continue
        alvo = os.path.realpath(os.path.join(destino, *partes))
        if os.path.commonpath([alvo, destino]) != destino:
            continue
        if m.isdir():
            os.makedirs(alvo, mode=0o755, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(alvo), mode=0o755, exist_ok=True)
        f = tf.extractfile(m)
        with open(alvo, "wb") as out:
            while True:
                bloco = f.read(1 << 20)
                if not bloco:
                    break
                out.write(bloco)
        os.chmod(alvo, 0o644)
        n += 1
print(f"extraidos {n} arquivos", file=sys.stderr)
PY
)
python3 -c "$PYEXTRAI" "$NOVO"

N=$(find "$NOVO" -type f -name '*.md' | wc -l)
if [ "$N" -lt 50 ]; then
  echo "recebido pouco conteudo ($N notas); mantendo a copia anterior" >&2
  rm -rf "$NOVO"
  exit 1
fi

date -Is > "$NOVO/.sync_ok"
ln -sfn "v-$TS" "$BASE/current.new"
mv -T "$BASE/current.new" "$BASE/current"

# mantem so as 2 versoes mais recentes
ls -1dt "$BASE"/v-* 2>/dev/null | tail -n +3 | xargs -r rm -rf
echo "ok: $N notas em v-$TS"

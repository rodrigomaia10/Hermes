# Envia o vault Obsidian do PC para o app-ti (o servidor nao alcanca o PC, entao quem inicia e o PC).
# Chamado pelo Agendador de Tarefas. Requer chave SSH dedicada (sem senha) restrita ao vault_receive.sh.
#   Chave:  $env:USERPROFILE\.ssh\hermes_vault_sync   (ed25519, so envia o vault)
#   Log:    $env:LOCALAPPDATA\hermes-vault-sync.log
$ErrorActionPreference = 'Stop'
$origem   = 'C:\project\claude_memory'
$servidor = 'cootrati@192.168.1.251'
$chave    = Join-Path $env:USERPROFILE '.ssh\hermes_vault_sync'
$log      = Join-Path $env:LOCALAPPDATA 'hermes-vault-sync.log'

function Log($m) { Add-Content -Path $log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m) }

try {
    if (-not (Test-Path $chave)) { throw "chave SSH ausente: $chave" }
    $pai  = Split-Path $origem -Parent
    $nome = Split-Path $origem -Leaf
    # tar.exe (bsdtar) vem no Windows 10/11. Exclui pastas de sistema do Obsidian.
    $cmd = 'tar.exe --exclude=.obsidian --exclude=.trash --exclude=.git -cf - -C "{0}" "{1}" | ssh -T -i "{2}" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new {3} 2>&1' -f $pai, $nome, $chave, $servidor
    # o 2>&1 vai DENTRO do cmd: assim o PowerShell nao enxerga stderr (com 'Stop' a 1a linha viraria excecao)
    $saida = (cmd.exe /c $cmd) -join ' | '
    if ($LASTEXITCODE -ne 0) { throw "falha ($LASTEXITCODE): $saida" }
    Log "ok: $saida"
} catch {
    Log "ERRO: $_"
    exit 1
}

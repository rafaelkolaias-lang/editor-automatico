# Regras Temporárias do Projeto

Este arquivo contém regras específicas e temporárias que se aplicam apenas ao projeto atual.
Diferente do `RULES.md` (regras globais e permanentes), as regras aqui podem ser adicionadas, alteradas ou removidas conforme a necessidade do momento.

---

## Regras ativas

### Notas de atualização — duplicar número da versão atual

Ao criar uma nova nota de atualização, **duplicar o número da versão atual** em vez de incrementar manualmente.

- A nova entrada (no topo) e a entrada antiga imediatamente abaixo devem usar o mesmo `{VERSAO}` (ex.: duas `5.7`).
- O bot de deploy (`build_e_deploy.bat`) atualiza o número da versão automaticamente ao subir, então a nova entrada passará a refletir a próxima versão sem intervenção manual.
- A entrada nova recebe o marcador ★ no título da aba; a anterior perde o ★.

### Economia de contexto - nao ler arquivos/pastas geradas

Ao trabalhar neste projeto, agentes devem ignorar e nunca ler, abrir, indexar, resumir ou pesquisar dentro dos seguintes arquivos e pastas, salvo pedido explicito do usuario:

- Arquivos: `*.zip`, `*.spec`, `*.exe`
- Pastas: `projeto-compilado/`, `cache/`, `animacao/`, `_internal/`, `__pycache__/`, `_pycache_/`, `projeto/`, `partes/`, `overlay/`, `narracao/`, `musica/`, `logo/`, `fontes/`, `temp_gh_code/`, `temp_gh_dir/`

Ao usar ferramentas de busca/listagem, aplicar exclusoes explicitas para esses padroes antes de analisar resultados.

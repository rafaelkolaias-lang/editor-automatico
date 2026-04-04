# Deploy - Regras de Seguranca

## REGRA NIVEL 10 - SEGURANCA MAXIMA: Protecao de Credenciais e Dados Sensiveis

**NUNCA** subir no Git informacoes sensiveis. Esta regra tem prioridade absoluta (nivel 10 de seguranca).

### O que NUNCA deve ser commitado:

- API keys (OpenAI, Gemini, AssemblyAI, ou qualquer outra)
- Tokens de autenticacao
- Senhas e credenciais de acesso
- Chaves privadas (SSH, SSL, etc.)
- Dados pessoais de usuarios
- Connection strings de banco de dados
- Secrets de qualquer servico externo

### Como proteger:

1. **Sempre adicionar ao `.gitignore`** arquivos que contenham credenciais (`settings.json`, `.env`, `*.key`, `credentials.*`)
2. **Usar variaveis de ambiente** ou arquivos locais ignorados pelo git para armazenar chaves
3. **Antes de qualquer commit ou push**, verificar se ha credenciais expostas nos arquivos sendo commitados
4. **Se uma chave vazar**, revogar imediatamente e gerar uma nova
5. **Nunca logar** chaves ou tokens em logs, prints ou saidas de debug

### Arquivos protegidos neste projeto:

- `settings.json` — contem API keys (ASSEMBLY_AI_KEY, OPENAI_API_KEY, GEMINI_API_KEY)
- `.env` — variaveis de ambiente locais
- Qualquer arquivo `*credentials*`, `*secret*`, `*.key`

### Checklist antes de subir codigo:

- [ ] `git diff --cached` nao contem nenhuma API key ou secret
- [ ] `settings.json` esta no `.gitignore`
- [ ] Nenhum arquivo sensivel esta sendo rastreado pelo git
- [ ] Logs e prints nao expoem credenciais

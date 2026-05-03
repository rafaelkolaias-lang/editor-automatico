# !projeto.md — Mapa do projeto Editor Premiere Premium

> **Propósito:** índice rápido para localizar código sem precisar varrer o repo.
> Leia o arquivo correspondente à sua tarefa em vez de tudo.
> **Manutenção:** atualize quando mudar estrutura, fluxo crítico ou convenção. Não documente todas as features — só o que ajuda a IA a navegar.
> Última atualização: 2026-04-15
> Versão atual: constantes `VERSAO` e `VERSAO_APLICACAO` em `app/__version__.py` (fonte única). São reescritas pela etapa **[1.5/6]** do `build_e_deploy.bat`, lendo de `versao.json` (manual `!deploy.md`).

---

## 1. Visão geral

Aplicativo desktop em Python com interface Tkinter que **automatiza edição de vídeo no Adobe Premiere Pro**.

- **Entry point:** `index.py`
- **Nome exibido:** `f'Automatizador do Premiere {VERSAO}'` — versão dinâmica vinda de `app/__version__.py`.
- **Versão:** `app/__version__.py` é a fonte única. Mantém formato exato (sem indentação, aspas duplas, em linhas separadas) exigido pelo regex do `.bat`. Reescrito na etapa [1.5/6] do `build_e_deploy.bat` para bater com `versao.json`. `index.py:24` apenas importa.
- **Janela inicial:** 480x520 (login), redimensiona para 800x620 após login.

O sistema conversa com o Premiere via `pymiere` e depende de:
- Adobe Premiere Pro aberto
- Um projeto do Premiere já aberto
- Plugin/painel de integração com o Premiere instalado e funcionando

O app monta vídeos quase automaticamente a partir de narrações, cenas, músicas e (opcionalmente) textos de impacto na tela. Também inclui um renomeador de cenas integrado com matching semântico via Gemini.

---

## 2. Stack e dependências

| Dependência | Uso |
|---|---|
| Python 3.x | Linguagem principal |
| Tkinter | Interface gráfica (SEM CustomTkinter) |
| pymiere | Controle do Adobe Premiere Pro via IPC |
| AssemblyAI | Transcrição de áudio (`speech_models` explícito: `universal-3-pro`/`universal-2`, detecção automática de idioma) |
| OpenAI (gpt-4o-mini) | Casamento semântico cena-fala + seleção de frases impactantes |
| google-genai (Gemini) | Descrição e matching semântico de cenas no renomeador |
| FFmpeg / ffprobe | Render de textos em .mov (alpha real), extensão de overlay/logo, conversão, validação/normalização de áudio |
| Pillow (PIL) | Thumbnails, conversão de imagem, preview de estilos |
| MoviePy | Conversão de vídeo |
| NumPy | Operações de embeddings |
| SciPy | `linear_sum_assignment` para matching global otimizado |
| OpenCV (cv2) | Hover-play de vídeo na tela de revisão do renomeador |
| pymysql | Conexão MySQL para autenticação |
| PyNaCl (nacl) | Decifragem NaCl de credenciais remotas |
| requests | Chamadas HTTP (credenciais remotas, etc.) |
| threading | Tarefas pesadas sem travar a UI |

---

## 3. Estrutura de pastas

```
editor-premiere-premium/
├── index.py                          # Entry point: login, update, credenciais, telas
├── app/__version__.py                # Fonte única de VERSAO / VERSAO_APLICACAO (reescrita pelo .bat)
├── settings.json                     # Configuracoes (UI cache, renamer UI). API keys NAO ficam aqui.
├── versao.json                       # Manifesto de versão local (lido pelo .bat e pelo updater)
├── versoes_publicadas.txt            # Histórico textual de versões publicadas (gerado pelo deploy)
├── build_e_deploy.bat                # Script de build PyInstaller + zip + upload FTP + bump de versão
├── updater.py                        # Bootstrap externo de auto-update (compilado em EditorUpdater.exe)
├── debug.py                          # Script auxiliar de debug
├── proxima-execucao.md               # Notas livres da próxima execução
├── exemplo-cenas-com-feedback.py     # Exemplo de uso do renomeador de cenas
├── Editor.exe                        # Executável principal compilado
├── EditorUpdater.exe                 # Executável do updater externo
├── Editor.spec                       # Spec do PyInstaller
├── README.md                         # Apresentação do repo
├── AGENTS.md                         # Notas de agentes (similar a CLAUDE.md, contexto multi-agente)
├── CLAUDE.md                         # Instruções para IA
├── temporary_rules.md                # Regras temporárias do projeto
├── !projeto.md                       # Este mapa
├── !executar.md                      # Plano de execução / fila de tarefas
│
├── core/                             # Módulos v2.1+ (login, update interno, credenciais)
│   ├── __init__.py
│   ├── auth.py                       # Autenticação MySQL + auto-login
│   ├── updater.py                    # Verificação e instalação de atualizações (in-app)
│   └── remote_credentials.py         # Busca e decifragem de API keys remotas
│
├── app/
│   ├── __version__.py                # (ver acima) — fonte única de versão
│   │
│   ├── entities/                     # Classes de dados simples
│   │   ├── Dimensions.py             # Largura e altura
│   │   ├── Extensions.py             # Extensões suportadas (audio, imagem, video)
│   │   ├── Part.py                   # Trecho identificado na transcrição (text, start)
│   │   ├── Result.py                 # Retorno padrão (success, data, error)
│   │   └── Transcription.py          # Transcrição + palavras temporizadas
│   │
│   ├── managers/                     # Lógica principal do sistema
│   │   ├── SettingsManager.py        # Leitura/escrita de settings.json
│   │   ├── DirectoriesManager.py     # Estrutura de pastas esperada
│   │   ├── ConversionManager.py      # Conversão de arquivos (audio→mp3, img→png, vid→mp4)
│   │   ├── TranscriptionManager.py   # AssemblyAI + casamento cena-fala via OpenAI
│   │   ├── TextOnScreenManager.py    # Frases impactantes (seleção GPT + render FFmpeg)
│   │   ├── SceneRenamerManager.py    # Matching semântico Gemini + embeddings + SciPy
│   │   ├── PremiereManager.py        # Núcleo de integração com o Premiere
│   │   └── premiere/                 # Sub-módulos do PremiereManager
│   │       ├── core.py               # Conexão, retries, heartbeat, fast_ops
│   │       ├── media.py              # Importação de arquivos
│   │       └── editing.py            # Montagem de timeline, zoom, fade, música
│   │
│   ├── ui/
│   │   ├── screens/
│   │   │   ├── InitialScreen.py      # Tela inicial (verificar Premiere)
│   │   │   ├── MainScreen.py         # Tela principal (config do video)
│   │   │   ├── SettingsScreen.py     # Tela de status das credenciais
│   │   │   ├── WorkingScreen.py      # Tela de processamento + TerminalPopup
│   │   │   └── RenamerFeedbackScreen.py  # Renomeador de cenas (split-screen)
│   │   ├── dialogs/
│   │   │   └── StyleEditorDialog.py  # Editor de estilos de frases impactantes
│   │   └── components/
│   │       └── select.py             # Componente de seleção reutilizável
│   │
│   └── utils/
│       ├── ffmpeg_path.py            # Resolve caminho do ffmpeg/ffprobe bundled
│       ├── create_renamed_file.py    # Cópia renomeada com _ para fallback de importação
│       ├── debug_print.py            # Logs de depuração
│       ├── get_error_handler.py      # Handler global de erros do Tkinter
│       ├── handle_thread_error.py    # Crash log + exibição de erro em thread
│       ├── pymiere_installer.py      # Instalador do painel pymiere (CEP) com detecção cross-PC + UI Tk
│       └── renamer_utils.py          # ~25 funções puras do renomeador (dataclasses, sanitize, etc.)
│
├── assets/                           # Recursos do app
│   ├── text_styles.json              # Estilos salvos para frases impactantes
│   ├── release_notes.txt             # Notas de release
│   └── pymiere_panel/                # Painéis CEP do pymiere bundled (Pymiere Link + com.qmasingarbe.PymiereLink)
│
├── ffmpeg/                           # FFmpeg bundled (bin, doc, lib, etc.)
│   └── bin/                          # ffmpeg.exe, ffprobe.exe
│
├── fontes/                           # Fontes TTF (arial, arialbd, arialbi, etc.)
│
├── cache/                            # Cache do renomeador de cenas
│   ├── scene_renamer_config.json
│   ├── scene_renamer_scene_cache.json
│   ├── scene_renamer_stable_matches.json
│   ├── scene_renamer_last_run.json
│   └── scene_renamer_undo_last_run.json
│
├── cenas/                            # Pasta de cenas (por roteiro)
├── narracao/                         # Pasta de narrações (por roteiro)
├── musica/                           # Pasta de músicas (por estilo)
├── projeto/                          # Saída: caches de transcrição, impact_text, etc.
├── partes/                           # Pasta do modo em massa
├── overlay/                          # Arquivos de overlay + cache 10min
├── logo/                             # Arquivos de logo + cache 10min
│
└── renomeador de cena1.1.py          # Script legado (backup, lógica migrada para app/)
```

---

## 4. Fluxo de execução

```
App abre (480x520, fundo escuro)
  └─> Login (auto-login com ~/.credenciais_rk.json OU tela manual)
       └─> Auto-update (somente se .exe / frozen)
            └─> Validação de credenciais (status_credencial das 3 slugs, em thread)
                 ├─ Alguma pendente? → SettingsScreen em modo bloqueado (não deixa sair)
                 └─ Todas OK? → Redimensiona (800x620) → InitialScreen (auto-check)
                                      └─> MainScreen → WorkingScreen
```

### Detalhamento de cada etapa:

**1. Login (`_init_login`)**
- Tenta ler `~/.credenciais_rk.json` (auto-login).
- Flag `_skip_auto_login`: se ativa (após logout manual), preenche campos mas NÃO autentica.
- Sucesso: `salvar_login()` + `set_credenciais_usuario()` → `_apos_login()`.

**2. Auto-update (`_verificar_update`)**
- Só em `.exe` (frozen). Dev pula direto para validação de credenciais.

**3. Validação de credenciais (`_ir_para_tela_inicial` + `_apos_checar_credenciais`)**
- Thread daemon chama `status_credencial(slug)` para OpenAI/AssemblyAI/Gemini.
- Se faltar alguma: abre `SettingsScreen` em `force_block=True` (sem botão Voltar efetivo).
- Todas OK: segue para `InitialScreen` + `_auto_check_premiere`.
- NÃO persiste valores em disco; aquecimento do cache é efeito colateral de `status_credencial`.

**4. InitialScreen / MainScreen / WorkingScreen**
- `InitialScreen`: auto-check do Premiere; READY → pula para MainScreen.
- `MainScreen`: configuração + mixer + menu com "Credenciais", "Sair (deslogar)", "Terminal".
- `WorkingScreen`: transcrição, casamento, montagem; TerminalPopup abre automaticamente.

**5. Logout (`_logout`)**
- Menu `Opções → Sair (deslogar)` em MainScreen. Também botão na SettingsScreen.
- Chama `limpar_cache()`, zera `_usuario`, desrenderiza telas, volta para login.
- `~/.credenciais_rk.json` preservado (campos ficam preenchidos). Seta `_skip_auto_login=True`.

---

## 5. Sistema de login (v2.1)

**Arquivo:** `core/auth.py`

### Componentes:
- `RepositorioAuth`: classe com conexão MySQL por thread (thread-local)
- `ler_login_salvo()`: lê `~/.credenciais_rk.json`
- `salvar_login()`: persiste `user_id` e `chave` em `~/.credenciais_rk.json`

### Banco de dados:
- Host: `76.13.112.108:3306`
- Database: `dados`
- Tabela: `usuarios` (campos: `user_id`, `chave`, `status_conta`, `nome_exibicao`)
- Query: `WHERE user_id = %s AND chave = %s AND status_conta = 'ativa'`

### Fluxo de auto-login:
1. `_init_login()` chama `ler_login_salvo()`
2. Se há credenciais salvas: monta tela de login já preenchida, mostra "Verificando..."
3. Thread separada chama `_auth.autenticar_usuario()`
4. Sucesso: `_login_sucesso()` → `salvar_login()` + `set_credenciais_usuario()` → `_apos_login()`
5. Falha: mostra "Sessão expirada. Faça login novamente."

### UI do login:
- Tela dark theme (#0d1117 / #161b22)
- Card centralizado com campos Usuário e Chave
- Botão azul (#1b6ef3) "Entrar"
- Enter no campo chave dispara login
- Enter no campo usuário foca o campo chave

---

## 6. Atualização automática

Há **dois updaters**:

### 6.1 `core/updater.py` — updater interno (in-app)
- Pré-condição: `getattr(sys, 'frozen', False)` é True (em dev é pulado).
- URL remota: `https://magiaparamor.com/aplicacoes/editor-premiere-premium/versao.json`
- Funções: `verificar_atualizacao(versao_local)`, `baixar_zip(url, destino, callback)`, `instalar_atualizacao(zip_path, status_callback)`.
- Comparação de versão: `_comparar_versoes()` normaliza prefixo `v` e faz padding com zeros (`v2.1 == 2.1.0`).
- Instalação: extrai ZIP → script PowerShell em pasta temporária → `robocopy /E` para substituir arquivos → reinicia o exe.
- UI: card dark theme com label de status + barra de progresso (ttk).

### 6.2 `updater.py` (raiz) → `EditorUpdater.exe` — bootstrap externo
- Script standalone (compilado separadamente em `EditorUpdater.exe`).
- Usado quando o app não consegue se atualizar a si mesmo (ex.: arquivos travados pelo próprio Editor.exe).
- Funções principais: `discover_package()`, `download_package()`, `terminate_running_app()`, etc.
- Lê credenciais e listing remoto, encerra o app rodando, baixa e aplica o ZIP, reinicia.

---

## 7. Credenciais remotas

**Arquivo:** `core/remote_credentials.py` — fonte ÚNICA das API keys em runtime.

### Arquitetura:
- Chave de decifragem NaCl embutida no binário (constante base64).
- Servidor: `https://banco-painel.cpgdmb.easypanel.host`.
- API: `/commands/credenciais/api/obter.php?identificador=<slug>&user_id=...&chave=...`.
- Resposta: `{ dados: { versao_cliente: 1, cipher, nonce } }`, decifrada com `nacl.secret.SecretBox`.

### Slugs → nome de API usado no código:
| Slug | Nome usado em `get_api_key(name)` |
|---|---|
| `chatgpt` | `OPENAI_API_KEY` |
| `assembly` | `ASSEMBLY_AI_KEY` |
| `gemini` | `GEMINI_API_KEY` |

### Política de segurança (alinhada ao `!manual-credenciais.md`):
- user_id e chave ficam apenas em memória.
- Valor decifrado fica em cache só na memória (`_cache` dict).
- **Nada persistido em disco.** `SettingsManager` remove bloco `env` automaticamente.
- `limpar_cache()` é chamado no `WM_DELETE_WINDOW` e no logout.

### API pública:
- `set_credenciais_usuario(user_id, chave)` — após login.
- `obter_credencial(slug, *, usar_cache=True)` — busca, decifra, cacheia.
- `get_api_key(env_name)` — helper para os managers; mapeia nome amigável → slug, nunca levanta exceção (retorna `""`).
- `status_credencial(slug, *, usar_cache=True)` — `(ok, msg)` sem expor valor. `usar_cache=False` força nova consulta.
- `listar_credenciais()`, `limpar_cache()`.

### Quem consome:
- `TranscriptionManager` e `TextOnScreenManager`: `OPENAI_API_KEY`/`ASSEMBLY_AI_KEY` via `@property` que chama `get_api_key`.
- `MainScreen`: valida antes de rodar fluxo GPT.
- `RenamerFeedbackScreen`: `GEMINI_API_KEY` via `get_api_key`.
- `index.py`: `status_credencial` no startup para decidir se bloqueia em SettingsScreen.

---

## 8. Mapa de arquivos

| Arquivo | Quando ler | O que faz |
|---|---|---|
| `index.py` | Sempre — é o entry point | Login, update, validação de credenciais, logout (`_logout`), flag `_skip_auto_login`, callbacks de navegação, auto-check Premiere. Importa `VERSAO`/`VERSAO_APLICACAO` de `app/__version__.py`. |
| `app/__version__.py` | Quando mexer em versionamento/build | Fonte única de `VERSAO` e `VERSAO_APLICACAO`. Reescrito automaticamente pelo `build_e_deploy.bat` a partir de `versao.json`. NÃO editar à mão. |
| `core/auth.py` | Para entender o login | MySQL auth, auto-login via ~/.credenciais_rk.json, salvar/ler credenciais |
| `core/updater.py` | Para entender o auto-update interno | Verificação de versão remota, download ZIP, script PS1 de instalação |
| `updater.py` (raiz) | Para entender o updater externo | Bootstrap standalone compilado em `EditorUpdater.exe`; encerra app, baixa pacote, aplica e reinicia |
| `core/remote_credentials.py` | Para entender credenciais | NaCl decrypt + cache em memória + `get_api_key` (helper por env name) + `status_credencial(..., usar_cache)` |
| `app/managers/SettingsManager.py` | Para entender configurações | Localiza raiz, garante settings.json, leitura/escrita. **Remove `env` automaticamente** (API keys não vão para disco) |
| `app/managers/DirectoriesManager.py` | Para entender estrutura de pastas | Garante pastas (cenas, narração, música, etc.), lista opções, modo em massa |
| `app/managers/ConversionManager.py` | Para entender fallback de importação | Converte audio→mp3, imagem→png, video→mp4, identifica tipo por extensão |
| `app/managers/TranscriptionManager.py` | Para entender transcrição | AssemblyAI com `speech_models=['universal-3-pro', 'universal-2']`, timeout 300s, retry para erros transitórios, validação via `ffprobe` + log antes do upload, cache JSON + casamento cena-fala via OpenAI (gpt-4o-mini). Em recusa da AssemblyAI, **não** normaliza nem reenviar — propaga o erro real via `_AssemblyRefusedError`. Keys via `@property` que chama `get_api_key()` |
| `app/managers/TextOnScreenManager.py` | Para entender frases impactantes | Seleção GPT + render FFmpeg (.mov alpha) + inserção em V6. `OPENAI_API_KEY` via `@property`/`get_api_key()` |
| `app/managers/SceneRenamerManager.py` | Para entender o renomeador | Gemini descrição + embeddings + SciPy matching + cache em cache/ |
| `app/managers/PremiereManager.py` | Para entender integração Premiere | Núcleo: status, importar, sequência, timeline, zoom, fade, música, overlay, logo |
| `app/managers/premiere/core.py` | Para entender conexão Premiere | Retries, backoff, heartbeat, reset, fast_ops |
| `app/managers/premiere/media.py` | Para entender importação | Importação de arquivos com fallback |
| `app/managers/premiere/editing.py` | Para entender montagem | Timeline, zoom, fade, música, overlay, logo |
| `app/ui/screens/InitialScreen.py` | Para entender tela inicial | Mensagem de status, botão Prosseguir, auto-check. **Menubar próprio** (Opcoes > Credenciais / Instalar pymiere; Ajuda; Sair) — `on_open_settings` e `on_logout` são wirados em `index.py` (mesmas funções usadas pela MainScreen). Aplicado em `render()` via `app.config(menu=...)`. |
| `app/ui/screens/MainScreen.py` | Para entender tela principal | Todas as opções de configuração, mixer de áudio, menu com "Sair (deslogar)", botão Renomear Cenas. **Menubar próprio** aplicado em `render()` (não em `__init__`) — cada tela (Initial/Main) instala seu menubar em `app.config(menu=...)` quando renderiza. |
| `app/ui/screens/SettingsScreen.py` | Para entender tela de credenciais | **Tela de status** dos 3 slugs (OpenAI/AssemblyAI/Gemini). Refresh async, modo `force_block` que bloqueia saída, botão "Sair (deslogar)". Sem campos de edição. |
| `app/ui/screens/WorkingScreen.py` | Para entender tela de processamento | Barra de progresso, TerminalPopup (captura stdout/stderr) |
| `app/ui/screens/RenamerFeedbackScreen.py` | Para entender o renomeador UI | Fase 1 (setup + processamento) e Fase 2 (revisão split-screen com thumbnails/hover-play) |
| `app/ui/dialogs/StyleEditorDialog.py` | Para entender editor de estilos | Cor, borda, sombra, animação, preview em tempo real, salvar/carregar estilos |
| `app/ui/components/select.py` | Para entender componentes | Componente de seleção reutilizável |
| `app/utils/ffmpeg_path.py` | Para entender caminho ffmpeg | Resolve caminho do ffmpeg/ffprobe bundled (usado antes de qualquer import de moviepy) |
| `app/utils/create_renamed_file.py` | Para entender fallback importação | Copia arquivo com _ no nome |
| `app/utils/debug_print.py` | Para entender logs | debug_print com categorias |
| `app/utils/get_error_handler.py` | Para entender tratamento de erro | Handler global para Tkinter report_callback_exception |
| `app/utils/handle_thread_error.py` | Para entender crash em thread | Registra crash log, mostra erro |
| `app/utils/pymiere_installer.py` | Para entender instalação do painel pymiere | Fluxo completo do menu "Opcoes > Instalar pymiere": (1) baixa e extrai Python 3.11.9 embeddable em `%LOCALAPPDATA%\PymiereInstaller\python`, habilita pip via `_pth` e roda `pip install pymiere`; (2) copia `assets/pymiere_panel/` para **todos** os destinos onde tem permissão (`%APPDATA%\Adobe\CEP\extensions` + `Program Files (x86)\Common Files\Adobe\CEP\extensions` + variante x64); (3) habilita `PlayerDebugMode=1` em `HKCU\Software\Adobe\CSXS.{9..12}`; (4) avisa para reiniciar o Premiere. Valida Premiere fechado antes. Toplevel Tk com log progressivo, download com %, mensagens de erro distintas para rede/timeout/permissão. |
| `app/utils/renamer_utils.py` | Para entender funções do renomeador | ~25 funções puras: dataclasses, sanitize, placeholders, ffmpeg clips, etc. |
| `app/entities/Dimensions.py` | Quando mexer em resolução | Largura e altura |
| `app/entities/Extensions.py` | Quando mexer em tipos de arquivo | Listas de extensões de áudio, imagem, vídeo |
| `app/entities/Part.py` | Quando mexer em transcrição | Trecho com text e start |
| `app/entities/Result.py` | Quando mexer em retornos | Padrão success/data/error |
| `app/entities/Transcription.py` | Quando mexer em transcrição | Transcrição + palavras temporizadas |
| `settings.json` | Para entender configurações atuais | **Apenas `ui_cache` e `renamer_ui`**. API keys NÃO ficam mais aqui (fonte = servidor) |
| `versao.json` | Para entender versionamento | Manifesto de versão local; lido pelo `.bat` para bumpar `app/__version__.py` |
| `versoes_publicadas.txt` | Para histórico de releases | Lista textual gerada pelo deploy |
| `build_e_deploy.bat` | Quando publicar versão | Build PyInstaller → zip → upload FTP → bump de versão (etapa [1.5/6] reescreve `app/__version__.py`) |
| `assets/text_styles.json` | Para entender estilos de texto | Estilos salvos pelo editor de frases |
| `assets/release_notes.txt` | Para entender o histórico de releases ao usuário | Notas de release exibidas dentro do app |
| `renomeador de cena1.1.py` | NÃO ler — backup legado | Script original do renomeador (lógica migrada para app/) |
| `debug.py` | Auxiliar pontual | Script de debug |
| `exemplo-cenas-com-feedback.py` | Exemplo de uso | Demo do renomeador com feedback |
| `proxima-execucao.md` | Notas livres | Anotações curtas para a próxima execução |
| `AGENTS.md` / `README.md` | Contexto de repo | Apresentação e notas para agentes/contribuintes |

---

## 9. Configurações (settings.json)

Contém **apenas** `ui_cache` e `renamer_ui`. API keys não são mais persistidas aqui — `SettingsManager.read_settings()` e `write_settings()` removem o bloco `env` automaticamente se encontrarem (migração silenciosa de versões antigas).

### `ui_cache` — Estado da última sessão
| Campo | Tipo | Descrição |
|---|---|---|
| `mode` | string | "transcription" ou "mass" |
| `script` / `script_name` | string | Roteiro selecionado |
| `music_style` | string | Estilo de música |
| `resolution` | string | "1920 x 1080" |
| `fade_percentage` | int | % de fade |
| `fade_live` | bool | Aplicar fade ao vivo |
| `zoom_min` / `zoom_max` | int | Escala de zoom (100 = sem zoom) |
| `duplicate_scenes` | bool | Duplicar cenas para preencher bloco |
| `fill_gaps_without_scene` | bool | Preencher lacunas com cenas aleatórias |
| `mass_order` | string | "asc" ou "shuffle" |
| `min_scene_seconds` / `max_scene_seconds` | int | Duração de cena no modo massa |
| `max_fill_scene_duration` | float | Duração máxima de cena de preenchimento |
| `impact` | object | Config completa de frases impactantes (ver abaixo) |
| `impact.enabled` | bool | Ativar frases |
| `impact.mode` | string | "phrase" ou "word" |
| `impact.max_phrases_total` | int | Máximo de frases no vídeo |
| `impact.min_gap_seconds` | float | Intervalo mínimo entre frases |
| `impact.font_choice` / `font_file` | string | Fonte selecionada |
| `impact.font_size_px` | int | Tamanho da fonte |
| `impact.text_style` | object | Estilo visual (cor, borda, sombra, animação, etc.) |
| `impact.use_cache` | bool | Reutilizar cache do GPT |
| `overlay_file` / `overlay_path` | string | Arquivo de overlay |
| `logo_file` / `logo_position` | string | Logo e posição (top_right, etc.) |
| `cta_enabled` | bool | Ativar CTA (inscreva-se) |
| `cta_anim_path` / `cta_file` | string | Arquivo do CTA |
| `cta_chroma_key` | bool | Chroma key no CTA |
| `vol_narration` / `vol_cta` / `vol_music` / `vol_scene` | float | Volumes em dB |

### `renamer_ui` — Estado do renomeador de cenas
| Campo | Tipo | Descrição |
|---|---|---|
| `cenas_dir` | string | Pasta de cenas |
| `roteiro_file` | string | Arquivo do roteiro |
| `output_dir` | string | Pasta de saída |
| `allow_pro` | int (0/1) | Permitir fallback Gemini Pro |
| `include_audio` | int (0/1) | Incluir áudio nas cenas |
| `allow_reuse` | int (0/1) | Permitir reutilizar cenas |
| `n_words` | int | Palavras no nome do arquivo (2-20) |
| `sentences_per_chunk` | int | Frases por item (1-5) |
| `max_uses` | int | Máximo de usos por cena (1-10) |

---

## 10. Build e deploy

### Dados do projeto:
| Item | Valor |
|---|---|
| Slug no servidor | `editor-premiere-premium` |
| URL versao.json remota | `https://magiaparamor.com/aplicacoes/editor-premiere-premium/versao.json` |
| Nome do executável | `Editor` (gera `Editor.exe`) |
| Updater externo | `EditorUpdater.exe` (compilado a partir de `updater.py` na raiz) |
| Arquivo de entrada | `index.py` |
| Fonte de versão | `app/__version__.py` (reescrito por `build_e_deploy.bat` a partir de `versao.json`) |
| Usa CustomTkinter? | **NÃO** (Tkinter puro) |

### Hidden imports obrigatórios:
- `pymysql`
- `pymysql.cursors`
- `PIL._tkinter_finder`
- `nacl`
- `nacl.secret`

### Collect-submodules:
- `assemblyai`
- `openai`
- `google.genai`

### Arquivos extras para copiar para `projeto-compilado/`:
- `versao.json`
- `ffmpeg/` (diretório completo)
- `fontes/` (diretório completo)
- `assets/` (diretório completo)

**NÃO incluir no build:**
- `settings.json` — será criado automaticamente pelo `SettingsManager.ensure_settings()` no primeiro uso
- `animacao/` — conteúdo do usuário, não pertence ao executável

### Referência de build:
Consultar `D:/regra-global-LLM/implementacoes/!deploy.md` para o template de `build_e_deploy.bat`. Esse manual contém o passo-a-passo completo para criar o .bat, incluindo build PyInstaller, zipagem, upload FTP e bump de versão.

### Faixas no Premiere (mapeamento):

**Vídeo:**
| Faixa | Index | Conteúdo |
|---|---|---|
| V1 | 0 | Cenas |
| V3 | 2 | CTA Inscreva-se |
| V4 | 3 | Overlay |
| V5 | 4 | Logo |
| V6 | 5 | Frases impactantes (legendas) |

**Áudio:**
| Faixa | Index | Conteúdo |
|---|---|---|
| A1 | 0 | Áudio das cenas |
| A2 | 1 | Narração |
| A3 | 2 | Inscreva-se (CTA) |
| A5 | 4 | Música |

Volume: dB convertido para ganho linear via `10^((dB - 15) / 20)`.

---

## 11. Histórico de versões

### v2.0
Versão base do projeto com todas as funcionalidades de edição:
- Transcrição AssemblyAI
- Casamento cena-fala via OpenAI
- Montagem automática de timeline
- Zoom, fade, música
- Frases impactantes com editor de estilos
- Renomeador de cenas integrado (Gemini + embeddings + SciPy)
- Overlay e logo com cache FFmpeg
- Modo em massa (implementado no backend, radio button comentado)

### v2.1
Adicionado sistema de autenticação, atualização automática e credenciais remotas:
- **Login:** MySQL auth com auto-login via `~/.credenciais_rk.json`, tela dark theme
- **Auto-update:** verificação de versão remota, download com progresso, script PowerShell de instalação (somente .exe)
- **Credenciais remotas:** busca API keys cifradas do servidor via NaCl, substitui valores locais em memória
- **Pasta `core/`:** três novos módulos (`auth.py`, `updater.py`, `remote_credentials.py`)
- **`index.py` refatorado:** fluxo linear Login → Update → Credenciais → Tela inicial

### v2.4 (atual compilada)
Endurecimento da transcrição AssemblyAI, melhorias de update e UI:
- **Renomeador de cenas:** seleção direta de vídeos (filtra extensões compatíveis com IA), defaults mais práticos.
- **Auto-update:** log detalhado passo a passo, janela permanece aberta em erro com botão "Fechar", arquivo de log salvo automaticamente, fim do loop de baixar atualização já instalada, encerramento garantido do app antigo antes de copiar.
- **UI:** título da janela e tela "Sobre" leem `VERSAO` em runtime; menu "Configurações" virou "Credenciais" (somente status); console preto do FFmpeg suprimido; fechar a tela do Renomeador no meio do processamento não gera mais erro.
- **Notas de atualização:** carregamento robusto no app compilado.
- **Transcrição AssemblyAI (Tarefas 5/6/7 do !executar):**
  - `TranscriptionConfig` envia `speech_models=['universal-3-pro', 'universal-2']` (a API exige lista não-vazia).
  - `aai.settings.http_timeout = 300s`, `ThreadPoolExecutor(max_workers=2)`, retry com backoff 2s/5s/10s para erros transitórios (`httpx.TimeoutException`, `NetworkError`, `TransportError`, `TimeoutError`, "timed out"/"connection reset").
  - Pré-validação por áudio (existe, tamanho > 0, extensão conhecida, duração > 0 via `ffprobe`) + log com tamanho_mb/extensão/duração/codec.
  - Em recusa (`TranscriptError`/`failed to transcribe url`/`select-the-speech-model`): embrulha o erro original em `_AssemblyRefusedError` e propaga (sem fallback de normalização).
  - Mensagens de erro distintas para configuração de `speech_models`, recusa final (lista nomes dos arquivos), `ValueError` de arquivo inválido e timeout. Sem `handle_thread_error` para falhas conhecidas.

### v2.5 (em desenvolvimento)
Próxima versão (conforme `versao.json`). Sem mudanças mapeadas aqui ainda.

---

## 12. Armadilhas conhecidas

### Conexão com o Premiere
- `pymiere` depende de um plugin/painel de integração instalado no Premiere. Sem ele, nada funciona.
- O painel oficial (`Pymiere Link` + `com.qmasingarbe.PymiereLink`) fica empacotado em `assets/pymiere_panel/` e é instalado pelo menu **Opcoes > Instalar pymiere** (`app/utils/pymiere_installer.py`). O instalador faz: (a) download e extração de Python 3.11.9 embeddable em `%LOCALAPPDATA%\PymiereInstaller\python` + `pip install pymiere`; (b) cópia dos painéis para **todos** os destinos com permissão (`%APPDATA%\Adobe\CEP\extensions` sempre; `Program Files (x86)/Common Files/Adobe/CEP/extensions` se admin); (c) `PlayerDebugMode=1` em `HKCU\Software\Adobe\CSXS.{9..12}` para painéis não-assinados; (d) instrução para reiniciar o Premiere. Requer internet e Premiere fechado.
- O Premiere pode perder conexão IPC a qualquer momento. O `PremiereManager` tem retries, backoff e heartbeat, mas ainda pode falhar em operações longas.
- `fast_ops()` agrupa operações para reduzir chamadas IPC — não quebrar esse padrão.

### Importação de arquivos
- Arquivos com caracteres especiais no nome podem falhar. O sistema tenta: (1) importar direto, (2) cópia com `_`, (3) conversão, (4) cópia da conversão.
- GIFs têm fluxo específico de conversão.

### Auto-update
- Updater interno (`core/updater.py`) só funciona quando executando como .exe compilado (`sys.frozen`). Em dev, é completamente pulado.
- Existe ainda um updater externo (`EditorUpdater.exe`, compilado de `updater.py` na raiz) para casos em que o app não pode se atualizar sozinho.
- O script PowerShell assume que o exe se chama `Editor.exe` — se mudar o nome, precisa atualizar `core/updater.py`.
- O `robocopy` no PS1 substitui todos os arquivos. Se o ZIP contiver arquivos corrompidos, o app quebra.

### Versionamento
- `app/__version__.py` é a **fonte única**. NÃO editar à mão — edite `versao.json` e rode o `build_e_deploy.bat` (etapa [1.5/6] reescreve o arquivo).
- Manter o formato exato (sem indentação, aspas duplas, em linhas separadas) para o regex do `.bat` funcionar.

### Credenciais remotas
- Se o servidor estiver fora ao iniciar, o app fica bloqueado na `SettingsScreen` (sem fallback local por design).
- A chave de decifragem NaCl está embutida no binário como constante. Se comprometida, todas as credenciais ficam expostas.
- Cache é em memória por processo: cada restart precisa consultar o servidor novamente.

### Settings
- `ui_cache` tem campos duplicados: `script` e `script_name` guardam o mesmo valor.
- `settings.json` de versões antigas pode ter bloco `env` com keys — ele é removido automaticamente na primeira leitura após atualizar.

### Transcrição AssemblyAI
- A API atual exige `speech_models` explícito; não usar `speech_model=aai.SpeechModel.best` nem config sem modelo.
- Config esperada em `TranscriptionManager`: `speech_models=['universal-3-pro', 'universal-2']`, `language_detection=True`, `punctuate=True`.
- O fluxo valida áudio com `ffprobe` e tenta o original. Se a AssemblyAI recusar, **não** normaliza nem reenvia — propaga o erro real via `_AssemblyRefusedError`. A mensagem agregada lista os arquivos recusados e o erro principal.
- Erro de AssemblyAI dizendo que `speech_models` deve ser lista não-vazia indica bug de configuração, não áudio/credencial.

### Frases impactantes
- O render FFmpeg usa codec `qtrle+argb` para alpha real. Se o FFmpeg bundled não suportar, falha silenciosamente.
- Cache de frases salvo em `projeto/<roteiro>/impact_text/_impact_cache.json`. Se o roteiro mudar e o cache não for limpo, frases antigas podem ser reutilizadas.

### Renomeador de cenas
- Gemini tem rate limits agressivos. O sistema tenta modelo barato → médio → pro, mas pode falhar em lotes grandes.
- O `SceneRenamerManager` usa `ThreadPoolExecutor(max_workers=4)` para descrição paralela. Muitas cenas podem causar throttling.
- `get_embeddings_batched(...)` tem retry/backoff próprio para 429/rate limit (respeita `Please retry in Xs/ms`, senão backoff 5/10/20/40/60s com jitter). Em falha definitiva levanta `QuotaExhaustedError` — `RenamerFeedbackScreen._processing_worker` salva `_last_script_items`/`_last_scene_descs`/`_last_manager` **antes** do matching e pré-popula `_assignments` vazios, para "Reprocessar Pendências" funcionar mesmo após falha nos embeddings.
- Os caches em `cache/` são por sessão do renomeador, não por roteiro. Se trocar de roteiro sem limpar, pode haver contaminação.

### Modo em massa
- O radio button do modo em massa está **comentado** na interface. O backend funciona, mas não é acessível pelo usuário.

### Threading
- Quase todas as operações pesadas rodam em threads daemon. Se o app fechar durante processamento, threads são interrompidas abruptamente.
- O `TerminalPopup` captura stdout/stderr desde o início — pode acumular muito texto em sessões longas.

### Paths
- O `ffmpeg_path.py` é chamado no topo de `index.py` (antes de qualquer import de moviepy) para setar `IMAGEIO_FFMPEG_EXE`. Se esse import falhar, moviepy usa o ffmpeg do sistema (se houver).

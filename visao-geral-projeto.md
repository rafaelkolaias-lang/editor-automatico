# Handoff do projeto

## Visão geral

Este projeto é um **aplicativo desktop em Python com interface Tkinter** para **automatizar edição de vídeo no Adobe Premiere Pro**.

O nome exibido na janela é **“Automatizador do Premiere”**.
O ponto de entrada é o arquivo **`index.py`**.

O sistema conversa com o Premiere via **`pymiere`**, então ele depende de:

* **Adobe Premiere Pro aberto**
* **um projeto do Premiere já aberto**
* **plugin/painel de integração com o Premiere instalado e funcionando**

O aplicativo foi desenhado para montar vídeos quase automaticamente a partir de:

* **narrações**
* **cenas**
* **músicas**
* e, opcionalmente, **textos de impacto na tela**

---

## Objetivo do projeto

O projeto atual serve para:

1. Ler uma estrutura local de pastas com os arquivos do vídeo.
2. Validar se o Premiere está pronto.
3. Transcrever os áudios de narração com **AssemblyAI**.
4. Encontrar, dentro da transcrição, os momentos em que os nomes das cenas aparecem.
5. Importar os arquivos para o Premiere.
6. Montar automaticamente a timeline:

   * narração
   * cenas
   * zoom
   * fade
   * música
   * opcionalmente textos de impacto
7. Em um fluxo separado, também existe um modo de **“vídeo em massa”**, baseado em uma pasta `partes/`.

Além disso, o pacote inclui um script separado chamado **`renomeador de cena1.1.py`**, que é um utilitário independente para renomear/casar cenas com frases.

---

## Stack atual

O projeto usa principalmente:

* **Python**
* **Tkinter** para interface
* **pymiere** para controlar o Premiere
* **AssemblyAI** para transcrição
* **OpenAI API** (`gpt-4o-mini` via `/v1/responses`) para casamento semântico de cenas e frases impactantes
* **Gemini API** (`google-genai`) para descrição e matching semântico de cenas no renomeador integrado
* **FFmpeg** para gerar vídeos de texto e alguns renders auxiliares
* **Pillow (PIL)** para conversão de imagens e thumbnails
* **MoviePy** para conversão de vídeo
* **NumPy** para operações de embeddings
* **SciPy** (`linear_sum_assignment`) para otimização de casamento global de cenas
* **OpenCV (cv2)** para hover-play de vídeo na tela de revisão
* **requests** para chamadas HTTP
* **threads** para executar tarefas pesadas sem travar a interface

---

## Estrutura principal do projeto

### Arquivo de entrada

* **`index.py`**
  Cria a janela principal, monta as telas e instancia o `PremiereManager`.

### Pasta `app/entities`

Contém classes simples de dados:

* **`Dimensions.py`**
  guarda largura e altura

* **`Part.py`**
  representa um trecho identificado na transcrição
  campos:

  * `text`
  * `start`

* **`Result.py`**
  padrão de retorno com:

  * `success`
  * `data`
  * `error`

* **`Transcription.py`**
  representa uma transcrição e suas palavras temporizadas

* **`Extensions.py`**
  lista as extensões suportadas de:

  * áudio
  * imagem
  * vídeo

### Pasta `app/managers`

Contém a lógica principal do sistema.

#### `SettingsManager.py`

Responsável por:

* localizar a raiz de execução do projeto
* garantir que exista `settings.json`
* ler e salvar configurações

O `settings.json` guarda:

* chaves de API
* cache da interface
* opções do último uso

#### `DirectoriesManager.py`

Responsável por:

* garantir a existência das pastas esperadas
* ler as opções disponíveis para a interface
* montar a estrutura do modo em massa

Pastas previstas na raiz do projeto:

* `musica`
* `cenas`
* `narracao`
* `xml`
* `projeto`
* `partes`

#### `ConversionManager.py`

Responsável por converter arquivos problemáticos quando o Premiere não importa direito.

Conversões atuais:

* áudio → **mp3**
* imagem → **png**
* vídeo → **mp4**
* gif mantém fluxo específico

Ele também identifica o tipo do arquivo pela extensão.

#### `TranscriptionManager.py`

Responsável por:

* enviar áudios para a AssemblyAI
* salvar a transcrição em `.json` ao lado do áudio
* reutilizar o `.json` se ele já existir
* transformar palavras transcritas em objetos internos
* procurar, dentro da transcrição, trechos que correspondam aos nomes das cenas

O casamento atual funciona de forma **Inteligente (LLM)**:

* Fatiam-se as palavras transcritas em segmentos cronológicos curtos (ex: a cada 6 segundos).
* Envia-se à OpenAI (`gpt-4o-mini`) a lista de cenas disponíveis e os segmentos do roteiro temporizados.
* A IA avalia semanticamente o contexto e define qual cena usar em qual segmento.
* O sistema recebe a resposta em JSON explícita e cria os objetos `Part` ordenados cronologicamente pelos `start_ms`.
* Retém a antiga busca baseada em regras exatas (string match) como fallback de segurança.

#### `TextOnScreenManager.py`

Responsável por gerar **textos de impacto na tela**.

Fluxo atual:

1. junta as palavras temporizadas da transcrição
2. monta segmentos com base em pausas
3. filtra segmentos ruins
4. pede para a OpenAI escolher os melhores trechos
5. renderiza esses trechos em `.mov` com fundo transparente usando FFmpeg
6. opcionalmente insere esses overlays na timeline do Premiere

Modos de texto existentes:

* **`phrase`** → frase inteira
* **`word`** → palavra por palavra

Também permite definir:

* posição: topo / centro / baixo
* fonte
* arquivo da fonte
* tamanho da fonte

#### `SceneRenamerManager.py`

Toda a lógica de matching semântico de cenas, extraída e modularizada do `renomeador de cena1.1.py`.

Responsabilidades:

* configuração e cache: `load_config/save_config`, cache de descrições de cenas, cache de matches estáveis, estado do último run, undo
* descrição de cenas via Gemini:
  * `describe_image_with_fallback` (tenta modelo barato → médio → pro)
  * `describe_video`
  * `describe_single_scene`
  * `describe_all_scenes` paralelizado com `ThreadPoolExecutor(max_workers=4)` e progresso thread-safe
* embeddings: `get_embeddings_batched`, `normalize_rows`
* matching global: `build_global_assignment` usando `scipy.optimize.linear_sum_assignment` com fallback min-cost max-flow em Python puro
* Facade `SceneRenamerManager` com métodos `describe_scenes()`, `compute_assignments()`, `get_embeddings()`

Arquivos de cache gerados em `cache/`:

* `scene_renamer_config.json`
* `scene_renamer_scene_cache.json`
* `scene_renamer_stable_matches.json`
* `scene_renamer_last_run.json`
* `scene_renamer_undo_last_run.json`

---

#### `PremiereManager.py`

É o núcleo da integração com o Premiere.

Responsabilidades:

* verificar status do Premiere
* importar arquivos
* criar ou abrir sequência
* montar timeline
* aplicar zoom
* aplicar fade
* inserir overlay
* inserir logo
* exportar XML
* salvar projeto
* controlar retries e reconexão com `pymiere`

Esse manager delega partes da lógica para:

* **`app/managers/premiere/media.py`**
* **`app/managers/premiere/editing.py`**
* **`app/managers/premiere/core.py`**

### Pasta `app/ui`

Contém a interface em Tkinter.

#### `InitialScreen.py`

Tela inicial com a mensagem pedindo para abrir o Premiere e um projeto antes de continuar.

#### `MainScreen.py`

Tela principal do app.
É onde ficam:

* seleção de roteiro
* seleção de música
* seleção de resolução
* zoom
* fade
* opções de duplicação/preenchimento de cena
* frases impactantes
* ações de exportação
* botão **`🎬 Renomear Cenas`** no `header_buttons_frame` que abre a `RenamerFeedbackScreen` como Toplevel

#### `SettingsScreen.py`

Tela de configurações onde o usuário salva:

* `ASSEMBLY_AI_KEY`
* `OPENAI_API_KEY`

#### `RenamerFeedbackScreen.py`

Tela de casamento de cenas com feedback visual. Abre como `Toplevel` a partir do botão `🎬 Renomear Cenas` da `MainScreen`.

Opera em duas fases:

**Fase 1 — Setup:**
* seletores de pasta de cenas, arquivo de roteiro e pasta de saída
* checkboxes para pro-fallback e remoção de áudio
* barra de progresso e log escuro
* botão `[▶ Processar]` → dispara `SceneRenamerManager` em thread
* botão `[Ir para Revisão →]` após processamento

**Fase 2 — Revisão:**
* `PanedWindow` split-screen:
  * **esquerda:** `Listbox` com frases coloridas em vermelho para "⚠ SEM CENA", filtros (Todos / Sem cena / Com cena), vínculo manual via combobox
  * **direita:** `Canvas` com grid responsivo de cards de cena, thumbnails PIL para imagens, hover-play cv2 para vídeo (50ms/frame, 3-frame skip ≈ 2x velocidade)
* botão `[✓ Aplicar Cópia de Arquivos]` → copia arquivos via `shutil.copy2` para a pasta de saída com nomes das frases

#### `WorkingScreen.py`

Tela de espera enquanto o app processa transcrição e edição.

### Pasta `app/utils`

Utilitários do projeto:

* **`create_renamed_file.py`**
  cria cópia renomeada com `_` no nome para tentar contornar falha de importação

* **`debug_print.py`**
  logs de depuração

* **`get_error_handler.py`**
  trata erros globais do Tkinter

* **`handle_thread_error.py`**
  registra crash log e mostra erro quando falha em thread

* **`renamer_utils.py`**
  funções puras extraídas do `renomeador de cena1.1.py`, usadas pelo `SceneRenamerManager`.
  Contém:
  * constantes de extensões, modelos Gemini e thresholds de matching
  * dataclasses: `SceneDesc`, `FallbackPolicy`, `Assignment`
  * exceção `ProcessingCancelled`
  * ~25 funções utilitárias: `build_script_items`, `sanitize_filename`, `unique_path`, `create_green_placeholder_image`, `_compute_content_id`, `_ffmpeg_extract_clip_to_temp`, `_ffmpeg_remove_audio_to_temp`, etc.

---

## Fluxo principal atual: vídeo com transcrição

Esse é o fluxo principal realmente exposto no GUI.

### 1. Tela inicial

O usuário abre o app e clica em **Prosseguir**.

O sistema chama `premiere.get_status()` e verifica:

* plugin não instalado
* Premiere fechado
* projeto não aberto
* pronto para uso

### 2. Tela principal

O usuário escolhe:

* **roteiro**
* **estilo de música**
* **resolução**
* **zoom inicial**
* **zoom final**
* **fade**
* se vai **duplicar cenas**
* se vai **preencher lacunas com cenas aleatórias**
* se quer **frases impactantes**

### 3. Estrutura esperada das pastas

No modo principal, o sistema espera algo assim:

* `narracao/<roteiro>/...áudios...`
* `cenas/<roteiro>/...imagens e vídeos...`
* `musica/<estilo>/...áudios...`

### 4. Leitura dos arquivos

O `PremiereManager.get_files_paths()` resolve:

* caminho base das narrações
* caminho base das cenas
* caminho base das músicas
* lista de arquivos de narração
* lista de cenas
* lista de músicas

### 5. Importação com fallback

A importação para o Premiere segue esta ordem:

1. tenta importar do jeito que está
2. se falhar, cria uma cópia renomeada com `_`
3. se ainda falhar, converte:

   * áudio para mp3
   * imagem para png
   * vídeo para mp4
4. se ainda falhar, renomeia a versão convertida

Esse fluxo está centralizado em `MainScreen.__import_with_fallback()`.

### 6. Transcrição

Os áudios de narração são enviados para a AssemblyAI.

A transcrição atual:

* usa detecção de idioma
* usa o modelo `best`
* salva cache `.json`
* não usa pontuação (`punctuate=False`)

### 7. Casamento fala → cena

Depois da transcrição, o sistema tenta encontrar, dentro das palavras faladas, os nomes dos arquivos das cenas.

Exemplo do comportamento atual:

* se existe uma cena chamada `carro vermelho.mp4`
* e a narração fala “carro vermelho”
* o sistema encontra esse ponto e usa essa cena a partir dali

O resultado é um mapa de `Part`s para cada narração.

### 8. Criação/abertura da sequência no Premiere

A sequência recebe o nome do roteiro.

O sistema:

* procura uma sequência existente com esse nome
* se não existir, cria uma nova
* escolhe automaticamente um preset `.sqpreset`
* ajusta largura e altura da sequência depois de criada

### 9. Montagem da timeline

Na montagem principal, o sistema trabalha assim:

* insere a narração
* para cada trecho identificado:

  * calcula início do bloco
  * importa a cena correspondente
  * coloca a cena no track de vídeo
  * repete a cena até a próxima marca, se essa opção estiver ligada
  * ou insere apenas uma vez
  * opcionalmente preenche o restante com cenas aleatórias
* se passar do ponto, corta o excesso
* aplica zoom
* aplica fade por bloco
* no fim insere música até cobrir toda a narração
* corta a sobra da música
* silencia o áudio das cenas

---

## Faixas usadas no Premiere

O mapeamento atual é este:

### Vídeo

* **V0** → cenas
* **V1** → logo
* **V2** → overlay
* **V3** → textos de impacto

### Áudio

* **A0** → áudio das cenas
* **A1** → narração
* **A2** → música

O áudio das cenas é mutado no final da montagem.

---

## Comportamento visual atual

### Zoom

Cada bloco de cena recebe animação de zoom.

O sistema calcula:

* escala inicial
* multiplicador mínimo
* multiplicador máximo
* função de animação ao longo do tempo

### Fade

O fade é aplicado por **bloco**, não necessariamente por clipe individual.

Existe opção para:

* aplicar imediatamente
* ou acumular e aplicar depois

### Resolução

A resolução padrão atual é:

* **1920 x 1080**

Na interface existe também:

* **1536 x 768 (não funcionando)**

---

## Frases impactantes na tela

Esse recurso existe e pode ser ativado no GUI.

### Como funciona hoje

1. o sistema pega as transcrições e offsets reais de cada narração na timeline
2. transforma em palavras absolutas
3. monta segmentos por pausa
4. filtra segmentos repetidos ou ruins
5. envia candidatos para a OpenAI
6. a OpenAI devolve os melhores trechos
7. o sistema renderiza `.mov` transparentes com FFmpeg
8. os overlays são inseridos no track de vídeo 3

### Configurações disponíveis

* ativar ou desativar
* modo:

  * frase inteira
  * palavra por palavra
* máximo de frases no vídeo
* intervalo mínimo entre frases
* posição
* fonte
* tamanho da fonte

### Saída dos textos

Os arquivos são salvos em:

* `projeto/<nome_do_roteiro>/impact_text/`

---

## Fluxo “vídeo em massa”

O código desse modo existe e está implementado, mas no GUI atual o radio button do modo em massa está comentado.

Mesmo assim, o projeto possui o fluxo completo.

### Estrutura esperada

O modo em massa usa a pasta:

* `partes/`

Estrutura esperada:

* `partes/<roteiro>/<cena>/`

Dentro de cada pasta de cena podem existir:

* áudios
* imagens
* vídeos

### O que o sistema lê

`DirectoriesManager.read_mass_structure()` monta uma estrutura com:

* roteiros
* cenas
* audios
* medias
* lista agregada de arquivos

### Como a montagem funciona

Para cada roteiro:

1. abre ou cria sequência com o nome do roteiro
2. lê `info.txt` do roteiro, se existir
3. cada linha do `info.txt` pode virar uma cartela
4. monta áudios da cena
5. monta as mídias da cena
6. define duração de cada bloco visual entre mínimo e máximo
7. embaralha ou mantém ordem, conforme configuração
8. acelera vídeo se necessário
9. estica imagem se necessário
10. aplica zoom
11. aplica fade
12. insere música
13. corta sobra
14. insere overlay do roteiro
15. insere logo do roteiro

### Cartelas

O modo em massa suporta cartelas de texto baseadas em `info.txt`.

Também existe leitura de estilo e duração para essas cartelas.

### Overlay e logo

No modo em massa, o sistema pode inserir automaticamente:

* overlay em V2
* logo em V1

A configuração de opacidade e modo de mesclagem pode ser lida de arquivos como:

* `overlay.txt`
* `logo.txt`

---

## XML e salvamento

Existe método para:

* exportar XML
* salvar projeto

O botão de XML existe no código, mas está comentado na interface principal.

---

## Configurações atuais (`settings.json`)

O arquivo guarda dois blocos principais:

### `env`

Armazena:

* `ASSEMBLY_AI_KEY`
* `OPENAI_API_KEY`

### `ui_cache`

Armazena o estado do último uso da interface, incluindo:

* resolução
* estilo de música
* modo
* fade
* zoom
* duplicar cenas
* preencher lacunas
* frases impactantes
* roteiro selecionado
* ordem do modo em massa
* duração mínima e máxima das cenas

---

## Script legado: `renomeador de cena1.1.py`

> **Status:** mantido como backup. Toda a lógica foi modularizada em `app/managers/SceneRenamerManager.py`, `app/utils/renamer_utils.py` e `app/ui/screens/RenamerFeedbackScreen.py`, integrados ao `index.py` via botão na `MainScreen`.

Além do app principal, o pacote inclui esse script independente original.

Ele é um **utilitário separado**, não o fluxo principal do `index.py`.

### O que ele faz hoje

Esse script serve para:

* analisar frases do roteiro
* **descrever multi-cenas paralelamente**: usa `ThreadPoolExecutor` para acelerar submissões em nuvem.
* gerar embeddings
* fazer matching semântico entre frases e cenas
* renomear/copiar arquivos
* manter cache de resultados
* permitir reprocessamento de pendências progressivo nas sobras de cenas
* criar placeholders verdes quando não há cena
* manter histórico e undo da última execução

### Tecnologias usadas nele

* Tkinter
* Gemini
* embeddings
* cache em JSON
* PIL
* ffmpeg/subprocess
* lógica de reuso e score

### Arquivos de cache usados por ele

Na pasta `cache/` existem arquivos como:

* `scene_renamer_config.json`
* `scene_renamer_last_run.json`
* `scene_renamer_scene_cache.json`
* `scene_renamer_undo_last_run.json`

Ou seja: esse script é parte do pacote, mas funciona como uma ferramenta própria.

---

## Comportamento de robustez e tolerância a falhas

O `PremiereManager` tem bastante lógica para tentar manter a conexão com o Premiere viva.

Ele inclui:

* retries
* backoff
* heartbeat
* reset suave de conexão
* reimportação
* polling de indexação
* operações rápidas em lote com `fast_ops()`

Também há tratamento de erro global no Tkinter e gravação de logs de crash.

---

## Estado atual do produto

Hoje, o projeto é uma **suíte unificada** composta por dois blocos integrados no mesmo `index.py`:

### 1. Aplicativo principal de automação do Premiere

* interface Tkinter
* transcrição AssemblyAI
* casamento cena→fala via OpenAI (`gpt-4o-mini`) com fallback literal
* montagem automática de timeline
* zoom/fade
* música
* textos de impacto
* modo em massa implementado no backend

### 2. Renomeador de cenas integrado (ex-script separado)

Acessível via botão `🎬 Renomear Cenas` na `MainScreen`:

* `SceneRenamerManager`: Gemini + embeddings + otimização SciPy
* `RenamerFeedbackScreen`: revisão split-screen com thumbnails e hover-play
* cache em `cache/scene_renamer_*.json`
* undo do último run
* cópia controlada para pasta de saída
* `renomeador de cena1.1.py` mantido como backup

---

## Resumo executivo do projeto atual

Sistema local em Python que automatiza edição no Premiere a partir de uma organização de pastas.

No fluxo principal: pega narrações, cenas e músicas → transcreve com AssemblyAI → casa cenas com fala via OpenAI (semântico, fallback literal) → monta timeline no Premiere com zoom, fade, música e textos de impacto opcionais.

No fluxo de renomeação (integrado): descreve cenas com Gemini → gera embeddings → casamento global otimizado → revisão visual com split-screen → cópia controlada para pasta de saída.
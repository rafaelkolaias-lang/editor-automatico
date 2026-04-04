# Editor Automático — Automatizador do Premiere

Sistema local em Python que automatiza a edição de vídeos diretamente no Adobe Premiere Pro.

Lê uma estrutura de pastas com narrações, cenas e músicas → transcreve com AssemblyAI → casa cenas com fala via OpenAI → monta a timeline automaticamente com zoom, fade, música e textos de impacto.

---

## Funcionalidades

- Importação automática de narrações, cenas e músicas para o Premiere
- Transcrição de áudio via **AssemblyAI**
- Casamento semântico cena↔fala via **OpenAI GPT-4o-mini** (com fallback literal)
- Montagem automática da timeline com:
  - Zoom animado por bloco
  - Fade por bloco
  - Música de fundo com corte automático
  - Mute do áudio das cenas
  - Preenchimento de lacunas com cenas aleatórias
  - Duração máxima configurável para cenas aleatórias
- Textos de impacto na tela (gerados com FFmpeg, inseridos no Premiere)
- Renomeador de cenas integrado (Gemini + embeddings + revisão visual)
- Modo "vídeo em massa" (backend implementado)

---

## Pré-requisitos

- Python 3.10+
- Adobe Premiere Pro instalado e aberto com um projeto ativo
- Plugin pymiere instalado no Premiere
- FFmpeg disponível em `ffmpeg/`
- Chaves de API:
  - AssemblyAI
  - OpenAI
  - Google Gemini (para o renomeador de cenas)

---

## Instalação

```bash
pip install pymiere assemblyai openai google-genai pillow moviepy numpy scipy opencv-python requests
```

---

## Configuração

Na primeira execução, acesse **Configurações** na interface e insira:

- `ASSEMBLY_AI_KEY`
- `OPENAI_API_KEY`

As chaves são salvas em `settings.json`.

---

## Estrutura de pastas esperada

```
narracao/
  <roteiro>/
    audio1.mp3
    audio2.mp3

cenas/
  <roteiro>/
    cena-nome.mp4
    outra-cena.jpg

musica/
  <estilo>/
    faixa.mp3

projeto/          ← saída de XMLs e textos de impacto
cache/            ← cache de transcrições e renomeador
```

---

## Como usar

### Fluxo principal

1. Abra o Adobe Premiere Pro com um projeto ativo
2. Execute `python index.py`
3. Clique em **Prosseguir** na tela inicial
4. Selecione:
   - **Roteiro** (pasta dentro de `narracao/` e `cenas/`)
   - **Estilo de música**
   - **Resolução**
   - Opções de zoom, fade, duplicar cenas, preenchimento de lacunas e frases impactantes
5. Clique em **Exportar** — o sistema transcreve, casa as cenas e monta a timeline automaticamente

### Renomeador de cenas

Clique em **Renomear Cenas** na tela principal para abrir o renomeador integrado:

1. Selecione a pasta de cenas, o arquivo de roteiro e a pasta de saída
2. Clique em **Processar** — o sistema descreve as cenas via Gemini e faz o matching semântico
3. Revise os resultados na tela split-screen (com thumbnails e hover-play para vídeos)
4. Clique em **Aplicar Cópia de Arquivos** para copiar as cenas renomeadas para a saída

---

## Faixas usadas no Premiere

| Track | Conteúdo |
|-------|----------|
| V0 | Cenas |
| V1 | Logo |
| V2 | Overlay |
| V3 | Textos de impacto |
| A0 | Áudio das cenas (mutado) |
| A1 | Narração |
| A2 | Música |

---

## Arquitetura

```
index.py                        ← ponto de entrada

app/
  entities/                     ← modelos de dados (Part, Result, Transcription, ...)
  managers/
    PremiereManager.py          ← integração com o Premiere via pymiere
    TranscriptionManager.py     ← AssemblyAI + casamento cena↔fala
    SceneRenamerManager.py      ← Gemini + embeddings + matching global
    ConversionManager.py        ← conversão de mídia com FFmpeg/MoviePy
    DirectoriesManager.py       ← leitura de pastas e estrutura de arquivos
    SettingsManager.py          ← leitura/escrita de settings.json
    TextOnScreenManager.py      ← geração de textos de impacto com FFmpeg
    premiere/
      core.py                   ← conexão e verificação de status
      editing.py                ← montagem da timeline
      media.py                  ← importação e cache de mídia
  ui/
    screens/
      InitialScreen.py          ← tela de verificação do Premiere
      MainScreen.py             ← tela principal de configuração
      SettingsScreen.py         ← tela de chaves de API
      RenamerFeedbackScreen.py  ← tela de revisão do renomeador
      WorkingScreen.py          ← tela de progresso
  utils/                        ← utilitários diversos

renomeador de cena1.1.py        ← script legado (mantido como backup)
```

---

## Stack

| Tecnologia | Uso |
|-----------|-----|
| Python + Tkinter | Interface desktop |
| pymiere | Controle do Adobe Premiere Pro |
| AssemblyAI | Transcrição de áudio |
| OpenAI GPT-4o-mini | Casamento semântico cena↔fala e seleção de frases de impacto |
| Google Gemini | Descrição de cenas para o renomeador |
| FFmpeg | Render de textos de impacto e conversão de mídia |
| Pillow | Thumbnails e conversão de imagens |
| MoviePy | Conversão de vídeo |
| NumPy + SciPy | Embeddings e otimização de matching global |
| OpenCV | Hover-play de vídeo na revisão do renomeador |

---

## Notas de performance

O sistema aplica diversas otimizações para manter a velocidade na montagem:

- **Importação em lote:** todos os arquivos são importados de uma vez antes do loop
- **ffprobe paralelo:** dimensões de todos os arquivos são lidas simultaneamente com `ThreadPoolExecutor`
- **Zoom deferido:** o zoom é acumulado e aplicado após a inserção de todos os clipes
- **Cache de clipes O(1):** cada clipe inserido é capturado diretamente do retorno, sem reescanear a track

---

## Contribuintes 🚀

Em construção por **Kolaias** e **Lucas** 🏗️


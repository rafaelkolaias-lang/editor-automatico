# Próxima Execução (Editor Premiere - Performance de Inserção)

## Problema Atacado
Decaimento de performance severo na montagem da timeline: primeiras cenas inseridas em ~1-2/s, mas após 40+ cenas a velocidade cai drasticamente. Causa raiz: o ExtendScript do Premiere recalcula o layout da Timeline, gera blocos de histórico (Undo State) e redesenha ondas de áudio (GUI Redraw) a cada comando atômico enviado via socket pelo Python. Overhead acumulado de IPC.

---

## ✅ IMPLEMENTADO (2026-04-03)

### Fase 1 — Pré-processamento Assíncrono (`PremiereManager.py`)

**Novo método `__prefetch_all_media`** adicionado a `PremiereManager`:

1. **Importação em lote:** coleta todos os paths únicos (narrações + cenas + músicas) e chama `importFiles([...todos...])` **uma única vez**, antes do loop. Elimina N chamadas individuais com 5s de polling cada.
2. **Polling único:** aguarda indexação de todos os arquivos em loop único com timeout de 20s, populando `project_item_cache` de uma vez.
3. **ffprobe paralelo:** usa `ThreadPoolExecutor(max_workers=5)` + `as_completed` para executar `ffprobe` em todos os arquivos simultaneamente, populando `dims_cache` antes da montagem.

**Chamada em `editing.py` (`mount_sequence`):**
- Antes do `with mgr.fast_ops():` principal, coleta todos os paths e chama `__prefetch_all_media`.
- Quando o loop começa, `project_item_cache` e `dims_cache` já estão completos → `__get_or_import_project_item` e `__get_scene_dimensions_cached` retornam do cache instantaneamente.

### Fase 2 — Zoom Deferido (`editing.py`)

- Zoom não é mais aplicado **dentro** do loop principal de inserção.
- Cada bloco acumula `zoom_jobs.append((clips, dims, start_point, end_point))` ao invés de chamar `__animate_zoom` inline.
- Após o loop principal (todos os clipes já inseridos), um `with mgr.fast_ops():` separado percorre `zoom_jobs` e aplica todos os zooms em sequência.
- **Efeito:** o loop de inserção não interrompe o fluxo de `insertClip()` com operações de keyframe, reduzindo o número de GUI redraws durante a fase crítica de montagem.

---

## Perguntas em Aberto — RESPONDIDAS

1. **Abreviação Assíncrona:** ✅ Confirmado: semáforo de 5 threads apenas para pré-preparação (importações + ffprobe). A colagem na timeline permanece sequencial e bloqueada — o Premiere não crashará.
2. **Área específica do gargalo:** ✅ A lentidão vem de três fontes acumuladas: (a) `importFiles` + 5s de polling por arquivo, (b) `ffprobe` sequencial por arquivo, (c) `__animate_zoom` intercalado com `insertClip` forçando redraws. Todas as três foram atacadas.

---

## ✅ BUG CORRIGIDO (2026-04-03) — Remoção de Áudio de Cenas Anteriores

**Sintoma:** Ao cortar cenas na timeline, o áudio de cenas anteriores era removido incorretamente.

**Causa raiz:** O segundo parâmetro de `clip.remove(ripple, linkAction)` é `linkAction`. Com `True`, ao remover `video_clips[-1]` o Premiere **também remove automaticamente o áudio linkado** via link interno. Em seguida, `audio_clips[-1]` (capturado antes do remove) apontava para o clipe anterior — cujo áudio era então deletado junto com seu próprio vídeo linkado.

**Arquivos alterados:** `app/managers/premiere/editing.py`

**Correção aplicada em todos os locais afetados:**
- **Corte principal de cena** (`Cut remaining scene`): `remove(False, True)` → `remove(False, False)` nos removes de vídeo e áudio, com re-busca do estado da faixa de áudio **após** o remove de vídeo.
- **`fill_gaps_with_random_scenes` — Regra 1** (clipe menor que mínimo): `remove(False, True)` → `remove(False, False)`.
- **`fill_gaps_with_random_scenes` — Regra 2** (parte direita após razor): `remove(False, True)` → `remove(False, False)`.
- **`fill_gaps_with_random_scenes` — Regra 2 fallback** (clipe não ajustável): `remove(False, True)` → `remove(False, False)`.

---

---

## ✅ PERFORMANCE + BUG CORRIGIDO (2026-04-03) — Degradação quadrática O(N²)

**Sintoma:** Primeiras cenas inseridas em ~2/s; após 40 cenas, 1 cena a cada 10s.

**Causa raiz (performance):** `_best_match_clip` em `PremiereManager.__insert_clip_with_retry` chamava `list(track.clips)` a cada inserção — que faz **N chamadas IPC** para buscar todos os clipes. Com N clipes na track, cada nova inserção custava O(N), total O(N²). Além disso, `editing.py` chamava `list(vtrack.clips)` separadamente após cada inserção (para obter o clipe inserido e para zoom), acumulando mais O(N) por cena.

**Correções aplicadas:**

1. **`PremiereManager.py` — `_do()` em `__insert_clip_with_retry`:**
   - Substituiu `_best_match_clip(t)` (O(N)) por `t.clips[len(t.clips) - 1]` (O(1): 2 chamadas IPC).
   - `_best_match_clip` mantido como fallback para erros.

2. **`editing.py` — `_insert_scene_clip`:**
   - Captura o retorno de `__insert_clip_with_retry` (que já devolve o clipe).
   - Elimina o fetch `clips[-1]` separado que existia após o insert.
   - Acumula em `inserted_scene_clips[]` para uso no zoom.

3. **`editing.py` — Zoom:**
   - Trocou `list(vtrack.clips)` (O(N) IPC) por `inserted_scene_clips[-n:]` (O(0) extra).
   - Mantém sincronizado com roll-backs de fill_gaps (Regra 1 e Regra 2 fallback) e com o corte razor (atualiza `inserted_scene_clips[-1]` com o clipe truncado).

**Efeito esperado:** inserção passa de O(N²) para O(N) — timeline de 40 cenas deve manter velocidade constante em vez de degradar progressivamente.

---

## ✅ BUG CORRIGIDO (2026-04-03) — Remoção de Áudio de Cenas sem Áudio

**Sintoma:** Ao cortar uma cena **sem áudio**, o áudio da cena anterior era removido.

**Causa raiz:** O código sempre chamava `audio_clips[-1].remove()` após o corte, independente de a cena ter áudio. Se a cena não tinha áudio, o razor não criava nenhum clipe novo no audio track — então `audio_clips[-1]` apontava para o áudio de uma cena anterior.

**Correção:** Antes de remover, verifica se `last_audio.start.seconds ≈ next_scene_start.seconds` (tolerância 0.1s). Se o start não bater, o áudio não foi cortado pelo razor (cena sem áudio) — skip do remove.

**Arquivo alterado:** `app/managers/premiere/editing.py` — seção "Cut remaining scene".

---

---

## ✅ IMPLEMENTADO (2026-04-03) — Duração máxima de cenas aleatórias

**Funcionalidade:** Quando "Preencher espaço sem cena" está ativo, cenas longas (ex.: 30s) eram inseridas inteiras. Agora é possível limitar a duração máxima de cada cena aleatória.

**GUI:** Campo "Dur. máx. cena aleatória (s):" aparece abaixo do checkbox de fill_gaps somente no modo transcrição. Padrão: 7. Valor 0 desativa o limite. Oculto no modo massa (que tem seu próprio controle de duração).

**Lógica (±1s de margem):** Em `_insert_scene_clip`, quando `max_dur > 0`, após inserir o clipe calcula `actual_max = max_dur + random.uniform(-1.0, 1.0)` (mínimo 2s). Se o clipe for mais longo, usa razor + remove para truncar ao `actual_max`, atualizando a referência do clipe.

**Arquivos alterados:**
- `app/managers/premiere/editing.py` — parâmetro `max_fill_scene_duration` em `mount_sequence`; `max_dur` em `_insert_scene_clip`; lógica de trim.
- `app/managers/PremiereManager.py` — parâmetro repassado para `editing.mount_sequence`.
- `app/ui/screens/MainScreen.py` — `fill_gaps_dur_frame`, `max_fill_scene_entry`, `get_max_fill_scene_duration()`, cache save/restore, captura no export, passagem para `mount_sequence`.

---

## ✅ IMPLEMENTADO (2026-04-04) — Overlay/Logo: Cache FFmpeg + Renderização Paralela

**Problema:** Overlay e logo eram processados de forma ineficiente:
- **Overlay:** vídeo curto (ex: 10s) era inserido N vezes na timeline via loop de `insertClip()` — para uma sequência de 10min, ~60 inserções IPC.
- **Logo:** já tinha renderização FFmpeg de 10min com cache, mas era feita de forma síncrona/bloqueante — o usuário ficava esperando o FFmpeg terminar antes de qualquer cena ser inserida.

### Solução 1 — Cache de vídeo estendido (overlay + logo)

**Overlay (`__insert_overlay_full`):**
- Antes: inseria o vídeo original curto em loop repetido na timeline.
- Agora: usa `__extend_video_ffmpeg()` para pré-renderizar um vídeo de 10 minutos (loop do overlay via `-stream_loop`). O resultado é cacheado como `{nome}_10m.mp4` no mesmo diretório do overlay original.
- Na segunda execução com o mesmo overlay, o cache já existe → retorno instantâneo.
- Com o vídeo de 10min, apenas 1 inserção é necessária para a maioria das sequências (em vez de ~60).

**Logo (`__insert_logo_full`):**
- Já usava `__render_logo_positioned_mp4()` com cache (`logo_10m_{posição}.mp4`).
- Agora aceita parâmetro `prerendered_logo_path` para receber o resultado da thread paralela, evitando a renderização bloqueante inline.

### Solução 2 — Renderização FFmpeg em threads paralelas

**Fluxo em `editing.py` (`mount_sequence`):**

1. **Antes do loop de cenas:** cria `ThreadPoolExecutor(max_workers=2)` e submete:
   - Thread 1: `__extend_video_ffmpeg(overlay_path, 600.0)` → gera overlay de 10min
   - Thread 2: `__render_logo_positioned_mp4(logo_path, position, 600.0)` → gera logo de 10min
2. **Durante o loop:** as cenas são inseridas normalmente na timeline do Premiere enquanto o FFmpeg roda em background.
3. **Após o loop (antes da inserção na timeline):** `future.result(timeout=300)` aguarda os FFmpeg terminarem (se ainda não terminaram). Os paths dos vídeos pré-renderizados são passados para `__insert_overlay_full` e `__insert_logo_full`.
4. Se o FFmpeg já terminou (cache existente), o `future.result()` retorna instantaneamente.

**Efeito:**
- Primeira execução com overlay/logo novo: o FFmpeg roda em paralelo com a montagem das cenas — tempo total ≈ max(montagem, FFmpeg) em vez de montagem + FFmpeg.
- Execuções seguintes com o mesmo overlay/logo: cache instantâneo (0s de FFmpeg).
- Menos inserções na timeline: ~1 inserção de vídeo de 10min vs ~60 inserções de vídeo curto.

**Arquivos alterados:**
- `app/managers/PremiereManager.py` — `__insert_overlay_full` (novo param `prerendered_overlay_path`, fallback para `__extend_video_ffmpeg`), `__insert_logo_full` (novo param `prerendered_logo_path`, skip de render inline).
- `app/managers/premiere/editing.py` — import `concurrent.futures`, lançamento de threads FFmpeg após prefetch, join + passagem de resultados na seção de overlay/logo.

---


## ✅ IMPLEMENTADO (2026-04-04) — Otimização de Renderização (Logo/Overlay)

**Correções:**
1. **Cache inteligente:** `__extend_video_ffmpeg` verifica via ffprobe se o vídeo original já tem >= 90% da duração alvo (540s). Se sim, usa o original sem FFmpeg. Prints de "cache encontrado" adicionados.
2. **Logo cache com nome único:** `logo_10m_{pos}.mp4` → `{logoname}_10m_{pos}.mp4` — logos diferentes não compartilham cache.
3. **Filtragem no GUI:** `__list_logo_files` e `__list_overlay_files` ignoram arquivos `_10m.mp4` e `_10m_`.
4. **Indicadores visuais:** Labels "(Renderizado)" verde ou "(Nao renderizado)" vermelho ao lado dos seletores de Logo e Overlay, com traces em tempo real.
5. **Console silencioso:** `creationflags=0x08000000` em todos os `subprocess.run` do FFmpeg no Windows.

---

## ✅ IMPLEMENTADO (2026-04-04) — Frases Impactantes em Paralelo + Melhorias

**Paralelização:**
- `build_text_overlays` (seleção OpenAI + renderização FFmpeg) roda em `ThreadPoolExecutor(1)` em paralelo com a inserção de overlay/logo/CTA na timeline.
- Após a inserção, `_impact_future.result()` coleta os `.mov` prontos e insere no Premiere.

**Track V6:** `IMPACT_TEXT_TRACK_INDEX` movido de index 2 (V3) para index 5 (V6), acima de overlay e logo.

**Mesclagem "Tela" fixa:** Frases impactantes sempre usam modo de mesclagem "Tela" (Screen), sem ler do overlay.txt.

**Mais frases geradas:**
- Prompt GPT: "exatamente N trechos" + instrução para distribuir ao longo do vídeo.
- `max_output_tokens`: 600 → 2000.
- Filtro de segmentos: `min_words` 5 → 2.
- Cap de candidatos ao GPT: 80 → 150.
- Gap auto-ajustável: se `gap * max_frases > duração total`, reduz automaticamente (mínimo 2s).

**Frases completas (sem cortar no meio):**
- Transcrição AssemblyAI: `punctuate=False` → `punctuate=True` — palavras agora têm pontuação.
- `__build_segments_from_words` corta prioritariamente em pontuação final (`.!?`), fallback em pausas longas.
- `max_words` como rede de segurança (mínimo 30) para transcrições sem pontuação (cache antigo).
- Aviso no terminal quando transcrição sem pontuação é detectada.
- Primeira letra maiúscula em cada palavra (modo word).

---

## ✅ IMPLEMENTADO (2026-04-04) — Editor de Estilos e Canal Alpha (Frases Impactantes)

**Renderização Alpha:**
- Codec primário alterado para **QuickTime Animation (qtrle + argb)** — compatível com Premiere no Windows.
- ProRes 4444 mantido como fallback.
- Blend mode alterado de "Tela" (Screen) para **"Normal"** — alpha real, sem necessidade de mesclagem.

**Editor de Estilos (`app/ui/dialogs/StyleEditorDialog.py`):**
- Configurações: cor da fonte, cor da borda, largura da borda, sombra (X/Y), fundo (box + opacidade), CAPS LOCK, tamanho da fonte (slider 20-200px), posição (Baixo/Centro/Topo).
- Animações: Nenhuma, Fade, Pop — com controles de % de entrada (5-40%) e saída (5-40%).
- Preview em tempo real via PIL com animação em loop ao selecionar Fade/Pop.
- Botão "Preview 1080p" — gera frame 1920x1080 em janela separada.
- Gerenciamento: Novo, Excluir, Salvar estilos em `assets/text_styles.json`.
- 3 estilos iniciais: Padrão, Impacto Amarelo, Vermelho Bold.

**Integração MainScreen:**
- Seletor de estilo + botão "Editar Estilos" + botão "Preview 1080p" no frame de Frases Impactantes.
- Preview inline (canvas 650x70) atualiza em tempo real ao trocar estilo ou fonte.
- Tamanho da fonte e posição movidos para dentro do estilo (removidos do frame principal).

**Pipeline:** Estilo propagado MainScreen → editing.py → build_text_overlays → render_overlays → render_text_clip_alpha.

---

## ✅ IMPLEMENTADO (2026-04-04) — Otimização: Frases em Paralelo com Cenas

**Pré-cálculo de offsets:** Durações das narrações obtidas via ffprobe antes do loop. Offsets calculados acumulando durações.

**Paralelização:** `build_text_overlays` (seleção OpenAI + renderização FFmpeg) submetido para thread **antes do loop de cenas** (era após). Fluxo:
```
[prefetch] → [overlay/logo thread] + [frases thread] → [loop cenas] → [coleta tudo] → [inserção]
```

**Animação por %:** Fade/Pop usam percentual da duração do clip (configurável 5-40%), não tempo fixo. Clips curtos têm fade proporcional.

---

## ✅ IMPLEMENTADO (2026-04-04) — Cache de Frases Impactantes

**Cache:** Seleção do GPT salva em `projeto/<roteiro>/impact_text/_impact_cache.json`. Checkbox "Usar cache" no GUI.
- Habilitado: reutiliza frases salvas, pula chamada OpenAI, re-renderiza apenas o FFmpeg.
- Desabilitado: recria seleção via OpenAI e substitui o cache.

---

## ✅ BUG CORRIGIDO (2026-04-04) — Configurações voltava para tela errada

**Sintoma:** Ao abrir Configurações pela tela inicial e fechar, o app ia para a MainScreen (que requer Premiere).

**Correção:** `on_open_settings` detecta de qual tela foi aberto (`initial` ou `main`) e `on_close_settings` retorna para a tela correta.

---

## ✅ IMPLEMENTADO (2026-04-04) — Tooltips de ajuda no GUI

Adicionados ícones **(?)** com tooltip (hover) nos controles:
- **MainScreen:** Duplicar cenas, Preencher gaps, max, Usar cache, Modo, Max frases, Intervalo, Chroma Key.
- **RenamerFeedbackScreen:** Palavras no nome, Frases por item, Repetições.

---

## ✅ IMPLEMENTADO (2026-04-04) — Renomeador de Cenas: funcionalidades do script antigo

**Novos controles no GUI:**
- **Palavras no nome** (spinbox 2-20): limita quantas palavras da frase viram nome de arquivo.
- **Frases por item** (spinbox 1-5): agrupa N frases do roteiro em 1 item para casamento.
- **Repetições** (spinbox 1-10): quantas vezes a mesma cena pode ser reutilizada (+ checkbox "Permitir repetir cena").

**Novos botões:**
- **Reprocessar Pendências:** reprocessa apenas frases sem cena, com score mais flexível (0.70) e max_uses aumentado.
- **Desfazer Última:** remove os arquivos copiados na última execução de "Aplicar Cópia".

**Parâmetros propagados:** `build_script_items` recebe `sentences_per_chunk`, `compute_assignments` recebe `max_uses_per_scene` e `initial_scene_use_counts`. Todos persistidos no `ui_cache`.

---

## ✅ IMPLEMENTADO (2026-04-04) — UX: auto-seleção e carregamento progressivo

**Auto-seleção de frase:** Ao atribuir uma cena a uma frase no modo revisão, a próxima frase sem cena é selecionada automaticamente.

**Carregamento progressivo da revisão:**
- Ao clicar "Ir para Revisão", aparece tela "Carregando revisão..." instantaneamente.
- Cards são criados com placeholder "carregando..." e thumbnails carregadas em lotes de 6 com `after(10)`.
- A UI não trava durante o carregamento.

---

## ✅ IMPLEMENTADO (2026-04-04) — Volume padrão de Cenas (A1) alterado para -99 dB

O valor padrão do campo "Cenas (A1)" no Mixer de Áudio foi alterado de 0.0 dB para -99 dB (efetivamente silenciado).

---

## ✅ IMPLEMENTADO (2026-04-04) — Renomeador: nomes sem prefixo numérico

O renomeador agora gera arquivos apenas com a frase (ex: `carro vermelho na estrada.mp4` em vez de `001 carro vermelho na estrada.mp4`), compatível com o editor automático que identifica cenas pelo nome.

---

## ✅ IMPLEMENTADO (2026-04-04) — Renomeador: carregamento progressivo da revisão

**Tela de carregamento:** Ao clicar "Ir para Revisão", mostra "Carregando revisão..." e a tela de revisão só aparece quando **todas** as thumbnails terminaram de carregar.

**Cards progressivos:** Cards criados em lotes de 10 (5ms entre lotes), thumbnails carregadas em lotes de 6 (10ms entre lotes). Grid calculado uma vez no final. `_on_canvas_configure` bloqueado durante construção.

**Thumbnails 16:9:** Tamanho 240x135 pixels em containers fixos (`pack_propagate(False)`), 3 colunas por padrão.

---

## ✅ IMPLEMENTADO (2026-04-04) — Logo proporcional à resolução da sequência

**Problema:** Em resoluções menores (ex: 1536x768), o logo ficava fora de quadro porque era escalado pelo tamanho original do logo, não pela resolução da sequência.

**Correção:** `__render_logo_positioned_mp4` agora usa `scale=-1:{FRAME_H*12%}` — a altura do logo é 12% da altura da sequência. Cache inclui resolução no nome (`{logo}_10m_{pos}_{WxH}.mp4`).

---

## ✅ BUG CORRIGIDO (2026-04-04) — Importação duplicada de arquivos no Premiere

**Sintoma:** Overlay, logo e CTA eram importados duas vezes no projeto do Premiere.

**Causa raiz:** `MainScreen` fazia `import_files()` para visuais antes de `mount_sequence`, mas o cache (`project_item_cache`) é local ao `editing.py`. O `__prefetch_all_media` não sabia que já foram importados e importava novamente.

**Correção:** Removido o pré-import do `MainScreen`. Overlay/logo/CTA adicionados ao `_prefetch_paths` do `editing.py`, que importa tudo em uma única chamada `importFiles` e popula o cache corretamente.


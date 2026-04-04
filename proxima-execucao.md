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

## Pendências

- **Teste de campo:** verificar na prática se o ganho de performance é perceptível com roteiros de 40+ cenas.
- **`editing.py` — `Part.start` em ms absolutos (LLM) vs fluxo literal:** ainda pendente de verificação — confirmar se `part.start / 1000` está correto para os dois modos (ms do GPT e ms do AssemblyAI). Herdado do plano anterior.

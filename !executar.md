# Plano de Execucao

> **Como usar:** Pendentes ficam no topo. Concluidas ficam abaixo, em formato enxuto, ou seja, ao concluir, transforme a tarefa em formato enxuto e mova ela para a sessao de `## Tarefas Concluidas Abaixo:`

---

## Tarefas Pendentes Abaixo:

## X 1. Reorganizar trilhas de video/audio para separar cenas coerentes de nao-coerentes

**Status:** PENDENTE
**Atribuida a:** Claude 1

### Proposito da mudanca

Hoje todas as cenas (coerentes + filler/duplicadas) ficam em V1/A1 misturadas, e o usuario nao tem como saber visualmente quais cenas foram casadas pela OpenAI com a fala (coerentes) e quais foram inseridas pelo algoritmo de "Preencher gaps" ou pela duplicacao forcada (nao-coerentes). Em narracoes longas, gaps grandes entre `parts` retornadas pela OpenAI geram blocos enormes de cenas filler — e isso so e perceptivel exportando e olhando o resultado.

A solucao e separar as cenas em duas trilhas: V1/A1 = coerentes, V2/A2 = nao-coerentes. Isso permite ao usuario bater o olho na timeline e identificar a proporcao e localizacao de cada categoria. Como o Premiere liga audio ao mesmo indice do video, e necessario tambem deslocar narracao/CTA/overlay/logo/frases/musica em uma posicao para liberar A2.

Tentativas anteriores de usar Label de cor ou markers para distinguir as categorias falharam: a API do Premiere Pro 2025 nao expoe `setColorLabel` no TrackItem (DOM nem QE), e markers (`createMarker(t)`) ignoram o argumento de tempo e ficam todos no playhead. A unica abordagem confiavel e separar em trilhas distintas.

### Layout final desejado

| Faixa | idx | Video | Audio |
|---|---|---|---|
| 1 | 0 | Cenas COERENTES | Audio cenas coerentes |
| 2 | 1 | Cenas NAO-COERENTES (filler / duplicacoes) | Audio cenas nao-coerentes |
| 3 | 2 | (vazio por design) | Narracao |
| 4 | 3 | CTA "Inscreva-se" | Audio do CTA |
| 5 | 4 | Overlay | (reservado para overlay com som) |
| 6 | 5 | Logo | (reservado para logo animada com som) |
| 7 | 6 | Frases impactantes | (vazio — texto nao tem audio) |
| 8 | 7 | — | Musica |

Sequencias novas terao 7 trilhas de video + 8 de audio. Sequencias antigas nao sao modificadas; se reabertas, recebem trilhas adicionais via `addTracks`.

---

### 1) `app/managers/PremiereManager.py` — bloco de constantes (linhas 63-80)

- Manter `SCENE_TRACK_INDEX = 0` (V1 + A1, coerentes).
- **Adicionar** `FILLER_SCENE_TRACK_INDEX = 1` (V2 + A2, nao-coerentes).
- `NARRATION_TRACK_INDEX`: 1 → **2** (A3).
- `CTA_AUDIO_TRACK_INDEX`: 2 → **3** (A4, mesmo idx que CTA video).
- `MUSIC_TRACK_INDEX`: 4 → **7** (A8).
- `CTA_TRACK_INDEX`: 2 → **3** (V4).
- `OVERLAY_TRACK_INDEX`: 3 → **4** (V5).
- `LOGO_TRACK_INDEX`: 4 → **5** (V6).
- `IMPACT_TEXT_TRACK_INDEX`: 5 → **6** (V7).
- Reescrever os comentarios das linhas 64-66 e 75-76 para refletir o novo mapeamento.

### 2) `app/managers/PremiereManager.py` — assinatura publica `mount_sequence` (linhas 450-484)

- Default `duplicate_scenes_until_next: bool = True` → `False`.
- Default `fill_gaps_with_random_scenes: bool = False` → `True`.
- Default `max_fill_scene_duration: float = 0.0` → `12.0`.
- Esses defaults raramente sao usados na pratica (MainScreen sempre passa o valor), mas precisam ficar consistentes com a nova politica.

### 3) `app/managers/premiere/editing.py` — assinatura interna `mount_sequence` (linhas 128-162)

- Mesmos defaults da chamada publica: `duplicate_scenes_until_next=False`, `fill_gaps_with_random_scenes=True`, `max_fill_scene_duration=12.0`.

### 4) `app/managers/premiere/editing.py` — criacao de trilhas no inicio do mount (linhas 215-231)

- Bloco que calcula `_max_video_idx` e `_max_audio_idx` precisa incluir `FILLER_SCENE_TRACK_INDEX` no calculo de video.
- Apos a mudanca, `_max_video_idx` deve ser **6** (V7 = frases) e `_max_audio_idx` deve ser **7** (A8 = musica).
- Confirmar que `__ensure_video_track_index(6)` e `__ensure_audio_track_index(7)` conseguem criar todas as trilhas. As funcoes iteram ate 8 vezes adicionando trilhas via QE, entao deve dar conta (sequencia padrao do Premiere comeca com 3+3 trilhas).

### 5) `app/managers/premiere/editing.py` — funcao interna `_insert_scene_clip` (linhas 420-475)

- Atualmente o `track_index` esta hardcoded como `mgr.SCENE_TRACK_INDEX`. Precisa receber `track_index` como parametro:
  - Adicionar parametro `track_index: int` na assinatura de `_insert_scene_clip`.
  - Trocar todas as ocorrencias internas de `mgr.SCENE_TRACK_INDEX` por `track_index`. Inclui:
    - Linha 426 (`__insert_clip_with_retry`)
    - Linha 448 (`__qe_razor_with_retry` video, dentro do corte por `max_dur`)
    - Linha 450 (`__qe_razor_with_retry` audio)
    - Linha 451-452 (`videoTracks[mgr.SCENE_TRACK_INDEX]`)
    - Linha 457-458 (`audioTracks[mgr.SCENE_TRACK_INDEX]`)
- Cada chamada externa de `_insert_scene_clip` (linhas 484, 494, 532) precisa passar o track_index correto.

### 6) `app/managers/premiere/editing.py` — caso `duplicate_scenes_until_next` (linhas 480-489)

- Comportamento desejado: a PRIMEIRA insercao da cena casada com a `part` vai em V1 (coerente). As repeticoes do `while current_scene_end < next_scene_start` vao em V2 (nao-coerente).
- Implementar via flag local: chamar `_insert_scene_clip(..., track_index=SCENE_TRACK_INDEX)` na primeira iteracao e `_insert_scene_clip(..., track_index=FILLER_SCENE_TRACK_INDEX)` nas seguintes.

### 7) `app/managers/premiere/editing.py` — caso `else: _insert_scene_clip + fill_gaps` (linhas 491-594)

- Linhas 491-495: a chamada solta de `_insert_scene_clip` (cena coerente unica) usa `SCENE_TRACK_INDEX` (V1).
- Linhas 498-594: o loop interno de fill_gaps insere cenas aleatorias com `FILLER_SCENE_TRACK_INDEX` (V2). Precisa atualizar:
  - Linha 532 (`_insert_scene_clip`): passar `track_index=FILLER_SCENE_TRACK_INDEX`.
  - Linhas 568-579 (logica de "nao deixar restinho < 3s"): `__qe_razor_with_retry` e remocoes precisam apontar para `FILLER_SCENE_TRACK_INDEX`. Mesmo na linha 568, 575-577.

### 8) `app/managers/premiere/editing.py` — corte de cena excedente (linhas 596-649)

- Esse bloco hoje corta sempre em `SCENE_TRACK_INDEX`. Mas a ULTIMA cena inserida pode ter ido para V2 (caso de duplicacao ou fill_gaps).
- Solucao: introduzir uma flag local `last_inserted_track_idx` setada por `_insert_scene_clip` (`nonlocal`), e usar essa flag nas linhas:
  - Linha 615 (`__qe_razor_with_retry` video)
  - Linha 617-618 (`videoTracks[...]`)
  - Linha 624-625 (recuperar clip pos-corte)
  - Linha 634-635 (`audioTracks[...]`)
  - Linha 641 (`__qe_razor_with_retry` audio)
  - Linha 642-643 (recuperar audio pos-corte)
- Sem isso, o corte feito em V1 nao removeria o excesso da cena que esta em V2 — bug visual com clipes ultrapassando `next_scene_start`.

### 9) `app/managers/premiere/editing.py` — bloco MIXER (linhas 894-901)

Atualizar para refletir o novo mapeamento. Volume "Cenas" (vol_scene_db) **aplica em A1 E A2**:

```
__set_audio_track_volume_db(SCENE_TRACK_INDEX, vol_scene_db)        # A1 (idx 0)
__set_audio_track_volume_db(FILLER_SCENE_TRACK_INDEX, vol_scene_db) # A2 (idx 1)  ← NOVO
__set_audio_track_volume_db(NARRATION_TRACK_INDEX, vol_narration_db) # A3 (idx 2)
__set_audio_track_volume_db(CTA_AUDIO_TRACK_INDEX, vol_cta_db)      # A4 (idx 3)
__set_audio_track_volume_db(MUSIC_TRACK_INDEX, vol_music_db)        # A8 (idx 7)
```

Atualizar tambem o print (`[mixer] Aplicando volumes:` linha 895) para deixar explicito que cenas afeta A1 e A2.

### 10) `app/managers/premiere/editing.py` — `mount_mass_project` (linhas 1010-1457)

Modo em massa nao tem casamento OpenAI (nao distingue coerente de filler) e nao e exposto pelo GUI hoje. **Decisao: manter inalterado** — todas as cenas continuam em V1/A1.
- Mas precisa verificar que `mount_mass_project` ainda funciona apos a mudanca dos demais indices (CTA, overlay, logo). As linhas 1404 (`__clear_video_track_range(OVERLAY_TRACK_INDEX, ...)`) e 1416 (`LOGO_TRACK_INDEX`) ja usam constantes — vao apontar para os novos indices automaticamente. Bom.
- O modo massa nao tem mixer, entao mudancas no bloco MIXER nao afetam.
- Comentario: como `NARRATION_TRACK_INDEX` mudou de 1 para 2, as linhas 1069 e 1076 (insercao de narracao) passam a usar A3 automaticamente. OK.

### 11) `app/managers/PremiereManager.py` — funcoes hardcoded em SCENE_TRACK_INDEX (linhas 2486, 2574, 2581, 2590)

- Linha 2486 (`apply_fade_to_scene_track_clips`): funcao publica que aplica fade em V1. **Atualmente nao e chamada em lugar nenhum** (verificado via grep), mas se for ativada no futuro, vai aplicar fade somente em cenas coerentes (V1) e nao nas nao-coerentes (V2). Anotar como TODO se essa funcao voltar a ser usada.
- Linhas 2574, 2581, 2590 (`__insert_title_card`): cartela do modo em massa. Cartela faz parte da cena, vai em V1 mesmo. Manter inalterado.

### 12) `app/managers/TextOnScreenManager.py:1186-1191` — comentario desatualizado

- Bloco de docstring na funcao `insert_overlays_into_premiere` descreve "V0 = cenas, V1 = logo, V2 = overlay, V3 = textos". E mapeamento antigo (incorreto ja hoje, irrelevante para a logica que usa `track_index` parametrizado).
- Atualizar comentario para refletir o novo layout: V7 (idx=6) = frases impactantes / textos.

### 13) `app/ui/screens/MainScreen.py` — labels do mixer (linhas 521-539)

Atualizar os textos dos `tk.Label` do mixer:

- `'Cenas (A1):'` → `'Cenas (A1+A2):'`
- `'Narracao (A2):'` → `'Narracao (A3):'`
- `'Inscreva-se (A3):'` → `'Inscreva-se (A4):'`
- `'Musica (A5):'` → `'Musica (A8):'`

### 14) Defaults do programa (5 lugares)

**`app/managers/SettingsManager.py:39-41`** (defaults usados quando settings.json nao existe):
- `'duplicate_scenes': True` → `False`
- `'fill_gaps_without_scene': False` → `True`
- Adicionar nova chave `'max_fill_scene_duration': 12.0`

**`app/ui/screens/MainScreen.py:1245`** (carregamento do checkbox no GUI):
- `ui_cache.get("duplicate_scenes", True)` → `ui_cache.get("duplicate_scenes", False)`

**`app/ui/screens/MainScreen.py:1250-1251`**:
- `ui_cache.get("fill_gaps_without_scene", False)` → `ui_cache.get("fill_gaps_without_scene", True)`

**`app/ui/screens/MainScreen.py:1256`**:
- `int(ui_cache.get("max_fill_scene_duration", 7))` → `int(ui_cache.get("max_fill_scene_duration", 12))`

**`app/ui/screens/MainScreen.py:1829-1834`** (fallbacks de `getattr` quando o usuario nao confirmou):
- `selected_duplicate_scenes` fallback: `True` → `False`
- `selected_fill_gaps_without_scene` fallback: `False` → `True`
- `selected_max_fill_scene_duration` fallback: `0.0` → `12.0`

**`app/ui/screens/MainScreen.py:1504-1510`** (fallback quando o campo de duracao maxima esta invalido):
- Docstring: "Padrao: 7." -> "Padrao: 12."
- `return 7.0` -> `return 12.0`
- Motivo: se o usuario apagar/digitar valor invalido no campo, o programa ainda cairia no default antigo de 7s, divergindo da nova politica.

### 15) Verificacoes pos-implementacao

- **Sobreposicao V1 x V2**: garantir que cena coerente (V1) e cena filler (V2) NUNCA ocupem o mesmo intervalo de tempo simultaneamente. A ordem cronologica do algoritmo (`current_scene_end` avanca a cada insercao, qualquer track) ja deve garantir isso, mas validar visualmente no Premiere apos export-teste.
- **Render preview**: o Premiere renderiza a track de video mais alta primeiro. Se V2 estiver vazia em uma regiao, V1 aparece. Se V1 estiver vazia em uma regiao, V2 aparece. Sem sobreposicao, render esta correto.
- **Cenas filler com volume**: `vol_scene_db` aplicado em A1 e A2 deve deixar ambas mudas no padrao (-99 dB). Validar.
- **Cortes razor em V2**: confirmar que cortes excedentes em cenas filler funcionam (nao sobra "rabo" da ultima cena ultrapassando `next_scene_start`).
- **Zoom e fade em clipes de V2**: validar que clipes em V2 recebem o efeito de zoom (animacao de escala) e fade-in/fade-out conforme esperado. Os efeitos sao aplicados via referencia direta ao clip (nao por track), entao tecnicamente devem funcionar — mas confirmar visualmente no Premiere.
- **Sequencia antiga reaberta**: abrir uma sequencia criada antes da mudanca e rodar export. O `__ensure_*_track_index` deve criar as trilhas faltantes sem perder conteudo existente.
- **`settings.json` antigo**: usuario com `settings.json` antigo (sem `max_fill_scene_duration`) deve cair no novo default 12.0 sem erro.

### 16) Atualizacao de documentacao apos implementar

- Atualizar `!projeto.md` secao 11.4 ("Faixas no Premiere") com o novo mapeamento.
- Atualizar `!projeto.md` secao 9 (`ui_cache`) adicionando `max_fill_scene_duration` se ainda nao estiver documentado.
- Atualizar `!projeto.md` secao 12 (Armadilhas) sobre as 2 trilhas de cena e o efeito do mixer "Cenas" controlando A1+A2.

### 17) Pontos pequenos descobertos na reanalise (precisam tratamento)

**`app/managers/TextOnScreenManager.py:1176`** — assinatura de `insert_overlays_into_premiere`:
- Default do parametro: `track_index: int = 3` → atualizar para `int = 6` (ou remover o default e exigir o argumento). O valor `3` referenciava o layout antigo de tracks. A chamada em `editing.py:879` ja passa `mgr.IMPACT_TEXT_TRACK_INDEX` explicitamente, entao o default nunca e usado na pratica — mas a inconsistencia confunde quem ler o codigo.

**`app/managers/PremiereManager.py:407-418`** — fallback QE em `__set_speed_with_retry`:
- A linha 409 usa `qe_seq.getVideoTrackAt(self.SCENE_TRACK_INDEX)` no caminho de fallback. Hoje so e chamado a partir de `mount_mass_project` (linha `editing.py:1239`), que mantem cenas em V1, entao funciona.
- Se no futuro essa funcao for usada para clipes de V2 (cenas filler), o fallback vai pegar o ultimo clip de V1 (errado). Anotar como TODO de robustez se a funcao for ampliada.

**`app/managers/PremiereManager.py:1025`** — `getattr(clip, '_track_index', 0)`:
- O atributo `_track_index` nao e setado em nenhum lugar do codigo (verificado via grep). O fallback `0` faz com que sempre caia em V1. Codigo possivelmente legado/incompleto. Nao bloqueia a tarefa atual, mas marcar como TODO de limpeza futura.

**Bloco de zoom/fade em `editing.py:651-709`**:
- Opera em `inserted_scene_clips` (lista misturada V1 + V2). Funciona automaticamente porque os efeitos sao aplicados em referencias diretas aos clips. Nao precisa de mudanca.
- Apenas validar empiricamente apos implementacao que zoom/fade sao aplicados corretamente em clips de V2 (deve ser, mas confirmar no Plano de validacao).

**Arquivo `debug_label_color.py`**:
- Removido em 2026-05-03 a pedido do usuario (cumpriu seu proposito durante o planejamento).

**`app/managers/premiere/editing.py` — comentario na funcao `_insert_cta`**:
- Linha aproximadamente em 1063: comentario "Insere video na V3 (o Premiere automaticamente linka o audio na trilha correspondente)" ficara desatualizado apos a mudanca, ja que o CTA passara para V4. Atualizar o comentario para "Insere video na V4 ...".
- O `print` da linha seguinte (`f"[cta] animacao inserida em V{mgr.CTA_TRACK_INDEX + 1}"`) ja e dinamico e mostrara o numero correto automaticamente — nao precisa mudar.

**`app/managers/PremiereManager.py` - comentarios/docstrings de trilhas que ficarao desatualizados**:
- Linha aproximadamente em 1356: docstring de `__insert_logo_full` diz "Logo em V5"; apos a mudanca deve virar "Logo em V6".
- Linha aproximadamente em 2264: comentario "garante trilha de video V2 (para o texto)" e legado/inexato; atualizar para comentario generico ou remover, porque a funcao `__ensure_video_track_index` e reutilizada para qualquer trilha.
- Linha aproximadamente em 2480: docstring de `apply_fade_to_scene_track_clips` diz "trilha de cenas (V0)"; atualizar para "V1/A1 de cenas coerentes" e anotar que nao cobre V2/filler.

**Logs `[gap-debug]` recem-adicionados em `editing.py`**:
- O usuario adicionou prints de diagnostico com prefixo `[gap-debug]` para mapear gaps grandes na transcricao (em ~5 pontos do `mount_sequence`). Esses logs nao alteram a logica de insercao nem indices de track, apenas observabilidade.
- **Impacto na Tarefa 1: nenhum**, mas as referencias numericas de linha listadas nos itens 5, 7, 8 deste plano podem estar levemente off (cerca de 20-50 linhas) — usar nome da funcao/bloco e contexto para localizar, nao confiar no numero literal.

**Resultado da reanalise Codex em 2026-05-03 (sem ler `!projeto.md`)**:
- A proposta central do `!executar.md` esta coerente com o codigo atual: hoje as cenas coerentes, fillers e duplicacoes entram todas por `SCENE_TRACK_INDEX` (V1/A1), e separar V1/A1 de V2/A2 e a abordagem correta para visualizacao confiavel na timeline.
- Nao ha uso atual confiavel de label/marker nos arquivos analisados; portanto o plano deve continuar evitando `setColorLabel` e `createMarker`.
- O unico ajuste obrigatorio encontrado foi o fallback de `get_max_fill_scene_duration()` ainda em 7s; adicionado acima.
- Comentarios/docstrings extras de trilha tambem precisam ser atualizados para evitar confusao futura, mas nao bloqueiam a logica principal.

### Plano de validacao final

1. Rodar export curto (1 narracao, 5 cenas) com defaults: `Duplicar cenas: OFF`, `Preencher gaps: ON`, `max: 12s`.
2. Verificar na timeline:
   - V1 tem so cenas casadas com a fala.
   - V2 tem so cenas filler aleatorias.
   - V3 esta vazio.
   - V4 tem o CTA.
   - V5 tem o overlay.
   - V6 tem o logo (se houver).
   - V7 tem frases impactantes.
   - A3 tem narracao.
   - A8 tem musica.
3. Slider "Cenas" do mixer: mover para -50dB e confirmar que A1 E A2 ficaram em -50dB.
4. Conferir que sequencia nova tem 7 video tracks + 8 audio tracks.
5. Re-rodar export com `Duplicar cenas: ON` e verificar:
   - Primeira ocorrencia da cena coerente em V1.
   - Repeticoes da mesma cena em V2.

---

## Tarefas Concluidas Abaixo:

_(nenhuma — historico anterior removido a pedido do usuario em 2026-05-03)_

---

*Ultima atualizacao: 2026-05-03 (revisao completa apos reanalise do codigo)*

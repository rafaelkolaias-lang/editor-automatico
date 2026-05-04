# Plano de Execucao

> **Como usar:** Pendentes ficam no topo. Concluidas ficam abaixo, em formato enxuto, ou seja, ao concluir, transforme a tarefa em formato enxuto e mova ela para a sessao de `## Tarefas Concluidas Abaixo:`

---

## Tarefas Pendentes Abaixo:

_(nenhuma tarefa pendente no momento)_

---

## Tarefas Concluidas Abaixo:

## ✅ 3. Bug 2 — cenas em V1 fora de sincronia com `fill_gaps=ON` (Opcao A: sempre cortar)

**Status:** CONCLUIDO (Claude 1, 2026-05-04)

**Sintoma:** Quando `fill_gaps=ON`, cenas COERENTES em V1 saiam fora
da posicao alvo apos a 1a cena. Apenas a 1a ficava correta, as demais
podiam aparecer com pequeno deslocamento ou nao aparecer.

**Causa raiz cravada via logs `[scene-debug]`:** A otimizacao
`skip_overwrite_V1` no bloco "Corte de cena excedente" deixava o
overwriteClip da proxima cena cortar V1 automaticamente. Funcionava
na maioria dos casos, mas falhava quando a duracao da proxima cena
fonte era menor ou igual ao overflow da cena anterior. O Premiere
quebrava a cena anterior em pedacos estranhos e o `t.clips[n-1]`
em `__insert_clip_with_retry` retornava um pedaco residual da cena
anterior em vez da cena recem-inserida. O `current_scene_clip`
ficava apontando para esse pedaco errado, deslocando todas as cenas
seguintes.

Confirmado via flag `⚠ DELTA` que so aparecia exatamente quando
overflow >= duracao_proxima_cena. No caso reportado: part#003
(71.34->83.87, overflow=3.63s) seguido de part#004 (cena fonte com
3.63s de duracao) -> part#004 saiu com DELTA=+1.926s.

**Correcao aplicada (Opcao A):** Removida a otimizacao - SEMPRE corta
V1 quando ha overflow, identico ao que ja era feito em V2. Custo: ~1
razor extra por part com overflow (desprezivel). Beneficio: elimina
o bug 100%, codigo simplifica.

**Ajustes finos:**
- Removida a guarda `if (not _modo_rapido) or is_last_part or _force_cut_filler:` no corte de video.
- Variavel `_modo_rapido` removida (nao era mais usada).
- Comentario do bloco atualizado explicando o porque do corte
  incondicional.
- Log `[scene-debug] OVERFLOW` simplificado: agora mostra
  `corte=ultima_part|filler_V2|coerente_V1` em vez do antigo motivo
  com 4 valores.

---

## ✅ 2. Bug 1 — gaps vazios em narracao longa + thread-safety dos logs

## ✅ 2. Bug 1 — gaps vazios em narracao longa + thread-safety dos logs

**Status:** CONCLUIDO (Claude 1, 2026-05-03)

**O que mudou (formato enxuto):**

- **`editing.py` (Bug 1 — safety dinamico):** o limite `safety < 50`
  era restritivo demais para narracoes longas (gaps de 1500+s).
  Substituido por `SAFETY_MAX = max(50, int(gap / 3.0) + 20)` calculado
  por part. Adapta automaticamente ao tamanho do gap; mantem 50 como
  minimo para gaps pequenos. Trava real anti-loop (`current_scene_end
  <= prev_end + 1e-6`) continua intacta. Mensagens `[gap-debug]`
  atualizadas para mostrar `safety/SAFETY_MAX`.
- **`WorkingScreen.py` (logs vazando para CMD):** `_TerminalWriter`
  era thread-safe so na escrita do `_original` (CMD). O `_write_fn`
  fazia `text.insert(...)` direto, e Tkinter NAO eh thread-safe -
  prints de threads worker (Premiere/FFmpeg/Impact) falhavam
  silenciosamente no widget e so apareciam no CMD. Agora `_write`
  detecta a thread (`threading.get_ident()`) e:
  - Se for main thread, escreve direto.
  - Se for worker, agenda `app.after(0, lambda: _write_to_widget(t))`
    para a main thread executar.
  - Buffer continua acumulando em qualquer thread (concatenacao str
    eh thread-safe sob GIL).
  - Removido `update_idletasks()` (causava risco de reentrancy).
  - Adicionada flag `_closed` para evitar agendamentos durante
    shutdown.
- **`editing.py` (logs novos `[scene-debug]`):** preparados para
  diagnosticar Bug 2 — cada part loga alvo_start + arquivo casado;
  cada insercao loga req/real start (com flag DELTA quando difere);
  overflow loga trilha e motivo do corte.

---

## ✅ 1. Reorganizar trilhas de video/audio para separar cenas coerentes de nao-coerentes

**Status:** CONCLUIDO (Claude 1, 2026-05-03)

**O que mudou (formato enxuto):**

- **Layout final** — V1=cenas coerentes, V2=cenas filler/duplicadas, V3=vazio, V4=CTA, V5=overlay, V6=logo, V7=frases. Audio: A1+A2=cenas, A3=narracao, A4=CTA, A8=musica.
- **Constantes** em `PremiereManager.py`: nova `FILLER_SCENE_TRACK_INDEX=1`; `NARRATION_TRACK_INDEX 1->2`; `CTA_AUDIO_TRACK_INDEX 2->3`; `MUSIC_TRACK_INDEX 4->7`; `CTA_TRACK_INDEX 2->3`; `OVERLAY 3->4`; `LOGO 4->5`; `IMPACT_TEXT 5->6`.
- **Defaults novos** (`mount_sequence` publica + interna + 5 lugares no GUI/SettingsManager): `duplicate_scenes_until_next=False`, `fill_gaps_with_random_scenes=True`, `max_fill_scene_duration=12.0`.
- **`_insert_scene_clip`** agora recebe `track_index` parametrizado (era hardcoded em SCENE_TRACK_INDEX). Tambem atualiza flag `last_inserted_track_idx` (`nonlocal`) para o bloco de corte excedente saber em qual trilha cortar.
- **Roteamento V1/V2**:
  - `duplicate_scenes_until_next=True`: 1a iteracao em V1, repeticoes em V2.
  - Caso `else + fill_gaps`: cena coerente unica em V1; cenas aleatorias do fill em V2.
  - REGRA 2 (razor anti-restinho < 3s) agora atua em V2.
- **Corte de cena excedente** usa `last_inserted_track_idx` em todas as 7 referencias (razor video, lookup videoTracks, recuperar pos-corte, audio analogo).
- **AJUSTE DEFENSIVO (Risco 1 reportado)**: corte de video excedente passa a ser forcado quando `last_inserted_track_idx == FILLER_SCENE_TRACK_INDEX`, mesmo no modo rapido. Motivo: `overwriteClip` da proxima cena coerente (V1) nao cobre o excedente em V2; sem este ajuste, a cena filler ultrapassaria visualmente a proxima cena coerente.
- **MIXER**: volume `vol_scene_db` aplicado em A1 E A2 (cenas coerentes + filler ficam mudas/iguais). Print do mixer atualizado para "(A1+A2)".
- **AJUSTE DEFENSIVO (Risco 2 reportado)**: `mount_mass_project` ganhou bloco que chama `__ensure_video_track_index(_max)`/`__ensure_audio_track_index(_max)` no inicio de cada roteiro, identico ao que ja existia em `mount_sequence`. Sem isso, MUSIC_TRACK_INDEX=7 (A8) podia falhar com IndexError em sequencias recem-criadas que comecam com so 3 trilhas.
- **MainScreen mixer labels**: `'Cenas (A1)' -> 'Cenas (A1+A2)'`, `'Narracao (A2)' -> 'Narracao (A3)'`, `'Inscreva-se (A3)' -> 'Inscreva-se (A4)'`, `'Musica (A5)' -> 'Musica (A8)'`.
- **Comentarios/docstrings desatualizados** atualizados em: `_insert_cta` ("V3 -> V4"), `__insert_logo_full` ("V5 -> V6"), `__insert_title_card` ("V0 -> V1"), `apply_fade_to_scene_track_clips` (anota que so cobre V1, nao V2), comentario "garante trilha V2" generalizado, `TextOnScreenManager.insert_overlays_into_premiere` default `track_index 3 -> 6` + docstring atualizada.
- **TODOs** adicionados em `__set_speed_with_retry` (fallback QE so cobre V1) e em `getattr(clip, '_track_index', 0)` (atributo legado nao setado em lugar nenhum).
- **Modo massa** (`mount_mass_project`): mantido inalterado em termos de logica - todas as cenas continuam em V1/A1 (modo massa nao tem casamento OpenAI). Outros indices se atualizam automaticamente via constantes.

**Plano de validacao final (a executar pelo usuario no Premiere):**
1. Export curto com defaults novos (`Duplicar OFF`, `Preencher gaps ON`, `max=12s`): conferir V1 com cenas coerentes, V2 com filler aleatorias, V3 vazio, V4 CTA, V5 overlay, V6 logo, V7 frases, A3 narracao, A8 musica.
2. Slider "Cenas (A1+A2)" -> -50dB e confirmar que A1 e A2 ficam ambas em -50dB.
3. Sequencia nova: confirmar 7 video tracks + 8 audio tracks.
4. Re-rodar com `Duplicar ON`: 1a ocorrencia da cena casada em V1, repeticoes em V2.
5. Confirmar que cortes em cenas que ultrapassam `next_scene_start` em V2 sao removidos (Risco 1).
6. Modo massa: rodar export e confirmar que insercao em A8 (musica) funciona sem IndexError em sequencia nova (Risco 2).

**Pendencia de documentacao (item 16 do plano original):**

A regra global pede atualizar `!projeto.md` (secoes 11.4 "Faixas no Premiere", 9 "ui_cache" e 12 "Armadilhas") apos implementar mudancas estruturais. Esta pendencia foi explicitamente NAO executada nesta sessao a pedido do usuario, que instruiu "nao ler projeto.md" durante a execucao. Sugestao: marcar como tarefa para o Codex/Antigravity atualizar o `!projeto.md` com o novo mapeamento de trilhas e os novos defaults na proxima passada de planejamento.

---

*Ultima atualizacao: 2026-05-04 (tarefa 3 concluida por Claude 1 - Opcao A aplicada)*

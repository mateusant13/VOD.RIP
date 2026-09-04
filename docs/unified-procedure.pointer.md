# Ponteiro — Playbook Unificado de Procedimentos (projeto: VOD.RIP)

Fonte canônica: `I:/!manager/state/procedure/playbook-unificado.md`
Leitura residente (manager): `I:/!manager/playbooks/unified-procedure.md`
Regra: em divergência prevalece a FONTE (`precedencia`); este arquivo nunca copia protocolos do core (`leitura-unica`, `unicidade`).
Drift: mudou comportamento operacional → fonte + este ponteiro NA MESMA SESSÃO (`quatro-camadas`); detalhe em "Persistência e drift" na fonte.
Oracle: todo card novo neste repo carrega `oracle:` conforme a Matriz TDD-oracle (G-2).
Nota: `docs/unified-procedure.md` (instância integral do TC-20260903) fica congelada como referência histórica.
TC: TC-20260904-playbook-pass3-final.

## Hardening 2026-09-04 (hardening de gaps — dono aprovou "faz tudo ao mesmo tempo")

- **`umbigo`** (novo, E1, Passo 1 do dono): ANTES de qualquer ação, inventário completo do que a tarefa precisa (o que já existe sobre o tema — reclamação repetida NUNCA é tarefa nova; estado real do alvo; credenciais/ambientes disponíveis AGORA; quem mais toca o alvo; scope implícito). Só depois: todo-imediato + card.
- **`quick-path`** (novo, E1): mudanças ≤3 arquivos, sem config/segredo/navegação/delegação/artefato durable → trilha curta (umbigo resumido → todo → edit → oracle → commit → report), dispensa card TC/worktree/manifest. Repetição de reclamação anterior = SEMPRE trilha completa.
- **`learning-close-gate`** (novo, E8): incidente/defeito de processo fechado exige responder "o playbook teria evitado isto?" e, se sim, patch no playbook na mesma sessão (ou issue no manager).
- **G-2 mecânico (omp)**: extensão `I:/!manager/.omp/extensions/oracle-gate.ts` lembra done sem evidência (passiva, nextTurn). Não cobre sessões Cursor — aqui vale pela disciplina do card.
- **G-5 repro de fallback**: `I:/!manager/scripts/fallback_repro.py` — força 401 por degrau b.ai e prova que cada degrau vive; rodar ao tocar em chain (exit 0 = OK). Nota VOD.RIP: este projeto não tem fallback chain de LLM; G-5 aplica-se ao ecossistema (manager/SH), aqui só como hábito de oracle.

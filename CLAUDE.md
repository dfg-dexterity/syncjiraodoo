# syncjiraodoo — guia para o Claude Code

Sincronizador de Horas: apontamentos do Jira/Clockwork Pro → planilhas de
horas do Odoo. Python só com a biblioteca padrão; testes com
`python3 -m unittest discover -t . -s tests`. App em produção:
https://horas.dexterityit.com.br (servidor Vultr `45.63.77.123`,
`/opt/syncjiraodoo`, Docker + Caddy).

## Processo de registro de cada melhoria (obrigatório)

Toda melhoria ou correção entregue neste repositório segue este roteiro,
na ordem, sem exceção:

1. **Jira — projeto JI ("IMI | Desenvolvimentos Claude")**: criar uma
   **Tarefa** sob o épico **JI-254** "[Sincronizador de Horas] Integração
   Jira/Clockwork → Odoo". Convenções do projeto:
   - Resumo com prefixo `[Sincronizador de Horas]` + emoji + título curto.
   - Descrição em Markdown com as seções *Objetivo*, *Entregas*,
     *Validação* (quantidade de testes) e *Referências* (link do PR e,
     quando houver, chamado de origem e seção do manual).
   - Labels: `TI` + `melhoria` (ou `bug` para correções). Responsável:
     Diego Gozer (`712020:3a98a142-a5ce-443c-b3f2-32cd080d2583`).
   - Cloud ID / site: `https://dexterityit.atlassian.net`.
2. **Horas**: **sempre perguntar ao Diego** quantas horas apontar em cada
   tarefa (nunca estimar por conta própria) e registrar o worklog na
   tarefa com `addWorklogToJiraIssue`.
3. **Passos manuais do Diego**: sempre que uma entrega depender de uma
   ação que só o Diego pode fazer (atualizar o servidor, criar projetos no
   Odoo, gerar um token, configurar algo pela tela…), criar **uma Tarefa
   por passo** no mesmo épico JI-254, com resumo
   `[Sincronizador de Horas] [Manual step] <o que fazer>`, descrição com
   o passo a passo (comandos prontos para colar) e **data de vencimento
   (`duedate`) igual ao dia da criação**. A única exceção é o "mergear"
   do PR, que não vira tarefa. Nunca deixar um passo manual só na
   conversa.
4. **Notion**: atualizar o manual do usuário **TI-14-005 — Integração
   Jira + Odoo** (página `3abc69371e1780e68214d5e8bac70285`): mapa da
   tela (seção 3) e a seção da funcionalidade afetada.
5. **Teams**: avisar o time no canal **Avisos Gerais** (team
   "Dexterity - Geral", `teamId 16c14e6a-955a-49df-a36e-7b35a0e29098`,
   `channelId 19:a5797f7659e142e5b348c5e2755f272e@thread.tacv2`), no mesmo
   formato usado para o Dexterity Hub / Jira Insights:
   - Título: `📣 ✨ N novidade(s) no Sincronizador de Horas`.
   - Um bullet por melhoria: emoji + **título em negrito** + uma frase de
     explicação para quem usa (não técnica).
   - Link "Abrir o aplicativo" → https://horas.dexterityit.com.br.
   - @menções: Diego Gozer (`98491966-b413-45f4-873e-c4ca9d364ebd`),
     Julian Cidade (`810394eb-feb2-4b97-bb7d-ebe1ab38b765`),
     Marcelo Bernadelli Garcia (`1bf6942b-2c96-4df3-94cc-9df9ba3eb904`),
     Henrique de Lara (`f6de0640-88b3-4407-adb7-46fa2ca0f60c`),
     Jéssica Cavalheiro (`d9773a58-d1b0-47a7-bc2a-dfc484d37ddf`),
     Gabriela Firmiano da Silva (`9d1fc12b-01b0-4e02-9f74-bba5474f2b8f`).

Histórico: o desenvolvimento inicial (PRs #1–#8) foi apontado na TAD-965;
a partir do PR #9 tudo é registrado no épico JI-254.

## Fluxo de código

- Branch de trabalho: `claude/jira-odoo-sync-test-vos5yi`, recriado a
  partir de `origin/main` após cada merge (`git checkout -B … origin/main`).
- Commits como Diego Gozer (`-c user.name="Diego Gozer"
  -c user.email="logindodiego@gmail.com"`), mensagem em português.
- PR em rascunho; o Diego diz "mergear" e o merge é feito por aqui.
- Depois do merge, a produção é atualizada pelo Diego:
  `cd /opt/syncjiraodoo && git pull && docker compose --env-file .env.compose up -d --build`.
- Credenciais nunca entram no repositório (`.env`, `.users.json` e
  `.sync_*` ficam fora do git).

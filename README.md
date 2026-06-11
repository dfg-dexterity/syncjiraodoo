# syncjiraodoo

Sincroniza apontamentos de horas do **Jira/Clockwork Pro** para timesheets do
**Odoo Online**, usando apenas a biblioteca padrão do Python (`xmlrpc.client`
para o Odoo, `urllib` para o Jira). Sem dependências externas.

> O Clockwork Pro grava os apontamentos como **worklogs nativos do Jira**;
> por isso o sync lê a API nativa de worklogs (`/rest/api/3/worklog/*`) e
> cobre tudo que é registrado pelo Clockwork — timers, ajustes manuais e
> lançamentos retroativos.

## Como funciona

Para cada worklog alterado desde a última execução, o sync garante a cadeia
projeto → tarefa → timesheet no Odoo:

| Jira | Odoo | Localizado por |
|---|---|---|
| Projeto (ex.: `CDV`) | `project.project` | marcador `[CDV]` no nome, ou `JIRA_ODOO_PROJECT_MAP` |
| Issue (ex.: `CDV-331`) | `project.task` | marcador `[CDV-331]` no nome |
| Worklog (ex.: id `27279`) | `account.analytic.line` | marcador `[jira-worklog:27279]` na descrição |
| Autor do worklog | `hr.employee` | e-mail (`work_email` ou login do usuário), ou `JIRA_ODOO_EMPLOYEE_MAP` |

Os marcadores tornam a sincronização **idempotente e sem estado no servidor**:
rodar duas vezes não duplica nada; worklogs editados no Jira atualizam o
timesheet correspondente; worklogs apagados podem remover o timesheet
(opt-in via `--delete`). O progresso incremental fica num arquivo local
`.sync_state.json` (fora do versionamento).

## Configuração

Credenciais **somente** por variáveis de ambiente — nunca em arquivos
versionados. Copie `.env.example` para `.env` (ignorado pelo git), preencha e
carregue no shell (`set -a; source .env; set +a`).

| Variável | Obrigatória | Descrição |
|---|---|---|
| `ODOO_URL` | sim | ex.: `https://dexterityit.odoo.com` |
| `ODOO_DB` | sim | ex.: `dexterityit` |
| `ODOO_USER` | sim | login do usuário Odoo |
| `ODOO_API_KEY` | sim | chave de API do Odoo (Preferências → Segurança da Conta) |
| `JIRA_URL` | sim | ex.: `https://dexterityit.atlassian.net` |
| `JIRA_USER` | sim | e-mail da conta Atlassian |
| `JIRA_API_TOKEN` | sim | token de API do Atlassian |
| `JIRA_PROJECT_KEYS` | não | filtro de projetos, ex.: `RDF,TAD,FIAG` (vazio = todos) |
| `JIRA_ODOO_PROJECT_MAP` | não | JSON key Jira → nome exato do projeto no Odoo |
| `JIRA_ODOO_EMPLOYEE_MAP` | não | JSON accountId/e-mail Jira → e-mail do funcionário no Odoo |
| `DEFAULT_EMPLOYEE_EMAIL` | não | funcionário usado quando o autor não for resolvido |

## Uso

```bash
# 1. Validar credenciais (Odoo version/authenticate + Jira /myself)
python3 -m sync_jira_odoo --test-connection

# 2. Ensaiar sem gravar nada
python3 -m sync_jira_odoo --since 2026-06-01 --dry-run -v

# 3. Sincronizar de verdade
python3 -m sync_jira_odoo --since 2026-06-01

# Execuções seguintes são incrementais (usam .sync_state.json)
python3 -m sync_jira_odoo

# Restringir projetos e propagar exclusões do Jira
python3 -m sync_jira_odoo --projects RDF,TAD --delete
```

Agendamento via cron (a cada 30 min):

```cron
*/30 * * * * cd /opt/syncjiraodoo && set -a && . ./.env && set +a && python3 -m sync_jira_odoo >> sync.log 2>&1
```

## Decisões de mapeamento

- **Autor → funcionário:** resolução por `JIRA_ODOO_EMPLOYEE_MAP[accountId]` →
  `JIRA_ODOO_EMPLOYEE_MAP[email]` → `emailAddress` exposto pelo Jira. Se o
  perfil Atlassian ocultar o e-mail, mapeie o `accountId` explicitamente.
  Autores não resolvidos geram aviso e o worklog é pulado (nada é perdido:
  a próxima execução com `--since` retroativo recupera).
- **Data do timesheet:** data local do campo `started` do worklog (respeita o
  fuso do apontamento, ex.: `-03:00`).
- **Horas:** `timeSpentSeconds / 3600`, arredondado a 2 casas.
- **Descrição:** texto do comentário do worklog (ADF achatado para texto puro)
  ou, sem comentário, o resumo da issue.

## Testes

```bash
python3 -m unittest discover -v
```

Os testes usam dublês em memória — não tocam Jira nem Odoo reais.

## Limitações conhecidas

- Worklogs com restrição de visibilidade no Jira aparecem conforme a
  permissão do token usado.
- Renomear manualmente um projeto/tarefa no Odoo removendo o marcador `[KEY]`
  faz o sync criar um novo registro na próxima execução.
- A exclusão (`--delete`) remove apenas linhas com o marcador
  `[jira-worklog:N]`; timesheets manuais nunca são tocados.

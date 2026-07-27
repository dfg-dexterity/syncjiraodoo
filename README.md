# syncjiraodoo

Sincroniza apontamentos de horas do **Jira/Clockwork Pro** para timesheets do
**Odoo Online**, usando apenas a biblioteca padrão do Python (`xmlrpc.client`
para o Odoo, `urllib` para o Jira). Sem dependências externas.

## Início rápido (sem terminal)

1. **Abra o aplicativo:** dê **duplo clique em `Iniciar Sincronizador.command`**
   (macOS) — ou rode `python3 -m sync_jira_odoo.web`. O navegador abre sozinho.
2. **Crie o primeiro acesso:** na primeira abertura, o app pede um e-mail e
   senha para o **usuário administrador**. Depois, todo acesso exige login.
3. **Configure as conexões na própria tela** (seção "Conexões"): endereço e
   credenciais do Jira e do Odoo, com botão *Testar conexões*. Fica salvo no
   `.env` local (fora do git) e carregado automaticamente nas próximas vezes —
   ninguém precisa exportar variáveis.
4. **Clique em "Simular (não grava nada)"** para conferir o que entraria e,
   estando tudo certo, **"▶ Sincronizar agora"**.

A mesma tela edita o de-para de projetos/pessoas e mostra a atividade:
resumo de cada execução, cada apontamento importado no Odoo (com filtro) e o
log técnico.

## Acesso da equipe (usuário e senha)

- **Todo acesso exige login.** Senhas ficam com hash `scrypt` + salt em
  `.users.json` (permissão 0600, fora do git); sessões expiram em 12 h e
  caem quando o app reinicia.
- **Administradores** veem a seção **Equipe** no app: adicionam pessoas
  (e-mail + senha inicial, opcionalmente administrador) e removem acessos.
  O último administrador não pode ser removido.
- **Recuperação pelo terminal:** `python3 -m sync_jira_odoo.web --add-user
  email@empresa.com.br` cadastra (ou redefine a senha de) um administrador.
- **Para o time acessar:** rode o app numa máquina fixa da rede interna com
  `--host 0.0.0.0` e compartilhe `http://ip-da-maquina:8765`. O app fala
  HTTP puro — em rede interna ok; para acesso pela internet, coloque um
  proxy HTTPS na frente (ex.: [Caddy](https://caddyserver.com), 2 linhas de
  configuração), senão a senha viaja sem criptografia.

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
| `MAPPING_FILE` | não | caminho da tabela de-para (padrão: `mapping.json`) |

## Tabela de-para (`mapping.json`)

A forma recomendada de configurar os mapeamentos é o arquivo **`mapping.json`**
(versionável — não contém segredos), editável à mão ou pela interface web:

```json
{
  "restrict_to_mapped_projects": false,
  "projects": [
    { "odoo": "Casa dos Ventos", "jira": ["CDV", "CCDV"] }
  ],
  "users": [
    { "jira": "diego@dexterityit.com.br", "odoo": "diego@dexterityit.com.br",
      "nome": "Diego Gozer" }
  ]
}
```

- **Projetos N:1** — uma linha aceita várias keys Jira apontando para o mesmo
  projeto Odoo (ex.: o projeto greenfield `CDV` e o AMS `CCDV` consolidando em
  "Casa dos Ventos").
- **Usuários** — a coluna `jira` aceita e-mail ou `accountId` (necessário
  quando o perfil Atlassian oculta o e-mail).
- **`restrict_to_mapped_projects`** — com `true`, só os projetos mapeados são
  sincronizados (a menos que `JIRA_PROJECT_KEYS`/`--projects` digam outra coisa).
- O arquivo tem precedência sobre `JIRA_ODOO_PROJECT_MAP` /
  `JIRA_ODOO_EMPLOYEE_MAP`, que continuam funcionando.

## Interface web de monitoramento

```bash
python3 -m sync_jira_odoo.web --port 8765
# abra http://127.0.0.1:8765/
```

Na interface você pode:

- **editar as tabelas de-para** de projetos e usuários (com validação) e
  salvar direto no `mapping.json`;
- **testar as conexões** com Odoo e Jira;
- **disparar um dry-run ou a sincronização**, com data inicial opcional e
  propagação de exclusões opt-in;
- **monitorar**: status em tempo real, histórico das últimas execuções
  (criados/atualizados/pulados/removidos/avisos — inclusive das execuções via
  cron, que gravam no mesmo `.sync_history.json`), avisos da última execução
  e o log ao vivo.

A interface não tem autenticação: mantenha-a em `127.0.0.1` ou rede interna.

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

## Administração compartilhada

Para outra pessoa administrar a integração, ela precisa de três coisas:

1. **Acesso ao código** — no GitHub: *Settings → Collaborators and teams →
   Add people*, papel **Write** (edita `mapping.json`, abre PRs) ou **Admin**
   (gerencia o repositório).
2. **Credenciais próprias** — nunca compartilhe chaves pessoais:
   - **Odoo:** o ideal é um usuário de serviço (ex.: `integracao@…`) com
     acesso a Projetos, Planilhas de Horas e Funcionários; a pessoa gera a
     chave em *Preferências → Segurança da Conta → Chaves de API*.
   - **Jira:** cada administrador cria seu token em
     <https://id.atlassian.com/manage-profile/security/api-tokens>; a conta
     precisa enxergar todos os projetos mapeados (os worklogs visíveis
     seguem a permissão do token).
   - As credenciais ficam no `.env` da máquina que executa (fora do git).
3. **Acesso à máquina que executa** — para operação realmente compartilhada,
   rode em uma máquina fixa (servidor interno) com cron, em vez do laptop de
   alguém. A interface web (`python3 -m sync_jira_odoo.web`) não tem
   autenticação: mantenha em `127.0.0.1` ou rede interna confiável.

## Arquivos de estado e logs

Todos ficam no diretório de execução, fora do versionamento:

| Arquivo | Conteúdo |
|---|---|
| `.sync_state.json` | ponto de avanço incremental (última execução ok) |
| `.sync_history.json` | resumo das últimas 200 execuções (contadores + avisos) — alimenta o Monitor da interface web |
| `.sync_import_log.jsonl` | um registro por timesheet criado/atualizado/removido no Odoo (últimos 5 000) — seção "Importados no Odoo" da interface web; dry-run não grava |
| `.sync_run.log` (+ `.1`…`.3`) | log completo de execução (cada linha do que o sync fez), rotativo em 2 MB × 3 — `--log-file` muda o caminho, `--log-file ''` desativa |
| `.users.json` | usuários do aplicativo (hash de senha `scrypt` + salt; nunca a senha) |

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

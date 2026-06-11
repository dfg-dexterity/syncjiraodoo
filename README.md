# syncjiraodoo

Sync utilities between **Jira** (Atlassian Cloud) and **Odoo**.

The project is dependency-free: Odoo is accessed via the stdlib
`xmlrpc.client` and Jira via the stdlib `urllib`.

## Setup

1. Copy the example env file and fill in real values:

   ```bash
   cp .env.example .env
   # then edit .env
   ```

   `.env` is gitignored — never commit secrets.

   | Variable          | Description                                                        |
   | ----------------- | ------------------------------------------------------------------ |
   | `ODOO_URL`        | Base URL, e.g. `https://mycompany.odoo.com` (no trailing slash)    |
   | `ODOO_DB`         | Database name (often the subdomain on odoo.com)                    |
   | `ODOO_USERNAME`   | Login email/username                                               |
   | `ODOO_API_KEY`    | API key (Preferences → Account Security → New API Key) or password |
   | `JIRA_URL`        | Site URL, e.g. `https://yourcompany.atlassian.net`                 |
   | `JIRA_EMAIL`      | Atlassian account email                                            |
   | `JIRA_API_TOKEN`  | Token from id.atlassian.com → Security → API tokens                |

2. (Optional) install as a CLI:

   ```bash
   pip install -e .
   ```

## Test the connections

```bash
python -m syncjiraodoo --test-connection
# or, if installed:
syncjiraodoo --test-connection
```

This pings Jira and Odoo independently and reports each result. Exit code is
`0` only when both succeed.

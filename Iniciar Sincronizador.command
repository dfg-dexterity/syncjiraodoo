#!/bin/bash
# Duplo clique neste arquivo (macOS) abre o Sincronizador de Horas no navegador.
cd "$(dirname "$0")"
exec python3 -m sync_jira_odoo.web

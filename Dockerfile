# Sincronizador de Horas — imagem mínima (só stdlib, sem pip install)
FROM python:3.12-slim

COPY sync_jira_odoo /app/sync_jira_odoo
COPY mapping.json /app/mapping.json

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# /data guarda tudo que persiste: .env, .users.json, mapping.json, estado e logs
WORKDIR /data
EXPOSE 8765

ENTRYPOINT ["/bin/sh", "-c", \
    "[ -f mapping.json ] || cp /app/mapping.json mapping.json; \
     exec python -m sync_jira_odoo.web --host 0.0.0.0 --no-browser"]

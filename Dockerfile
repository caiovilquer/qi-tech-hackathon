# Imagem oficial e publica do Python. Qualquer pessoa consegue baixar.
FROM python:3.11-slim AS base

# Roda a aplicacao com um usuario sem privilegios, nunca como root.
RUN adduser --system --no-create-home user

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PYTHONPATH=/opt/pix
ENV PYTHONDONTWRITEBYTECODE=1

# Copiar so o requirements primeiro faz o Docker reaproveitar o cache:
# enquanto as dependencias nao mudam, ele nao reinstala tudo de novo.
COPY requirements.txt /requirements.txt
COPY requirements.lock /requirements.lock
RUN pip install -r /requirements.txt -c /requirements.lock

WORKDIR /app
COPY src /app
COPY migrations /opt/pix/migrations

FROM base AS test
COPY requirements-dev.txt /requirements-dev.txt
RUN pip install -r /requirements-dev.txt -c /requirements.lock
WORKDIR /workspace
COPY . /workspace
RUN chown -R user /workspace
USER user
CMD ["python", "-m", "pytest", "-q"]

FROM base AS api
RUN chown -R user /app
USER user

# --no-server-header hides the server version; --no-access-log avoids duplicating
# the request logger lines, which carry the request id.
# An analysis is CPU-bound Python (~90 ms), so one process caps a burst at about
# 10 POST/s. Workers coordinate only through PostgreSQL (claims, leases, advisory
# locks). Each holds up to 20 Pix connections: keep API_WORKERS * 20 under max_connections.
ENV API_WORKERS=4
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 3000 --workers \"$API_WORKERS\" --no-server-header --no-access-log"]


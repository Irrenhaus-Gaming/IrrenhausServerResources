# Tools image for mcdeploy. Local runs (mc.ps1 / mc.sh), act and GitHub Actions all execute
# inside this image so the code, Python packages and gitleaks version are identical everywhere.
FROM ghcr.io/gitleaks/gitleaks:v8.28.0 AS gitleaks
FROM docker:27.5.1-cli AS dockercli

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'
COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY requirements.txt /opt/mcdeploy/requirements.txt
RUN pip install --no-cache-dir -r /opt/mcdeploy/requirements.txt
COPY mcdeploy /opt/mcdeploy/mcdeploy
COPY tests /opt/mcdeploy/tests
COPY pytest.ini /opt/mcdeploy/pytest.ini
ENV PYTHONPATH=/opt/mcdeploy PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo
ENTRYPOINT ["python", "-m", "mcdeploy"]

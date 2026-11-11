# 허브 이미지. 빌드 컨텍스트는 저장소 루트다 (예제 클라이언트 scripts/ 를 테스트에 쓴다).
#   docker build --target runtime -t secretary .
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 테스트 단계: 테스트가 하나라도 실패하면 이미지 빌드가 실패한다.
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts
RUN python -m pytest -q

# 실행 이미지: 앱 코드만 담는다.
FROM base AS runtime
ENV SECRETARY_DATA_DIR=/data
COPY app ./app

# root가 아닌 전용 사용자로 실행한다. NAS의 data 폴더 소유자를 이 UID로 맞춘다.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin secretary \
    && mkdir -p /data && chown secretary /data
USER secretary

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

# 워커는 1개. Nonce와 승인 대기열을 SQLite 하나로 관리하기 때문이다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-server-header"]

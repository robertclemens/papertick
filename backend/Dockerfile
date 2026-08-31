FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 10001 app

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R app:app /srv

USER app
EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]

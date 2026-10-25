# Pyrtan: one image for a service or a hub. The command decides which.
#
#   docker run -p 8701:8701 -v ./trees:/trees pyrtan serve /trees --host 0.0.0.0
#   docker run -p 8700:8700 pyrtan hub --host 0.0.0.0
#
# The examples are baked in at /examples so the image demos on its own.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="pyrtan" \
      org.opencontainers.image.description="A swarm of small services that act on the web through hardcoded, inspectable decision trees." \
      org.opencontainers.image.source="https://github.com/b-i-t-8/pyrtan" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
COPY --from=build /install /usr/local
COPY examples /examples
RUN useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin pyrtan \
 && mkdir -p /trees && chown pyrtan:pyrtan /trees
USER pyrtan
VOLUME ["/trees"]
ENV PYTHONUNBUFFERED=1 PYRTAN_TREES=/trees
EXPOSE 8700 8701
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request;p=os.environ.get('PYRTAN_PORT','8701');sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health',timeout=4).status==200 else 1)"
ENTRYPOINT ["pyrtan"]
CMD ["serve", "/examples", "--host", "0.0.0.0", "--port", "8701"]

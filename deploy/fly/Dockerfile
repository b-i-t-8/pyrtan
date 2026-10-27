# Pyrtan's image for Fly.io: the root Dockerfile's, but it starts as root
# just long enough to hand the volume at /data to the pyrtan user, since a
# Fly volume mounts root-owned, then runs pyrtan as that user. Build it from
# the repository root; see README.md beside this file.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="pyrtan" \
      org.opencontainers.image.source="https://github.com/b-i-t-8/pyrtan" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"
COPY --from=build /install /usr/local
COPY examples /examples
RUN chmod -R a+rX /examples \
 && useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin pyrtan \
 && mkdir -p /data && chown pyrtan:pyrtan /data
ENV PYTHONUNBUFFERED=1
EXPOSE 8700
ENTRYPOINT ["sh", "-c", "chown pyrtan:pyrtan /data && exec setpriv --reuid=pyrtan --regid=pyrtan --init-groups --no-new-privs pyrtan \"$@\"", "pyrtan"]
CMD ["hub", "--host", "0.0.0.0", "--port", "8700"]

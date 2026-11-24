FROM ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server python3 \
    && rm -f /etc/ssh/ssh_host_* && rm -rf /var/lib/apt/lists/* \
    && useradd --system --user-group --home-dir /var/lib/qiban --shell /usr/sbin/nologin qiban \
    && useradd --system --user-group --no-create-home --home-dir / --shell /bin/sh qiban-status \
    && usermod --password '*' qiban-status

WORKDIR /opt/qiban
COPY src/companion_core/__init__.py src/companion_core/passages.py src/companion_core/index_cache.py companion_core/
COPY platforms/linux/server/service.py platforms/linux/server/jobs.py platforms/linux/server/gateway.py platforms/linux/server/supervisor.py ./
COPY platforms/linux/server/sshd_config /etc/qiban/sshd_config
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 22
ENTRYPOINT ["/usr/bin/python3", "/opt/qiban/supervisor.py"]

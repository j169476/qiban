ARG STATUS_IMAGE=qiban-linux-status:local
FROM ${STATUS_IMAGE}
RUN apt-get update && printf 'create_main_cluster = false\n' > /tmp/createcluster.conf \
    && mkdir -p /etc/postgresql-common/createcluster.d \
    && cp /tmp/createcluster.conf /etc/postgresql-common/createcluster.d/01-qiban.conf \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends postgresql-16 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/core-venv
COPY platforms/linux/core/requirements.txt /opt/qiban/core-requirements.txt
RUN /opt/core-venv/bin/pip install --no-cache-dir -r /opt/qiban/core-requirements.txt
COPY src/companion_core/*.py src/companion_core/*.json /opt/qiban/companion_core/
COPY src/companion_core/sql/*.sql /opt/qiban/companion_core/sql/
COPY platforms/linux/core/core_init.py platforms/linux/core/core_control.py platforms/linux/core/dialogue_service.py platforms/linux/core/dialogue_store.py /opt/qiban/
ENTRYPOINT ["/opt/core-venv/bin/python", "/opt/qiban/core_init.py"]

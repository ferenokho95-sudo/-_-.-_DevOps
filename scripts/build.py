"""Generate JSON-formatted Compose files (JSON is a YAML subset)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def write(path, value):
    (ROOT / path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

def service(image, **kwargs):
    return dict(image=image, restart="unless-stopped", networks=["metrics"], **kwargs)

def config(name, target):
    return {"source": name, "target": target}

networks = {"metrics": {"external": True, "name": "uptime-metrics"}}
app = {
    "name": "uptime-app", "networks": networks,
    "services": {
        "db": service("mariadb:11.4.5", environment={
            "MARIADB_ROOT_PASSWORD": "${DB_ROOT_PASSWORD:?required}",
            "MARIADB_DATABASE": "kuma", "MARIADB_USER": "kuma",
            "MARIADB_PASSWORD": "${DB_PASSWORD:?required}"},
            volumes=["db-data:/var/lib/mysql"],
            configs=[config("exporter-init", "/docker-entrypoint-initdb.d/exporter.sql")],
            healthcheck={"test": ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"],
                         "interval": "5s", "timeout": "5s", "retries": 30, "start_period": "30s"}),
        "kuma": service("louislam/uptime-kuma:2.0.2", ports=["127.0.0.1:3001:3001"],
            volumes=["kuma-data:/app/data"],
            command=["sh", "-ec", "cp /platform-config/db-config.json /app/data/db-config.json; exec node server/server.js"],
            configs=[config("db-config", "/app/data/db-config.json")],
            depends_on={"db": {"condition": "service_healthy"}},
            healthcheck={"test": ["CMD", "extra/healthcheck"],
                         "interval": "5s", "timeout": "5s", "retries": 40, "start_period": "60s"})},
    "volumes": {"db-data": {}, "kuma-data": {}},
    "configs": {
        "db-config": {"content": json.dumps({"type": "mariadb", "hostname": "db", "port": 3306,
            "username": "kuma", "password": "${DB_PASSWORD:?required}", "dbName": "kuma"})},
        "exporter-init": {"content": "CREATE USER IF NOT EXISTS 'exporter'@'%' IDENTIFIED BY '${EXPORTER_PASSWORD:?required}' WITH MAX_USER_CONNECTIONS 3;\nGRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';\n"}}}

prom = {"global": {"scrape_interval": "5s", "evaluation_interval": "5s"}, "scrape_configs": []}
for name, address in [("prometheus", "prometheus:9090"), ("node", "node:9100"),
                      ("cadvisor", "cadvisor:8080"), ("mariadb", "mysqld:9104")]:
    prom["scrape_configs"].append({"job_name": name, "static_configs": [{"targets": [address]}]})
prom["scrape_configs"].append({"job_name": "http", "metrics_path": "/probe",
    "params": {"module": ["http_2xx"]},
    "static_configs": [{"targets": ["http://kuma:3001", "http://grafana:3000/api/health"]}],
    "relabel_configs": [{"source_labels": ["__address__"], "target_label": "__param_target"},
                        {"source_labels": ["__param_target"], "target_label": "instance"},
                        {"target_label": "__address__", "replacement": "blackbox:9115"}]})
blackbox = {"modules": {"http_2xx": {"prober": "http", "timeout": "3s",
    "http": {"preferred_ip_protocol": "ip4", "follow_redirects": True}}}}
monitor = {"name": "uptime-monitoring", "networks": networks,
    "volumes": {"prom-data": {}, "grafana-data": {}},
    "configs": {"prometheus": {"content": json.dumps(prom)},
                "blackbox": {"content": json.dumps(blackbox)},
                "mysql": {"content": "[client]\nhost=db\nuser=exporter\npassword=${EXPORTER_PASSWORD:?required}\n"}},
    "services": {
        "prometheus": service("prom/prometheus:v3.2.1", ports=["127.0.0.1:9090:9090"],
            command=["--config.file=/etc/platform/prometheus.yml", "--storage.tsdb.path=/prometheus",
                     "--storage.tsdb.retention.time=15d"], volumes=["prom-data:/prometheus"],
            configs=[config("prometheus", "/etc/prometheus/prometheus.yml")]),
        "grafana": service("grafana/grafana:11.6.0", ports=["127.0.0.1:3000:3000"],
            environment={"GF_SECURITY_ADMIN_USER": "admin", "GF_SECURITY_ADMIN_PASSWORD": "${GRAFANA_PASSWORD:?required}",
                         "GF_USERS_ALLOW_SIGN_UP": "false", "GF_AUTH_ANONYMOUS_ENABLED": "false"},
            volumes=["grafana-data:/var/lib/grafana"]),
        "blackbox": service("prom/blackbox-exporter:v0.26.0", command=["--config.file=/etc/platform/blackbox.yml"],
            configs=[config("blackbox", "/etc/blackbox.yml")]),
        "mysqld": service("prom/mysqld-exporter:v0.17.2", command=["--config.my-cnf=/etc/platform/mysql.cnf", "--mysqld.address=db:3306"],
            configs=[config("mysql", "/etc/mysql.cnf")]),
        "node": service("prom/node-exporter:v1.9.0", pid="host",
            command=["--path.rootfs=/host", "--path.procfs=/host/proc", "--path.sysfs=/host/sys"],
            volumes=["/:/host:ro"]),
        "cadvisor": service("ghcr.io/google/cadvisor:0.55.1", privileged=True,
            pid="host", cgroup="host",
            command=["--containerd=/proc/1/root/run/containerd/containerd.sock"],
            devices=["/dev/kmsg:/dev/kmsg"],
            volumes=["/:/rootfs:ro", "/var/run:/var/run:ro",
                     "/var/lib/docker:/var/lib/docker:ro", "/dev/disk:/dev/disk:ro"])}}

# Compose configs are implemented using client-side bind mounts outside Swarm.
# Such paths live in Dagger, not on the target daemon. Stage configs in named
# Docker volumes instead; no dependence on /var/lib/docker or runner host paths.
contents = {}
locations = {
    "db-config": ("/platform-config", "db-config.json"),
    "exporter-init": ("/docker-entrypoint-initdb.d", "exporter.sql"),
    "prometheus": ("/etc/platform", "prometheus.yml"),
    "blackbox": ("/etc/platform", "blackbox.yml"),
    "mysql": ("/etc/platform", "mysql.cnf"),
}
for stack in (app, monitor):
    for name, payload in stack.pop("configs").items():
        volume = "config-" + name
        stack["volumes"][volume] = {"external": True, "name": "uptime-" + volume}
        contents[name] = {"filename": locations[name][1], "content": payload["content"]}
    for svc in stack["services"].values():
        for mount in svc.pop("configs", []):
            name = mount["source"]
            svc.setdefault("volumes", []).append("config-" + name + ":" + locations[name][0] + ":ro")
write("compose.app.json", app)
write("compose.monitoring.json", monitor)
write("config-content.json", contents)

# Missing metrics count as unavailable, rather than producing an empty panel.
db = 'max((mysql_up{job="mariadb"} or vector(0)))'
kuma = 'max((probe_success{job="http",instance="http://kuma:3001"} or vector(0)))'
health = f'({db}) * ({kuma})'
write("monitoring/queries.json", {"db": db, "kuma": kuma, "health": health})
panels = []
def panel(title, expr, kind="timeseries", unit="short", legend="{{instance}}"):
    i = len(panels)
    stat = kind == "stat"
    panels.append({"id": i+1, "title": title, "type": kind,
        "gridPos": {"x": (i % 2)*12, "y": (i//2)*8, "w": 12, "h": 8},
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "instant": stat}],
        "fieldConfig": {"defaults": {"unit": unit,
            "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]},
            "mappings": ([{"type": "value", "options": {"0": {"text": "ВСЁ ПЛОХО", "color": "red"},
                        "1": {"text": "OK", "color": "green"}}}] if stat else [])}, "overrides": []},
        "options": ({"colorMode": "background", "reduceOptions": {"calcs": ["lastNotNull"], "values": False}} if stat else {})})
panel("Контур: Kuma × MariaDB", health, "stat")
panel("MariaDB SQL connection", db, "stat")
panel("Uptime Kuma HTTP", kuma, "stat")
panel("HTTP latency", 'probe_duration_seconds{job="http"}', unit="s")
panel("Container CPU cores", 'sum by (container_label_com_docker_compose_service) (rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_project=~"uptime-app|uptime-monitoring"}[1m]))', legend="{{container_label_com_docker_compose_service}}")
panel("Container RAM", 'sum by (container_label_com_docker_compose_service) (container_memory_working_set_bytes{container_label_com_docker_compose_project=~"uptime-app|uptime-monitoring"})', unit="bytes", legend="{{container_label_com_docker_compose_service}}")
panel("Host CPU", '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))', unit="percent")
panel("Host RAM used", 'node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes', unit="bytes")
panel("Host disk available", 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}', unit="bytes", legend="{{mountpoint}}")
panel("MariaDB connections", 'mysql_global_status_threads_connected')
panel("Scrape targets", 'up', "timeseries", legend="{{job}}")
panel("Container network RX", 'sum by (container_label_com_docker_compose_service) (rate(container_network_receive_bytes_total{container_label_com_docker_compose_project=~"uptime-app|uptime-monitoring"}[1m]))', unit="Bps", legend="{{container_label_com_docker_compose_service}}")
write("monitoring/dashboard.json", {"uid": "uptime-health", "title": "Uptime platform health",
    "schemaVersion": 40, "version": 1, "refresh": "5s", "time": {"from": "now-30m", "to": "now"}, "panels": panels})

if __name__ == "__main__":
    print("Compose and dashboard generated")

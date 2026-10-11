# claude-cost-otel

See what your Claude Code agents cost, locally. One command starts an OpenTelemetry
stack, loads two Perses dashboards and turns on Claude Code's telemetry export.

![Agent cost dashboard](docs/agent-cost-dashboard.png)

- **Agent cost** answers: how much did each agent (`main`, `reviewer`, `implementer`,
  `general-purpose`, ...) cost, on which model, and which MCP tools drive spend.
- **Tools** shows what the agents' tools do: how often each runs, how long it takes,
  what waits on you, what fails and how much each returns into the context.

## Quick start

Requirements: Docker with Compose, Python 3 (tested on 3.13, Windows 11). No Python
packages needed.

```bash
python claude_cost_otel.py setup
```

This:

1. runs `docker compose up -d` and waits for Perses and Prometheus,
2. loads the **Agent cost** and **Tools** dashboards into Perses (project `ai-agents`),
3. merges the telemetry variables below into `~/.claude/settings.json`
   (a timestamped backup is kept, unrelated settings are left alone).

Then restart Claude Code, send a prompt, and open <http://localhost:8080> →
**ai-agents**. Data shows up within about a minute; spawn a subagent to see the
per-agent panels fill in.

Preview without changing anything: `python claude_cost_otel.py setup --dry-run`.

## How it fits together

```
Claude Code ──OTLP gRPC :4317──▶ Collector ──┬─ metrics ──────────────────▶ Prometheus ──┐
                                             ├─ logs ─────────────────────▶ Loki ────────┼──▶ Perses
                                             └─ spans ─▶ spanmetrics ─▶ (metrics) ─▶ Prometheus
```

| Signal | Where it goes | Used for |
|---|---|---|
| Metrics (cost, tokens, active time, ...) | Prometheus | Every **Agent cost** panel |
| Log events (`api_request`, `tool_result`, ...) | Loki | **Tool result size by tool** |
| Spans | Turned into metrics by the collector's `spanmetrics` connector, then dropped | Tool calls, duration, errors and time blocked on user in **Tools** |

Spans are not stored. `spanmetrics` produces `claude_code_span_calls_total` and
`claude_code_span_duration_milliseconds_{bucket,count,sum}`, labelled by `tool_name`,
`session.id`, `user.email`, `span.type`, `subagent_type` and `query_source_safe`.

## What gets enabled in Claude Code

These go into the `env` block of your **user** settings (`~/.claude/settings.json`).
`CLAUDE_CODE_ENABLE_TELEMETRY` is ignored in project and local settings, so the tool
only writes user-level settings.

| Variable | Value | Why |
|---|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | Master switch for OpenTelemetry export. |
| `OTEL_METRICS_EXPORTER` | `otlp` | Cost and token counters. |
| `OTEL_LOGS_EXPORTER` | `otlp` | Per-request events (`api_request`, `tool_result`, ...). |
| `OTEL_TRACES_EXPORTER` | `otlp` | Spans per interaction, model request and tool call, which become the **Tools** metrics. |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` | `1` | Required for traces (beta), and so for the span-based **Tools** panels. Cost metrics and logs work without it. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | Claude Code has no default protocol; the collector listens on gRPC. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | The local collector. |
| `OTEL_METRIC_EXPORT_INTERVAL` | `10000` | Export every 10 s instead of the default 60 s, so the dashboards react quickly. |
| **`OTEL_LOG_TOOL_DETAILS`** | `1` | **The detail flag.** Puts real agent, skill, plugin and MCP server/tool names on the cost and token counters. |

Without `OTEL_LOG_TOOL_DETAILS`, Claude Code does not report real agent and MCP names
on those counters, so the per-agent and per-MCP-tool panels lose their meaning. See the
[Claude Code monitoring docs](https://code.claude.com/docs/en/monitoring-usage).

### Privacy

`OTEL_LOG_TOOL_DETAILS=1` also adds tool parameters to log events and trace spans:
Bash commands, file paths, URLs, search patterns and MCP tool names. Where that data
ends up:

- **Loki** stores the log events on your machine (bound to `127.0.0.1`, 15-day
  retention).
- **The collector's `debug` exporter** prints everything it receives, in detail, to its
  container log (`docker compose logs otelcol`).
- **Spans** are not stored.

Prompt text stays redacted (`<REDACTED>` with only its length) unless you also set
`OTEL_LOG_USER_PROMPTS=1`, which this tool does not do. Pass `--no-tool-details` to
`setup` or `enable` to leave the detail flag off.

## The tool

```
python claude_cost_otel.py setup      # stack + dashboards + Claude Code settings
python claude_cost_otel.py enable     # only the Claude Code settings
python claude_cost_otel.py disable    # remove the variables this tool manages
python claude_cost_otel.py dashboard  # (re)load dashboards into a running Perses
python claude_cost_otel.py status     # settings, service health, dashboards present?
python claude_cost_otel.py down       # stop the stack (--purge also deletes volumes)
```

Useful options: `--dry-run`, `--settings PATH`, `--endpoint URL`, `--interval MS`,
`--no-tool-details`, `--perses-url URL`, and for `setup` `--skip-stack`,
`--skip-dashboard`, `--skip-claude`.

`enable` is idempotent and `disable` removes only the variables listed above.
`dashboard` imports every `*.json` file in `perses/provisioning/dashboards/`, creating
or updating each one.

## What runs

| Service | Image | Port | Purpose |
|---|---|---|---|
| OpenTelemetry Collector | `otel/opentelemetry-collector-contrib:0.158.0` | `4317` (OTLP gRPC) | Receives everything, routes each signal |
| Prometheus | `prom/prometheus:v3.13.2` | [`9090`](http://localhost:9090) | Metrics; 15-day retention |
| Loki | `grafana/loki:3.4.2` | [`3100`](http://localhost:3100) | Log events; single binary on the local filesystem, 15-day retention |
| Perses | `persesdev/perses:v0.54.0` | [`8080`](http://localhost:8080) | Dashboards; no login |

All ports are bound to `127.0.0.1`. Prometheus runs with `--web.enable-otlp-receiver`
and `--enable-feature=otlp-deltatocumulative` so it accepts OTLP directly, and Loki
accepts OTLP at `/otlp` with structured metadata enabled. The Perses container runs as
`0:0` because its data volume is root-owned. Data lives in the named volumes
`prometheus_data`, `loki_data` and `perses_data`.

## The dashboards

Both are defined as JSON in
[`perses/provisioning/dashboards/`](perses/provisioning/dashboards/). Perses provisions
them on start (and re-reads every hour), and `claude_cost_otel.py dashboard` pushes them
into a running Perses immediately. Two datasources are provisioned: `prometheus` and
`loki`, each the default for its kind.

**Agent cost** ([`agent-cost.json`](perses/provisioning/dashboards/agent-cost.json)):

- **Total cost**
- **Cost per subagent** (pie with a legend, main session excluded)
- **Cost by agent** (`main` is the session itself)
- **Cost over time by agent** (USD/h)
- **Cost by agent and model**
- **Output tokens by agent**
- **Cost by MCP tool**

**Tools** ([`tools.json`](perses/provisioning/dashboards/tools.json)):

- **Tool calls** per tool
- **Average tool duration** per tool
- **Time blocked on user** (time tool calls spent waiting for a permission decision)
- **Tool errors**
- **Tool result size by tool** (bytes returned into the context, from Loki log events)

### Limits worth knowing

- **No cost per built-in tool.** Claude Code attributes cost to agents, models and MCP
  tools, not to Bash, Read, Edit and the like. The **Tools** dashboard shows calls,
  duration and result size instead, and result size is the best proxy for what a tool
  adds to the context, and so to later cache-read cost.
- **Prompts are not shown.** Prompt text is redacted by default, so a list of prompts
  would be trace IDs with a time and a span count.
- **The log panel uses a fixed 5-minute window.** Perses documents its built-in range
  variables only for Prometheus queries, so the Loki panel does not use them.
- **Loki is slow to report ready.** Its `/ready` answers 503 for the first minutes after
  start. `status` shows that as `wait`, and logs are accepted in the meantime.
- **Short-lived agents can be slightly undercounted.** The first sample of a new
  metric series has no baseline, so `increase()` skips it. The panels fall back to
  `last_over_time` for series with a single sample, but a series with several samples
  can still under-count its first batch.
- **Right after a run, panels may lag.** Metrics are exported on an interval, so a
  subagent that just finished may take up to the export interval to appear.

## Repository layout

| Path | What it is |
|---|---|
| `claude_cost_otel.py` | The setup tool |
| `docker-compose.yml` | The four services above |
| `otelcol.yaml` | Collector: OTLP receiver, `spanmetrics` connector, exporters to Prometheus and Loki |
| `prometheus.yml` | Prometheus config |
| `loki/config.yaml` | Loki single-binary config (filesystem, tsdb schema v13, OTLP structured metadata) |
| `perses/config.yaml` | Perses config (file database, provisioning folder) |
| `perses/provisioning/` | Project, datasources and dashboards loaded into Perses |
| `docs/` | Screenshot used in this README |

## Troubleshooting

```bash
python claude_cost_otel.py status      # what is up, what is configured
docker compose ps
docker compose logs -f otelcol         # telemetry arriving, export errors
claude --debug                         # OpenTelemetry export errors on the Claude side
```

- **Dashboard is empty:** restart Claude Code after `setup`; the variables are read at
  start. Then send a prompt and wait one export interval.
- **A panel shows `main` only:** no subagents have run yet in the selected time range.
- **Port already in use:** another stack is bound to `4317`, `8080`, `9090` or `3100`;
  stop it first.

## Cleanup

```bash
python claude_cost_otel.py disable        # remove the Claude Code variables
python claude_cost_otel.py down --purge   # stop the stack and delete its data
```

## License

Apache-2.0, see [LICENSE](LICENSE). The stack is derived from
[dash0-community/claude-code-monitoring](https://github.com/dash0-community/claude-code-monitoring);
[NOTICE](NOTICE) lists the files that differ from it.

# Antigravity Subscription Throughput Benchmark: MVP Summary

## Purpose

The project measures how much successful coding work a consumer AI subscription can complete before its included usage allowance is exhausted.

The Minimum Viable Product (MVP) focuses solely on **Google Antigravity** paired with a **Google AI Pro** subscription, comparing different Google models and relevant configuration settings available within the product. The long-term objective is to extend this methodology to **Claude Code Desktop** (Claude Pro) and the **Codex Desktop** experience (ChatGPT Plus).

### Core Question
> **Which model completes the most coding tasks successfully per unit of subscription allowance?**

Unlike conventional coding benchmarks that focus primarily on raw task accuracy, this benchmark explicitly evaluates the relationship between **autonomous task success** and **consumer subscription quota depletion**.

---

## Scope

* **Agent Harness View**: The benchmark treats Antigravity as a composite system: `agent harness + model + subscription quota`, rather than attempting to evaluate the subjective quality of its graphical desktop interface.
* **Automation & Repeatability**: While the desktop UI serves as the standard user-facing vehicle for submitting tasks, selecting models, and viewing usage, experiments will run via Antigravity's CLI or SDK wherever possible to ensure automated, repeatable execution.
* **No Human-in-the-Loop Fixes**: Like SWE-bench, tasks are scored strictly on autonomous completion. A generated patch is applied to a pristine repository and evaluated against containerized test suites. Human repairs, interactive hints, or conversational guidance are excluded from the scored result.

---

## Tasks & Evaluation

### Task Suite
* **Source**: Curated subset drawn from **SWE-bench Verified** or **SWE-bench Lite** (providing real GitHub issues, pinned commit states, and reproducible Docker-based grading).
* **Composition**: A compact, fixed suite covering a variety of task difficulties and repository complexities. Keeping the suite constant across runs enables longitudinal tracking of model and quota changes over time.
* **Success Criteria**: Evaluated strictly by whether the agent's generated patch resolves the issue when executed against the repository's test harness.

### Execution Workflow
For every task in the suite:
1. **Provision**: Spin up a clean task container or isolated repository worktree.
2. **Dispatch**: Supply the standardized issue description to the selected Antigravity configuration.
3. **Autonomous Execution**: Allow the agent to work independently without human intervention.
4. **Halt**: Terminate upon agent completion, timeout, fatal error, or subscription quota restriction.
5. **Capture Artifacts**: Collect the generated Git patch, agent transcripts, and execution logs.
6. **Grade**: Apply and test the patch inside a pristine grading container using the SWE-bench evaluation harness.
7. **Record Quota**: Measure subscription usage allowance before and after the task.

> [!NOTE]
> No conversational hints, manual code edits, or ad-hoc retries are allowed unless explicitly defined in advance as a distinct experimental condition.

---

## Model Configurations

The MVP evaluates three primary baseline configurations:
1. **Default / Recommended**: Antigravity's default Google model.
2. **Efficiency-Optimized**: The fastest or most quota-efficient available model.
3. **Capability-Optimized**: The most capable frontier model available under Google AI Pro.

### Controlled Variables
* **Single-Variable Testing**: Model comparisons use identical prompts, tool access, permissions, timeouts, network policies, and stopping conditions. Reasoning or agent parameters will be varied systematically in subsequent experiments.
* **Sequential Execution**: All tasks execute sequentially. Parallel execution is avoided to prevent artificial concurrency throttling or transient rate limits that do not reflect standard subscriber usage capacity.

---

## Measurements & Metrics

### Data Collected Per Task
* Tasks attempted vs. successfully resolved
* Overall success rate
* Wall-clock execution time
* Agent or infrastructure failures
* Quota limit interruptions or rate-limiting events
* Model identifier, application version, and harness metadata
* Reported token and tool usage (where exposed)
* Subscription allowance / quota state before and after each task
* Cumulative successful tasks completed before subscription allowance becomes restrictive

### Primary Metric

$$\text{Subscription Efficiency} = \frac{\text{Successfully Resolved Tasks}}{\text{Subscription Allowance Consumed}}$$

To prevent ineffective or cheap models from ranking deceptively high by failing fast with minimal quota consumption, reports will always present **raw success rate**, **time-to-complete**, and **efficiency** side-by-side.

---

## Technical Architecture

* **Environment**: Native Linux host running Docker.
  * **OS**: Linux Mint 22.3 (64-bit, Kernel `6.18.1-061801-generic`)
  * **Desktop / Display**: KDE Plasma 5.27.12 (Wayland), Qt 5.15.13 / Frameworks 5.115.0
  * **Hardware**: AMD Ryzen 7 5700G (16 vCPUs), 32 GB RAM (30.6 GiB usable), AMD Radeon RX 9060 XT GPU
* **Orchestration**: Automated shell and Python scripts to provision task environments, drive the Antigravity agent, archive patches and logs, execute grading harnesses in Docker, and persist structured metrics.
* **Storage**: Results logged to SQLite, CSV, or Parquet.

### Stored Artifacts
* Experiment manifest (run configurations and parameters)
* Exact task manifest and sequence order
* Model and configuration labels
* Raw agent transcripts and output logs
* Generated Git patch files
* Evaluation / grading test logs
* Timestamps and execution durations
* Observed quota metrics
* Tool, agent, and harness version metadata

> [!WARNING]
> **Quota Tracking Challenge**: Automated quota retrieval may be constrained if Antigravity lacks a public CLI/SDK endpoint for usage metrics. Initial runs may require manual quota checkpoints before and after execution. Any measurement constraints must be reported transparently rather than substituted with estimated API token costs.

---

## Outputs & Future Roadmap

### Reporting
Each benchmark run produces a reproducible dataset and a concise summary report highlighting:
* Highest task-completion rate
* Highest throughput of successfully completed tasks
* Lowest quota consumption per successful task
* Total tasks resolved prior to subscription throttling
* Longitudinal deltas compared against prior benchmark runs

### Longitudinal Schedule & Expansion
* **Cadence**: Bi-weekly re-runs to monitor changes in underlying model quality, server-side routing, and subscription quota limits.
* **Cross-Ecosystem Comparison**: Once the evaluation protocol stabilizes on Antigravity, extend the identical benchmark suite and grading rules to:
  * **Claude Code Desktop** (Claude Pro)
  * **Codex Desktop** (ChatGPT Plus)

---

### Proposition Summary
> **A longitudinal benchmark measuring successful autonomous coding work delivered per real-world consumer subscription, starting with Google Antigravity model configurations.**

---

## Development

* Plan: [`docs/implementation_plan.md`](docs/implementation_plan.md). Phase 0 results: [`docs/phase0_findings.md`](docs/phase0_findings.md). Next-batch handover: [`docs/handover_phase3.md`](docs/handover_phase3.md).
* Requires [`uv`](https://docs.astral.sh/uv/), Python 3.12, and Docker (suite validation and grading).

```bash
uv sync                                   # runtime + dev tools
uv run pytest                             # unit tests
uv run ruff check . && uv run ruff format --check .
uv run subbench init-db                   # create data/subbench.sqlite
uv run subbench validate experiments/baseline.yaml

# Suite curation needs the extra `suite` dependency group (pyarrow, swebench)
uv run --group suite subbench suite build --revision <sha> --version verified-mvp-v1 \
    --out suites/verified-mvp-v1.json
uv run --group suite subbench suite validate suites/verified-mvp-v1.json
```

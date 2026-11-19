(() => {
  "use strict";

  const state = {
    data: null,
    theme: localStorage.getItem("subbench-theme") || "light",
    metric: "primary", // "primary" | "token"
    era: "compare", // "compare" | "current" | "nov7"
    xScale: "log", // "log" | "linear"
    pools: new Set([
      "gemini-weekly",
      "gemini-argon-wtu-weekly",
      "3p-weekly",
      "codex-30d",
    ]),
    showPareto: true,
    showCurves: true,
    showCI: false,
    apiMetric: "usd", // "usd" | "mult" | "burn"
    tableView: "projections", // "projections" | "measured"
    tableSearch: "",
    tableSort: { key: "aa_intelligence", dir: "desc" },
  };

  const CFG = window.SUBBENCH_CONFIG || {
    repoUrl: "https://github.com/YOUR_USERNAME/SubscriptionBench",
    kofiUrl: "https://ko-fi.com/YOUR_KOFI_HANDLE",
    subscriptionPriceUsd: 19.99,
  };

  function applyConfigLinks() {
    const setHref = (id, url) => {
      const el = document.getElementById(id);
      if (el && url) el.href = url;
    };
    setHref("nav-github", CFG.repoUrl);
    setHref("footer-github", CFG.repoUrl);
    setHref("nav-kofi", CFG.kofiUrl);
    setHref("banner-kofi", CFG.kofiUrl);
    setHref("footer-kofi", CFG.kofiUrl);
  }

  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("subbench-theme", theme);
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "dark" ? "Light" : "Dark";
    if (state.data) {
      renderParetoChart();
      renderApiChart();
    }
  }

  function getThemeColors() {
    const dark = state.theme === "dark";
    return {
      paper: "rgba(0,0,0,0)",
      plot: "rgba(0,0,0,0)",
      text: dark ? "#e2e8f0" : "#0f172a",
      muted: dark ? "#94a3b8" : "#64748b",
      grid: dark ? "#1e293b" : "#e2e8f0",
      surface: dark ? "#111827" : "#ffffff",
      pareto: dark ? "#f8fafc" : "#0f172a",
      paretoBaseline: dark ? "#94a3b8" : "#64748b",
      refLine: dark ? "#f59e0b" : "#dc2626",
    };
  }

  function tasksValue(row, metric = state.metric) {
    return metric === "token"
      ? row.token_weighted_tasks_per_month
      : row.tasks_per_month_primary;
  }

  // Compute upper-right Pareto frontier (maximize both X=tasks and Y=intelligence)
  function computePareto(points, metric = state.metric) {
    const valid = points.filter(
      (p) => tasksValue(p, metric) != null && p.aa_intelligence != null
    );
    // Sort by X descending, then Y descending
    const sorted = [...valid].sort((a, b) => {
      const dx = tasksValue(b, metric) - tasksValue(a, metric);
      if (Math.abs(dx) > 1e-9) return dx;
      return b.aa_intelligence - a.aa_intelligence;
    });
    const frontier = [];
    let maxY = -Infinity;
    for (const p of sorted) {
      if (p.aa_intelligence > maxY + 1e-9) {
        frontier.push(p);
        maxY = p.aa_intelligence;
      }
    }
    // Return sorted by X ascending for clean line drawing
    return frontier.sort(
      (a, b) => tasksValue(a, metric) - tasksValue(b, metric)
    );
  }

  function fmtNum(v, decimals = 1) {
    if (v == null || Number.isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  }

  function fmtInt(v) {
    if (v == null || Number.isNaN(v)) return "—";
    return Math.round(v).toLocaleString("en-US");
  }

  function fmtUsd(v, decimals = 0) {
    if (v == null || Number.isNaN(v)) return "—";
    return (
      "$" +
      Number(v).toLocaleString("en-US", {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      })
    );
  }

  function buildHoverText(p) {
    const tVal = tasksValue(p);
    const isPrimary = state.metric === "primary";
    const ciLine =
      isPrimary && p.tasks_mo_ci_low != null && p.tasks_mo_ci_high != null
        ? `<br>95% CI: <b>${fmtInt(p.tasks_mo_ci_low)}–${fmtInt(p.tasks_mo_ci_high)}</b> tasks/mo`
        : "";
    const apiLine =
      p.implied_pool_usd_per_month != null
        ? `<br>Implied Pool API Value: <b>${fmtUsd(p.implied_pool_usd_per_month, 0)}/mo</b>` +
          (p.api_multiplier ? ` (${p.api_multiplier.toFixed(1)}× Pro)` : "")
        : "";
    const harnessLine =
      !isPrimary && p.harness_ratio != null
        ? `<br>Primary / Token-Weighted: <b>${p.harness_ratio.toFixed(2)}×</b>`
        : "";
    const src = p.measured
      ? `Empirically measured (Cycle ${p.source_cycle_id}, ${p.source_date})`
      : `Effort-scaled from Cycle ${p.source_cycle_id} (${p.source_config_label})`;

    return (
      `<b>${p.family_label} (${p.effort})</b>` +
      `<br>Pool: <code>${p.weekly_bucket}</code>` +
      `<br>AA Intelligence v5: <b>${fmtNum(p.aa_intelligence, 1)}</b>` +
      `<br>${isPrimary ? "Est. Tasks/mo (Real agy)" : "Token-Weighted Tasks/mo"}: <b>${fmtNum(tVal, 1)}</b>` +
      ciLine +
      `<br>AA Cost/Task: <b>${fmtUsd(p.aa_ii_cost_per_task, 4)}</b>` +
      apiLine +
      harnessLine +
      `<br><span style="opacity:0.8">${src}</span>`
    );
  }

  function getFilteredProjections() {
    return state.data.projections.filter((p) => {
      if (!state.pools.has(p.weekly_bucket)) return false;
      if (state.era === "current" && p.era !== "current") return false;
      if (state.era === "nov7" && p.era !== "nov7") return false;
      return true;
    });
  }

  function renderParetoChart() {
    const colors = getThemeColors();
    const filtered = getFilteredProjections();
    const traces = [];

    // Group by family_key
    const families = new Map();
    for (const p of filtered) {
      if (!families.has(p.family_key)) families.set(p.family_key, []);
      families.get(p.family_key).push(p);
    }

    // Effort curves + markers per family
    for (const [fk, rows] of families.entries()) {
      const sorted = [...rows].sort((a, b) => a.effort_order - b.effort_order);
      const famLabel = sorted[0].family_label;
      const famColor = sorted[0].color;
      const isNov7 = sorted[0].era === "nov7";

      if (state.showCurves && sorted.length > 1) {
        traces.push({
          type: "scatter",
          mode: "lines",
          name: famLabel,
          legendgroup: fk,
          showlegend: false,
          x: sorted.map((r) => tasksValue(r)),
          y: sorted.map((r) => r.aa_intelligence),
          line: {
            color: famColor,
            width: isNov7 ? 1.5 : 2,
            dash: isNov7 ? "dot" : "solid",
          },
          opacity: isNov7 && state.era === "compare" ? 0.55 : 0.7,
          hoverinfo: "skip",
        });
      }

      const measured = sorted.filter((r) => r.measured);
      const scaled = sorted.filter((r) => !r.measured);

      if (measured.length > 0) {
        const errX =
          state.showCI && state.metric === "primary"
            ? {
                type: "data",
                symmetric: false,
                array: measured.map((r) =>
                  Math.max(0, (r.tasks_mo_ci_high ?? tasksValue(r)) - tasksValue(r))
                ),
                arrayminus: measured.map((r) =>
                  Math.max(0, tasksValue(r) - (r.tasks_mo_ci_low ?? tasksValue(r)))
                ),
                color: famColor,
                thickness: 1.4,
                width: 4,
              }
            : undefined;

        traces.push({
          type: "scatter",
          mode: "markers",
          name: `${famLabel}${isNov7 ? " (Nov 7)" : ""}`,
          legendgroup: fk,
          showlegend: true,
          x: measured.map((r) => tasksValue(r)),
          y: measured.map((r) => r.aa_intelligence),
          text: measured.map(buildHoverText),
          hovertemplate: "%{text}<extra></extra>",
          error_x: errX,
          marker: {
            size: 11,
            color: famColor,
            line: { color: colors.surface, width: 1.5 },
            opacity: isNov7 && state.era === "compare" ? 0.72 : 0.96,
          },
        });
      }

      if (scaled.length > 0) {
        traces.push({
          type: "scatter",
          mode: "markers",
          name: `${famLabel}${isNov7 ? " (Nov 7)" : ""}`,
          legendgroup: fk,
          showlegend: measured.length === 0,
          x: scaled.map((r) => tasksValue(r)),
          y: scaled.map((r) => r.aa_intelligence),
          text: scaled.map(buildHoverText),
          hovertemplate: "%{text}<extra></extra>",
          marker: {
            size: 9.5,
            color: colors.surface,
            line: { color: famColor, width: 2.2 },
            opacity: isNov7 && state.era === "compare" ? 0.65 : 0.92,
          },
        });
      }
    }

    // Pareto frontier lines
    const annotations = [];
    if (state.showPareto) {
      if (state.era === "compare") {
        const nov7Pts = filtered.filter((p) => p.era === "nov7");
        const nov7Frontier = computePareto(nov7Pts);
        if (nov7Frontier.length >= 2) {
          traces.push({
            type: "scatter",
            mode: "lines",
            name: "Nov 7 Baseline Frontier",
            x: nov7Frontier.map((p) => tasksValue(p)),
            y: nov7Frontier.map((p) => p.aa_intelligence),
            line: { color: colors.paretoBaseline, width: 2, dash: "dash" },
            hoverinfo: "skip",
          });
        }
      }

      const currentPoolPts =
        state.era === "nov7"
          ? filtered
          : filtered.filter((p) => p.era === "current");
      const currentFrontier = computePareto(
        currentPoolPts.length ? currentPoolPts : filtered
      );

      if (currentFrontier.length >= 2) {
        traces.push({
          type: "scatter",
          mode: "lines",
          name:
            state.era === "nov7"
              ? "Nov 7 Pareto Frontier"
              : "Current Pareto Frontier",
          x: currentFrontier.map((p) => tasksValue(p)),
          y: currentFrontier.map((p) => p.aa_intelligence),
          line: { color: colors.pareto, width: 2.4 },
          hoverinfo: "skip",
        });

        // Label frontier points cleanly
        for (const p of currentFrontier) {
          const shortFam = p.family_label
            .replace("Gemini 4 ", "")
            .replace("Gemini 3.8 ", "")
            .replace("Claude ", "");
          annotations.push({
            x: state.xScale === "log" ? Math.log10(tasksValue(p)) : tasksValue(p),
            y: p.aa_intelligence,
            text: `${shortFam} ${p.effort}`,
            showarrow: false,
            xanchor: "left",
            yanchor: "bottom",
            xshift: 7,
            yshift: 4,
            font: { size: 11, color: colors.text, family: "Inter, sans-serif" },
            bgcolor: colors.surface,
            opacity: 0.88,
          });
        }
      }
    }

    const layout = {
      paper_bgcolor: colors.paper,
      plot_bgcolor: colors.plot,
      font: { color: colors.text, family: "Inter, sans-serif", size: 12 },
      margin: { t: 22, r: 24, b: 56, l: 58 },
      hovermode: "closest",
      xaxis: {
        title: {
          text:
            state.metric === "primary"
              ? "Projected AA Tasks / Month (Real agy Multi-Turn)"
              : "Theoretical Token-Weighted AA Tasks / Month",
          standoff: 12,
        },
        type: state.xScale,
        gridcolor: colors.grid,
        zerolinecolor: colors.grid,
        tickfont: { color: colors.muted },
      },
      yaxis: {
        title: {
          text: "Artificial Analysis Intelligence Index v5",
          standoff: 10,
        },
        gridcolor: colors.grid,
        zerolinecolor: colors.grid,
        tickfont: { color: colors.muted },
        range: [6, 66],
      },
      legend: {
        orientation: "h",
        yanchor: "top",
        y: -0.18,
        xanchor: "left",
        x: 0,
        font: { size: 11.5 },
      },
      annotations,
    };

    Plotly.react("pareto-chart", traces, layout, {
      responsive: true,
      displaylogo: false,
      modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
    });
  }

  function renderApiChart() {
    const colors = getThemeColors();
    const rows = [...state.data.measured_cycles];

    const famColorFor = (r) => {
      const id = r.model_id || "";
      if (id.includes("neon")) return "#2563eb";
      if (id.includes("hydrogen")) return "#0891b2";
      if (id.includes("argon")) return "#7c3aed";
      if (id.includes("3.8-flash")) return "#64748b";
      if (id.includes("opus")) return "#d97706";
      if (id.includes("sonnet")) return "#ea580c";
      return "#16a34a";
    };

    const metricFn = (r) => {
      if (state.apiMetric === "usd") return r.implied_pool_usd_per_month;
      if (state.apiMetric === "mult")
        return r.implied_pool_usd_per_month / CFG.subscriptionPriceUsd;
      // Burn pp per $1 API spent
      return r.weekly_burn_pp / r.api_cost_usd;
    };

    const sorted = rows.sort((a, b) => metricFn(a) - metricFn(b));
    const labels = sorted.map(
      (r) => `c${r.cycle_id} · ${r.config_label} (${r.weekly_bucket})`
    );
    const values = sorted.map(metricFn);

    let errX;
    if (state.apiMetric === "usd") {
      errX = {
        type: "data",
        symmetric: false,
        array: sorted.map((r) =>
          Math.max(0, r.implied_pool_ci_high - r.implied_pool_usd_per_month)
        ),
        arrayminus: sorted.map((r) =>
          Math.max(0, r.implied_pool_usd_per_month - r.implied_pool_ci_low)
        ),
        color: colors.muted,
        thickness: 1.4,
      };
    } else if (state.apiMetric === "mult") {
      errX = {
        type: "data",
        symmetric: false,
        array: sorted.map(
          (r) =>
            Math.max(0, r.implied_pool_ci_high - r.implied_pool_usd_per_month) /
            CFG.subscriptionPriceUsd
        ),
        arrayminus: sorted.map(
          (r) =>
            Math.max(0, r.implied_pool_usd_per_month - r.implied_pool_ci_low) /
            CFG.subscriptionPriceUsd
        ),
        color: colors.muted,
        thickness: 1.4,
      };
    }

    const trace = {
      type: "bar",
      orientation: "h",
      y: labels,
      x: values,
      error_x: errX,
      marker: {
        color: sorted.map(famColorFor),
        opacity: 0.9,
      },
      text: sorted.map((r, i) => {
        const v = values[i];
        if (state.apiMetric === "usd") return fmtUsd(v, v < 10 ? 2 : 0);
        if (state.apiMetric === "mult") return `${v.toFixed(1)}×`;
        return `${v.toFixed(1)} pp/$`;
      }),
      textposition: "outside",
      cliponaxis: false,
      hovertemplate: sorted.map(
        (r) =>
          `<b>${r.aa_name}</b> (Cycle ${r.cycle_id}, ${r.date})<br>` +
          `Pool: <code>${r.weekly_bucket}</code><br>` +
          `Implied Pool Value: <b>${fmtUsd(r.implied_pool_usd_per_month, r.implied_pool_usd_per_month < 10 ? 2 : 0)}/mo</b> ` +
          `(95% CI: ${fmtUsd(r.implied_pool_ci_low, r.implied_pool_ci_low < 10 ? 2 : 0)}–${fmtUsd(r.implied_pool_ci_high, r.implied_pool_ci_high < 10 ? 2 : 0)})<br>` +
          `Run API Cost: ${fmtUsd(r.api_cost_usd, 3)} · Burn: ${r.weekly_burn_pp.toFixed(2)} pp<br>` +
          `SWE-bench Resolved: ${r.resolved_valid}/${r.attempted_valid}<extra></extra>`
      ),
    };

    const shapes = [];
    const annotations = [];
    if (state.apiMetric === "usd" || state.apiMetric === "mult") {
      const refVal = state.apiMetric === "usd" ? CFG.subscriptionPriceUsd : 1.0;
      shapes.push({
        type: "line",
        x0: refVal,
        x1: refVal,
        y0: -0.5,
        y1: labels.length - 0.5,
        line: { color: colors.refLine, width: 1.6, dash: "dash" },
      });
      annotations.push({
        x: refVal,
        y: labels.length - 0.5,
        text:
          state.apiMetric === "usd"
            ? `$${CFG.subscriptionPriceUsd} Pro Price`
            : "1.0× Break-even",
        showarrow: false,
        xanchor: "left",
        yanchor: "bottom",
        xshift: 5,
        font: { size: 11, color: colors.refLine },
      });
    }

    const xTitles = {
      usd: "Implied Monthly API Value at 100% Quota Utilization (USD / mo)",
      mult: `Implied API Value Multiplier vs. $${CFG.subscriptionPriceUsd}/mo Google AI Pro`,
      burn: "Weekly Quota Burn per $1 of API Spend (pp / $1 — lower is more generous)",
    };

    const layout = {
      paper_bgcolor: colors.paper,
      plot_bgcolor: colors.plot,
      font: { color: colors.text, family: "Inter, sans-serif", size: 12 },
      margin: { t: 24, r: 64, b: 50, l: 245 },
      xaxis: {
        title: { text: xTitles[state.apiMetric], standoff: 10 },
        gridcolor: colors.grid,
        zerolinecolor: colors.grid,
        tickfont: { color: colors.muted },
      },
      yaxis: {
        automargin: true,
        tickfont: { size: 11.5, family: "JetBrains Mono, monospace", color: colors.text },
      },
      shapes,
      annotations,
    };

    Plotly.react("api-value-chart", [trace], layout, {
      responsive: true,
      displaylogo: false,
      modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
    });
  }

  // Interactive Monthly Quota Planner
  function initPlanner() {
    const byId = {};
    for (const p of state.data.projections) byId[p.id] = p;

    const slots = [
      { key: "neon-med", slider: "sl-neon-med", val: "val-neon-med", proj: byId["gemini-4-neon@gemini-weekly:medium"] },
      { key: "neon-low", slider: "sl-neon-low", val: "val-neon-low", proj: byId["gemini-4-neon@gemini-weekly:low"] },
      { key: "neon-high", slider: "sl-neon-high", val: "val-neon-high", proj: byId["gemini-4-neon@gemini-weekly:high"] },
      { key: "hydro-med", slider: "sl-hydro-med", val: "val-hydro-med", proj: byId["gemini-4-hydrogen@gemini-weekly:medium"] },
      { key: "hydro-low", slider: "sl-hydro-low", val: "val-hydro-low", proj: byId["gemini-4-hydrogen@gemini-weekly:low"] },
    ];
    const opusMed = byId["claude-opus-5-5@3p-weekly:medium"];

    const presets = {
      "neon-balanced": { util: 75, "neon-med": 50, "neon-low": 30, "neon-high": 20, "hydro-med": 0, "hydro-low": 0 },
      "neon-frontier": { util: 75, "neon-med": 30, "neon-low": 0, "neon-high": 70, "hydro-med": 0, "hydro-low": 0 },
      "high-volume": { util: 75, "neon-med": 15, "neon-low": 55, "neon-high": 0, "hydro-med": 0, "hydro-low": 30 },
      "hydrogen-only": { util: 75, "neon-med": 0, "neon-low": 0, "neon-high": 0, "hydro-med": 70, "hydro-low": 30 },
    };

    const utilSlider = document.getElementById("sl-util");
    const utilLabel = document.getElementById("val-util");
    const chk3p = document.getElementById("chk-include-3p");

    function updatePlanner() {
      const utilFrac = Number(utilSlider.value) / 100;
      utilLabel.textContent = `${utilSlider.value}%`;

      const rawWeights = slots.map((s) => Number(document.getElementById(s.slider).value));
      const sumW = rawWeights.reduce((a, b) => a + b, 0);

      let totalTasks = 0;
      let weightedIntelSum = 0;
      let totalApiUsd = 0;
      const lines = [];

      slots.forEach((s, idx) => {
        const share = sumW > 0 ? rawWeights[idx] / sumW : 0;
        document.getElementById(s.val).textContent = `${Math.round(share * 100)}%`;
        if (share > 0 && s.proj) {
          const tasks = s.proj.tasks_per_month_primary * utilFrac * share;
          const apiUsd = tasks * s.proj.aa_ii_cost_per_task;
          totalTasks += tasks;
          weightedIntelSum += tasks * s.proj.aa_intelligence;
          totalApiUsd += apiUsd;
          lines.push(
            `<li><strong>${s.proj.family_label} (${s.proj.effort})</strong>: ` +
              `<span class="tabular">${fmtNum(tasks, 1)} tasks</span> · ` +
              `<span class="tabular">${fmtUsd(apiUsd, 0)} API eq.</span></li>`
          );
        }
      });

      if (chk3p.checked && opusMed) {
        const tasks3p = opusMed.tasks_per_month_primary * utilFrac;
        const api3p = tasks3p * opusMed.aa_ii_cost_per_task;
        totalTasks += tasks3p;
        weightedIntelSum += tasks3p * opusMed.aa_intelligence;
        totalApiUsd += api3p;
        lines.push(
          `<li><strong>Claude Opus 5.5 (medium, 3p-weekly)</strong>: ` +
            `<span class="tabular">${fmtNum(tasks3p, 1)} tasks</span> · ` +
            `<span class="tabular">${fmtUsd(api3p, 0)} API eq.</span></li>`
        );
      }

      const avgIntel = totalTasks > 0 ? weightedIntelSum / totalTasks : 0;
      const mult = totalApiUsd / CFG.subscriptionPriceUsd;
      const netSavings = Math.max(0, totalApiUsd - CFG.subscriptionPriceUsd);

      document.getElementById("res-tasks").textContent = fmtInt(totalTasks);
      document.getElementById("res-intel").textContent = fmtNum(avgIntel, 1);
      document.getElementById("res-api-usd").textContent = fmtUsd(totalApiUsd, 0);
      document.getElementById("res-mult").textContent = `${mult.toFixed(1)}×`;

      document.getElementById("res-breakdown").innerHTML =
        `<div>At <strong>${utilSlider.value}%</strong> weekly utilization, this mix saves roughly <strong>${fmtUsd(netSavings, 0)}/mo</strong> vs. direct API billing (after $${CFG.subscriptionPriceUsd}/mo Pro).</div>` +
        `<ul>${lines.join("")}</ul>`;
    }

    document.querySelectorAll("#planner-presets button").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#planner-presets button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const p = presets[btn.dataset.preset];
        if (!p) return;
        utilSlider.value = p.util;
        slots.forEach((s) => {
          document.getElementById(s.slider).value = p[s.key];
        });
        updatePlanner();
      });
    });

    utilSlider.addEventListener("input", updatePlanner);
    chk3p.addEventListener("change", updatePlanner);
    slots.forEach((s) => {
      document.getElementById(s.slider).addEventListener("input", () => {
        document.querySelectorAll("#planner-presets button").forEach((b) => b.classList.remove("active"));
        updatePlanner();
      });
    });

    updatePlanner();
  }

  // Sortable Data Table
  function renderTable() {
    const thead = document.querySelector("#benchmark-table thead");
    const tbody = document.querySelector("#benchmark-table tbody");
    const q = state.tableSearch.trim().toLowerCase();

    const currentParetoIds = new Set(
      computePareto(
        state.data.projections.filter((p) => p.era === "current"),
        "primary"
      ).map((p) => p.id)
    );

    const columns =
      state.tableView === "projections"
        ? [
            { key: "family_label", label: "Model", align: "left" },
            { key: "effort", label: "Effort", align: "left" },
            { key: "weekly_bucket", label: "Pool", align: "left" },
            { key: "measured", label: "Source", align: "left" },
            { key: "aa_intelligence", label: "AA Intel", align: "right" },
            { key: "tasks_per_month_primary", label: "Tasks/mo (Real)", align: "right" },
            { key: "token_weighted_tasks_per_month", label: "Tasks/mo (Token)", align: "right" },
            { key: "aa_ii_cost_per_task", label: "AA $/Task", align: "right" },
            { key: "implied_pool_usd_per_month", label: "Implied API $/mo", align: "right" },
            { key: "api_multiplier", label: "× Pro", align: "right" },
          ]
        : [
            { key: "cycle_id", label: "Cycle", align: "right" },
            { key: "date", label: "Date", align: "left" },
            { key: "config_label", label: "Config", align: "left" },
            { key: "weekly_bucket", label: "Pool", align: "left" },
            { key: "aa_intelligence", label: "AA Intel", align: "right" },
            { key: "resolved_valid", label: "SWE Resolved", align: "right" },
            { key: "api_cost_usd", label: "Run API $", align: "right" },
            { key: "weekly_burn_pp", label: "Burn (pp)", align: "right" },
            { key: "implied_pool_usd_per_month", label: "Implied $/mo (95% CI)", align: "right" },
            { key: "tasks_per_month_attempted", label: "Tasks/mo (95% CI)", align: "right" },
          ];

    let rows =
      state.tableView === "projections"
        ? [...state.data.projections]
        : [...state.data.measured_cycles];

    if (q) {
      rows = rows.filter((r) =>
        JSON.stringify(r).toLowerCase().includes(q)
      );
    }

    const { key, dir } = state.tableSort;
    rows.sort((a, b) => {
      const av = a[key];
      const bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        return dir === "asc" ? av - bv : bv - av;
      }
      return dir === "asc"
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });

    thead.innerHTML =
      "<tr>" +
      columns
        .map((c) => {
          const arrow =
            state.tableSort.key === c.key
              ? state.tableSort.dir === "asc"
                ? " ▲"
                : " ▼"
              : "";
          return `<th data-key="${c.key}" class="${c.align === "right" ? "num-col" : ""}">${c.label}${arrow}</th>`;
        })
        .join("") +
      "</tr>";

    thead.querySelectorAll("th").forEach((th) => {
      th.addEventListener("click", () => {
        const k = th.dataset.key;
        if (state.tableSort.key === k) {
          state.tableSort.dir = state.tableSort.dir === "asc" ? "desc" : "asc";
        } else {
          state.tableSort = { key: k, dir: "desc" };
        }
        renderTable();
      });
    });

    if (state.tableView === "projections") {
      tbody.innerHTML = rows
        .map((r) => {
          const isPareto = currentParetoIds.has(r.id);
          return (
            `<tr>` +
            `<td><span class="model-dot" style="background:${r.color}"></span><strong>${r.family_label}</strong>${isPareto ? '<span class="badge badge-pareto">Pareto</span>' : ""}</td>` +
            `<td class="mono">${r.effort}</td>` +
            `<td class="mono">${r.weekly_bucket}</td>` +
            `<td>${
              r.measured
                ? `<span class="badge badge-measured">Measured c${r.source_cycle_id}</span>`
                : `<span class="badge badge-scaled">Scaled c${r.source_cycle_id}</span>`
            }</td>` +
            `<td class="num-col"><strong>${fmtNum(r.aa_intelligence, 1)}</strong></td>` +
            `<td class="num-col"><strong>${fmtInt(r.tasks_per_month_primary)}</strong></td>` +
            `<td class="num-col">${fmtInt(r.token_weighted_tasks_per_month)}</td>` +
            `<td class="num-col">${fmtUsd(r.aa_ii_cost_per_task, 4)}</td>` +
            `<td class="num-col">${fmtUsd(r.implied_pool_usd_per_month, r.implied_pool_usd_per_month < 10 ? 2 : 0)}</td>` +
            `<td class="num-col">${r.api_multiplier != null ? `${r.api_multiplier.toFixed(1)}×` : "—"}</td>` +
            `</tr>`
          );
        })
        .join("");
    } else {
      tbody.innerHTML = rows
        .map(
          (r) =>
            `<tr>` +
            `<td class="num-col mono">${r.cycle_id}</td>` +
            `<td class="mono">${r.date}</td>` +
            `<td class="mono"><strong>${r.config_label}</strong></td>` +
            `<td class="mono">${r.weekly_bucket}</td>` +
            `<td class="num-col"><strong>${fmtNum(r.aa_intelligence, 1)}</strong></td>` +
            `<td class="num-col">${r.resolved_valid}/${r.attempted_valid}</td>` +
            `<td class="num-col">${fmtUsd(r.api_cost_usd, 3)}</td>` +
            `<td class="num-col">${fmtNum(r.weekly_burn_pp, 2)}</td>` +
            `<td class="num-col"><strong>${fmtUsd(r.implied_pool_usd_per_month, r.implied_pool_usd_per_month < 10 ? 2 : 0)}</strong> <span style="opacity:0.7">(${fmtUsd(r.implied_pool_ci_low, r.implied_pool_ci_low < 10 ? 2 : 0)}–${fmtUsd(r.implied_pool_ci_high, r.implied_pool_ci_high < 10 ? 2 : 0)})</span></td>` +
            `<td class="num-col"><strong>${fmtInt(r.tasks_per_month_attempted)}</strong> <span style="opacity:0.7">(${fmtInt(r.tasks_mo_ci_low)}–${fmtInt(r.tasks_mo_ci_high)})</span></td>` +
            `</tr>`
        )
        .join("");
    }
  }

  function downloadCurrentCsv() {
    const rows =
      state.tableView === "projections"
        ? state.data.projections
        : state.data.measured_cycles;
    if (!rows.length) return;
    const keys = Object.keys(rows[0]);
    const csv = [
      keys.join(","),
      ...rows.map((r) =>
        keys
          .map((k) => {
            const val = r[k] == null ? "" : String(r[k]);
            return /[",\n]/.test(val) ? `"${val.replace(/"/g, '""')}"` : val;
          })
          .join(",")
      ),
    ].join("\n");

    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `subscriptionbench_${state.tableView}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function wireControls() {
    const wireSegmented = (id, onChange) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => {
          el.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          onChange(btn.dataset.value);
        });
      });
    };

    wireSegmented("metric-toggle", (v) => {
      state.metric = v;
      renderParetoChart();
    });
    wireSegmented("era-toggle", (v) => {
      state.era = v;
      renderParetoChart();
    });
    wireSegmented("scale-toggle", (v) => {
      state.xScale = v;
      renderParetoChart();
    });
    wireSegmented("api-metric-toggle", (v) => {
      state.apiMetric = v;
      renderApiChart();
    });
    wireSegmented("table-view-toggle", (v) => {
      state.tableView = v;
      state.tableSort =
        v === "projections"
          ? { key: "aa_intelligence", dir: "desc" }
          : { key: "cycle_id", dir: "asc" };
      renderTable();
    });

    document.querySelectorAll("#pool-filters .pool-pill").forEach((pill) => {
      const input = pill.querySelector("input");
      input.addEventListener("change", () => {
        if (input.checked) {
          state.pools.add(input.value);
          pill.classList.add("active");
        } else {
          state.pools.delete(input.value);
          pill.classList.remove("active");
        }
        renderParetoChart();
      });
    });

    document.getElementById("toggle-pareto").addEventListener("change", (e) => {
      state.showPareto = e.target.checked;
      renderParetoChart();
    });
    document.getElementById("toggle-curves").addEventListener("change", (e) => {
      state.showCurves = e.target.checked;
      renderParetoChart();
    });
    document.getElementById("toggle-ci").addEventListener("change", (e) => {
      state.showCI = e.target.checked;
      renderParetoChart();
    });

    document.getElementById("table-search").addEventListener("input", (e) => {
      state.tableSearch = e.target.value;
      renderTable();
    });
    document.getElementById("btn-download-csv").addEventListener("click", downloadCurrentCsv);

    document.getElementById("theme-toggle").addEventListener("click", () => {
      applyTheme(state.theme === "dark" ? "light" : "dark");
    });
  }

  async function init() {
    applyConfigLinks();
    applyTheme(state.theme);
    wireControls();

    const resp = await fetch("./data/benchmark_data.json");
    if (!resp.ok) {
      throw new Error(`Failed to load benchmark_data.json (${resp.status})`);
    }
    state.data = await resp.json();

    const snapDate = state.data.meta.aa_snapshot.slice(0, 10);
    document.getElementById("meta-snapshot").textContent =
      `${state.data.meta.aa_index_version} · Snapshot ${snapDate} · ${state.data.meta.measured_cycle_count} empirical cycles`;
    document.getElementById("footer-date").textContent = snapDate;

    renderParetoChart();
    renderApiChart();
    initPlanner();
    renderTable();
  }

  window.addEventListener("DOMContentLoaded", () => {
    init().catch((err) => {
      console.error(err);
      const el = document.getElementById("pareto-chart");
      if (el) {
        el.innerHTML = `<p style="padding:20px;color:#dc2626">Failed to load benchmark data: ${err.message}. Serve <code>site/</code> via a local HTTP server (e.g. <code>python3 -m http.server -d site 8000</code>).</p>`;
      }
    });
  });
})();

(() => {
  "use strict";

  const FRIENDLY_POOL = {
    "gemini-weekly": "Google AI Pro (Gemini)",
    "gemini-argon-wtu-weekly": "Google AI Pro (Legacy Argon)",
    "3p-weekly": "Google AI Pro (Claude Bonus)",
    "codex-30d": "Codex Free",
  };

  const EFFORT_LABEL = {
    "non-reasoning": "Non-reasoning",
    low: "Low",
    medium: "Medium",
    high: "High",
    xhigh: "xHigh",
    max: "Max",
  };

  // Curated default set for the clean "Key Models" bar chart view
  const KEY_MODEL_IDS = new Set([
    "gemini-4-hydrogen@gemini-weekly:low",
    "gemini-4-neon@gemini-weekly:low",
    "gemini-4-hydrogen@gemini-weekly:medium",
    "gemini-4-neon@gemini-weekly:medium",
    "gemini-4-neon@gemini-weekly:high",
    "gemini-4-neon@gemini-weekly:xhigh",
    "claude-sonnet-5-5@3p-weekly:medium",
    "claude-opus-5-5@3p-weekly:medium",
    "gpt-6-luna@codex-30d:high",
  ]);

  const state = {
    data: null,
    theme: localStorage.getItem("subbench-theme") || "light",
    metric: "primary", // "primary" | "token"
    era: "compare", // "compare" | "current" | "nov7"
    scope: "pro", // "pro" (All Antigravity) | "gemini" (Gemini Pool Only)
    includeCodexFree: false, // opt-in for GPT-6 Luna (Codex Free)
    xScale: "log", // "log" | "linear"
    showCurves: true,
    showCI: false,
    barMetric: "tasks", // "tasks" | "api"
    barFilter: "defaults", // "defaults" | "current" | "all"
    tableView: "projections", // "projections" | "measured"
    tableSearch: "",
    tableSort: { key: "aa_intelligence", dir: "desc" },
  };

  const CFG = window.SUBBENCH_CONFIG || {
    repoUrl: "https://github.com/data-john/SubscriptionBench",
    kofiUrl: "https://ko-fi.com/datajohn",
    subscriptionPriceUsd: 19.99,
  };

  function friendlyPool(pool) {
    return FRIENDLY_POOL[pool] || pool;
  }

  function friendlyEffort(effort) {
    return EFFORT_LABEL[effort] || effort;
  }

  function friendlyMeasuredName(r) {
    const clean = (r.aa_name || r.config_label).replace(", Default Fallback", "");
    const suffix = r.weekly_bucket === "codex-30d" ? " · Codex Free" : "";
    const eraTag = r.era === "nov7" ? " (Nov 7)" : "";
    return `${clean}${eraTag}${suffix}`;
  }

  function applyConfigLinks() {
    const setHref = (id, url) => {
      const el = document.getElementById(id);
      if (el && url) el.href = url;
    };
    setHref("nav-github", CFG.repoUrl);
    setHref("footer-github", CFG.repoUrl);
    setHref("method-github", CFG.repoUrl);
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
      renderBarChart();
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

  function computePareto(points, metric = state.metric) {
    const valid = points.filter(
      (p) => tasksValue(p, metric) != null && p.aa_intelligence != null
    );
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

  // Concise, non-technical hover tooltip (4 lines max)
  function buildCleanHover(p) {
    const tVal = tasksValue(p);
    const apiLine =
      p.implied_pool_usd_per_month != null
        ? `<br>API-equivalent pool: <b>~${fmtUsd(p.implied_pool_usd_per_month, p.implied_pool_usd_per_month < 10 ? 2 : 0)}/mo</b>`
        : "";
    return (
      `<b>${p.family_label} (${friendlyEffort(p.effort)})</b>` +
      `<br>${friendlyPool(p.weekly_bucket)}` +
      `<br>Intelligence: <b>${fmtNum(p.aa_intelligence, 1)}</b>  ·  Capacity: <b>~${fmtInt(tVal)} tasks/mo</b>` +
      apiLine
    );
  }

  function getFilteredProjections() {
    return state.data.projections.filter((p) => {
      const isCodex = p.weekly_bucket === "codex-30d";
      if (isCodex) {
        return state.includeCodexFree && state.era !== "nov7";
      }
      if (state.era === "current" && p.era !== "current") return false;
      if (state.era === "nov7" && p.era !== "nov7") return false;
      if (
        state.scope === "gemini" &&
        p.weekly_bucket !== "gemini-weekly" &&
        p.weekly_bucket !== "gemini-argon-wtu-weekly"
      ) {
        return false;
      }
      return true;
    });
  }

  function renderParetoChart() {
    const colors = getThemeColors();
    const filtered = getFilteredProjections();
    const traces = [];

    const families = new Map();
    for (const p of filtered) {
      if (!families.has(p.family_key)) families.set(p.family_key, []);
      families.get(p.family_key).push(p);
    }

    for (const [fk, rows] of families.entries()) {
      const sorted = [...rows].sort((a, b) => a.effort_order - b.effort_order);
      const famLabel = sorted[0].family_label;
      const famColor = sorted[0].color;
      const isNov7 = sorted[0].era === "nov7";
      const legendName =
        sorted[0].weekly_bucket === "codex-30d"
          ? `${famLabel} (Codex Free)`
          : isNov7
          ? `${famLabel} (Nov 7)`
          : famLabel;

      if (state.showCurves && sorted.length > 1) {
        traces.push({
          type: "scatter",
          mode: "lines",
          name: legendName,
          legendgroup: fk,
          showlegend: false,
          x: sorted.map((r) => tasksValue(r)),
          y: sorted.map((r) => r.aa_intelligence),
          line: {
            color: famColor,
            width: isNov7 ? 1.5 : 2,
            dash: isNov7 ? "dot" : "solid",
          },
          opacity: isNov7 && state.era === "compare" ? 0.5 : 0.72,
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
          name: legendName,
          legendgroup: fk,
          showlegend: true,
          x: measured.map((r) => tasksValue(r)),
          y: measured.map((r) => r.aa_intelligence),
          text: measured.map(buildCleanHover),
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
          name: legendName,
          legendgroup: fk,
          showlegend: measured.length === 0,
          x: scaled.map((r) => tasksValue(r)),
          y: scaled.map((r) => r.aa_intelligence),
          text: scaled.map(buildCleanHover),
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

    const annotations = [];
    if (state.era === "compare") {
      const nov7Pts = filtered.filter((p) => p.era === "nov7");
      const nov7Frontier = computePareto(nov7Pts);
      if (nov7Frontier.length >= 2) {
        traces.push({
          type: "scatter",
          mode: "lines",
          name: "Nov 7 Frontier",
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
        name: state.era === "nov7" ? "Nov 7 Frontier" : "Best-Value Frontier",
        x: currentFrontier.map((p) => tasksValue(p)),
        y: currentFrontier.map((p) => p.aa_intelligence),
        line: { color: colors.pareto, width: 2.4 },
        hoverinfo: "skip",
      });

      for (const p of currentFrontier) {
        const shortFam = p.family_label
          .replace("Gemini 4 ", "")
          .replace("Gemini 3.8 ", "")
          .replace("Claude ", "");
        annotations.push({
          x: state.xScale === "log" ? Math.log10(tasksValue(p)) : tasksValue(p),
          y: p.aa_intelligence,
          text: `${shortFam} ${friendlyEffort(p.effort)}`,
          showarrow: false,
          xanchor: "left",
          yanchor: "bottom",
          xshift: 7,
          yshift: 4,
          font: { size: 11, color: colors.text, family: "Inter, sans-serif" },
          bgcolor: colors.surface,
          opacity: 0.9,
        });
      }
    }

    const layout = {
      paper_bgcolor: colors.paper,
      plot_bgcolor: colors.plot,
      font: { color: colors.text, family: "Inter, sans-serif", size: 12 },
      margin: { t: 20, r: 24, b: 56, l: 56 },
      hovermode: "closest",
      hoverlabel: {
        bgcolor: colors.surface,
        bordercolor: colors.grid,
        font: { color: colors.text, family: "Inter, sans-serif", size: 12.5 },
      },
      xaxis: {
        title: {
          text:
            state.metric === "primary"
              ? "Estimated Tasks per Month"
              : "Theoretical Token-Only Tasks per Month",
          standoff: 12,
        },
        type: state.xScale,
        gridcolor: colors.grid,
        zerolinecolor: colors.grid,
        tickfont: { color: colors.muted },
      },
      yaxis: {
        title: {
          text: "Intelligence Score (Artificial Analysis v5)",
          standoff: 10,
        },
        gridcolor: colors.grid,
        zerolinecolor: colors.grid,
        tickfont: { color: colors.muted },
        range: state.includeCodexFree ? [6, 66] : [22, 66],
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

  // Chart 2: Clean Interactive Bar Chart (Tasks / Month OR Equivalent API Value)
  function renderBarChart() {
    const colors = getThemeColors();
    const titleEl = document.getElementById("bar-title");
    const descEl = document.getElementById("bar-desc");
    const filterEl = document.getElementById("bar-filter-toggle");
    const chartEl = document.getElementById("bar-chart");

    if (state.barMetric === "tasks") {
      titleEl.textContent = "Monthly Task Capacity by Model";
      descEl.textContent =
        "Estimated coding tasks you can complete per month from each model's included subscription quota (at full weekly usage).";
      filterEl.style.display = "inline-flex";

      let rows = state.data.projections.filter(
        (r) => state.includeCodexFree || r.weekly_bucket !== "codex-30d"
      );
      if (state.barFilter === "defaults") {
        rows = rows.filter((r) => KEY_MODEL_IDS.has(r.id));
      } else if (state.barFilter === "current") {
        rows = rows.filter((r) => r.era === "current");
      }

      rows.sort((a, b) => a.tasks_per_month_primary - b.tasks_per_month_primary);

      const height = Math.max(380, rows.length * 36 + 90);
      chartEl.style.minHeight = `${height}px`;

      const labels = rows.map((r) => {
        const eraTag = r.era === "nov7" ? " (Nov 7)" : "";
        const planTag = r.weekly_bucket === "codex-30d" ? " · Free" : "";
        return `${r.family_label} (${friendlyEffort(r.effort)})${eraTag}${planTag}`;
      });
      const values = rows.map((r) => r.tasks_per_month_primary);

      const trace = {
        type: "bar",
        orientation: "h",
        y: labels,
        x: values,
        marker: {
          color: rows.map((r) => r.color),
          opacity: rows.map((r) => (r.era === "nov7" ? 0.6 : 0.92)),
        },
        text: rows.map(
          (r) => `~${fmtInt(r.tasks_per_month_primary)} tasks  (Intel ${fmtNum(r.aa_intelligence, 1)})`
        ),
        textposition: "outside",
        cliponaxis: false,
        hovertemplate: rows.map(
          (r) =>
            `<b>${r.family_label} (${friendlyEffort(r.effort)})</b><br>` +
            `${friendlyPool(r.weekly_bucket)}<br>` +
            `Capacity: <b>~${fmtInt(r.tasks_per_month_primary)} tasks/mo</b>  ·  Intelligence: <b>${fmtNum(r.aa_intelligence, 1)}</b><br>` +
            `API-equivalent pool: <b>~${fmtUsd(r.implied_pool_usd_per_month, r.implied_pool_usd_per_month < 10 ? 2 : 0)}/mo</b><extra></extra>`
        ),
      };

      const maxX = Math.max(...values, 10);
      const layout = {
        height,
        paper_bgcolor: colors.paper,
        plot_bgcolor: colors.plot,
        font: { color: colors.text, family: "Inter, sans-serif", size: 12 },
        margin: { t: 16, r: 165, b: 48, l: 225 },
        hoverlabel: {
          bgcolor: colors.surface,
          bordercolor: colors.grid,
          font: { color: colors.text, family: "Inter, sans-serif", size: 12.5 },
        },
        xaxis: {
          title: { text: "Estimated Tasks per Month", standoff: 10 },
          gridcolor: colors.grid,
          zerolinecolor: colors.grid,
          tickfont: { color: colors.muted },
          range: [0, maxX * 1.24],
        },
        yaxis: {
          automargin: true,
          tickfont: { size: 12, color: colors.text },
        },
      };

      Plotly.react("bar-chart", [trace], layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
      });
    } else {
      // Equivalent Monthly API Value ($/mo) across directly measured models
      titleEl.textContent = "Equivalent Monthly API Value by Model";
      descEl.textContent =
        "What the same monthly quota would cost if billed at standard API rates—compared against the $19.99/mo Google AI Pro price.";
      filterEl.style.display = "none";

      // Deduplicate by keeping latest cycle per model_id (e.g. Cycle 12 Hydrogen over Cycle 5)
      const latestByModel = new Map();
      for (const r of state.data.measured_cycles) {
        if (!state.includeCodexFree && r.weekly_bucket === "codex-30d") continue;
        const key = `${r.model_id}:${r.effort}:${r.weekly_bucket}`;
        const prev = latestByModel.get(key);
        if (!prev || r.cycle_id > prev.cycle_id) latestByModel.set(key, r);
      }
      const rows = [...latestByModel.values()].sort(
        (a, b) => a.implied_pool_usd_per_month - b.implied_pool_usd_per_month
      );

      const height = Math.max(400, rows.length * 38 + 90);
      chartEl.style.minHeight = `${height}px`;

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

      const labels = rows.map(friendlyMeasuredName);
      const values = rows.map((r) => r.implied_pool_usd_per_month);

      const trace = {
        type: "bar",
        orientation: "h",
        y: labels,
        x: values,
        marker: {
          color: rows.map(famColorFor),
          opacity: rows.map((r) => (r.era === "nov7" ? 0.6 : 0.92)),
        },
        text: rows.map((r) => {
          const v = r.implied_pool_usd_per_month;
          const mult = r.api_multiplier ? `  (${r.api_multiplier.toFixed(1)}× Pro)` : "";
          return `${fmtUsd(v, v < 10 ? 2 : 0)}/mo${mult}`;
        }),
        textposition: "outside",
        cliponaxis: false,
        hovertemplate: rows.map((r) => {
          const v = r.implied_pool_usd_per_month;
          const d = v < 10 ? 2 : 0;
          return (
            `<b>${friendlyMeasuredName(r)}</b><br>` +
            `${friendlyPool(r.weekly_bucket)}<br>` +
            `Equivalent API Value: <b>${fmtUsd(v, d)}/mo</b><br>` +
            `Capacity: <b>~${fmtInt(r.tasks_per_month_attempted)} tasks/mo</b>  ·  Intelligence: <b>${fmtNum(r.aa_intelligence, 1)}</b><extra></extra>`
          );
        }),
      };

      const maxX = Math.max(...values, 50);
      const layout = {
        height,
        paper_bgcolor: colors.paper,
        plot_bgcolor: colors.plot,
        font: { color: colors.text, family: "Inter, sans-serif", size: 12 },
        margin: { t: 24, r: 150, b: 48, l: 230 },
        hoverlabel: {
          bgcolor: colors.surface,
          bordercolor: colors.grid,
          font: { color: colors.text, family: "Inter, sans-serif", size: 12.5 },
        },
        xaxis: {
          title: { text: "Equivalent Monthly API Value (USD / month)", standoff: 10 },
          gridcolor: colors.grid,
          zerolinecolor: colors.grid,
          tickfont: { color: colors.muted },
          range: [0, maxX * 1.24],
        },
        yaxis: {
          automargin: true,
          tickfont: { size: 12, color: colors.text },
        },
        shapes: [
          {
            type: "line",
            x0: CFG.subscriptionPriceUsd,
            x1: CFG.subscriptionPriceUsd,
            y0: -0.5,
            y1: labels.length - 0.5,
            line: { color: colors.refLine, width: 1.6, dash: "dash" },
          },
        ],
        annotations: [
          {
            x: CFG.subscriptionPriceUsd,
            y: labels.length - 0.5,
            text: `$${CFG.subscriptionPriceUsd} Pro Price`,
            showarrow: false,
            xanchor: "left",
            yanchor: "bottom",
            xshift: 5,
            font: { size: 11, color: colors.refLine },
          },
        ],
      };

      Plotly.react("bar-chart", [trace], layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"],
      });
    }
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
            `<li><strong>${s.proj.family_label} (${friendlyEffort(s.proj.effort)})</strong>: ` +
              `<span class="tabular">~${fmtInt(tasks)} tasks</span> · ` +
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
          `<li><strong>Claude Opus 5.5 (Bonus Quota)</strong>: ` +
            `<span class="tabular">~${fmtNum(tasks3p, 1)} tasks</span> · ` +
            `<span class="tabular">${fmtUsd(api3p, 0)} API eq.</span></li>`
        );
      }

      const avgIntel = totalTasks > 0 ? weightedIntelSum / totalTasks : 0;
      const mult = totalApiUsd / CFG.subscriptionPriceUsd;
      const netSavings = Math.max(0, totalApiUsd - CFG.subscriptionPriceUsd);

      document.getElementById("res-tasks").textContent = `~${fmtInt(totalTasks)}`;
      document.getElementById("res-intel").textContent = fmtNum(avgIntel, 1);
      document.getElementById("res-api-usd").textContent = fmtUsd(totalApiUsd, 0);
      document.getElementById("res-mult").textContent = `${mult.toFixed(1)}×`;

      document.getElementById("res-breakdown").innerHTML =
        `<div>At <strong>${utilSlider.value}%</strong> weekly usage, this mix saves roughly <strong>${fmtUsd(netSavings, 0)}/mo</strong> compared to direct API pricing.</div>` +
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

  // Sortable Data Table (inside collapsed deep-dive drawer)
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
            { key: "weekly_bucket", label: "Subscription Pool", align: "left" },
            { key: "measured", label: "Source", align: "left" },
            { key: "aa_intelligence", label: "Intelligence", align: "right" },
            { key: "tasks_per_month_primary", label: "Tasks/mo", align: "right" },
            { key: "token_weighted_tasks_per_month", label: "Token-Only/mo", align: "right" },
            { key: "implied_pool_usd_per_month", label: "API Value/mo", align: "right" },
            { key: "api_multiplier", label: "× Pro", align: "right" },
          ]
        : [
            { key: "date", label: "Date", align: "left" },
            { key: "aa_name", label: "Model Tested", align: "left" },
            { key: "weekly_bucket", label: "Subscription Pool", align: "left" },
            { key: "aa_intelligence", label: "Intelligence", align: "right" },
            { key: "resolved_valid", label: "Tasks Solved", align: "right" },
            { key: "api_cost_usd", label: "Run API $", align: "right" },
            { key: "implied_pool_usd_per_month", label: "API Value/mo (95% CI)", align: "right" },
            { key: "tasks_per_month_attempted", label: "Tasks/mo (95% CI)", align: "right" },
          ];

    let rows =
      state.tableView === "projections"
        ? [...state.data.projections]
        : [...state.data.measured_cycles];

    if (q) {
      rows = rows.filter((r) =>
        `${JSON.stringify(r)} ${friendlyPool(r.weekly_bucket)}`.toLowerCase().includes(q)
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
            `<td><span class="model-dot" style="background:${r.color}"></span><strong>${r.family_label}</strong>${isPareto ? '<span class="badge badge-pareto">Frontier</span>' : ""}</td>` +
            `<td>${friendlyEffort(r.effort)}</td>` +
            `<td>${friendlyPool(r.weekly_bucket)}</td>` +
            `<td>${
              r.measured
                ? `<span class="badge badge-measured">Measured</span>`
                : `<span class="badge badge-scaled">Effort-scaled</span>`
            }</td>` +
            `<td class="num-col"><strong>${fmtNum(r.aa_intelligence, 1)}</strong></td>` +
            `<td class="num-col"><strong>${fmtInt(r.tasks_per_month_primary)}</strong></td>` +
            `<td class="num-col">${fmtInt(r.token_weighted_tasks_per_month)}</td>` +
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
            `<td class="mono">${r.date}</td>` +
            `<td><strong>${friendlyMeasuredName(r)}</strong></td>` +
            `<td>${friendlyPool(r.weekly_bucket)}</td>` +
            `<td class="num-col"><strong>${fmtNum(r.aa_intelligence, 1)}</strong></td>` +
            `<td class="num-col">${r.resolved_valid}/${r.attempted_valid}</td>` +
            `<td class="num-col">${fmtUsd(r.api_cost_usd, 3)}</td>` +
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

    wireSegmented("era-toggle", (v) => {
      state.era = v;
      renderParetoChart();
    });
    wireSegmented("scope-toggle", (v) => {
      state.scope = v;
      renderParetoChart();
    });
    wireSegmented("metric-toggle", (v) => {
      state.metric = v;
      renderParetoChart();
    });
    wireSegmented("scale-toggle", (v) => {
      state.xScale = v;
      renderParetoChart();
    });
    wireSegmented("bar-metric-toggle", (v) => {
      state.barMetric = v;
      renderBarChart();
    });
    wireSegmented("bar-filter-toggle", (v) => {
      state.barFilter = v;
      renderBarChart();
    });
    wireSegmented("table-view-toggle", (v) => {
      state.tableView = v;
      state.tableSort =
        v === "projections"
          ? { key: "aa_intelligence", dir: "desc" }
          : { key: "date", dir: "desc" };
      renderTable();
    });

    const syncCodexToggle = (checked) => {
      state.includeCodexFree = checked;
      const a = document.getElementById("toggle-codex-free");
      const b = document.getElementById("toggle-codex-free-bar");
      if (a) a.checked = checked;
      if (b) b.checked = checked;
      renderParetoChart();
      renderBarChart();
    };
    const codexA = document.getElementById("toggle-codex-free");
    const codexB = document.getElementById("toggle-codex-free-bar");
    if (codexA) codexA.addEventListener("change", (e) => syncCodexToggle(e.target.checked));
    if (codexB) codexB.addEventListener("change", (e) => syncCodexToggle(e.target.checked));

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
    document.getElementById("footer-date").textContent = snapDate;

    renderParetoChart();
    renderBarChart();
    initPlanner();
    renderTable();
  }

  window.addEventListener("DOMContentLoaded", () => {
    init().catch((err) => {
      console.error(err);
      const el = document.getElementById("pareto-chart");
      if (el) {
        el.innerHTML = `<p style="padding:20px;color:#dc2626">Failed to load benchmark data: ${err.message}.</p>`;
      }
    });
  });
})();

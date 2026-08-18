import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/walloris/Downloads/5828d199-db9f-45e6-9d2f-c8e4fe9f986f.xlsx";
const outputPath = "/Users/walloris/Documents/kventin/.tmp_dashboard_slide/analysis.json";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sheet = workbook.worksheets.getItem("Sheet1");
const rows = sheet.getRange("A1:O100").values;
const headers = rows[0];
const records = rows.slice(1).map((row, idx) => ({
  sourceRow: idx + 2,
  ...Object.fromEntries(headers.map((header, i) => [header, row[i]])),
}));

function percentNumber(value) {
  if (typeof value === "number") return value <= 1 ? value * 100 : value;
  if (typeof value !== "string") return null;
  const normalized = value.trim().replace("%", "").replace(",", ".");
  const number = Number(normalized);
  return Number.isFinite(number) ? number : null;
}

function shortTeam(name) {
  return String(name ?? "").replace(/\s*\([^)]*\)\s*$/, "");
}

const teamRows = records.filter((r) => r["Команда"] && !r["Этап APP"] && !r["Метрика"]);
const teams = teamRows.map((r) => ({
  team: shortTeam(r["Команда"]),
  completion: percentNumber(r["Доля метрик"]),
  achieved: Number(r["Метрик достигли"]),
  total: Number(r["Всего метрик"]),
  sourceRow: r.sourceRow,
}));

const metricRows = records.filter((r) => r["Команда"] && r["Метрика"]);
const metricGroups = new Map();
for (const r of metricRows) {
  const name = r["Метрика"];
  if (!metricGroups.has(name)) metricGroups.set(name, []);
  const actual = percentNumber(r["Значение метрики"]);
  const target = percentNumber(r["Цель метрики"]);
  metricGroups.get(name).push({
    team: shortTeam(r["Команда"]),
    actual,
    target,
    rawActual: r["Значение метрики"],
    measured: actual !== null,
    achieved: actual !== null && target !== null ? actual >= target : null,
    sourceRow: r.sourceRow,
  });
}

const metricSummary = [...metricGroups.entries()].map(([metric, observations]) => {
  const measured = observations.filter((o) => o.measured);
  const achieved = measured.filter((o) => o.achieved);
  return {
    metric,
    achievedTeams: achieved.length,
    measuredTeams: measured.length,
    shareAchieved: measured.length ? Math.round((achieved.length / measured.length) * 100) : null,
    observations,
  };
}).sort((a, b) => b.shareAchieved - a.shareAchieved);

const output = {
  scope: {
    block: records[0]["Блок"],
    tribe: records.find((r) => r["Трайб"])?.["Трайб"],
    cluster: records.find((r) => r["Кластер"])?.["Кластер"],
  },
  teamCount: teams.length,
  completeTeams: teams.filter((t) => t.completion === 100).length,
  teamsAtOrAbove80: teams.filter((t) => t.completion >= 80).length,
  achievedChecks: teams.reduce((sum, t) => sum + t.achieved, 0),
  totalChecks: teams.reduce((sum, t) => sum + t.total, 0),
  teams: teams.sort((a, b) => b.completion - a.completion || a.team.localeCompare(b.team, "ru")),
  metricSummary,
};

await fs.writeFile(outputPath, JSON.stringify(output, null, 2), "utf8");
console.log(JSON.stringify(output, null, 2));

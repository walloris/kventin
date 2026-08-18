import fs from "node:fs/promises";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const buildDir = "/Users/walloris/Documents/kventin/.tmp_dashboard_slide";
const outputDir = "/Users/walloris/Documents/kventin/outputs";
const finalPath = `${outputDir}/puls-karkas-dashboard-summary.pptx`;
const analysis = JSON.parse(await fs.readFile(`${buildDir}/analysis.json`, "utf8"));

await fs.mkdir(outputDir, { recursive: true });
await fs.mkdir(`${buildDir}/rendered`, { recursive: true });

const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });
const slide = presentation.slides.add();
slide.background.fill = "#FFFFFF";

function addText({ name, text, left, top, width, height, fontSize, bold = false, color = "#000000", align = "left", valign = "top", fill = "none", lineFill = "none", lineWidth = 0 }) {
  const box = slide.shapes.add({
    geometry: "textbox",
    name,
    position: { left, top, width, height },
    fill,
    line: { style: "solid", fill: lineFill, width: lineWidth },
  });
  box.text = text;
  box.text.style = {
    typeface: "Helvetica Neue",
    fontSize,
    bold,
    color,
    alignment: align,
    verticalAlignment: valign,
  };
  return box;
}

addText({
  name: "takeaway-title",
  text: "Все цели выполняет 1 из 8 команд; проблема — PR ↔ задача",
  left: 41,
  top: 34,
  width: 1198,
  height: 82,
  fontSize: 35,
  bold: true,
});

addText({
  name: "scope-line",
  text: `${analysis.scope.tribe}  ·  кластер ${analysis.scope.cluster}  ·  блок «${analysis.scope.block}»`,
  left: 42,
  top: 112,
  width: 1196,
  height: 28,
  fontSize: 16,
  color: "#5E6673",
});

slide.shapes.add({
  geometry: "roundRect",
  name: "chart-frame",
  position: { left: 41, top: 154, width: 581, height: 488 },
  fill: "#FFFFFF",
  line: { style: "solid", fill: "#B8BCC4", width: 1.2 },
  borderRadius: "rounded-md",
});

addText({
  name: "chart-title",
  text: "Доля команд, достигших цели по метрике",
  left: 65,
  top: 174,
  width: 525,
  height: 34,
  fontSize: 22,
  bold: true,
});

slide.charts.add("bar", {
  position: { left: 62, top: 218, width: 535, height: 360 },
  categories: [
    "PR ↔ задача",
    "Авторазвертывания",
    "Истории/баги ↔ код",
    "Документирование*",
    "Зрелость ПСИ",
  ],
  series: [{
    name: "Доля команд",
    values: [25, 75, 75, 100, 100],
    valuesFormatCode: '0"%"',
    fill: "#B9E7FA",
    points: [
      { idx: 0, fill: "#3D8DFF" },
      { idx: 1, fill: "#A7DCF2" },
      { idx: 2, fill: "#A7DCF2" },
      { idx: 3, fill: "#D7EEF8" },
      { idx: 4, fill: "#D7EEF8" },
    ],
  }],
  hasLegend: false,
  dataLabels: {
    showValue: true,
    position: "inEnd",
    textStyle: { fill: "#000000", fontSize: 16, bold: true },
  },
  chartFill: "#FFFFFF",
  chartLine: { style: "solid", fill: "#FFFFFF", width: 0 },
  plotAreaFill: { type: "none" },
  plotAreaLine: { style: "solid", fill: "#FFFFFF", width: 0 },
  xAxis: {
    visible: true,
    line: { style: "solid", fill: "#FFFFFF", width: 0 },
    majorGridlines: null,
    textStyle: { fill: "#5E6673", fontSize: 16 },
  },
  yAxis: {
    visible: false,
    min: 0,
    max: 100,
    majorUnit: 25,
    numberFormatCode: '0"%"',
    tickLabelPosition: "none",
    line: { style: "solid", fill: "#FFFFFF", width: 0 },
    majorGridlines: { style: "solid", fill: "#EDEDED", width: 1 },
    textStyle: { fill: "#5E6673", fontSize: 14 },
  },
  barOptions: { direction: "bar", grouping: "clustered", gapWidth: 52 },
});

addText({
  name: "chart-footnote",
  text: "* 7 из 7 измеренных команд; у «Продуктовой аналитики» нет релизов.",
  left: 65,
  top: 592,
  width: 525,
  height: 28,
  fontSize: 13,
  color: "#5E6673",
});

addText({
  name: "gap-eyebrow",
  text: "СИСТЕМНЫЙ РАЗРЫВ",
  left: 676,
  top: 176,
  width: 270,
  height: 26,
  fontSize: 15,
  bold: true,
  color: "#3D8DFF",
});

addText({
  name: "gap-number",
  text: "6 из 8",
  left: 673,
  top: 210,
  width: 260,
  height: 74,
  fontSize: 52,
  bold: true,
});

addText({
  name: "gap-description",
  text: "команд не достигают цели 80% по связи PR с задачами. У отстающих результат — от 64,3% до 79,1%.",
  left: 676,
  top: 286,
  width: 535,
  height: 112,
  fontSize: 22,
  color: "#23272F",
});

slide.shapes.add({
  geometry: "line",
  name: "stats-rule",
  position: { left: 676, top: 420, width: 536, height: 0 },
  fill: "none",
  line: { style: "solid", fill: "#B8BCC4", width: 1 },
});

addText({
  name: "stat-one",
  text: "74%",
  left: 676,
  top: 452,
  width: 220,
  height: 62,
  fontSize: 44,
  bold: true,
});
addText({
  name: "stat-one-label",
  text: "проверок метрик\nвыполнено · 29 из 39",
  left: 676,
  top: 520,
  width: 230,
  height: 68,
  fontSize: 18,
  color: "#3F4651",
});

addText({
  name: "stat-two",
  text: "5 из 8",
  left: 974,
  top: 452,
  width: 230,
  height: 62,
  fontSize: 44,
  bold: true,
});
addText({
  name: "stat-two-label",
  text: "команд выполняют\nне менее 80% метрик",
  left: 974,
  top: 520,
  width: 238,
  height: 68,
  fontSize: 18,
  color: "#3F4651",
});

addText({
  name: "source-footer",
  text: "Источник: выгрузка дашборда · Sheet1 · 8 команд",
  left: 42,
  top: 674,
  width: 700,
  height: 20,
  fontSize: 12,
  color: "#6B7280",
});

slide.speakerNotes.textFrame.setText(
  "[Sources]\n" +
  "- /Users/walloris/Downloads/5828d199-db9f-45e6-9d2f-c8e4fe9f986f.xlsx — Sheet1, rows 2–100.\n" +
  "- Calculations: 29/39 achieved metric checks; 5/8 teams at or above 80%; PR-to-task target met by 2/8 teams.\n" +
  "- Documentation denominator is 7 because Product Analytics has no releases.\n" +
  "[/Sources]"
);

const preview = await presentation.export({ slide, format: "png", scale: 2 });
await fs.writeFile(`${buildDir}/rendered/slide-1.png`, new Uint8Array(await preview.arrayBuffer()));
const layout = await slide.export({ format: "layout" });
await fs.writeFile(`${buildDir}/rendered/slide-1.layout.json`, await layout.text(), "utf8");
const inspection = await presentation.inspect({
  kind: "slide,textbox,shape,chart,notes",
  maxChars: 12000,
});
await fs.writeFile(`${buildDir}/rendered/inspect.ndjson`, inspection.ndjson, "utf8");

const pptx = await PresentationFile.exportPptx(presentation);
await pptx.save(finalPath);
console.log(JSON.stringify({ finalPath, preview: `${buildDir}/rendered/slide-1.png` }));

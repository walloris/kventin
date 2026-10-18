import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "/Users/walloris/Downloads/5828d199-db9f-45e6-9d2f-c8e4fe9f986f.xlsx";
const outputDir = "/Users/walloris/Documents/kventin/.tmp_dashboard_slide/xlsx-preview";
await fs.mkdir(outputDir, { recursive: true });

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const summary = await workbook.inspect({
  kind: "workbook,sheet,table,region,drawing",
  maxChars: 18000,
  tableMaxRows: 40,
  tableMaxCols: 24,
  tableMaxCellChars: 160,
});
await fs.writeFile(path.join(outputDir, "inspect.ndjson"), summary.ndjson, "utf8");
console.log(summary.ndjson);

for (const [index, sheet] of workbook.worksheets.items.entries()) {
  const safeName = sheet.name.replace(/[^a-zA-Z0-9_-]+/g, "_");
  const used = sheet.getUsedRange();
  console.log(JSON.stringify({ index, name: sheet.name, usedRange: used?.address ?? null }));
  const preview = await workbook.render({
    sheetName: sheet.name,
    autoCrop: "all",
    scale: 2,
    format: "png",
  });
  await fs.writeFile(
    path.join(outputDir, `${String(index + 1).padStart(2, "0")}-${safeName}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
}

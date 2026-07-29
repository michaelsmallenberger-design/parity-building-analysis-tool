import { readFileSync, writeFileSync } from "node:fs";

const indexPath = new URL("../../dist/index.html", import.meta.url);
const normalized = readFileSync(indexPath, "utf8").replace(/\r\n?/g, "\n");
writeFileSync(indexPath, normalized, "utf8");

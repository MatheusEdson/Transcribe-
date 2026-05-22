#!/usr/bin/env node
/**
 * Baixa language data do Tesseract OCR para ~/AppData/Local/tessdata
 * Uso: node scripts/download-tessdata.js <lang>
 * Exemplo: node scripts/download-tessdata.js por
 */

const https = require("https");
const fs = require("fs");
const path = require("path");
const os = require("os");

const lang = process.argv[2];
if (!lang) {
  console.error("Uso: node scripts/download-tessdata.js <lang>");
  console.error("Exemplos: por  eng  spa  fra  deu");
  process.exit(1);
}

const tessdata = path.join(os.homedir(), "AppData", "Local", "tessdata");
fs.mkdirSync(tessdata, { recursive: true });

const dest = path.join(tessdata, `${lang}.traineddata`);
if (fs.existsSync(dest)) {
  console.log(`Ja existe: ${dest}`);
  process.exit(0);
}

const url = `https://github.com/tesseract-ocr/tessdata/raw/main/${lang}.traineddata`;
console.log(`Baixando ${lang}.traineddata...`);

const file = fs.createWriteStream(dest);
https.get(url, (res) => {
  if (res.statusCode === 302 || res.statusCode === 301) {
    https.get(res.headers.location, (r) => r.pipe(file));
  } else {
    res.pipe(file);
  }
  file.on("finish", () => {
    file.close();
    const mb = (fs.statSync(dest).size / 1024 / 1024).toFixed(1);
    console.log(`Salvo: ${dest} (${mb} MB)`);
  });
}).on("error", (err) => {
  fs.unlinkSync(dest);
  console.error("Erro:", err.message);
  process.exit(1);
});

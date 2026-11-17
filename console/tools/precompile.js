// Precompile config/prompts/**/*.j2 with Nunjucks so the browser renders them WITHOUT eval
// (nunjucks-slim runtime + precompiled template functions). Usage: node precompile.js <promptsDir> <out.js>
const fs = require("fs"), path = require("path");
const nunjucks = require(process.env.NUNJUCKS_PATH || "nunjucks");
const [promptsDir, outFile] = process.argv.slice(2);
const env = new nunjucks.Environment(null, { autoescape: false, trimBlocks: true, lstripBlocks: true });
let out = "";
const names = [];
for (const agent of fs.readdirSync(promptsDir).sort()) {
  const dir = path.join(promptsDir, agent);
  if (!fs.statSync(dir).isDirectory()) continue;
  for (const f of fs.readdirSync(dir).sort()) {
    if (!f.endsWith(".j2")) continue;
    const name = `${agent}/${f.slice(0, -3)}`;
    const src = fs.readFileSync(path.join(dir, f), "utf8");
    out += nunjucks.precompileString(src, { name, env, asFunction: false }) + "\n";
    names.push(name);
  }
}
fs.writeFileSync(outFile, out);
console.log(JSON.stringify({ templates: names.length, bytes: out.length }));

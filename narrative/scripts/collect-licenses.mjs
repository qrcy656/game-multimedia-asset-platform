import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const seen = new Map();
function packageDirectory(name, from) {
  let directory = from;
  while (true) {
    const candidate = path.join(directory, 'node_modules', name);
    if (fs.existsSync(path.join(candidate, 'package.json'))) return candidate;
    const parent = path.dirname(directory);
    if (parent === directory) throw new Error(`Missing dependency package: ${name}`);
    directory = parent;
  }
}
function collect(name, from) {
  const directory = packageDirectory(name, from);
  const pkg = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'));
  const key = `${pkg.name}@${pkg.version}`;
  if (seen.has(key)) return;
  const files = fs.readdirSync(directory).filter(file => /^(licen[cs]e|copying|copyright)(\.|$)/i.test(file) && fs.statSync(path.join(directory, file)).isFile());
  if (!files.length) throw new Error(`No license file found for ${key}`);
  seen.set(key, { pkg, files: files.map(file => [file, fs.readFileSync(path.join(directory, file), 'utf8')]) });
  for (const dependency of Object.keys(pkg.dependencies || {})) collect(dependency, directory);
}
const project = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
for (const name of Object.keys(project.dependencies)) collect(name, root);
const sections = ['Fantasy 剧情设计 — Third-party runtime dependency notices\n\nThe project uses the following open-source packages. Their original license notices are reproduced below.'];
for (const [key, { pkg, files }] of [...seen.entries()].sort(([a], [b]) => a.localeCompare(b))) {
  sections.push(`${'='.repeat(80)}\n${key}\nLicense: ${pkg.license || 'See original license text'}\n${typeof pkg.repository === 'string' ? pkg.repository : pkg.repository?.url || ''}\n\n${files.map(([file, text]) => `--- ${file} ---\n${text}`).join('\n\n')}`);
}
fs.mkdirSync(path.join(root, 'static'), { recursive: true });
fs.writeFileSync(path.join(root, 'static', 'THIRD_PARTY_LICENSES.txt'), sections.join('\n\n'), 'utf8');
console.log(`Retained license notices for ${seen.size} runtime packages.`);

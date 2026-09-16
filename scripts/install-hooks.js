/** Install versioned native git hooks (hooks/* -> .git/hooks/*). Run once per clone. */
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const srcDir = path.join(root, 'hooks');
const dstDir = path.join(root, '.git', 'hooks');

for (const name of ['pre-commit', 'pre-push']) {
  const src = path.join(srcDir, name);
  const dst = path.join(dstDir, name);
  if (!fs.existsSync(src)) {
    console.error(`MISSING hooks/${name}`);
    process.exit(1);
  }
  fs.copyFileSync(src, dst);
  try {
    fs.chmodSync(dst, 0o755);
  } catch {
    // Windows: chmod is a no-op under Git for Windows sh — ignore.
  }
  console.log(`installed .git/hooks/${name}`);
}
console.log('hooks installed — pre-commit runs spec:check, pre-push mirrors CI');

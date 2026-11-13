const fs = require('node:fs');
const path = require('node:path');
function edit(name, replace) {
  const file = path.resolve(__dirname, '../vendor', name);
  const raw = fs.readFileSync(file, 'utf8');
  const updated = replace(raw.replace(/\r\n/g, '\n'));
  fs.writeFileSync(file, raw.includes('\r\n') ? updated.replace(/\n/g, '\r\n') : updated);
}
edit('js/ui/app.js', text => text.replace(
  '    const id = AFX.state.doc.id;\n    // чистим флаг',
  '    const id = AFX.state.doc.id;\n    const savedSnapshot = AFX.Model.stripJson(AFX.state.doc);\n    // чистим флаг'
).replace(
  '        if (!ok) return;\n        AFX.wsSyncSaved(id);',
  '        if (!ok) return;\n        const current = AFX.wsGetDoc(id);\n        if (!current || AFX.Model.stripJson(current) !== savedSnapshot) {\n            AFX.flushAutosave();\n            return; // Edits made while saving are still unsaved.\n        }\n        AFX.wsSyncSaved(id);'
));
edit('js/core/i18n.js', text => text
  .replace("    'Mine': 'Мои', 'Factory':", "    'Factory':")
  .replace("'Saved': 'Сохранено', 'Saved to localStorage': 'Сохранено в localStorage',", "'Saved': 'Сохранено',")
  .replace("    'localStorage is full (likely textures). Use run.bat: the server stores effects as files.':\n        'localStorage переполнен (вероятно, из-за текстур). Используй run.bat: сервер хранит эффекты в файлах.',\n", '')
);

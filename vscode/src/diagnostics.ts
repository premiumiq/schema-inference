import * as vscode from 'vscode';

/**
 * MAP-7 demo-ready plan phase 4: warning squiggles on generated staging
 * models for canonical fields that came back genuinely unmapped (no
 * approved mapping, no missing_field_resolutions entry -- see
 * sql_scaffold.find_unmapped_fields). One shared collection for the whole
 * extension, lazily created so extension.ts doesn't need to thread a
 * reference into ReviewPanel just for this.
 */
let collection: vscode.DiagnosticCollection | undefined;

function getCollection(): vscode.DiagnosticCollection {
  if (!collection) collection = vscode.languages.createDiagnosticCollection('schemaInference');
  return collection;
}

export function setUnmappedFieldDiagnostics(
  uri: vscode.Uri,
  fields: Array<{ field_name: string; line: number }>,
): void {
  const diagnostics = fields.map(({ field_name, line }) => {
    const range = new vscode.Range(line, 0, line, Number.MAX_SAFE_INTEGER);
    const diagnostic = new vscode.Diagnostic(
      range,
      `${field_name} has no source mapping -- defaulted to NULL`,
      vscode.DiagnosticSeverity.Warning,
    );
    diagnostic.source = 'Schema Inference';
    return diagnostic;
  });
  getCollection().set(uri, diagnostics);
}

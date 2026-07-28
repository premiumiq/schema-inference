import * as vscode from 'vscode';

/**
 * Serves prompt text for the Layer 2 "View diff" action under a custom
 * `schema-inference-prompt:` scheme, so `vscode.diff` can compare two
 * prompt versions without writing temp files -- the standard VS Code
 * pattern for diffing non-file content.
 */
export class PromptDiffProvider implements vscode.TextDocumentContentProvider {
  static readonly scheme = 'schema-inference-prompt';

  private readonly emitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.emitter.event;
  private readonly texts = new Map<string, string>();

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.texts.get(uri.path) ?? `(prompt text unavailable for ${uri.path})`;
  }

  /** Registers text under a key (e.g. "active" or a version_id) and
   * returns the Uri to open/diff it with. */
  set(key: string, text: string): vscode.Uri {
    this.texts.set(key, text);
    const uri = vscode.Uri.from({ scheme: PromptDiffProvider.scheme, path: key });
    this.emitter.fire(uri);
    return uri;
  }
}

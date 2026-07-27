import * as vscode from 'vscode';

import { BridgeClient, BridgeError } from './bridgeClient';
import { MapRunResult, MappingProposal, ProfileRunResult } from './types';

let bridge: BridgeClient | undefined;

/**
 * One MappingProposal per profiled file, keyed by absolute fsPath. In-memory
 * only -- MAP-7 step 3 scope is "profile a .dat file, see hover cards," not
 * persistence across window reloads. The proposal JSON the bridge already
 * wrote to disk is the durable copy; this cache just avoids re-reading it
 * on every hover.
 */
const proposalsByFile = new Map<string, { proposal: MappingProposal; delimiter: string }>();

function resolvePythonPath(): string {
  const configured = vscode.workspace.getConfiguration('schemaInference').get<string>('pythonPath');
  if (configured) return configured;
  // Simplification for this shell: no ms-python interpreter-API integration
  // yet (see design doc sec 3.4). Falls back to whatever "python"/"python3"
  // resolves to on PATH; promptForInterpreter() recovers when that's wrong.
  return process.platform === 'win32' ? 'python' : 'python3';
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function startBridge(): BridgeClient | undefined {
  const cwd = workspaceRoot();
  if (!cwd) {
    vscode.window.showErrorMessage('Schema Inference: no workspace folder open.');
    return undefined;
  }
  const pythonPath = resolvePythonPath();

  const client = new BridgeClient(pythonPath, cwd, (code, stderrTail) => {
    bridge = undefined;
    if (code !== 0) {
      const detail = stderrTail ? stderrTail.trim().split('\n').slice(-3).join(' ') : 'No stderr captured.';
      void vscode.window
        .showErrorMessage(
          `Schema Inference bridge exited unexpectedly (${pythonPath} -m schema_inference.bridge): ${detail}`,
          'Restart Bridge',
          'Select Python Interpreter',
        )
        .then((choice) => {
          if (choice === 'Restart Bridge') bridge = startBridge();
          if (choice === 'Select Python Interpreter') void promptForInterpreter();
        });
    }
  });
  client.start();
  return client;
}

async function promptForInterpreter(): Promise<void> {
  const picked = await vscode.window.showOpenDialog({
    canSelectMany: false,
    title: 'Select the Python interpreter with schema_inference installed',
  });
  if (!picked || picked.length === 0) return;

  await vscode.workspace
    .getConfiguration('schemaInference')
    .update('pythonPath', picked[0].fsPath, vscode.ConfigurationTarget.Workspace);

  bridge?.stop();
  bridge = startBridge();
}

function getBridge(): BridgeClient {
  if (!bridge) bridge = startBridge();
  if (!bridge) throw new Error('bridge unavailable (no workspace folder)');
  return bridge;
}

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand('schemaInference.profileAndMap', profileAndMapCurrentFile),
    vscode.commands.registerCommand('schemaInference.restartBridge', () => {
      bridge?.stop();
      bridge = startBridge();
      vscode.window.showInformationMessage('Schema Inference: bridge restarted.');
    }),
    vscode.languages.registerHoverProvider([{ pattern: '**/*.dat' }, { pattern: '**/*.csv' }], { provideHover }),
  );
}

export function deactivate(): void {
  bridge?.stop();
  bridge = undefined;
}

async function profileAndMapCurrentFile(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('Schema Inference: open a .dat/.csv file first.');
    return;
  }
  const filePath = editor.document.uri.fsPath;

  const config = vscode.workspace.getConfiguration('schemaInference');
  let sourceName = config.get<string>('sourceName');
  if (!sourceName) {
    sourceName = await vscode.window.showInputBox({
      prompt: 'Logical source name (e.g. pasl, pasm)',
      placeHolder: 'pasl',
    });
    if (!sourceName) return;
  }

  let client: BridgeClient;
  try {
    client = getBridge();
  } catch (err) {
    vscode.window.showErrorMessage(`Schema Inference: ${(err as Error).message}`);
    return;
  }

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Schema Inference: profiling & mapping...' },
    async () => {
      try {
        const profileResult = await client.request<ProfileRunResult>('profile.run', {
          file_path: filePath,
          source_name: sourceName,
        });

        // Rule-only (no_llm) for this shell -- the agent pipeline is a
        // later slice once map.progress notifications exist to show
        // per-column status during a long-running agent call (see design
        // doc's step-2 "deferred" note).
        const mapResult = await client.request<MapRunResult>('map.run', {
          profile_path: profileResult.profile_path,
          table_name: profileResult.table_name,
          no_llm: true,
        });

        proposalsByFile.set(filePath, {
          proposal: mapResult.proposal,
          delimiter: profileResult.delimiter,
        });

        const mapped = mapResult.proposal.mappings.filter((m) => m.target_field).length;
        vscode.window.showInformationMessage(
          `Schema Inference: ${mapped}/${mapResult.proposal.mappings.length} columns mapped. ` +
            'Hover the header row to inspect.',
        );
      } catch (err) {
        const message = err instanceof BridgeError ? err.message : String(err);
        vscode.window.showErrorMessage(`Schema Inference: ${message}`);
      }
    },
  );
}

function provideHover(document: vscode.TextDocument, position: vscode.Position): vscode.ProviderResult<vscode.Hover> {
  // Step 3 scope: only the header row (line 0) carries column names to
  // hover over. Per-row value hovers are a later slice.
  if (position.line !== 0) return undefined;

  const cached = proposalsByFile.get(document.uri.fsPath);
  if (!cached) return undefined;

  const { delimiter, proposal } = cached;
  const headerLine = document.lineAt(0).text;
  const columns = headerLine.split(delimiter);

  let offset = 0;
  let columnName: string | undefined;
  for (const col of columns) {
    const start = offset;
    const end = offset + col.length;
    if (position.character >= start && position.character <= end) {
      columnName = col;
      break;
    }
    offset = end + delimiter.length;
  }
  if (!columnName) return undefined;

  const mapping = proposal.mappings.find((m) => m.source_column === columnName);
  if (!mapping) return undefined;

  const md = new vscode.MarkdownString();
  md.appendMarkdown(`**${mapping.source_column}**\n\n`);
  md.appendMarkdown(`Target: \`${mapping.target_field ?? 'extended_attributes'}\`  \n`);
  md.appendMarkdown(`Confidence: ${(mapping.confidence * 100).toFixed(0)}%  \n`);
  md.appendMarkdown(`Method: ${mapping.method}  \n`);
  if (mapping.notes) md.appendMarkdown(`Notes: ${mapping.notes}\n`);

  return new vscode.Hover(md);
}

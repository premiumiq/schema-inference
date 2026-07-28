import * as path from 'path';
import * as vscode from 'vscode';

import { BridgeClient, BridgeError } from './bridgeClient';
import { HealthTreeDataProvider } from './healthSidebar';
import { ReviewPanel } from './reviewPanel';
import { MapRunResult, MappingProposal, ProfileRunResult, SchemaChangeReport, TrackerCheckResult } from './types';

let bridge: BridgeClient | undefined;
let healthProvider: HealthTreeDataProvider | undefined;

/** Last source_name used by profile.run this session -- the health sidebar
 * has no other way to know which source to query, since sourceName is
 * either read from settings or typed into a one-off input box (never
 * persisted anywhere the sidebar could read it back from). */
let lastSourceName: string | undefined;

/**
 * One MappingProposal per profiled file, keyed by absolute fsPath. In-memory
 * only -- persistence across window reloads is the proposal JSON already
 * written to disk (proposalPath), which review.start reads directly; this
 * cache just avoids re-reading it on every hover / avoids re-running
 * profile+map to open the review panel for a file already done this session.
 */
interface CachedProposal {
  proposal: MappingProposal;
  delimiter: string;
  proposalPath: string;
  sourceName: string;
  stale: boolean;
  staleSummary?: string;
}

const proposalsByFile = new Map<string, CachedProposal>();

function summarizeChangeReport(report: SchemaChangeReport): string {
  const parts = report.changes.map((c) => {
    switch (c.change_type) {
      case 'added':
        return `${c.column_name} added`;
      case 'removed':
        return `${c.column_name} removed`;
      case 'renamed':
        return `${c.old_value} -> ${c.new_value} renamed`;
      case 'type_changed':
        return `${c.column_name} type ${c.old_value} -> ${c.new_value}`;
    }
  });
  return parts.join(', ') || 'schema changed';
}

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
  healthProvider = new HealthTreeDataProvider(
    () => bridge,
    () => lastSourceName,
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('schemaInference.profileAndMap', profileAndMapCurrentFile),
    vscode.commands.registerCommand('schemaInference.restartBridge', () => {
      bridge?.stop();
      bridge = startBridge();
      vscode.window.showInformationMessage('Schema Inference: bridge restarted.');
    }),
    vscode.commands.registerCommand('schemaInference.openReviewPanel', openReviewPanelForCurrentFile),
    vscode.commands.registerCommand('schemaInference.refreshHealthSidebar', () => healthProvider?.refresh()),
    vscode.languages.registerHoverProvider([{ pattern: '**/*.dat' }, { pattern: '**/*.csv' }], { provideHover }),
    vscode.window.createTreeView('schemaInferenceHealth', { treeDataProvider: healthProvider }),
  );

  // Stale-proposal detection (design doc sec 5): cheap trigger (file
  // touched on disk), real diff via the existing tracker.check bridge
  // method -- not a second staleness check, reuses tracker.py's fingerprint
  // machinery. Only fires for files this session has actually profiled.
  const watcher = vscode.workspace.createFileSystemWatcher('**/*.{dat,csv}');
  const onFileTouched = (uri: vscode.Uri) => void checkForStaleness(uri);
  watcher.onDidChange(onFileTouched);
  watcher.onDidCreate(onFileTouched);
  context.subscriptions.push(watcher);
}

async function checkForStaleness(uri: vscode.Uri): Promise<void> {
  const cached = proposalsByFile.get(uri.fsPath);
  if (!cached || !bridge) return;

  try {
    const result = await bridge.request<TrackerCheckResult>('tracker.check', {
      file_path: uri.fsPath,
      source_name: cached.sourceName,
      table_name: cached.proposal.table_name,
    });
    if (result.report) {
      cached.stale = true;
      cached.staleSummary = summarizeChangeReport(result.report);
      ReviewPanel.notifyStale(cached.proposalPath, cached.staleSummary);
    }
  } catch {
    // Best-effort -- a failed staleness check must not interrupt editing.
  }
}

const STAGE_LABELS: Record<string, string> = {
  rule_pass: 'Rule pass complete',
  mapping_agent: 'MappingAgent complete',
  critic_agent: 'CriticAgent complete',
  sql_agent: 'SQLAgent complete',
  row_shape: 'Row shape inferred',
  done: 'Finalizing...',
};

function describeStage(params: unknown): string {
  const stage = (params as { stage?: string })?.stage;
  return (stage && STAGE_LABELS[stage]) || 'Running agent pipeline...';
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
  lastSourceName = sourceName;
  healthProvider?.refresh();

  const pipelinePick = await vscode.window.showQuickPick(
    [
      { label: 'Rule-only (fast)', detail: 'No LLM calls -- name/type/pattern matching only.', useAgent: false },
      {
        label: 'Agent pipeline (slower, more accurate)',
        detail: 'MappingAgent + CriticAgent + SQLAgent -- needs ANTHROPIC_API_KEY.',
        useAgent: true,
      },
    ],
    { placeHolder: 'How should columns be mapped?' },
  );
  if (!pipelinePick) return;
  const useAgent = pipelinePick.useAgent;

  let client: BridgeClient;
  try {
    client = getBridge();
  } catch (err) {
    vscode.window.showErrorMessage(`Schema Inference: ${(err as Error).message}`);
    return;
  }

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'Schema Inference: profiling & mapping...' },
    async (progress) => {
      try {
        const profileResult = await client.request<ProfileRunResult>('profile.run', {
          file_path: filePath,
          source_name: sourceName,
        });

        // Written next to the profile under the same registry/{source}/
        // convention (profile_{table}.json already exists there) so
        // review.start has a stable path to reload from later, including
        // across window reloads.
        const proposalPath = path.join(
          path.dirname(profileResult.profile_path),
          `proposal_${profileResult.table_name}.json`,
        );

        let mapResult: MapRunResult;
        if (useAgent) {
          // Single in-flight assumption: only this command drives an agent
          // map.run call at a time, so a plain assignment (not a per-request
          // correlation map) on the client's notification hook is enough.
          client.onNotification = (method, params) => {
            if (method === 'map.progress') progress.report({ message: describeStage(params) });
          };
          try {
            mapResult = await client.request<MapRunResult>('map.run', {
              profile_path: profileResult.profile_path,
              table_name: profileResult.table_name,
              agent: true,
              output: proposalPath,
            });
          } finally {
            client.onNotification = undefined;
          }
        } else {
          mapResult = await client.request<MapRunResult>('map.run', {
            profile_path: profileResult.profile_path,
            table_name: profileResult.table_name,
            no_llm: true,
            output: proposalPath,
          });
        }

        proposalsByFile.set(filePath, {
          proposal: mapResult.proposal,
          delimiter: profileResult.delimiter,
          proposalPath,
          sourceName: sourceName!,
          stale: false,
        });

        // Best-effort baseline for later staleness checks (checkForStaleness
        // above) -- tracker.check only ever gets called from the CLI's
        // `track` command otherwise, so without this there's no prior
        // SchemaVersion for a subsequent file edit to diff against.
        try {
          await client.request('tracker.check', {
            file_path: filePath,
            source_name: sourceName,
            table_name: profileResult.table_name,
          });
        } catch {
          // Non-fatal -- staleness detection degrades, nothing else does.
        }

        const mapped = mapResult.proposal.mappings.filter((m) => m.target_field).length;
        vscode.window.showInformationMessage(
          `Schema Inference: ${mapped}/${mapResult.proposal.mappings.length} columns mapped. ` +
            'Hover the header row, or run "Schema Inference: Open Review Panel" to review.',
        );
      } catch (err) {
        const message = err instanceof BridgeError ? err.message : String(err);
        vscode.window.showErrorMessage(`Schema Inference: ${message}`);
      }
    },
  );
}

async function openReviewPanelForCurrentFile(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  const filePath = editor?.document.uri.fsPath;
  const cached = filePath ? proposalsByFile.get(filePath) : undefined;

  if (!cached) {
    vscode.window.showWarningMessage(
      'Schema Inference: run "Profile & Map Current File" on this file first.',
    );
    return;
  }

  let client: BridgeClient;
  try {
    client = getBridge();
  } catch (err) {
    vscode.window.showErrorMessage(`Schema Inference: ${(err as Error).message}`);
    return;
  }

  try {
    await ReviewPanel.createOrShow(client, cached.proposalPath);
  } catch (err) {
    const message = err instanceof BridgeError ? err.message : String(err);
    vscode.window.showErrorMessage(`Schema Inference: ${message}`);
  }
}

function provideHover(document: vscode.TextDocument, position: vscode.Position): vscode.ProviderResult<vscode.Hover> {
  // Step 3 scope: only the header row (line 0) carries column names to
  // hover over. Per-row value hovers are a later slice.
  if (position.line !== 0) return undefined;

  const cached = proposalsByFile.get(document.uri.fsPath);
  if (!cached) return undefined;

  if (cached.stale) {
    const md = new vscode.MarkdownString();
    md.appendMarkdown(
      `**Profile out of date**\n\n${cached.staleSummary ?? 'File changed since last Profile & Map.'}\n\n` +
        'Re-run "Schema Inference: Profile & Map Current File" to refresh mappings.',
    );
    return new vscode.Hover(md);
  }

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

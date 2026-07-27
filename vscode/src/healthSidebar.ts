import * as vscode from 'vscode';

import { BridgeClient } from './bridgeClient';

interface LossRun {
  run_id: string;
  source_name: string;
  table_name: string;
  recorded_at: string;
  metrics: Record<string, unknown>;
  config_snapshot: Record<string, unknown>;
}

class LossRunNode {
  readonly kind = 'run' as const;
  constructor(readonly run: LossRun) {}
}
class MetricNode {
  readonly kind = 'metric' as const;
  constructor(
    readonly key: string,
    readonly value: unknown,
  ) {}
}
class MessageNode {
  readonly kind = 'message' as const;
  constructor(readonly text: string) {}
}

type Node = LossRunNode | MetricNode | MessageNode;

function formatValue(v: unknown): string {
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4);
  return String(v);
}

/**
 * Mapping-health sidebar: renders whatever metrics a metamodel loss_runs
 * row actually recorded (F1/hard-F1/mean-loss from scripts/
 * score_mappings.py's full AggregateMetrics, or just mean_loss_before/
 * after from tools/tune_rule_weights.py's Layer 0 grid search -- the
 * shape isn't fixed across producers, so this doesn't assume one).
 *
 * Native TreeView, not a webview -- per design doc sec 7 open question 1:
 * this is tiles/numbers with no interaction beyond refresh, so it doesn't
 * need a webview's custom layout control.
 */
export class HealthTreeDataProvider implements vscode.TreeDataProvider<Node> {
  private readonly emitter = new vscode.EventEmitter<Node | undefined | void>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(
    private readonly getBridge: () => BridgeClient | undefined,
    private readonly getSourceName: () => string | undefined,
  ) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(node: Node): vscode.TreeItem {
    if (node.kind === 'run') {
      const item = new vscode.TreeItem(
        `${node.run.table_name} -- ${node.run.recorded_at}`,
        vscode.TreeItemCollapsibleState.Collapsed,
      );
      item.description = node.run.run_id;
      item.iconPath = new vscode.ThemeIcon('graph');
      item.tooltip = new vscode.MarkdownString(
        '```json\n' + JSON.stringify(node.run.config_snapshot, null, 2) + '\n```',
      );
      return item;
    }
    if (node.kind === 'metric') {
      const item = new vscode.TreeItem(`${node.key}: ${formatValue(node.value)}`);
      item.iconPath = new vscode.ThemeIcon('symbol-number');
      return item;
    }
    const item = new vscode.TreeItem(node.text);
    item.iconPath = new vscode.ThemeIcon('info');
    return item;
  }

  async getChildren(node?: Node): Promise<Node[]> {
    if (node?.kind === 'run') {
      return Object.entries(node.run.metrics).map(([k, v]) => new MetricNode(k, v));
    }
    if (node) return [];

    const bridge = this.getBridge();
    const sourceName = this.getSourceName();
    if (!bridge) return [new MessageNode('Bridge not running -- run a Schema Inference command first.')];
    if (!sourceName) return [new MessageNode('No source name yet -- run "Profile & Map Current File" first.')];

    try {
      const resp = await bridge.request<{ loss_runs: LossRun[]; metamodel_available: boolean }>(
        'metamodel.query_loss_runs',
        { source_name: sourceName, limit: 20 },
      );
      if (!resp.metamodel_available) return [new MessageNode('Metamodel store unavailable (no metamodel.db yet).')];
      if (resp.loss_runs.length === 0) return [new MessageNode(`No scoring runs recorded yet for "${sourceName}".`)];
      return resp.loss_runs.map((r) => new LossRunNode(r));
    } catch (err) {
      return [new MessageNode(`Error: ${(err as Error).message}`)];
    }
  }
}

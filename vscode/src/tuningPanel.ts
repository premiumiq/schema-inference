import * as vscode from 'vscode';

import { BridgeClient, BridgeError } from './bridgeClient';
import { PromptDiffProvider } from './promptDiffProvider';
import {
  FewShotStatsResult,
  Layer0StatusResult,
  Layer2Round,
  Layer2SessionResult,
  PromptVersionsResult,
  RunLayer0Result,
  RunLayer1CurationResult,
  RunLayer3Result,
} from './types';

function getNonce(): string {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let text = '';
  for (let i = 0; i < 32; i++) text += chars.charAt(Math.floor(Math.random() * chars.length));
  return text;
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

interface InboundMessage {
  command:
    | 'load'
    | 'runLayer0'
    | 'curateLayer1'
    | 'retireFewShot'
    | 'loadLayer2'
    | 'runLayer2'
    | 'viewDiff'
    | 'acceptVersion';
  source_name?: string;
  agent_name?: string;
  step?: number;
  apply?: boolean;
  example_id?: string;
  reason?: string;
  max_rounds?: number;
  version_id?: string;
  prompt_text?: string;
}

/**
 * MAP-7 self-tuning panel: read/trigger insight into Layer 0 (rule
 * weights), Layer 1 (few-shot bank), and Layer 2 (LLM prompt tuning) --
 * see docs/map-7-vscode-extension-design.md and the self-tuning plan doc
 * for why these three get different treatment. Unlike ReviewPanel this is
 * a dashboard, not per-file -- one singleton instance, full re-render
 * after each action rather than incremental DOM patching (simpler and
 * appropriate for "click a button, see updated data," not a high-frequency
 * editing UX). The one exception is Layer 2's round-by-round progress
 * during a running session, which streams via postMessage so a multi-
 * minute session doesn't lose the user's place on every tick.
 */
export class TuningPanel {
  private static current: TuningPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];

  private sourceName: string;
  private agentName: 'mapping' | 'critic' = 'mapping';

  private layer0: Layer0StatusResult | null = null;
  private layer0RunResult: RunLayer0Result | null = null;
  private layer1: FewShotStatsResult | null = null;
  private layer1CurationResult: RunLayer1CurationResult | null = null;
  private layer2: PromptVersionsResult | null = null;
  private layer2SessionResult: Layer2SessionResult | null = null;
  private layer2Progress: Layer2Round[] = [];
  private layer2Running = false;
  private layer3: RunLayer3Result | null = null;

  private constructor(
    private bridge: BridgeClient,
    private readonly diffProvider: PromptDiffProvider,
    defaultSourceName: string,
  ) {
    this.sourceName = defaultSourceName;
    this.panel = vscode.window.createWebviewPanel(
      'schemaInferenceTuning',
      'Schema Inference: Self-Tuning',
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    this.panel.webview.onDidReceiveMessage((msg: InboundMessage) => this.handleMessage(msg), null, this.disposables);
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    void this.loadAll();
  }

  static createOrShow(bridge: BridgeClient, diffProvider: PromptDiffProvider, defaultSourceName: string): void {
    if (TuningPanel.current) {
      // Rebind to whatever bridge client is live *now*, not whichever one
      // was live when this panel was first constructed. Without this, a
      // bridge restart (crash recovery, "Restart Bridge Process", Python
      // interpreter change) while this panel is still open leaves every
      // button silently talking to a dead child process -- requests never
      // resolve or reject (Node doesn't reliably surface a write to a dead
      // process's stdin), so nothing in the panel ever visibly reacts to a
      // click, with no error, banner, or console output anywhere to point
      // at why. Cheap to always do, not just when the bridge actually
      // changed underneath it.
      TuningPanel.current.bridge = bridge;
      TuningPanel.current.panel.reveal(vscode.ViewColumn.Beside);
      void TuningPanel.current.loadAll();
      return;
    }
    TuningPanel.current = new TuningPanel(bridge, diffProvider, defaultSourceName);
  }

  private dispose(): void {
    TuningPanel.current = undefined;
    for (const d of this.disposables.splice(0)) d.dispose();
  }

  private async handleMessage(msg: InboundMessage): Promise<void> {
    try {
      switch (msg.command) {
        case 'load':
          this.sourceName = msg.source_name || this.sourceName;
          await this.loadLayer0();
          await this.loadLayer1();
          await this.loadLayer3();
          break;
        case 'runLayer0':
          this.layer0RunResult = await this.bridge.request<RunLayer0Result>('tuning.run_layer0', {
            source_name: msg.source_name || this.sourceName,
            step: msg.step ?? 0.05,
            apply: !!msg.apply,
          });
          await this.loadLayer0();
          break;
        case 'curateLayer1':
          this.layer1CurationResult = await this.bridge.request<RunLayer1CurationResult>(
            'tuning.run_layer1_curation',
            { source_name: msg.source_name || this.sourceName },
          );
          await this.loadLayer1();
          break;
        case 'retireFewShot':
          await this.bridge.request('tuning.retire_few_shot_example', {
            example_id: msg.example_id,
            reason: msg.reason || '',
          });
          await this.loadLayer1();
          break;
        case 'loadLayer2':
          this.agentName = (msg.agent_name as 'mapping' | 'critic') || this.agentName;
          await this.loadLayer2();
          break;
        case 'runLayer2':
          await this.runLayer2(msg.agent_name || this.agentName, msg.source_name || this.sourceName, msg.max_rounds || 5);
          return; // runLayer2 renders itself throughout
        case 'viewDiff':
          this.viewDiff(msg.version_id!, msg.prompt_text || '');
          return; // no state change, no re-render
        case 'acceptVersion': {
          const choice = await vscode.window.showWarningMessage(
            `Accept this prompt version as active for "${this.agentName}"? This changes agent behavior for every future run.`,
            { modal: true },
            'Accept',
          );
          if (choice !== 'Accept') return;
          await this.bridge.request('tuning.accept_prompt_version', { version_id: msg.version_id });
          await this.loadLayer2();
          break;
        }
        default:
          return;
      }
      this.render();
    } catch (err) {
      const message = err instanceof BridgeError ? err.message : String(err);
      void this.panel.webview.postMessage({ command: 'error', message });
    }
  }

  private async loadAll(): Promise<void> {
    try {
      await Promise.all([this.loadLayer0(), this.loadLayer1(), this.loadLayer2(), this.loadLayer3()]);
    } catch (err) {
      const message = err instanceof BridgeError ? err.message : String(err);
      vscode.window.showErrorMessage(`Schema Inference: ${message}`);
    }
    this.render();
  }

  private async loadLayer0(): Promise<void> {
    this.layer0 = await this.bridge.request<Layer0StatusResult>('tuning.layer0_status', { source_name: this.sourceName });
  }

  private async loadLayer1(): Promise<void> {
    this.layer1 = await this.bridge.request<FewShotStatsResult>('tuning.few_shot_stats', { source_name: this.sourceName });
  }

  private async loadLayer2(): Promise<void> {
    this.layer2 = await this.bridge.request<PromptVersionsResult>('tuning.prompt_versions', { agent_name: this.agentName });
  }

  private async loadLayer3(): Promise<void> {
    this.layer3 = await this.bridge.request<RunLayer3Result>('tuning.run_layer3', { source_name: this.sourceName });
  }

  private viewDiff(versionId: string, candidateText: string): void {
    const activeUri = this.diffProvider.set('active', this.layer2?.active_prompt ?? '(no active prompt)');
    const candidateUri = this.diffProvider.set(versionId, candidateText);
    void vscode.commands.executeCommand(
      'vscode.diff', activeUri, candidateUri,
      `Active prompt (${this.agentName}) <-> candidate ${versionId.slice(0, 8)}`,
    );
  }

  private async runLayer2(agentName: string, sourceName: string, maxRounds: number): Promise<void> {
    this.layer2Running = true;
    this.layer2Progress = [];
    this.layer2SessionResult = null;
    this.render();

    this.bridge.onNotification = (method, params) => {
      if (method === 'tuning.progress') {
        const info = params as Layer2Round;
        this.layer2Progress.push(info);
        void this.panel.webview.postMessage({ command: 'layer2Progress', rounds: this.layer2Progress });
      }
    };
    try {
      this.layer2SessionResult = await this.bridge.request<Layer2SessionResult>('tuning.run_layer2_session', {
        agent_name: agentName, source_name: sourceName, max_rounds: maxRounds,
      });
      await this.loadLayer2();
    } catch (err) {
      const message = err instanceof BridgeError ? err.message : String(err);
      vscode.window.showErrorMessage(`Schema Inference: Layer 2 session failed -- ${message}`);
    } finally {
      this.bridge.onNotification = undefined;
      this.layer2Running = false;
      this.render();
    }
  }

  private render(): void {
    this.panel.webview.html = this.html();
  }

  private html(): string {
    const nonce = getNonce();
    const csp = `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';`;

    return `<!doctype html>
<html>
<head>
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 0 1rem 2rem; }
  h2 { border-bottom: 1px solid var(--vscode-panel-border); padding-bottom: 4px; margin-top: 2rem; }
  h3 { margin-top: 1.25rem; margin-bottom: 0.25rem; font-size: 1em; opacity: 0.9; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 0.75rem; }
  td, th { padding: 4px 8px; border-bottom: 1px solid var(--vscode-panel-border); vertical-align: top; text-align: left; }
  input, select { font-family: inherit; }
  button { cursor: pointer; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 0.5rem; flex-wrap: wrap; }
  .hint { opacity: 0.8; font-size: 0.9em; }
  .badge { padding: 1px 6px; border-radius: 3px; font-size: 0.85em; }
  .badge.improved { background: #2ea04326; color: #2ea043; }
  .badge.rejected { background: #f8514926; color: #f85149; }
  #banner { display: none; padding: 6px 10px; margin-bottom: 10px; border-radius: 3px; background: #f8514926; color: #f85149; }
  #banner.visible { display: block; }
</style>
</head>
<body>
  <div id="banner"></div>

  ${this.renderLayer0()}
  ${this.renderLayer1()}
  ${this.renderLayer2()}
  ${this.renderLayer3()}

<script nonce="${nonce}">
  // acquireVsCodeApi() may only be called once per webview lifetime, but
  // render() reassigns panel.webview.html (full re-render) after every
  // action, re-running this whole script block -- a second call throws,
  // which aborts the script before any addEventListener below runs. That
  // silently kills every button in the panel after the first successful
  // click, not just whichever section triggered the re-render. Cache on
  // window so a re-run script reuses the same handle instead of
  // re-acquiring.
  const vscode = window.__schemaInferenceVscodeApi || (window.__schemaInferenceVscodeApi = acquireVsCodeApi());

  function q(sel) { return document.querySelector(sel); }

  q('#load-source').addEventListener('click', () => {
    vscode.postMessage({ command: 'load', source_name: q('#source-name').value.trim() });
  });
  q('#run-layer0-dry').addEventListener('click', () => {
    vscode.postMessage({ command: 'runLayer0', source_name: q('#source-name').value.trim(), step: parseFloat(q('#layer0-step').value), apply: false });
  });
  q('#run-layer0-apply').addEventListener('click', () => {
    vscode.postMessage({ command: 'runLayer0', source_name: q('#source-name').value.trim(), step: parseFloat(q('#layer0-step').value), apply: true });
  });
  q('#curate-layer1').addEventListener('click', () => {
    vscode.postMessage({ command: 'curateLayer1', source_name: q('#source-name').value.trim() });
  });
  document.querySelectorAll('.retire-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const row = btn.closest('tr');
      const reason = row.querySelector('.retire-reason').value;
      vscode.postMessage({ command: 'retireFewShot', example_id: btn.getAttribute('data-id'), reason });
    });
  });
  q('#load-agent').addEventListener('click', () => {
    vscode.postMessage({ command: 'loadLayer2', agent_name: q('#agent-name').value });
  });
  q('#run-layer2').addEventListener('click', () => {
    vscode.postMessage({
      command: 'runLayer2', agent_name: q('#agent-name').value,
      source_name: q('#source-name').value.trim(), max_rounds: parseInt(q('#layer2-rounds').value, 10) || 5,
    });
  });
  document.querySelectorAll('.diff-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      vscode.postMessage({ command: 'viewDiff', version_id: btn.getAttribute('data-id'), prompt_text: btn.getAttribute('data-prompt') });
    });
  });
  document.querySelectorAll('.accept-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      vscode.postMessage({ command: 'acceptVersion', version_id: btn.getAttribute('data-id') });
    });
  });

  window.addEventListener('message', (event) => {
    const msg = event.data;
    const banner = q('#banner');
    if (msg.command === 'error') {
      banner.className = 'visible';
      banner.textContent = msg.message;
    } else if (msg.command === 'layer2Progress') {
      const el = q('#layer2-progress');
      if (el) {
        el.innerHTML = msg.rounds.map((r) =>
          '<div>Round ' + r.round + ': loss ' + r.loss_before.toFixed(4) + ' -> ' + r.loss_after.toFixed(4) +
          ' <span class="badge ' + (r.improved ? 'improved">IMPROVED' : 'rejected">rejected') + '</span></div>'
        ).join('');
      }
    }
  });
</script>
</body>
</html>`;
  }

  private renderLayer0(): string {
    const weights = this.layer0?.active_weights;
    const result = this.layer0RunResult;
    return `
    <h2>Layer 0 -- Rule weights</h2>
    <div class="row">
      <label>Source: <input id="source-name" value="${escapeHtml(this.sourceName)}" size="12" /></label>
      <button id="load-source">Load</button>
      <label>Step: <input id="layer0-step" value="0.05" size="4" /></label>
      <button id="run-layer0-dry">Run (dry run)</button>
      <button id="run-layer0-apply">Run &amp; Apply</button>
    </div>
    ${weights ? `
    <table>
      <tr><th>name_sim</th><th>type_compat</th><th>pattern_bonus</th></tr>
      <tr><td>${weights.name_sim.toFixed(4)}</td><td>${weights.type_compat.toFixed(4)}</td><td>${weights.pattern_bonus.toFixed(4)}</td></tr>
    </table>` : '<p class="hint">Not loaded.</p>'}
    ${result ? `
    <p>
      mean_loss: ${result.baseline_metrics.mean_loss.toFixed(4)} -&gt; ${result.best_metrics.mean_loss.toFixed(4)}
      | f1: ${result.baseline_metrics.f1.toFixed(4)} -&gt; ${result.best_metrics.f1.toFixed(4)}
      | ${result.applied ? '<span class="badge improved">APPLIED</span>' : '<span class="hint">not applied</span>'}
    </p>` : ''}
    `;
  }

  private renderLayer1(): string {
    const stats = this.layer1;
    const curation = this.layer1CurationResult;
    const rows = stats?.active ?? [];
    return `
    <h2>Layer 1 -- Few-shot bank</h2>
    <div class="row">
      <button id="curate-layer1">Curate now</button>
      ${stats ? `<span class="hint">${stats.active_count} active, ${stats.retired_count} retired`
        + (Object.keys(stats.by_origin).length
          ? ' (' + Object.entries(stats.by_origin).map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(', ') + ')'
          : '') + '</span>' : ''}
    </div>
    ${curation ? `<p class="hint">Last curation: +${curation.hard_tp_inserted} hard_tp, +${curation.critic_inserted} critic, ${curation.skipped_existing} already banked, ${curation.skipped_no_signature} skipped (no signature)</p>` : ''}
    ${rows.length ? `
    <table>
      <tr><th>Column</th><th>Target</th><th>Origin</th><th>Added</th><th>Retire</th></tr>
      ${rows.map((r) => `
      <tr>
        <td>${escapeHtml(r.source_column)}</td>
        <td>${escapeHtml(r.target_field ?? '')}</td>
        <td>${escapeHtml(r.origin)}</td>
        <td>${escapeHtml(r.added_at)}</td>
        <td><input class="retire-reason" placeholder="reason" size="10" /> <button class="retire-btn" data-id="${escapeHtml(r.example_id)}">Retire</button></td>
      </tr>`).join('')}
    </table>` : '<p class="hint">No active examples for this source.</p>'}
    `;
  }

  private renderLayer2(): string {
    const versions = this.layer2?.versions ?? [];
    const activePrompt = this.layer2?.active_prompt;
    const session = this.layer2SessionResult;
    return `
    <h2>Layer 2 -- Prompt tuning</h2>
    <div class="row">
      <label>Agent:
        <select id="agent-name">
          <option value="mapping" ${this.agentName === 'mapping' ? 'selected' : ''}>mapping</option>
          <option value="critic" ${this.agentName === 'critic' ? 'selected' : ''}>critic</option>
        </select>
      </label>
      <button id="load-agent">Load</button>
      <label>Max rounds: <input id="layer2-rounds" value="5" size="3" /></label>
      <button id="run-layer2" ${this.layer2Running ? 'disabled' : ''}>${this.layer2Running ? 'Running...' : 'Run tuning session'}</button>
    </div>
    <p class="hint">Each round re-runs the full agent pipeline multiple times (baseline, train-diagnose, holdout-validate, plus a determinism check on any improving candidate) through a shared 5 requests/minute throttle -- expect several minutes per round, even for a single round, not a quick check.</p>
    <p class="hint">Active prompt: ${activePrompt ? '(accepted, ' + activePrompt.length + ' chars)' : '(none accepted yet -- module default in use)'}</p>
    <div id="layer2-progress">${this.layer2Progress.map((r) =>
      `<div>Round ${r.round}: loss ${r.loss_before.toFixed(4)} -> ${r.loss_after.toFixed(4)} <span class="badge ${r.improved ? 'improved">IMPROVED' : 'rejected">rejected'}</span></div>`,
    ).join('')}</div>
    ${session ? `<p>Session complete: baseline ${session.baseline_loss.toFixed(4)} -&gt; best ${session.best_loss.toFixed(4)}${session.best_version_id ? ` (candidate ${session.best_version_id.slice(0, 8)})` : ' (no improving candidate)'}</p>` : ''}
    ${versions.length ? `
    <table>
      <tr><th>Version</th><th>Loss</th><th>Status</th><th>Created</th><th>Actions</th></tr>
      ${versions.map((v) => `
      <tr>
        <td>${escapeHtml(v.version_id.slice(0, 8))}</td>
        <td>${v.loss_before != null && v.loss_after != null ? `${v.loss_before.toFixed(4)} -&gt; ${v.loss_after.toFixed(4)}` : '(n/a)'}</td>
        <td>${v.accepted ? '<span class="badge improved">accepted</span>' : ''}</td>
        <td>${escapeHtml(v.created_at)}</td>
        <td>
          <button class="diff-btn" data-id="${escapeHtml(v.version_id)}" data-prompt="${escapeHtml(v.prompt_text)}">View diff</button>
          ${!v.accepted ? `<button class="accept-btn" data-id="${escapeHtml(v.version_id)}">Accept</button>` : ''}
        </td>
      </tr>`).join('')}
    </table>` : '<p class="hint">No candidates logged yet for this agent.</p>'}
    `;
  }

  private renderLayer3(): string {
    const l3 = this.layer3;
    const eff = l3?.call_efficiency;
    const marginal = l3?.marginal_value ?? [];
    const under = l3?.under_triggering ?? [];
    return `
    <h2>Layer 3 -- Tool usage (report only)</h2>
    <p class="hint">Uses the Source field above -- click Load to refresh. Report-only, no apply/accept action: any mandatory_tool_triggers entry suggested below is hand-added to agent_config.yml after review, same as Layer 0/2's never-auto-promote convention.</p>
    ${!l3 ? '<p class="hint">Not loaded.</p>' : l3.rows === 0 ? `
    <p class="hint">No tool_usage_history for "${escapeHtml(l3.source_name)}" yet -- run the agent pipeline (--agent) at least once, then Load again.</p>` : `
    <p class="hint">${l3.rows} scored tool-call trace(s).</p>
    ${eff ? `
    <p>
      max_tool_calls_per_column: ${eff.max_tool_calls_per_column}
      | forced cutoff: ${eff.cutoff_count}/${eff.total} (${(eff.cutoff_pct * 100).toFixed(1)}%)
      | duplicate calls: ${eff.duplicate_count}
    </p>` : ''}
    <h3>Per-tool marginal value</h3>
    ${marginal.length ? `
    <table>
      <tr><th>Group</th><th>Tool</th><th>Called acc (n)</th><th>Not called acc (n)</th><th>Delta</th></tr>
      ${marginal.map((r) => `
      <tr>
        <td>${escapeHtml(r.group)}</td>
        <td>${escapeHtml(r.tool)}</td>
        <td>${r.called_acc.toFixed(2)} (${r.called_n})</td>
        <td>${r.not_called_acc.toFixed(2)} (${r.not_called_n})</td>
        <td>${r.delta >= 0 ? '+' : ''}${r.delta.toFixed(2)}</td>
      </tr>`).join('')}
    </table>` : '<p class="hint">Not enough grouped data yet.</p>'}
    <h3>Under-triggering</h3>
    ${under.length ? `
    <table>
      <tr><th>Group</th><th>Tool</th><th>Error rate without (n)</th><th>Error rate with (n)</th><th>Delta</th></tr>
      ${under.map((r) => `
      <tr>
        <td>${escapeHtml(r.group)}</td>
        <td>${escapeHtml(r.tool)}</td>
        <td>${r.error_rate_without.toFixed(2)} (${r.n_without})</td>
        <td>${r.error_rate_with.toFixed(2)} (${r.n_with})</td>
        <td>+${r.delta.toFixed(2)}</td>
      </tr>`).join('')}
    </table>` : '<p class="hint">No skip-vs-error correlation above threshold yet.</p>'}
    `}
    `;
  }
}

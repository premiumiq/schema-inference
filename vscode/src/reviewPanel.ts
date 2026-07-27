import * as vscode from 'vscode';

import { BridgeClient, BridgeError } from './bridgeClient';
import { ColumnMapping, ReviewStartResult } from './types';

// Same thresholds as reviewer.py's _fmt_confidence / review_proposal() tier
// split -- deliberately not a second scheme, see design doc sec 3.5.
const AUTO_TIER = 0.85;
const FLAGGED_TIER = 0.5;

type Decision = { action: 'accepted' | 'modified' | 'skipped'; target_field: string | null };

interface WebviewInboundMessage {
  command: 'accept' | 'modify' | 'skip' | 'resolveMissingField' | 'resolveContest' | 'assignExtendedAttr' | 'finalize';
  source_column?: string;
  target_field?: string | null;
  sql_expression?: string;
  notes?: string;
  field_name?: string;
  resolution?: 'NULL' | 'HARDCODED' | 'DERIVED';
  hardcoded_value?: string;
  derivation_sql?: string;
  winner?: string | null;
  keep_as_extended?: boolean;
  target?: string;
}

function getNonce(): string {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let text = '';
  for (let i = 0; i < 32; i++) text += chars.charAt(Math.floor(Math.random() * chars.length));
  return text;
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * MAP-7 step 4: accept/reject review panel, wired to the review.* JSON-RPC
 * methods built in step 2 (schema_inference/bridge.py's ReviewSession).
 * One column decision = one round trip, in whatever order the reviewer
 * clicks -- the thing review_proposal()'s blocking input() loop couldn't do.
 *
 * The server (ReviewSession) is the source of truth for what finalize
 * actually writes. This panel keeps its own `decisions` map purely to
 * render already-decided rows without re-fetching status after every
 * click; if it ever disagreed with the server, only the display would be
 * stale -- finalize() still runs the real logic bridge-side.
 */
export class ReviewPanel {
  private static current: ReviewPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];
  private readonly mappingsByCol = new Map<string, ColumnMapping>();
  private readonly decisions = new Map<string, Decision>();
  private readonly contestsResolved = new Set<string>();

  private constructor(
    private readonly bridge: BridgeClient,
    private readonly sessionId: string,
    private readonly start: ReviewStartResult,
  ) {
    this.panel = vscode.window.createWebviewPanel(
      'schemaInferenceReview',
      `Review: ${start.source_name}/${start.table_name}`,
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true },
    );

    for (const m of start.mappings) {
      this.mappingsByCol.set(m.source_column, m);
      // ReviewSession seeds the >=0.85 auto-approve tier server-side;
      // mirror that here so those rows don't render as pending.
      if (m.confidence >= AUTO_TIER) {
        this.decisions.set(m.source_column, { action: 'accepted', target_field: m.target_field });
      }
    }

    this.panel.webview.html = this.render();
    this.panel.webview.onDidReceiveMessage((msg: WebviewInboundMessage) => this.handleMessage(msg), null, this.disposables);
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
  }

  static async createOrShow(bridge: BridgeClient, proposalPath: string): Promise<void> {
    if (ReviewPanel.current) {
      ReviewPanel.current.panel.reveal(vscode.ViewColumn.Beside);
      return;
    }
    const start = await bridge.request<ReviewStartResult>('review.start', { proposal_path: proposalPath });
    ReviewPanel.current = new ReviewPanel(bridge, start.session_id, start);
  }

  private dispose(): void {
    ReviewPanel.current = undefined;
    for (const d of this.disposables.splice(0)) d.dispose();
  }

  private async handleMessage(msg: WebviewInboundMessage): Promise<void> {
    try {
      switch (msg.command) {
        case 'accept': {
          const col = msg.source_column!;
          await this.bridge.request('review.accept_column', { session_id: this.sessionId, source_column: col });
          this.decisions.set(col, { action: 'accepted', target_field: this.mappingsByCol.get(col)!.target_field });
          break;
        }
        case 'modify': {
          const col = msg.source_column!;
          await this.bridge.request('review.modify_column', {
            session_id: this.sessionId,
            source_column: col,
            target_field: msg.target_field || null,
            sql_expression: msg.sql_expression,
            notes: msg.notes,
          });
          this.decisions.set(col, { action: 'modified', target_field: msg.target_field || null });
          break;
        }
        case 'skip': {
          const col = msg.source_column!;
          await this.bridge.request('review.skip_column', { session_id: this.sessionId, source_column: col });
          this.decisions.set(col, { action: 'skipped', target_field: null });
          break;
        }
        case 'resolveMissingField':
          await this.bridge.request('review.resolve_missing_field', {
            session_id: this.sessionId,
            field_name: msg.field_name,
            resolution: msg.resolution,
            hardcoded_value: msg.hardcoded_value,
            derivation_sql: msg.derivation_sql,
          });
          break;
        case 'resolveContest': {
          const target = msg.target_field!;
          const contest = this.start.contested_mappings.find((c) => c.target_field === target);
          await this.bridge.request('review.resolve_contest', {
            session_id: this.sessionId,
            target_field: target,
            winner: msg.winner || null,
          });
          this.contestsResolved.add(target);
          if (contest) {
            for (const col of contest.competing_columns) {
              this.decisions.set(col, { action: 'modified', target_field: col === msg.winner ? target : null });
            }
          }
          break;
        }
        case 'assignExtendedAttr':
          await this.bridge.request('review.assign_extended_attr', {
            session_id: this.sessionId,
            source_column: msg.source_column,
            keep_as_extended: msg.keep_as_extended,
            target: msg.target,
          });
          break;
        case 'finalize': {
          const resp = await this.bridge.request<{ definition_path: string }>('review.finalize', {
            session_id: this.sessionId,
          });
          void this.panel.webview.postMessage({ command: 'finalized', definitionPath: resp.definition_path });
          vscode.window.showInformationMessage(`Schema Inference: review saved -> ${resp.definition_path}`);
          return;
        }
        default:
          return;
      }
      this.postState();
    } catch (err) {
      const message = err instanceof BridgeError ? err.message : String(err);
      void this.panel.webview.postMessage({ command: 'error', message });
    }
  }

  private postState(): void {
    const decisions: Record<string, Decision> = {};
    for (const [col, d] of this.decisions) decisions[col] = d;
    void this.panel.webview.postMessage({
      command: 'status',
      decisions,
      contestsResolved: [...this.contestsResolved],
    });
  }

  private render(): string {
    const nonce = getNonce();
    const csp = `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';`;

    const tiers: Array<{ label: string; klass: string; mappings: ColumnMapping[] }> = [
      { label: 'Auto-approved (>= 85%)', klass: 'auto', mappings: [] },
      { label: 'Flagged (50-84%)', klass: 'flagged', mappings: [] },
      { label: 'Low-confidence (< 50%)', klass: 'low', mappings: [] },
    ];
    for (const m of this.start.mappings) {
      if (m.confidence >= AUTO_TIER) tiers[0].mappings.push(m);
      else if (m.confidence >= FLAGGED_TIER) tiers[1].mappings.push(m);
      else tiers[2].mappings.push(m);
    }

    const rowsHtml = tiers
      .filter((t) => t.mappings.length > 0)
      .map(
        (t) => `
        <tr class="tier-header"><td colspan="6">${escapeHtml(t.label)} (${t.mappings.length})</td></tr>
        ${t.mappings
          .map((m) => this.renderRow(m, t.klass))
          .join('')}`,
      )
      .join('');

    const missingFieldsHtml = this.start.missing_standard_fields.length
      ? `<h2>Missing required fields</h2>
         <table>
           ${this.start.missing_standard_fields
             .map(
               (f) => `
             <tr data-missing-field="${escapeHtml(f)}">
               <td>${escapeHtml(f)}</td>
               <td>
                 <select class="mf-resolution">
                   <option value="NULL">NULL</option>
                   <option value="HARDCODED">Hardcode value</option>
                   <option value="DERIVED">SQL derivation</option>
                 </select>
                 <input class="mf-value" placeholder="value / SQL" />
                 <button class="mf-resolve" data-field="${escapeHtml(f)}">Resolve</button>
                 <span class="mf-state"></span>
               </td>
             </tr>`,
             )
             .join('')}
         </table>`
      : '';

    const contestedHtml = this.start.contested_mappings.length
      ? `<h2>Contested mappings</h2>
         <table>
           ${this.start.contested_mappings
             .map((c) => {
               const options = c.competing_columns
                 .map((col) => `<option value="${escapeHtml(col)}">${escapeHtml(col)}</option>`)
                 .join('');
               return `
             <tr data-contest-target="${escapeHtml(c.target_field)}">
               <td>${escapeHtml(c.target_field)}</td>
               <td>competing: ${c.competing_columns.map(escapeHtml).join(', ')}</td>
               <td>
                 <select class="contest-winner">
                   <option value="">(none -&gt; extended_attributes)</option>
                   ${options}
                 </select>
                 <button class="contest-resolve" data-target="${escapeHtml(c.target_field)}">Resolve</button>
                 <span class="contest-state"></span>
               </td>
             </tr>`;
             })
             .join('')}
         </table>`
      : '';

    return `<!doctype html>
<html>
<head>
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 0 1rem 2rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
  td { padding: 4px 8px; border-bottom: 1px solid var(--vscode-panel-border); vertical-align: top; }
  tr.tier-header td { font-weight: 600; background: var(--vscode-editor-inactiveSelectionBackground); }
  .badge { padding: 1px 6px; border-radius: 3px; font-size: 0.85em; }
  .badge.auto { background: #2ea04326; color: #2ea043; }
  .badge.flagged { background: #d2992226; color: #d29922; }
  .badge.low { background: #f8514926; color: #f85149; }
  .state { font-style: italic; opacity: 0.8; }
  .state.done { color: #2ea043; }
  button { cursor: pointer; }
  input, select { font-family: inherit; }
  #banner { display: none; padding: 6px 10px; margin-bottom: 10px; border-radius: 3px; }
  #banner.error { display: block; background: #f8514926; color: #f85149; }
  #footer { position: sticky; bottom: 0; background: var(--vscode-editor-background); padding: 10px 0; border-top: 1px solid var(--vscode-panel-border); }
</style>
</head>
<body>
  <div id="banner"></div>
  <p id="progress"></p>
  <table>
    <tr><th>Source column</th><th>Target</th><th>Confidence</th><th>Method</th><th>Notes</th><th>Actions</th></tr>
    ${rowsHtml}
  </table>
  ${missingFieldsHtml}
  ${contestedHtml}
  <div id="footer">
    <button id="finalize">Finalize review</button>
    <span id="finalize-state"></span>
  </div>

<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  const decisions = ${JSON.stringify(Object.fromEntries(this.decisions))};
  const contestsResolved = new Set(${JSON.stringify([...this.contestsResolved])});
  const totalColumns = ${this.start.mappings.length};

  function renderRowState(col) {
    const el = document.querySelector('tr[data-col="' + CSS.escape(col) + '"] .row-state');
    if (!el) return;
    const d = decisions[col];
    if (!d) { el.textContent = ''; el.className = 'row-state state'; return; }
    el.textContent = d.action + (d.target_field ? (' -> ' + d.target_field) : ' -> extended_attributes');
    el.className = 'row-state state done';
  }

  function updateProgress() {
    const decided = Object.keys(decisions).length;
    document.getElementById('progress').textContent = decided + ' / ' + totalColumns + ' columns decided';
  }

  document.querySelectorAll('tr[data-col]').forEach((row) => {
    const col = row.getAttribute('data-col');
    renderRowState(col);
    row.querySelector('.accept')?.addEventListener('click', () => vscode.postMessage({ command: 'accept', source_column: col }));
    row.querySelector('.skip')?.addEventListener('click', () => vscode.postMessage({ command: 'skip', source_column: col }));
    row.querySelector('.modify-save')?.addEventListener('click', () => {
      const target = row.querySelector('.modify-target').value.trim();
      const sql = row.querySelector('.modify-sql').value;
      const notes = row.querySelector('.modify-notes').value;
      vscode.postMessage({ command: 'modify', source_column: col, target_field: target || null, sql_expression: sql, notes });
    });
  });

  document.querySelectorAll('.mf-resolve').forEach((btn) => {
    btn.addEventListener('click', () => {
      const field = btn.getAttribute('data-field');
      const row = btn.closest('tr');
      const resolution = row.querySelector('.mf-resolution').value;
      const value = row.querySelector('.mf-value').value;
      vscode.postMessage({
        command: 'resolveMissingField',
        field_name: field,
        resolution,
        hardcoded_value: resolution === 'HARDCODED' ? value : undefined,
        derivation_sql: resolution === 'DERIVED' ? value : undefined,
      });
      row.querySelector('.mf-state').textContent = 'resolved';
    });
  });

  document.querySelectorAll('.contest-resolve').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-target');
      const row = btn.closest('tr');
      const winner = row.querySelector('.contest-winner').value || null;
      vscode.postMessage({ command: 'resolveContest', target_field: target, winner });
      row.querySelector('.contest-state').textContent = 'resolved';
    });
  });

  document.getElementById('finalize').addEventListener('click', () => {
    vscode.postMessage({ command: 'finalize' });
  });

  window.addEventListener('message', (event) => {
    const msg = event.data;
    const banner = document.getElementById('banner');
    if (msg.command === 'status') {
      Object.assign(decisions, msg.decisions);
      Object.keys(decisions).forEach(renderRowState);
      updateProgress();
      banner.className = '';
      banner.textContent = '';
    } else if (msg.command === 'error') {
      banner.className = 'error';
      banner.textContent = msg.message;
    } else if (msg.command === 'finalized') {
      document.getElementById('finalize-state').textContent = 'Saved -> ' + msg.definitionPath;
      document.getElementById('finalize').disabled = true;
    }
  });

  updateProgress();
</script>
</body>
</html>`;
  }

  private renderRow(m: ColumnMapping, klass: string): string {
    return `
      <tr data-col="${escapeHtml(m.source_column)}">
        <td>${escapeHtml(m.source_column)}</td>
        <td>${escapeHtml(m.target_field ?? 'extended_attributes')}</td>
        <td><span class="badge ${klass}">${(m.confidence * 100).toFixed(0)}%</span></td>
        <td>${escapeHtml(m.method)}</td>
        <td>${escapeHtml(m.notes)}</td>
        <td>
          <button class="accept">Accept</button>
          <button class="skip">Skip</button>
          <details>
            <summary>Modify</summary>
            <input class="modify-target" placeholder="target field" value="${escapeHtml(m.target_field ?? '')}" />
            <input class="modify-sql" placeholder="SQL expression" value="${escapeHtml(m.sql_expression)}" />
            <input class="modify-notes" placeholder="notes" value="${escapeHtml(m.notes)}" />
            <button class="modify-save">Save</button>
          </details>
          <span class="row-state state"></span>
        </td>
      </tr>`;
  }
}

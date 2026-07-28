import { ChildProcess, spawn } from 'child_process';

interface RpcMessage {
  jsonrpc: '2.0';
  id?: number | string | null;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code: number; message: string };
}

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void };

export class BridgeError extends Error {
  constructor(message: string, readonly code: number) {
    super(message);
  }
}

/**
 * JSON-RPC 2.0 client over the bridge's newline-delimited stdio protocol
 * (schema_inference/bridge.py). Not LSP -- see
 * docs/map-7-vscode-extension-design.md sec 3.1 for why a custom bridge was
 * chosen over textDocument/* messages.
 */
export class BridgeClient {
  private proc: ChildProcess | undefined;
  private buffer = '';
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private stderrTail = '';

  /** Server-initiated JSON-RPC notifications (no `id`) -- currently only
   * `map.progress` during an agent-pipeline map.run. Public/mutable rather
   * than a constructor param: only one caller drives an agent call at a
   * time in this extension, so the caller sets this right before issuing
   * that request and clears it in a `finally`, instead of needing a
   * per-request correlation mechanism this single-flow UI doesn't need. */
  onNotification: ((method: string, params: unknown) => void) | undefined;

  constructor(
    private readonly pythonPath: string,
    private readonly cwd: string,
    private readonly onExit: (code: number | null, stderrTail: string) => void,
  ) {}

  start(): void {
    // Force UTF-8 stdio regardless of the console codepage -- on Windows,
    // a spawned Python process otherwise inherits cp1252 and crashes the
    // instant any pipeline code prints a non-ASCII character (tracker.py's
    // "->" arrows, in particular -- discovered wiring up tracker.check for
    // staleness detection). Confirmed harmless everywhere else since these
    // are plain informational print()s to stdout, not JSON-RPC traffic;
    // bridgeClient's onStdout already drops any line that doesn't parse as
    // JSON, so stray print() output was never going to corrupt a response.
    this.proc = spawn(this.pythonPath, ['-m', 'schema_inference.bridge'], {
      cwd: this.cwd,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    });

    this.proc.stdout?.on('data', (chunk: Buffer) => this.onStdout(chunk));
    this.proc.stderr?.on('data', (chunk: Buffer) => {
      this.stderrTail = (this.stderrTail + chunk.toString()).slice(-4000);
    });
    this.proc.on('error', (err) => {
      this.rejectAll(new Error(`failed to start bridge process: ${err.message}`));
      this.onExit(null, this.stderrTail);
    });
    this.proc.on('exit', (code) => {
      this.rejectAll(new Error(`bridge process exited (code ${code})`));
      this.onExit(code, this.stderrTail);
    });
  }

  private onStdout(chunk: Buffer): void {
    this.buffer += chunk.toString();
    let idx: number;
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;

      let msg: RpcMessage;
      try {
        msg = JSON.parse(line);
      } catch {
        continue; // malformed line -- drop it, don't take down the client over one bad frame
      }
      if (msg.id === null || msg.id === undefined) {
        if (msg.method) this.onNotification?.(msg.method, msg.params);
        continue;
      }

      const id = Number(msg.id);
      const pending = this.pending.get(id);
      if (!pending) continue;
      this.pending.delete(id);

      if (msg.error) {
        pending.reject(new BridgeError(msg.error.message, msg.error.code));
      } else {
        pending.resolve(msg.result);
      }
    }
  }

  private rejectAll(err: Error): void {
    for (const { reject } of this.pending.values()) reject(err);
    this.pending.clear();
  }

  request<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (!this.proc?.stdin) {
      return Promise.reject(new Error('bridge process is not running'));
    }
    const id = this.nextId++;
    const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n';
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.proc!.stdin!.write(payload);
    });
  }

  stop(): void {
    this.proc?.kill();
    this.proc = undefined;
  }
}

import * as net from "node:net";

export interface GuiHostResponse {
  events: unknown[];
}

export interface GuiHostPort {
  request(action: unknown): Promise<GuiHostResponse>;
  close?(): void;
}

export class GuiHostRequestError extends Error {
  constructor(
    message: string,
    public readonly code?: string,
  ) {
    super(message);
    this.name = "GuiHostRequestError";
  }
}

interface PendingRequest {
  id: number;
  events: unknown[];
  resolve: (result: GuiHostResponse) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class SocketGuiHostPort implements GuiHostPort {
  private socket: net.Socket | null = null;
  private connecting: Promise<void> | null = null;
  private connected = false;
  private connectionResolve: (() => void) | null = null;
  private connectionReject: ((error: Error) => void) | null = null;
  private buffer = "";
  private nextId = 1;
  private pending: PendingRequest | null = null;
  private requestTail: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly endpoint: string,
    private readonly authToken?: string,
    private readonly timeoutMs = 5_000,
  ) {}

  request(action: unknown): Promise<GuiHostResponse> {
    const result = this.requestTail.then(() => this.execute(action));
    this.requestTail = result.catch(() => undefined);
    return result;
  }

  private async execute(action: unknown): Promise<GuiHostResponse> {
    await this.connect();
    if (!this.socket || !this.connected) throw new GuiHostRequestError("Veyyon GUI host is unavailable");
    const id = this.nextId++;
    return new Promise<GuiHostResponse>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending?.id === id) this.pending = null;
        reject(new GuiHostRequestError("Veyyon GUI host request timed out", "TIMEOUT"));
      }, this.timeoutMs);
      timer.unref?.();
      this.pending = { id, events: [], resolve, reject, timer };
      this.socket?.write(`${JSON.stringify({ id, action })}\n`);
    });
  }

  private connect(): Promise<void> {
    if (this.connected && this.socket && !this.socket.destroyed) return Promise.resolve();
    if (this.connecting) return this.connecting;

    this.connecting = new Promise<void>((resolve, reject) => {
      this.connectionResolve = resolve;
      this.connectionReject = reject;
      const tcp = /^tcp:([^:]+):(\d+)$/.exec(this.endpoint);
      const unix = this.endpoint.startsWith("unix:") ? this.endpoint.slice(5) : null;
      if (!tcp && !unix) {
        reject(new GuiHostRequestError("GUI host endpoint must be tcp:host:port or unix:path", "INVALID_ENDPOINT"));
        return;
      }
      const socket = tcp
        ? net.createConnection({ host: tcp[1], port: Number(tcp[2]) })
        : net.createConnection(unix as string);
      this.socket = socket;
      socket.setEncoding("utf8");
      socket.on("connect", () => {
        if (this.authToken) socket.write(`${JSON.stringify({ Authenticate: { token: this.authToken } })}\n`);
      });
      socket.on("data", chunk => this.onData(String(chunk)));
      socket.on("error", error => this.fail(new GuiHostRequestError(error.message, "SOCKET_ERROR")));
      socket.on("close", () => this.fail(new GuiHostRequestError("Veyyon GUI host connection closed", "SOCKET_CLOSED")));
      const timer = setTimeout(() => this.fail(new GuiHostRequestError("Veyyon GUI host connection timed out", "TIMEOUT")), this.timeoutMs);
      timer.unref?.();
      this.connecting?.then(() => clearTimeout(timer), () => clearTimeout(timer));
    }).finally(() => {
      this.connecting = null;
    });
    return this.connecting;
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    if (this.buffer.length > 8 * 1024 * 1024) {
      this.fail(new GuiHostRequestError("Veyyon GUI host sent an oversized frame", "FRAME_TOO_LARGE"));
      return;
    }
    let newline = this.buffer.indexOf("\n");
    while (newline >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (line) {
        try {
          this.onFrame(JSON.parse(line) as unknown);
        } catch {
          this.fail(new GuiHostRequestError("Veyyon GUI host sent invalid JSON", "INVALID_FRAME"));
          return;
        }
      }
      newline = this.buffer.indexOf("\n");
    }
  }

  private onFrame(frame: unknown): void {
    const record = frame !== null && typeof frame === "object" ? frame as Record<string, unknown> : null;
    const connection = record?.ConnectionChanged as Record<string, unknown> | undefined;
    if (connection && "Connected" in connection && !this.connected) {
      this.connected = true;
      this.connectionResolve?.();
      this.connectionResolve = null;
      this.connectionReject = null;
      return;
    }
    if (!this.pending || !record) return;
    this.pending.events.push(frame);
    const succeeded = record.RequestSucceeded as { request?: unknown } | undefined;
    if (succeeded?.request === this.pending.id) {
      const pending = this.pending;
      this.pending = null;
      clearTimeout(pending.timer);
      pending.resolve({ events: pending.events });
      return;
    }
    const failed = record.RequestFailed as { request?: unknown; error?: unknown } | undefined;
    if (failed?.request === this.pending.id) {
      const pending = this.pending;
      this.pending = null;
      clearTimeout(pending.timer);
      const error = failed.error && typeof failed.error === "object" ? failed.error as Record<string, unknown> : {};
      pending.reject(new GuiHostRequestError(
        typeof error.message === "string" ? error.message : "Veyyon GUI host rejected the request",
        typeof error.code === "string" ? error.code : undefined,
      ));
    }
  }

  private fail(error: Error): void {
    this.connected = false;
    this.connectionReject?.(error);
    this.connectionResolve = null;
    this.connectionReject = null;
    if (this.pending) {
      clearTimeout(this.pending.timer);
      this.pending.reject(error);
      this.pending = null;
    }
    if (this.socket && !this.socket.destroyed) this.socket.destroy();
    this.socket = null;
  }

  close(): void {
    this.fail(new GuiHostRequestError("Veyyon GUI host client closed", "CLIENT_CLOSED"));
  }
}

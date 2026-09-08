/**
 * types.ts — Telegram Bot Pool & Session Channel Type Contracts.
 */

export interface ManifestSlot {
  slotId: string;
  stateDir: string;
  preferredProjects: string[];
  enabled: boolean;
}

export interface BotPoolManifest {
  version: number;
  slots: ManifestSlot[];
}

export interface DiscoveredSlot {
  slotId: string;
  stateDir: string;
  botId: string;
  fingerprint: string;
  preferredProjects: string[];
  enabled: boolean;
}

export type LeaseStatus = "ACTIVE" | "RELEASED";

export interface BotLeaseRecord {
  slotId: string;
  sessionId: string;
  projectPath: string;
  ownerPid: number;
  ownerProcStart: string;
  acquiredAt: number;
  heartbeatAt: number;
  ttlSeconds: number;
  leaseStatus: LeaseStatus;
}

export interface ProcessIdentity {
  alive: boolean;
  creationTime: bigint;
  uncertain?: boolean;
}

export interface AccessConfig {
  dmPolicy: string;
  allowFrom: string[];
}

export interface ClaimResult {
  ok: boolean;
  slot?: DiscoveredSlot;
  error?: string;
  reason?: string;
  activeOwnerPid?: number;
}

export interface PoolStatusSummary {
  manifestPath: string;
  dbPath: string;
  totalSlots: number;
  enabledSlots: number;
  activeLeases: number;
  freeSlots: number;
  slots: Array<{
    slotId: string;
    stateDir: string;
    botId: string;
    fingerprint: string;
    preferredProjects: string[];
    enabled: boolean;
    lease: BotLeaseRecord | null;
    claudePid: number | null;
    veyyonPid: number | null;
    isBusy: boolean;
    busyReason?: string;
  }>;
}

export interface TelegramCallbackQuery {
  id: string;
  from: {
    id: number;
    is_bot: boolean;
    first_name?: string;
    username?: string;
  };
  message?: {
    message_id: number;
    chat: {
      id: number;
      type: "private" | "group" | "supergroup" | "channel";
      title?: string;
      username?: string;
    };
    date: number;
    text?: string;
  };
  data?: string;
}

export interface TelegramUpdate {
  update_id: number;
  message?: {
    message_id: number;
    from?: {
      id: number;
      is_bot: boolean;
      first_name?: string;
      username?: string;
    };
    chat: {
      id: number;
      type: "private" | "group" | "supergroup" | "channel";
      title?: string;
      username?: string;
    };
    date: number;
    text?: string;
    caption?: string;
    photo?: Array<{ file_id: string; file_unique_id: string; width?: number; height?: number; file_size?: number }>;
    document?: { file_id: string; file_unique_id: string; file_name?: string; mime_type?: string; file_size?: number };
    reply_to_message?: {
      message_id: number;
      from?: {
        id: number;
        is_bot: boolean;
        first_name?: string;
        username?: string;
      };
      chat?: {
        id: number;
      };
      date?: number;
      text?: string;
      caption?: string;
      photo?: Array<{ file_id: string; file_unique_id: string }>;
      document?: { file_id: string; file_name?: string; mime_type?: string };
    };
  };
  callback_query?: TelegramCallbackQuery;
}

export interface TelegramGetUpdatesResponse {
  ok: boolean;
  result?: TelegramUpdate[];
  description?: string;
  error_code?: number;
}

export interface TelegramSendMessageResponse {
  ok: boolean;
  result?: {
    message_id: number;
    chat: {
      id: number;
    };
    date: number;
    text?: string;
  };
  description?: string;
  error_code?: number;
}

/**
 * Durable identity of a single outbound Telegram message, keyed by
 * (botId, chatId, messageId), bound to the session (and optionally the request)
 * that produced it. Shared verbatim with the portable Python sender via the
 * `message_correlations` table in bot_pool.db.
 */
export interface OutboundMessageCorrelation {
  botId: string;
  chatId: string;
  messageId: number;
  slotId: string;
  sessionId: string;
  requestId: string | null;
  decisionId: string | null;
  projectPath: string | null;
  createdAt: number;
}

export type ReplyRoutingDecision =
  | "deliver"
  | "reject_unknown"
  | "reject_unbound"
  | "reject_foreign_session"
  | "reject_unavailable";

export interface ReplyRoutingResolution {
  decision: ReplyRoutingDecision;
  correlation?: OutboundMessageCorrelation;
  detail: string;
}

/**
 * Correlation surface the poller needs. Implemented over BotPoolCoordinator by
 * index.ts; injected so the transport stays testable and so a missing bridge
 * fails closed instead of routing replies blindly.
 */
export interface DecisionCallbackRecord {
  callbackToken: string;
  decisionId: string;
  choiceId: string;
  sessionId: string;
  chatId: string;
  userId: string;
  questionHash: string;
  expiresAt: number;
  consumedAt: number | null;
  createdAt: number;
}

export type CallbackValidationDecision =
  | "deliver"
  | "reject_unknown"
  | "reject_unauthorized"
  | "reject_foreign_session"
  | "reject_expired"
  | "reject_already_consumed"
  | "reject_already_answered";

export interface DecisionCallbackResolution {
  decision: CallbackValidationDecision;
  record?: DecisionCallbackRecord;
  detail: string;
}

export interface MessageCorrelationBridge {
  getSessionId: () => string;
  getSlotId: () => string;
  record: (correlation: OutboundMessageCorrelation) => void;
  resolveReply: (botId: string, chatId: string, replyToMessageId: number) => ReplyRoutingResolution;
  resolveCallback?: (callbackToken: string, userId: string, chatId: string) => DecisionCallbackResolution;
  consumeCallback?: (callbackToken: string) => boolean;
}

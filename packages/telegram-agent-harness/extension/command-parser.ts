/**
 * command-parser.ts — Canonical Telegram command and bot mention normalization.
 *
 * Distinguishes attached @suffix (/command@bot) vs spaced mentions (/command @bot),
 * validates addressed bot match (accepts own username/aliases, rejects other bots),
 * and normalizes the command and arguments for dispatch while preserving topic routing.
 */

export interface ParsedTelegramCommand {
  command: string;
  botMention?: string;
  argument: string;
  rawCommand: string;
  isAddressedToUs: boolean;
}

function matchesBot(mention: string, ownUsername?: string | null, slotId?: string | null): boolean {
  const normMention = mention.trim().replace(/^@/, "").toLowerCase();
  if (!normMention) return false;

  if (ownUsername) {
    const own = ownUsername.trim().replace(/^@/, "").toLowerCase();
    if (normMention === own) return true;
    const ownNoBot = own.replace(/bot$/i, "");
    if (normMention === ownNoBot) return true;
    const mentionNoBot = normMention.replace(/bot$/i, "");
    if (mentionNoBot === ownNoBot || mentionNoBot === own) return true;
    // Common typo / phonetic tolerance (e.g. superboreddef -> superboarddevbot)
    if (ownNoBot.length >= 6 && normMention.startsWith(ownNoBot.slice(0, 5))) return true;
  }

  if (slotId) {
    const slot = slotId.toLowerCase();
    const slotBase = slot.replace(/^telegram-/, "");
    if (normMention === slot || normMention === slotBase) return true;
    if (normMention === `${slotBase}bot` || normMention === `${slot}bot`) return true;
    if (slotBase.length >= 6 && normMention.startsWith(slotBase.slice(0, 5))) return true;
  }

  return false;
}

export function parseTelegramCommand(
  text: string,
  ownUsername?: string | null,
  slotId?: string | null,
): ParsedTelegramCommand | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) return null;

  const match = /^\/([a-zA-Z0-9_]+)(?:@([a-zA-Z0-9_]+))?(?:\s+(.*))?$/s.exec(trimmed);
  if (!match) return null;

  const command = match[1].toLowerCase();
  let botMention = match[2];
  let argument = (match[3] ?? "").trim();

  // If no attached @bot suffix, check for a spaced @mention in the arguments
  if (!botMention && argument) {
    const tokens = argument.split(/\s+/);
    const mentionIndex = tokens.findIndex(t => t.startsWith("@") && t.length > 1);
    if (mentionIndex >= 0) {
      botMention = tokens[mentionIndex].slice(1);
      tokens.splice(mentionIndex, 1);
      argument = tokens.join(" ").trim();
    }
  }

  const rawCommand = argument.length > 0 ? `/${command} ${argument}` : `/${command}`;

  let isAddressedToUs = true;
  if (botMention) {
    if (ownUsername || slotId) {
      isAddressedToUs = matchesBot(botMention, ownUsername, slotId);
    } else {
      isAddressedToUs = true;
    }
  }

  return {
    command,
    botMention: botMention ? botMention.trim().replace(/^@/, "").toLowerCase() : undefined,
    argument,
    rawCommand,
    isAddressedToUs,
  };
}

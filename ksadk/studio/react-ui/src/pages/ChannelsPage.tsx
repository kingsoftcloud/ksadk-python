import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Check,
  ChevronDown,
  Link2,
  MessageSquare,
  Plus,
  Send,
  QrCode,
  UserPlus,
  X,
  Users,
  RefreshCw,
  Settings,
} from "lucide-react";
import QRCode from "qrcode";
import { apiFetch } from "../api";
import { groupRunsBySession, type ChatSession } from "../chatProtocol";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Drawer } from "../components/Drawer";
import { MoreActionsMenu } from "../components/MoreActionsMenu";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { FormField } from "../components/ui/FormField";
import { StudioDialog } from "../components/ui/StudioDialog";
import { StudioDataTable, type StudioDataColumn } from "../components/ui/StudioDataTable";
import { StudioSelect } from "../components/ui/StudioSelect";
import "./channels.css";

// ── Channel API helper ─────────────────────────────────────────────────────
// 契约: agentengine-channel-api-contract.md
// 控制面 Base path: /agentengine/api/v1  全部 POST + JSON body
// 请求头: X-Ksc-Account-Id (必填), X-Ksc-User-uuid (可选)
// 统一响应: { Code, Message, RequestId, Action, Data }  Code=0 表示成功

const CHANNEL_API_BASE = "/agentengine/api/v1";
// 测试租户 ID，正式环境应从 Studio session 获取
const CHANNEL_ACCOUNT_ID = "2000003485";

interface ChannelEnvelope<T> {
  Code: number;
  Message: string;
  RequestId: string;
  Action: string;
  Data: T;
}

interface ListResult<T> {
  Items: T[];
  Total: number;
}

async function channelApi<T>(action: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await apiFetch(`${CHANNEL_API_BASE}/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Ksc-Account-Id": CHANNEL_ACCOUNT_ID,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const errorBody = await response.json();
      message = errorBody.Message || errorBody.message || message;
    } catch { /* keep HTTP status fallback */ }
    throw new Error(message);
  }
  const payload: ChannelEnvelope<T> = await response.json();
  if (payload.Code !== 0) {
    throw new Error(payload.Message || `错误码 ${payload.Code}`);
  }
  return payload.Data;
}

// ── Types (PascalCase 对齐契约) ──────────────────────────────────────────────

type ChannelType = "wps-xiezuo" | "feishu" | "wecom";
type DmPolicy = "pairing" | "open";
type GroupPolicy = "allowlist" | "open";
type SessionScope = "per-peer" | "per-group";
type PairingStatus = "pending" | "approved" | "rejected" | "expired";
type MessageDirection = "inbound" | "outbound";
type ChatType = "dm" | "group";

interface Channel {
  Id: string;
  AgentId: string;
  Channel: ChannelType;
  ChannelAccountId: string;
  Enabled: boolean;
  DmPolicy: DmPolicy;
  GroupPolicy: GroupPolicy;
  RequireMention: boolean;
  SessionScope: SessionScope;
  SecretRef: string;
  ConfigJson: string;
  CreatedBy: string;
  CreatedAt: string;
  UpdatedAt: string;
}

interface PairingRequest {
  Id: string;
  AgentId: string;
  Channel: ChannelType;
  ChannelAccountId: string;
  ChatType: ChatType;
  PeerId: string;
  SenderId: string;
  SenderName: string;
  Status: PairingStatus;
  ExpiresAt: string;
  ApprovedBy: string | null;
  ApprovedAt: string | null;
  CreatedAt: string;
}

interface ChannelBinding {
  Id: string;
  AgentId: string;
  Channel: ChannelType;
  ChannelAccountId: string;
  ChatType: ChatType;
  PeerId: string;
  GroupId: string;
  SenderId: string;
  SessionId: string;
  UserId: string;
  CreatedAt: string;
  UpdatedAt?: string;
}

interface ChannelMessage {
  Id: string;
  Channel: ChannelType;
  ChannelAccountId: string;
  PlatformEventId: string;
  Direction: MessageDirection;
  DedupeKey: string;
  Payload: Record<string, unknown> | string;
  Error: string;
  RetryCount: number;
  NextRetryAt: string | null;
  CreatedAt: string;
}

interface ConversationGroup {
  id: string;
  channel: ChannelType;
  channelAccountId: string;
  chatId: string;
  senderId: string;
  title: string;
  messages: ChannelMessage[];
  messageCount: number;
  lastMessage: ChannelMessage;
}

interface StudioAgent {
  metadata: { id: string; name: string };
}

interface CloudAgentSummary {
  agentId?: string;
  name?: string;
  status?: string;
}

interface ConnectQrData {
  QrUrl: string;
  Label: string;
  Platform: string;
}

// ── Labels & options ────────────────────────────────────────────────────────

const CHANNEL_LABELS: Record<ChannelType, string> = {
  "wps-xiezuo": "WPS 协作",
  feishu: "飞书",
  wecom: "企业微信",
};

const CHANNEL_OPTIONS = (Object.keys(CHANNEL_LABELS) as ChannelType[]).map(value => ({
  value,
  label: CHANNEL_LABELS[value],
}));

const PLATFORM_BADGE: Record<ChannelType, string> = {
  "wps-xiezuo": "WPS",
  feishu: "飞书",
  wecom: "企微",
};

const PLATFORM_LIST: Array<{ value: ChannelType; label: string; desc: string; Icon: typeof MessageSquare }> = [
  { value: "wps-xiezuo", label: "WPS 协作", desc: "金山办公协作平台", Icon: MessageSquare },
  { value: "feishu", label: "飞书", desc: "字节跳动企业协作", Icon: Send },
  { value: "wecom", label: "企业微信", desc: "腾讯企业即时通讯", Icon: Users },
];

const DM_POLICY_LABELS: Record<DmPolicy, string> = {
  pairing: "需配对",
  open: "开放",
};

const DM_POLICY_OPTIONS = (Object.keys(DM_POLICY_LABELS) as DmPolicy[]).map(value => ({
  value,
  label: DM_POLICY_LABELS[value],
}));

const GROUP_POLICY_LABELS: Record<GroupPolicy, string> = {
  allowlist: "白名单",
  open: "开放",
};

const GROUP_POLICY_OPTIONS = (Object.keys(GROUP_POLICY_LABELS) as GroupPolicy[]).map(value => ({
  value,
  label: GROUP_POLICY_LABELS[value],
}));

const SESSION_SCOPE_LABELS: Record<SessionScope, string> = {
  "per-peer": "按用户",
  "per-group": "按群聊",
};

const SESSION_SCOPE_OPTIONS = (Object.keys(SESSION_SCOPE_LABELS) as SessionScope[]).map(value => ({
  value,
  label: SESSION_SCOPE_LABELS[value],
}));

const PAIRING_STATUS_LABEL: Record<PairingStatus, string> = {
  pending: "待处理",
  approved: "已通过",
  rejected: "已拒绝",
  expired: "已过期",
};

const PAIRING_STATUS_STATE: Record<PairingStatus, string> = {
  pending: "pending",
  approved: "ready",
  rejected: "failed",
  expired: "idle",
};

const CHAT_TYPE_LABEL: Record<ChatType, string> = {
  dm: "私聊",
  group: "群聊",
};

// ── Seed data (后端未就绪时保留预览) ───────────────────────────────────────────

const SEED_CHANNELS: Channel[] = [
  {
    Id: "9b8939273af74e98",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "wps-xiezuo",
    ChannelAccountId: "wps-default",
    Enabled: true,
    DmPolicy: "pairing",
    GroupPolicy: "allowlist",
    RequireMention: true,
    SessionScope: "per-peer",
    SecretRef: "cred-xxxxxxxx",
    ConfigJson: "{}",
    CreatedBy: "user-xxx",
    CreatedAt: "2026-08-27T10:00:00",
    UpdatedAt: "2026-08-27T10:00:00",
  },
  {
    Id: "a1c2d3e4f5g6h7i8",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "feishu",
    ChannelAccountId: "feishu-cs",
    Enabled: true,
    DmPolicy: "open",
    GroupPolicy: "open",
    RequireMention: false,
    SessionScope: "per-group",
    SecretRef: "cred-yyyyyyyy",
    ConfigJson: "{}",
    CreatedBy: "user-xxx",
    CreatedAt: "2026-08-25T14:30:00",
    UpdatedAt: "2026-08-26T09:00:00",
  },
  {
    Id: "b2d3e4f5g6h7i8j9",
    AgentId: "",
    Channel: "wecom",
    ChannelAccountId: "wecom-internal",
    Enabled: false,
    DmPolicy: "pairing",
    GroupPolicy: "allowlist",
    RequireMention: true,
    SessionScope: "per-peer",
    SecretRef: "cred-zzzzzzzz",
    ConfigJson: "{}",
    CreatedBy: "user-xxx",
    CreatedAt: "2026-08-22T10:00:00",
    UpdatedAt: "2026-08-22T10:00:00",
  },
];

const SEED_PAIRINGS: PairingRequest[] = [
  {
    Id: "pair-001",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "wps-xiezuo",
    ChannelAccountId: "wps-default",
    ChatType: "dm",
    PeerId: "user-zhang3",
    SenderId: "user-zhang3",
    SenderName: "张三",
    Status: "pending",
    ExpiresAt: "2026-08-27T18:00:00",
    ApprovedBy: null,
    ApprovedAt: null,
    CreatedAt: "2026-08-27T08:15:00",
  },
  {
    Id: "pair-002",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "feishu",
    ChannelAccountId: "feishu-cs",
    ChatType: "dm",
    PeerId: "user-li4",
    SenderId: "user-li4",
    SenderName: "李四",
    Status: "pending",
    ExpiresAt: "2026-08-27T20:00:00",
    ApprovedBy: null,
    ApprovedAt: null,
    CreatedAt: "2026-08-27T08:40:00",
  },
  {
    Id: "pair-003",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "wps-xiezuo",
    ChannelAccountId: "wps-default",
    ChatType: "dm",
    PeerId: "user-wang5",
    SenderId: "user-wang5",
    SenderName: "王五",
    Status: "approved",
    ExpiresAt: "2026-08-27T12:00:00",
    ApprovedBy: "user-xxx",
    ApprovedAt: "2026-08-26T16:20:00",
    CreatedAt: "2026-08-26T16:00:00",
  },
];

const SEED_BINDINGS: ChannelBinding[] = [
  {
    Id: "bind-001",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "wps-xiezuo",
    ChannelAccountId: "wps-default",
    ChatType: "dm",
    PeerId: "user-wang5",
    GroupId: "",
    SenderId: "user-wang5",
    SessionId: "sess-20260826-001",
    UserId: "u-wang5",
    CreatedAt: "2026-08-26T16:20:00",
    UpdatedAt: "2026-08-27T09:15:00",
  },
  {
    Id: "bind-002",
    AgentId: "ar-20260825114524-bf942afc",
    Channel: "feishu",
    ChannelAccountId: "feishu-cs",
    ChatType: "group",
    PeerId: "",
    GroupId: "grp-tech-team",
    SenderId: "user-zhao6",
    SessionId: "sess-20260825-002",
    UserId: "u-zhao6",
    CreatedAt: "2026-08-25T11:00:00",
    UpdatedAt: "2026-08-26T14:30:00",
  },
];

const SEED_MESSAGES: ChannelMessage[] = [
  {
    Id: "msg-001",
    Channel: "wps-xiezuo",
    ChannelAccountId: "wps-default",
    PlatformEventId: "evt-20260827-001",
    Direction: "inbound",
    DedupeKey: "wps-xiezuo:wps-default:evt-20260827-001",
    Payload: { text: "帮我查一下本周的销售数据汇总", sender_name: "张三" },
    Error: "",
    RetryCount: 0,
    NextRetryAt: null,
    CreatedAt: "2026-08-27T09:01:00",
  },
  {
    Id: "msg-002",
    Channel: "wps-xiezuo",
    ChannelAccountId: "wps-default",
    PlatformEventId: "evt-20260827-002",
    Direction: "outbound",
    DedupeKey: "wps-xiezuo:wps-default:evt-20260827-002",
    Payload: { text: "已为您查询到本周销售数据，华东区同比增长 12.3%。", sender_name: "Agent" },
    Error: "",
    RetryCount: 0,
    NextRetryAt: null,
    CreatedAt: "2026-08-27T09:01:05",
  },
  {
    Id: "msg-003",
    Channel: "feishu",
    ChannelAccountId: "feishu-cs",
    PlatformEventId: "evt-20260827-003",
    Direction: "inbound",
    DedupeKey: "feishu:feishu-cs:evt-20260827-003",
    Payload: { text: "产品 A 的库存还有多少？", sender_name: "李四" },
    Error: "",
    RetryCount: 0,
    NextRetryAt: null,
    CreatedAt: "2026-08-27T09:10:00",
  },
  {
    Id: "msg-004",
    Channel: "feishu",
    ChannelAccountId: "feishu-cs",
    PlatformEventId: "evt-20260827-004",
    Direction: "outbound",
    DedupeKey: "feishu:feishu-cs:evt-20260827-004",
    Payload: { text: "产品 A 当前库存 1,280 件，安全库存 500 件，库存充足。", sender_name: "Agent" },
    Error: "",
    RetryCount: 0,
    NextRetryAt: null,
    CreatedAt: "2026-08-27T09:10:08",
  },
  {
    Id: "msg-005",
    Channel: "wecom",
    ChannelAccountId: "wecom-internal",
    PlatformEventId: "evt-20260827-005",
    Direction: "outbound",
    DedupeKey: "wecom:wecom-internal:evt-20260827-005",
    Payload: { text: "群聊消息推送异常，正在重试…", sender_name: "Agent" },
    Error: "gateway timeout",
    RetryCount: 2,
    NextRetryAt: "2026-08-27T09:00:00",
    CreatedAt: "2026-08-27T08:55:00",
  },
];

// ── Helpers ─────────────────────────────────────────────────────────────────

function formatChannelDate(iso: string): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatChatTime(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatChatDate(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  const isSameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (isSameDay(date, today)) return "今天";
  if (isSameDay(date, yesterday)) return "昨天";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function parsePayload(payload: Record<string, unknown> | string): Record<string, unknown> {
  if (typeof payload === "string") {
    try {
      return JSON.parse(payload) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return payload ?? {};
}

function messagePreview(msg: ChannelMessage): string {
  const payload = parsePayload(msg.Payload);
  if (typeof payload.text === "string") return payload.text;
  const message = payload.message as Record<string, unknown> | undefined;
  if (message && typeof message === "object") {
    const content = message.content as Record<string, unknown> | undefined;
    if (content) {
      const text = content.text as Record<string, unknown> | undefined;
      if (text && typeof text.content === "string") return text.content;
    }
    if (typeof message.content === "string") return message.content;
  }
  if (typeof payload.content === "string") return payload.content;
  if (typeof payload.message === "string") return payload.message;
  const json = JSON.stringify(payload);
  return json.length > 120 ? `${json.slice(0, 120)}…` : json;
}

function messageSender(msg: ChannelMessage): string {
  const p = parsePayload(msg.Payload);
  const candidates = ["sender_name", "senderName", "from_name", "fromName", "user_name", "userName"];
  for (const key of candidates) {
    const val = p[key];
    if (typeof val === "string" && val.trim()) return val.trim();
  }
  const sender = p.sender as Record<string, unknown> | undefined;
  if (sender && typeof sender.id === "string") return sender.id;
  const from = p.from as Record<string, unknown> | undefined;
  if (from && typeof from.id === "string") return from.id;
  return "";
}

const EMPTY_FORM = {
  AgentId: "",
  Channel: "wps-xiezuo" as ChannelType,
  ChannelAccountId: "",
  AppId: "",
  AppSecret: "",
  DmPolicy: "pairing" as DmPolicy,
  GroupPolicy: "allowlist" as GroupPolicy,
  RequireMention: true,
  SessionScope: "per-peer" as SessionScope,
  Enabled: true,
  ConfigJson: "{}",
};

// ── Component ─────────────────────────────────────────────────────────────────

type Tab = "channels" | "pairings" | "bindings" | "messages";

export function ChannelsPage({ refreshTick }: { refreshTick?: number } = {}) {
  const [tab, setTab] = useState<Tab>("channels");
  const [localRefreshTick, setLocalRefreshTick] = useState(0);
  // When rendered through the plugin workspace (no global refreshTick),
  // fall back to an internal refresh counter so the page still reloads on demand.
  const effectiveRefreshTick = refreshTick ?? localRefreshTick;
  const [channels, setChannels] = useState<Channel[]>(SEED_CHANNELS);
  const [pairings, setPairings] = useState<PairingRequest[]>(SEED_PAIRINGS);
  const [bindings, setBindings] = useState<ChannelBinding[]>(SEED_BINDINGS);
  const [messages, setMessages] = useState<ChannelMessage[]>(SEED_MESSAGES);
  const [agents, setAgents] = useState<StudioAgent[]>([]);
  const [cloudAgents, setCloudAgents] = useState<CloudAgentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Channel | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [unbindTarget, setUnbindTarget] = useState<ChannelBinding | null>(null);
  const [unbinding, setUnbinding] = useState(false);
 const [editBindingTarget, setEditBindingTarget] = useState<ChannelBinding | null>(null);
 const [editSessionId, setEditSessionId] = useState("");
 const [editSubmitting, setEditSubmitting] = useState(false);
  const [studioSessions, setStudioSessions] = useState<ChatSession[]>([]);
  const [activeTakeovers, setActiveTakeovers] = useState<Record<string, string>>({});
  const [takeoverTarget, setTakeoverTarget] = useState<ChannelBinding | null>(null);
  const [takeoverReason, setTakeoverReason] = useState("");
  const [takeoverByName, setTakeoverByName] = useState("");
  const [takeoverBusy, setTakeoverBusy] = useState(false);
  const [releaseTarget, setReleaseTarget] = useState<ChannelBinding | null>(null);
  const [releaseBusy, setReleaseBusy] = useState(false);
 const [actionTarget, setActionTarget] = useState<PairingRequest | null>(null);
  const [actionKind, setActionKind] = useState<"approve" | "reject" | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const chatBodyRef = useRef<HTMLDivElement>(null);

  const [qrTarget, setQrTarget] = useState<Channel | null>(null);
  const [qrLoading, setQrLoading] = useState(false);
  const [qrData, setQrData] = useState<ConnectQrData | null>(null);
  const [qrImageData, setQrImageData] = useState("");

const loadAll = useCallback(async () => {
    const [chRes, pairRes, bindRes, msgRes, agentRes, cloudAgentRes, runsRes] = await Promise.allSettled([
      channelApi<ListResult<Channel>>("ListChannels", { Offset: 0, Limit: 100 }),
      channelApi<ListResult<PairingRequest>>("ListPairingRequests", { Offset: 0, Limit: 100 }),
      channelApi<ListResult<ChannelBinding>>("ListBindings", { Offset: 0, Limit: 100 }),
      channelApi<ListResult<ChannelMessage>>("ListMessages", { Offset: 0, Limit: 100 }),
      apiFetch("/api/v1/agents?limit=100").then(r => r.ok ? r.json() : Promise.reject(new Error("agents"))),
      apiFetch("/api/v1/cloud-agents?size=100").then(r => r.ok ? r.json() : Promise.reject(new Error("cloud-agents"))),
      apiFetch("/api/v1/runs").then(r => r.ok ? r.json() : Promise.reject(new Error("runs"))),
    ]);
    if (chRes.status === "fulfilled") setChannels(chRes.value.Items || []);
    if (pairRes.status === "fulfilled") setPairings(pairRes.value.Items || []);
    if (bindRes.status === "fulfilled") setBindings(bindRes.value.Items || []);
    if (msgRes.status === "fulfilled") setMessages(msgRes.value.Items || []);
    if (agentRes.status === "fulfilled") setAgents(agentRes.value.items || []);
    if (cloudAgentRes.status === "fulfilled") setCloudAgents(cloudAgentRes.value.items || []);
    if (runsRes.status === "fulfilled" && runsRes.value.items) {
      const allSessions: ChatSession[] = [];
      const seen = new Set<string>();
      for (const item of runsRes.value.items) {
        const agentId = (item as { agentId?: string }).agentId || "";
        const grouped = groupRunsBySession([item] as Parameters<typeof groupRunsBySession>[0], agentId);
        for (const s of grouped) {
          if (!seen.has(s.id)) { seen.add(s.id); allSessions.push(s); }
        }
      }
      allSessions.sort((a, b) => (b.updatedAt || "").localeCompare(a.updatedAt || ""));
      setStudioSessions(allSessions);
    }
    // Check active takeovers for each binding
    if (bindRes.status === "fulfilled") {
      const bindingsList = bindRes.value.Items || [];
      const takeoverMap: Record<string, string> = {};
      await Promise.allSettled(bindingsList.map(async (bd) => {
        try {
          const res = await channelApi<{ Takeover?: { Id: string } }>("GetActiveTakeover", { BindingId: bd.Id });
          if (res.Takeover) takeoverMap[bd.Id] = res.Takeover.Id;
        } catch { /* no active takeover */ }
      }));
      setActiveTakeovers(takeoverMap);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    setLoading(true);
    void loadAll();
  }, [loadAll, effectiveRefreshTick]);

  const agentName = useCallback(
    (id: string) => {
      const local = agents.find(a => a.metadata.id === id)?.metadata.name;
      if (local) return local;
      const cloud = cloudAgents.find(a => a.agentId === id)?.name;
      if (cloud) return cloud;
      return id || "未绑定";
    },
    [agents, cloudAgents],
  );

  const agentOptions = useMemo(() => {
    const local = agents.map(a => ({
      value: a.metadata.id,
      label: `本地 · ${a.metadata.name}`,
    }));
    const cloud = cloudAgents
      .filter(a => a.agentId && a.agentId.trim())
      .map(a => ({
        value: a.agentId!.trim(),
        label: `云端 · ${a.name || a.agentId}`,
      }));
    return [...local, ...cloud];
  }, [agents, cloudAgents]);

  const sessionOptions = useMemo(() => {
    const opts = studioSessions.map(s => ({
      value: s.id,
      label: s.title || s.id,
    }));
    return [{ value: "", label: "(不关联会话)" }, ...opts];
  }, [studioSessions]);

  const isEdit = Boolean(editingId);

  function openCreate() {
    setEditingId(null);
    setForm({ ...EMPTY_FORM });
    setAdvancedOpen(false);
    setFormOpen(true);
  }

  function openEdit(channel: Channel) {
    setEditingId(channel.Id);
    setForm({
      AgentId: channel.AgentId,
      Channel: channel.Channel,
      ChannelAccountId: channel.ChannelAccountId,
      AppId: "",
      AppSecret: "",
      DmPolicy: channel.DmPolicy,
      GroupPolicy: channel.GroupPolicy,
      RequireMention: channel.RequireMention,
      SessionScope: channel.SessionScope,
      Enabled: channel.Enabled,
      ConfigJson: channel.ConfigJson || "{}",
    });
   setAdvancedOpen(true);
   setFormOpen(true);
 }

  function openQr(channel: Channel) {
    setQrTarget(channel);
    setQrLoading(true);
    setQrData(null);
    setQrImageData("");
    void (async () => {
      try {
        const data = await channelApi<ConnectQrData>("GetConnectQr", { Id: channel.Id });
        setQrData(data);
        if (data.QrUrl) {
          const dataUrl = await QRCode.toDataURL(data.QrUrl, { width: 200, margin: 1 });
          setQrImageData(dataUrl);
        }
      } catch (error) {
        showToast("获取二维码失败", error instanceof Error ? error.message : "请稍后重试", "error");
        setQrTarget(null);
      } finally {
        setQrLoading(false);
      }
    })();
  }

  function closeQr() {
    setQrTarget(null);
    setQrData(null);
    setQrImageData("");
  }

async function submitChannel(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        AgentId: form.AgentId,
        Channel: form.Channel,
        ChannelAccountId: form.ChannelAccountId.trim(),
        DmPolicy: form.DmPolicy,
        GroupPolicy: form.GroupPolicy,
        RequireMention: form.RequireMention,
        SessionScope: form.SessionScope,
        Enabled: form.Enabled,
        ConfigJson: form.ConfigJson || "{}",
      };
      if (form.AppId.trim()) body.AppId = form.AppId.trim();
      if (form.AppSecret.trim()) body.AppSecret = form.AppSecret.trim();
      if (isEdit) {
        body.Id = editingId;
        await channelApi("UpdateChannel", body);
        showToast("渠道已更新", `${CHANNEL_LABELS[form.Channel]} · ${form.ChannelAccountId}`);
      } else {
        await channelApi("CreateChannel", body);
        showToast("渠道已创建", `${CHANNEL_LABELS[form.Channel]} · ${form.ChannelAccountId}`);
      }
      setFormOpen(false);
      await loadAll();
    } catch (error) {
      showToast(isEdit ? "渠道更新失败" : "渠道创建失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await channelApi("DeleteChannel", { Id: deleteTarget.Id });
      setChannels(prev => prev.filter(c => c.Id !== deleteTarget.Id));
      showToast("渠道已删除", `${CHANNEL_LABELS[deleteTarget.Channel]} · ${deleteTarget.ChannelAccountId}`);
      setDeleteTarget(null);
    } catch (error) {
      showToast("渠道删除失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setDeleting(false);
    }
  }

  async function confirmUnbind() {
    if (!unbindTarget) return;
    setUnbinding(true);
    try {
      await channelApi("DeleteBinding", { Id: unbindTarget.Id });
      setBindings(prev => prev.filter(b => b.Id !== unbindTarget.Id));
      showToast("解绑成功", `会话 ${unbindTarget.SessionId} 已解绑`);
      setUnbindTarget(null);
    } catch (error) {
      showToast("解绑失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setUnbinding(false);
    }
  }

  async function submitTakeover() {
    if (!takeoverTarget) return;
    setTakeoverBusy(true);
    try {
      await channelApi("Takeover", {
        BindingId: takeoverTarget.Id,
        TakenOverBy: "admin",
        TakenOverByName: takeoverByName.trim() || "Admin",
        Reason: takeoverReason.trim() || undefined,
      });
      const res = await channelApi<{ Takeover?: { Id: string } }>("GetActiveTakeover", { BindingId: takeoverTarget.Id });
      if (res.Takeover) setActiveTakeovers(prev => ({ ...prev, [takeoverTarget.Id]: res.Takeover!.Id }));
      showToast("已接管", "该会话的消息将不再自动回复");
      setTakeoverTarget(null);
    } catch (error) {
      showToast("接管失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setTakeoverBusy(false);
    }
  }

  async function submitRelease() {
    if (!releaseTarget) return;
    const takeoverId = activeTakeovers[releaseTarget.Id];
    if (!takeoverId) {
      showToast("无需释放", "当前没有活跃的接管", "info");
      setReleaseTarget(null);
      return;
    }
    setReleaseBusy(true);
    try {
      await channelApi("ReleaseTakeover", { Id: takeoverId });
      setActiveTakeovers(prev => { const next = { ...prev }; delete next[releaseTarget.Id]; return next; });
      showToast("已释放接管", "该会话恢复自动回复");
      setReleaseTarget(null);
    } catch (error) {
      showToast("释放失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setReleaseBusy(false);
    }
  }

  async function submitEditBinding() {
    if (!editBindingTarget) return;
    const sessionId = editSessionId.trim();
    setEditSubmitting(true);
    try {
      await channelApi("UpdateBinding", { Id: editBindingTarget.Id, SessionId: sessionId });
      setBindings(prev => prev.map(b => b.Id === editBindingTarget.Id ? { ...b, SessionId: sessionId } : b));
      showToast(sessionId ? "会话关联成功" : "已取消关联", sessionId ? `会话已关联到 ${sessionId}` : "该绑定已取消会话关联");
      setEditBindingTarget(null);
    } catch (error) {
      showToast("关联失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setEditSubmitting(false);
    }
  }

  async function handlePairingAction() {
    if (!actionTarget || !actionKind) return;
    setActionBusy(true);
    try {
      const action = actionKind === "approve" ? "ApprovePairing" : "RejectPairing";
      await channelApi(action, { Id: actionTarget.Id });
      const nextStatus: PairingStatus = actionKind === "approve" ? "approved" : "rejected";
      setPairings(prev => prev.map(p => (p.Id === actionTarget.Id ? { ...p, Status: nextStatus } : p)));
      showToast(actionKind === "approve" ? "配对已通过" : "配对已拒绝", actionTarget.SenderName);
      setActionTarget(null);
      setActionKind(null);
    } catch (error) {
      showToast(actionKind === "approve" ? "配对通过失败" : "配对拒绝失败", error instanceof Error ? error.message : "请稍后重试", "error");
    } finally {
      setActionBusy(false);
    }
  }

  // ── Columns ────────────────────────────────────────────────────────────────

  const channelColumns = useMemo<StudioDataColumn<Channel>[]>(() => [
    {
      id: "name",
      header: "渠道",
      minWidth: 240,
      cell: ch => (
        <>
          <span className="channel-platform-tag" data-platform={ch.Channel} aria-hidden="true">{PLATFORM_BADGE[ch.Channel]}</span>
          <strong>{CHANNEL_LABELS[ch.Channel]} · {ch.ChannelAccountId}</strong>
          <span className="resource-origin mono">{ch.Id}</span>
        </>
      ),
    },
    {
      id: "agent",
      header: "绑定 Agent",
      minWidth: 150,
      cell: ch => ch.AgentId ? <span>{agentName(ch.AgentId)}</span> : <span className="text-muted">未绑定</span>,
    },
    {
      id: "dmPolicy",
      header: "私聊策略",
      width: 100,
      cell: ch => <span className="channel-policy-tag" data-policy={ch.DmPolicy}>{DM_POLICY_LABELS[ch.DmPolicy]}</span>,
    },
    {
      id: "groupPolicy",
      header: "群聊策略",
      width: 100,
      cell: ch => <span className="channel-policy-tag" data-policy={ch.GroupPolicy}>{GROUP_POLICY_LABELS[ch.GroupPolicy]}</span>,
    },
    {
      id: "requireMention",
      header: "@机器人",
      width: 90,
      cell: ch => ch.RequireMention ? <span className="tag">需要</span> : <span className="text-muted">否</span>,
    },
    {
      id: "enabled",
      header: "状态",
      width: 100,
      cell: ch => <span className="badge" data-state={ch.Enabled ? "ready" : "idle"}>{ch.Enabled ? "已启用" : "已停用"}</span>,
    },
    {
      id: "createdAt",
      header: "创建时间",
      minWidth: 140,
      cell: ch => formatChannelDate(ch.CreatedAt),
    },
    {
      id: "actions",
      header: "",
      width: 56,
      className: "channels-page__row-actions",
      cell: ch => (
       <MoreActionsMenu
         label={`${ch.ChannelAccountId} 操作`}
         items={[
            ...(ch.Channel === "feishu" || ch.Channel === "wecom"
              ? [{ label: "扫码连接", onSelect: () => openQr(ch) }]
              : []),
           { label: "编辑", onSelect: () => openEdit(ch) },
           { label: "删除", danger: true, onSelect: () => setDeleteTarget(ch) },
         ]}
        />
      ),
    },
  ], [agentName]);

  const pairingColumns = useMemo<StudioDataColumn<PairingRequest>[]>(() => [
    {
      id: "sender",
      header: "发起人",
      minWidth: 120,
      cell: pr => <strong>{pr.SenderName || pr.SenderId}</strong>,
    },
    {
      id: "channel",
      header: "渠道",
      minWidth: 180,
      cell: pr => (
        <>
          <span className="channel-platform-tag" data-platform={pr.Channel} aria-hidden="true">{PLATFORM_BADGE[pr.Channel]}</span>
          <span>{CHANNEL_LABELS[pr.Channel]} · {pr.ChannelAccountId}</span>
        </>
      ),
    },
    {
      id: "chatType",
      header: "会话类型",
      width: 90,
      cell: pr => <span className="tag">{CHAT_TYPE_LABEL[pr.ChatType]}</span>,
    },
    {
      id: "peerId",
      header: "对端 ID",
      minWidth: 130,
      cell: pr => <span className="mono">{pr.PeerId || "-"}</span>,
    },
    {
      id: "status",
      header: "状态",
      width: 100,
      cell: pr => <span className="badge" data-state={PAIRING_STATUS_STATE[pr.Status]}>{PAIRING_STATUS_LABEL[pr.Status]}</span>,
    },
    {
      id: "createdAt",
      header: "创建时间",
      minWidth: 140,
      cell: pr => formatChannelDate(pr.CreatedAt),
    },
    {
      id: "expiresAt",
      header: "过期时间",
      minWidth: 140,
      cell: pr => formatChannelDate(pr.ExpiresAt),
    },
    {
      id: "actions",
      header: "",
      width: 130,
      className: "channels-page__row-actions",
      cell: pr => pr.Status === "pending" ? (
        <div style={{ display: "flex", gap: 6 }}>
          <button className="button accent compact" type="button" onClick={() => { setActionTarget(pr); setActionKind("approve"); }} title="通过配对">
            <Check size={14} />
            通过
          </button>
          <button className="button tertiary compact" type="button" onClick={() => { setActionTarget(pr); setActionKind("reject"); }} title="拒绝配对">
            <X size={14} />
            拒绝
          </button>
        </div>
      ) : <span className="text-muted">-</span>,
    },
  ], []);

  const bindingColumns = useMemo<StudioDataColumn<ChannelBinding>[]>(() => [
    {
      id: "channel",
      header: "渠道",
      minWidth: 180,
      cell: bd => (
        <>
          <span className="channel-platform-tag" data-platform={bd.Channel} aria-hidden="true">{PLATFORM_BADGE[bd.Channel]}</span>
          <span>{CHANNEL_LABELS[bd.Channel]} · {bd.ChannelAccountId}</span>
        </>
      ),
    },
    {
      id: "chatType",
      header: "会话类型",
      width: 90,
      cell: bd => <span className="tag">{CHAT_TYPE_LABEL[bd.ChatType]}</span>,
    },
    {
      id: "peer",
      header: "对端 / 群",
      minWidth: 140,
      cell: bd => <span className="mono">{bd.ChatType === "group" ? bd.GroupId : bd.PeerId}</span>,
    },
    {
      id: "sessionId",
      header: "Session ID",
      minWidth: 150,
      cell: bd => (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span className="mono resource-origin">{bd.SessionId}</span>
            {activeTakeovers[bd.Id] && <span className="channels-page__takeover-badge">已接管</span>}
          </span>
        ),
    },
    {
      id: "userId",
      header: "用户 ID",
      minWidth: 120,
      cell: bd => <span className="mono">{bd.UserId || "-"}</span>,
    },
    {
      id: "createdAt",
      header: "创建时间",
      minWidth: 140,
      cell: bd => formatChannelDate(bd.CreatedAt),
    },
    {
      id: "actions",
      header: "",
      width: 48,
      cell: bd => (
        <MoreActionsMenu
          label={`会话绑定 ${bd.SessionId} 操作`}
          items={[
            { label: "编辑会话", onSelect: () => { setEditBindingTarget(bd); setEditSessionId(bd.SessionId); } },
            activeTakeovers[bd.Id]
              ? { label: "释放接管", onSelect: () => setReleaseTarget(bd) }
              : { label: "接管", onSelect: () => { setTakeoverTarget(bd); setTakeoverReason(""); setTakeoverByName(""); } },
            { label: "解绑", danger: true, onSelect: () => setUnbindTarget(bd) },
          ]}
        />
      ),
    },
  ], []);

  const messageColumns = useMemo<StudioDataColumn<ChannelMessage>[]>(() => [
    {
      id: "direction",
      header: "方向",
      width: 90,
      cell: msg => (
        <span className={`channel-message-direction ${msg.Direction}`}>
          {msg.Direction === "inbound" ? <ArrowDownLeft size={14} /> : <ArrowUpRight size={14} />}
          {msg.Direction === "inbound" ? "接收" : "发送"}
        </span>
      ),
    },
    {
      id: "channel",
      header: "渠道",
      minWidth: 160,
      cell: msg => (
        <>
          <span className="channel-platform-tag" data-platform={msg.Channel} aria-hidden="true">{PLATFORM_BADGE[msg.Channel]}</span>
          <span>{CHANNEL_LABELS[msg.Channel]} · {msg.ChannelAccountId}</span>
        </>
      ),
    },
    {
      id: "sender",
      header: "发送者",
      minWidth: 110,
      cell: msg => {
        const sender = messageSender(msg);
        return sender ? <span className="mono">{sender}</span> : <span className="text-muted">-</span>;
      },
    },
    {
      id: "payload",
      header: "内容",
      minWidth: 280,
      cell: msg => (
        <span className="resource-origin" style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {messagePreview(msg)}
        </span>
      ),
    },
    {
      id: "error",
      header: "错误",
      width: 120,
      cell: msg => msg.Error ? <span className="badge" data-state="failed">{msg.Error}</span> : <span className="text-muted">-</span>,
    },
    {
      id: "retryCount",
      header: "重试",
      width: 70,
      cell: msg => <span className="mono">{msg.RetryCount}</span>,
    },
    {
      id: "createdAt",
      header: "时间",
      minWidth: 140,
      cell: msg => formatChannelDate(msg.CreatedAt),
    },
  ], []);

  // ── Conversations ──

  const conversations = useMemo<ConversationGroup[]>(() => {
    const groups = new Map<string, ConversationGroup>();
    for (const msg of messages) {
      const p = parsePayload(msg.Payload);
      let chatId = "";
      let senderId = "";
      if (msg.Direction === "inbound") {
        const chat = p.chat as Record<string, unknown> | undefined;
        if (chat && typeof chat.id === "string") chatId = chat.id;
        const sender = p.sender as Record<string, unknown> | undefined;
        if (sender && typeof sender.id === "string") senderId = sender.id;
      } else {
        const replyTo = p.reply_to as string | undefined;
        if (replyTo) {
          const matched = messages.find(m => m.PlatformEventId === replyTo);
          if (matched) {
            const mp = parsePayload(matched.Payload);
            const mchat = mp.chat as Record<string, unknown> | undefined;
            if (mchat && typeof mchat.id === "string") chatId = mchat.id;
            const msender = mp.sender as Record<string, unknown> | undefined;
            if (msender && typeof msender.id === "string") senderId = msender.id;
          }
        }
      }
      if (!chatId) {
        const sender = p.sender as Record<string, unknown> | undefined;
        if (sender && typeof sender.id === "string") {
          chatId = sender.id;
          senderId = sender.id;
        }
      }
      if (!chatId) chatId = "default";
      const groupKey = `${msg.Channel}|${msg.ChannelAccountId}|${chatId}`;
      const existing = groups.get(groupKey);
      if (existing) {
        existing.messages.push(msg);
        existing.messageCount++;
        if (msg.CreatedAt > existing.lastMessage.CreatedAt) {
          existing.lastMessage = msg;
        }
      } else {
        const title = senderId || chatId !== "default" ? (senderId || chatId) : `${CHANNEL_LABELS[msg.Channel]} · ${msg.ChannelAccountId}`;
        groups.set(groupKey, {
          id: groupKey,
          channel: msg.Channel,
          channelAccountId: msg.ChannelAccountId,
          chatId,
          senderId,
          title,
          messages: [msg],
          messageCount: 1,
          lastMessage: msg,
        });
      }
    }
    for (const g of groups.values()) {
      g.messages.sort((a, b) => a.CreatedAt.localeCompare(b.CreatedAt));
    }
    return Array.from(groups.values()).sort((a, b) =>
      b.lastMessage.CreatedAt.localeCompare(a.lastMessage.CreatedAt)
    );
  }, [messages]);

  const selectedConversation = useMemo(
    () => conversations.find(c => c.id === selectedConversationId) ?? null,
    [conversations, selectedConversationId],
  );

  useEffect(() => {
    if (chatBodyRef.current) {
      chatBodyRef.current.scrollTop = chatBodyRef.current.scrollHeight;
    }
  }, [selectedConversationId, selectedConversation?.messages.length]);

  // // ── Metrics ────────────────────────────────────────────────────────────────

  const enabledCount = channels.filter(c => c.Enabled).length;
  const pendingPairings = pairings.filter(p => p.Status === "pending").length;

  const tabConfigs: Array<{ id: Tab; label: string; count: number }> = [
    { id: "channels", label: "渠道列表", count: channels.length },
    { id: "pairings", label: "配对请求", count: pairings.length },
    { id: "bindings", label: "会话绑定", count: bindings.length },
    { id: "messages", label: "消息记录", count: messages.length },
  ];

  const formValid = form.ChannelAccountId.trim() && form.AgentId && (isEdit || (form.AppId.trim() && form.AppSecret.trim()));

  return (
    <div className="page-container channels-page" data-layout="data" data-scroll-mode="data">
     <PageHeaderActions>
        <button className="icon-button tertiary" type="button" aria-label="刷新" title="刷新" onClick={() => setLocalRefreshTick(t => t + 1)}>
          <RefreshCw size={15} />
        </button>
       <button className="button accent" type="button" onClick={openCreate}>
          <Plus size={15} /><span>新建渠道</span>
        </button>
      </PageHeaderActions>

      <section className="channels-page__metrics" aria-label="渠道汇总">
        <div><span>渠道总数</span><strong>{channels.length}</strong></div>
        <div><span>已启用</span><strong>{enabledCount}</strong></div>
        <div><span>待配对</span><strong>{pendingPairings}</strong></div>
        <div><span>会话绑定</span><strong>{bindings.length}</strong></div>
      </section>

      <nav className="channels-page__tabs" aria-label="消息渠道视图">
        {tabConfigs.map(t => (
          <button
            key={t.id}
            className={`channels-page__tab${tab === t.id ? " active" : ""}`}
            type="button"
            onClick={() => setTab(t.id)}
          >
            {t.label}
            <span className="channels-page__tab-badge">{t.count}</span>
          </button>
        ))}
      </nav>

      {tab === "channels" && (
        <section className="channels-page__panel" aria-label="渠道列表">
          <div className="channels-page__panel-header">
            <div><strong>渠道列表</strong><span>{channels.length} 个渠道</span></div>
          </div>
          <StudioDataTable
            columns={channelColumns}
            data={channels}
            getRowId={ch => ch.Id}
            caption="渠道列表"
            minWidth={1000}
            loading={loading}
            onRowActivate={ch => openEdit(ch)}
            rowAriaLabel={ch => `编辑渠道 ${ch.ChannelAccountId}`}
            empty={{ icon: <MessageSquare size={22} />, title: "还没有渠道", description: "新建渠道后即可在这里管理平台接入。" }}
          />
        </section>
      )}

      {tab === "pairings" && (
        <section className="channels-page__panel" aria-label="配对请求">
          <div className="channels-page__panel-header">
            <div><strong>配对请求</strong><span>{pairings.length} 条请求</span></div>
          </div>
          <StudioDataTable
            columns={pairingColumns}
            data={pairings}
            getRowId={pr => pr.Id}
            caption="配对请求列表"
            minWidth={960}
            empty={{ icon: <UserPlus size={22} />, title: "没有配对请求", description: "开启配对后，用户可通过配对码绑定渠道。" }}
          />
        </section>
      )}

      {tab === "bindings" && (
        <section className="channels-page__panel" aria-label="会话绑定">
          <div className="channels-page__panel-header">
            <div><strong>会话绑定</strong><span>{bindings.length} 条绑定</span></div>
          </div>
          <StudioDataTable
            columns={bindingColumns}
            data={bindings}
            getRowId={bd => bd.Id}
            caption="会话绑定列表"
            minWidth={900}
            empty={{ icon: <Link2 size={22} />, title: "没有会话绑定", description: "用户通过配对后，会话绑定将出现在这里。" }}
            expandRowContent={bd => (
              <div className="channels-page__message-detail">
                <pre className="channels-page__message-payload">
                  {JSON.stringify({
                    PeerId: bd.PeerId,
                    GroupId: bd.GroupId,
                    SenderId: bd.SenderId,
                    AgentId: bd.AgentId,
                    SessionId: bd.SessionId,
                    UserId: bd.UserId,
                    Channel: bd.Channel,
                    ChannelAccountId: bd.ChannelAccountId,
                    ChatType: bd.ChatType,
                    CreatedAt: bd.CreatedAt,
                    UpdatedAt: bd.UpdatedAt,
                  }, null, 2)}
                </pre>
                <div className="channels-page__message-meta-row">
                  <span><small>Agent</small><code className="mono">{bd.AgentId || "-"}</code></span>
                  <span><small>Session</small><code className="mono">{bd.SessionId || "-"}</code></span>
                  <span><small>对端 ID</small><code className="mono">{bd.PeerId || "-"}</code></span>
                  <span><small>群 ID</small><code className="mono">{bd.GroupId || "-"}</code></span>
                  <span><small>发送者 ID</small><code className="mono">{bd.SenderId || "-"}</code></span>
                  <span><small>用户 ID</small><code className="mono">{bd.UserId || "-"}</code></span>
                  {bd.UpdatedAt && <span><small>更新时间</small>{formatChannelDate(bd.UpdatedAt)}</span>}
                </div>
              </div>
            )}
          />
        </section>
      )}

      {tab === "messages" && (
        <section className="channels-page__chat-view" aria-label="消息记录">
          <aside className="channels-page__chat-sidebar">
            <div className="channels-page__chat-sidebar-header">
              <span>会话列表</span>
              <span className="channels-page__chat-sidebar-count">{conversations.length}</span>
            </div>
            <div className="channels-page__chat-sidebar-list">
              {conversations.length === 0 ? (
                <div className="channels-page__chat-empty">
                  <Send size={22} />
                  <p>没有消息记录</p>
                  <span>渠道接入并产生对话后，消息将出现在这里。</span>
                </div>
              ) : (
                conversations.map(conv => (
                  <button
                    key={conv.id}
                    className={`channels-page__chat-item${selectedConversationId === conv.id ? " active" : ""}`}
                    type="button"
                    onClick={() => setSelectedConversationId(conv.id)}
                  >
                    <div className="channels-page__chat-item-avatar">
                      <span className="channel-platform-tag" data-platform={conv.channel}>{PLATFORM_BADGE[conv.channel]}</span>
                    </div>
                    <div className="channels-page__chat-item-body">
                      <div className="channels-page__chat-item-top">
                        <span className="channels-page__chat-item-name">{conv.title}</span>
                        <span className="channels-page__chat-item-time">{formatChatTime(conv.lastMessage.CreatedAt)}</span>
                      </div>
                      <div className="channels-page__chat-item-bottom">
                        <span className="channels-page__chat-item-preview">
                          {conv.lastMessage.Direction === "outbound" ? "↗ " : ""}
                          {messagePreview(conv.lastMessage)}
                        </span>
                        <span className="channels-page__chat-item-badge">{conv.messageCount}</span>
                      </div>
                    </div>
                  </button>
                ))
              )}
            </div>
          </aside>
          <div className="channels-page__chat-main">
            {selectedConversation ? (
              <>
                <div className="channels-page__chat-header">
                  <div className="channels-page__chat-header-info">
                    <span className="channel-platform-tag" data-platform={selectedConversation.channel}>{PLATFORM_BADGE[selectedConversation.channel]}</span>
                    <span className="channels-page__chat-header-name">{selectedConversation.title}</span>
                    <span className="channels-page__chat-header-meta">
                      {CHANNEL_LABELS[selectedConversation.channel]} · {selectedConversation.channelAccountId}
                      {selectedConversation.senderId && ` · ${selectedConversation.senderId}`}
                    </span>
                  </div>
                  <span className="channels-page__chat-header-count">{selectedConversation.messageCount} 条消息</span>
                </div>
                <div className="channels-page__chat-body" ref={chatBodyRef}>
                  {selectedConversation.messages.map((msg, idx) => {
                    const prevMsg = idx > 0 ? selectedConversation.messages[idx - 1] : null;
                    const showDateSep = !prevMsg || formatChatDate(prevMsg.CreatedAt) !== formatChatDate(msg.CreatedAt);
                    const sender = msg.Direction === "inbound" ? messageSender(msg) : "";
                    return (
                      <div key={msg.Id}>
                        {showDateSep && (
                          <div className="channels-page__chat-date-sep">{formatChatDate(msg.CreatedAt)}</div>
                        )}
                        <div className={`channels-page__chat-bubble ${msg.Direction}`}>
                          {msg.Direction === "inbound" && sender && (
                            <div className="channels-page__chat-bubble-sender">{sender}</div>
                          )}
                          <div className="channels-page__chat-bubble-content">{messagePreview(msg)}</div>
                          <div className="channels-page__chat-bubble-time">{formatChatTime(msg.CreatedAt)}</div>
                          {msg.Error && (
                            <div className="channels-page__chat-bubble-error">
                              <span className="badge" data-state="failed">{msg.Error}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            ) : (
              <div className="channels-page__chat-empty channels-page__chat-empty--center">
                <MessageSquare size={32} />
                <p>选择左侧会话查看聊天记录</p>
              </div>
            )}
          </div>
        </section>
      )}

      {formOpen && (
        <Drawer
          title={isEdit ? "编辑渠道" : "新建渠道"}
          subtitle="配置消息平台接入信息与对话策略。"
          wide
          closeDisabled={submitting}
          onClose={() => setFormOpen(false)}
          footer={(
            <>
              <span className="drawer-footer-spacer" />
              <button className="button tertiary" type="button" onClick={() => setFormOpen(false)} disabled={submitting}>取消</button>
              <button className="button accent" type="submit" form="channel-create-form" disabled={submitting || !formValid}>
                {submitting ? "正在保存" : isEdit ? "保存" : "创建"}
              </button>
            </>
          )}
       >
          <form id="channel-create-form" className="channels-page__create-form" onSubmit={submitChannel}>
            {/* Step 1: 平台选择 */}
            <div className="channels-page__form-section">
              <div className="channels-page__form-section-title" data-step="1">选择平台</div>
              <div className="channels-page__platform-cards">
                {PLATFORM_LIST.map(({ value, label, desc, Icon }) => (
                  <button
                    key={value}
                    type="button"
                    className={`channels-page__platform-card${form.Channel === value ? " selected" : ""}`}
                    onClick={() => setForm(prev => ({ ...prev, Channel: value }))}
                  >
                    <span className="channels-page__platform-card-icon"><Icon size={22} /></span>
                    <span className="channels-page__platform-card-text">
                      <span className="channels-page__platform-card-name">{label}</span>
                      <span className="channels-page__platform-card-desc">{desc}</span>
                    </span>
                    {form.Channel === value && <Check size={14} className="channels-page__platform-card-check" />}
                  </button>
                ))}
              </div>
            </div>

            {/* Step 2: 接入配置 - 使用统一栅格 */}
            <div className="channels-page__form-section">
              <div className="channels-page__form-section-title" data-step="2">接入配置</div>
              <div className="channels-page__form-grid">
                <div className="channels-page__form-col-12">
                  <FormField label="渠道账号 ID" htmlFor="channel-account-id" requirement="required">
                    <input
                      id="channel-account-id"
                      value={form.ChannelAccountId}
                      onChange={event => setForm(prev => ({ ...prev, ChannelAccountId: event.target.value }))}
                      placeholder="如：wps-default"
                      required
                    />
                  </FormField>
                </div>
                <div className="channels-page__form-col-6">
                  <FormField label="App ID" htmlFor="channel-app-id" requirement={isEdit ? "optional" : "required"}>
                    <input
                      id="channel-app-id"
                      value={form.AppId}
                      onChange={event => setForm(prev => ({ ...prev, AppId: event.target.value }))}
                      placeholder={isEdit ? "留空则不修改" : "平台分配的应用 ID"}
                    />
                  </FormField>
                </div>
                <div className="channels-page__form-col-6">
                  <FormField label="App Secret" htmlFor="channel-app-secret" requirement={isEdit ? "optional" : "required"}>
                    <input
                      id="channel-app-secret"
                      type="password"
                      value={form.AppSecret}
                      onChange={event => setForm(prev => ({ ...prev, AppSecret: event.target.value }))}
                      placeholder={isEdit ? "留空则不修改" : "平台分配的应用密钥"}
                    />
                  </FormField>
                </div>
              </div>
            </div>

            {/* Step 3: Agent 绑定 */}
            <div className="channels-page__form-section">
              <div className="channels-page__form-section-title" data-step="3">Agent 绑定</div>
              <div className="channels-page__form-grid">
                <div className="channels-page__form-col-12">
                  <FormField label="Agent" htmlFor="channel-agent" requirement="required">
                    <StudioSelect
                      id="channel-agent"
                      ariaLabel="绑定 Agent"
                      options={agentOptions}
                      value={form.AgentId}
                      placeholder="选择要绑定的 Agent"
                      onValueChange={value => setForm(prev => ({ ...prev, AgentId: value }))}
                    />
                  </FormField>
                </div>
                <div className="channels-page__form-col-6">
                  <label className="channel-toggle-row">
                    <input
                      type="checkbox"
                      checked={form.Enabled}
                      onChange={event => setForm(prev => ({ ...prev, Enabled: event.target.checked }))}
                    />
                    <span>
                      <strong>启用渠道</strong>
                      <small>停用后渠道不再接收和发送消息</small>
                    </span>
                  </label>
                </div>
              </div>
            </div>

            {/* 高级设置 */}
            <div className="channels-page__advanced-toggle">
              <button
                type="button"
                className="channels-page__advanced-header"
                onClick={() => setAdvancedOpen(prev => !prev)}
                aria-expanded={advancedOpen}
              >
                <ChevronDown
                  size={16}
                  className={`channels-page__chevron${advancedOpen ? "" : " channels-page__chevron--closed"}`}
                />
                <Settings size={16} />
                <span>高级设置</span>
                <small>私聊策略、群聊策略、会话隔离等</small>
              </button>
            </div>
            {advancedOpen && (
              <div className="channels-page__advanced-body">
                {/* 策略设置 */}
                <div className="channels-page__advanced-section">
                  <div className="channels-page__advanced-section-title">策略设置</div>
                  <div className="channels-page__form-grid">
                    <div className="channels-page__form-col-6">
                      <FormField label="私聊策略">
                        <StudioSelect
                          ariaLabel="私聊策略"
                          value={form.DmPolicy}
                          options={DM_POLICY_OPTIONS}
                          onValueChange={value => setForm(prev => ({ ...prev, DmPolicy: value as DmPolicy }))}
                        />
                      </FormField>
                    </div>
                    <div className="channels-page__form-col-6">
                      <FormField label="群聊策略">
                        <StudioSelect
                          ariaLabel="群聊策略"
                          value={form.GroupPolicy}
                          options={GROUP_POLICY_OPTIONS}
                          onValueChange={value => setForm(prev => ({ ...prev, GroupPolicy: value as GroupPolicy }))}
                        />
                      </FormField>
                    </div>
                  </div>
                </div>

                {/* 会话设置 */}
                <div className="channels-page__advanced-section">
                  <div className="channels-page__advanced-section-title">会话设置</div>
                  <div className="channels-page__form-grid">
                    <div className="channels-page__form-col-6">
                      <FormField label="会话隔离">
                        <StudioSelect
                          ariaLabel="会话隔离"
                          value={form.SessionScope}
                          options={SESSION_SCOPE_OPTIONS}
                          onValueChange={value => setForm(prev => ({ ...prev, SessionScope: value as SessionScope }))}
                        />
                      </FormField>
                    </div>
                    <div className="channels-page__form-col-6">
                      <label className="channel-toggle-row channel-toggle-row--compact">
                        <input
                          type="checkbox"
                          checked={form.RequireMention}
                          onChange={event => setForm(prev => ({ ...prev, RequireMention: event.target.checked }))}
                        />
                        <span>
                          <strong>要求 @机器人</strong>
                          <small>群聊中需 @机器人才会触发响应</small>
                        </span>
                      </label>
                    </div>
                  </div>
                </div>

                {/* 扩展配置 */}
                <div className="channels-page__advanced-section">
                  <FormField label="扩展配置" htmlFor="channel-config-json" hint="JSON 格式的扩展配置，默认为空对象 {}">
                    <textarea
                      id="channel-config-json"
                      value={form.ConfigJson}
                      onChange={event => setForm(prev => ({ ...prev, ConfigJson: event.target.value }))}
                      placeholder="{}"
                      rows={4}
                      className="channels-page__config-textarea"
                    />
                  </FormField>
                </div>
              </div>
            )}
          </form>
        </Drawer>
      )}

      {deleteTarget && (
        <ConfirmDialog
          title="删除渠道"
          description={`确定要删除「${CHANNEL_LABELS[deleteTarget.Channel]} · ${deleteTarget.ChannelAccountId}」吗？已绑定的会话将断开连接。`}
          confirmText="删除"
          danger
          busy={deleting}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}

      {unbindTarget && (
        <ConfirmDialog
          title="解绑会话"
          description="解绑后该用户下次发消息将重新走配对流程，确认解绑？"
          confirmText="解绑"
          danger
          busy={unbinding}
          onConfirm={confirmUnbind}
          onCancel={() => setUnbindTarget(null)}
        />
      )}

      {editBindingTarget && (
        <StudioDialog
          open
          onOpenChange={open => { if (!open && !editSubmitting) setEditBindingTarget(null); }}
          title="编辑会话绑定"
          closeDisabled={editSubmitting}
          footer={(
            <>
              <button className="button tertiary" type="button" onClick={() => setEditBindingTarget(null)} disabled={editSubmitting}>取消</button>
              <button className="button accent" type="button" onClick={submitEditBinding} disabled={editSubmitting}>
                {editSubmitting ? "处理中…" : "关联"}
              </button>
            </>
          )}
        >
          <FormField label="关联会话" htmlFor="edit-binding-session" requirement="required">
            <StudioSelect
              ariaLabel="选择会话"
              value={editSessionId}
              options={sessionOptions}
              onValueChange={value => setEditSessionId(value)}
            />
          </FormField>
         <p className="channels-page__form-hint">将此绑定关联到 Studio 中的会话，用户在 IM 发送的消息将使用此 SessionId 调用 Agent Runtime，实现 Studio 会话和 IM 会话的互通。</p>
       </StudioDialog>
     )}

      {takeoverTarget && (
        <StudioDialog
          open
          onOpenChange={open => { if (!open && !takeoverBusy) setTakeoverTarget(null); }}
          title="接管会话"
          closeDisabled={takeoverBusy}
          footer={(
            <>
              <button className="button tertiary" type="button" onClick={() => setTakeoverTarget(null)} disabled={takeoverBusy}>取消</button>
              <button className="button accent" type="button" onClick={submitTakeover} disabled={takeoverBusy}>
                {takeoverBusy ? "处理中…" : "确认接管"}
              </button>
            </>
          )}
        >
          <div className="form-grid">
            <FormField label="接管人名称" htmlFor="takeover-name">
              <input
                id="takeover-name"
                value={takeoverByName}
                onChange={event => setTakeoverByName(event.target.value)}
                placeholder="Admin"
              />
            </FormField>
            <FormField label="接管原因" htmlFor="takeover-reason">
              <input
                id="takeover-reason"
                value={takeoverReason}
                onChange={event => setTakeoverReason(event.target.value)}
                placeholder="可选"
              />
            </FormField>
          </div>
          <p className="channels-page__form-hint">接管后，该会话的 IM 消息将不再自动触发 Agent 回复，直到释放接管。</p>
        </StudioDialog>
      )}

      {releaseTarget && (
        <ConfirmDialog
          title="释放接管"
          description="释放后该会话将恢复 Agent 自动回复，确认释放？"
          busy={releaseBusy}
          onConfirm={submitRelease}
          onCancel={() => setReleaseTarget(null)}
        />
      )}

      {qrTarget && (
        <StudioDialog
          open
          onOpenChange={open => { if (!open && !qrLoading) closeQr(); }}
          title="扫码连接"
          icon={<QrCode size={18} />}
          closeDisabled={qrLoading}
          footer={(
            <button className="button tertiary" type="button" onClick={closeQr} disabled={qrLoading}>关闭</button>
          )}
        >
          {qrLoading ? (
            <div className="channels-page__qr-loading">正在获取二维码…</div>
          ) : qrData && !qrData.QrUrl ? (
            <div className="channels-page__qr-empty">该平台不支持扫码连接</div>
          ) : (
            <div className="channels-page__qr-content">
              {qrImageData && (
                <img
                  src={qrImageData}
                  alt="扫码连接二维码"
                  className="channels-page__qr-image"
                  width={200}
                  height={200}
                />
              )}
              {qrData?.Label && <p className="channels-page__qr-label">{qrData.Label}</p>}
            </div>
          )}
        </StudioDialog>
      )}

{actionTarget && actionKind && (
        <ConfirmDialog
          title={actionKind === "approve" ? "通过配对请求" : "拒绝配对请求"}
          description={actionKind === "approve"
            ? `确定要通过「${actionTarget.SenderName}」的配对请求吗？通过后用户即可绑定该渠道。`
            : `确定要拒绝「${actionTarget.SenderName}」的配对请求吗？拒绝后用户需重新发起配对。`}
          confirmText={actionKind === "approve" ? "通过" : "拒绝"}
          danger={actionKind === "reject"}
          busy={actionBusy}
          onConfirm={handlePairingAction}
          onCancel={() => { setActionTarget(null); setActionKind(null); }}
        />
      )}
    </div>
  );
}

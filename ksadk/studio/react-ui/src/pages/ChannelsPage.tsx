import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Check,
  ChevronDown,
  Link2,
  MessageSquare,
  Plus,
  Send,
  UserPlus,
  X,
} from "lucide-react";
import { apiFetch } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Drawer } from "../components/Drawer";
import { MoreActionsMenu } from "../components/MoreActionsMenu";
import { PageHeaderActions } from "../components/PageHeaderPortal";
import { showToast } from "../components/Toast";
import { FormField } from "../components/ui/FormField";
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
}

interface ChannelMessage {
  Id: string;
  Channel: ChannelType;
  ChannelAccountId: string;
  PlatformEventId: string;
  Direction: MessageDirection;
  DedupeKey: string;
  Payload: Record<string, unknown>;
  Error: string;
  RetryCount: number;
  NextRetryAt: string | null;
  CreatedAt: string;
}

interface StudioAgent {
  metadata: { id: string; name: string };
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

function messagePreview(msg: ChannelMessage): string {
  const payload = msg.Payload;
  if (typeof payload.text === "string") return payload.text;
  if (typeof payload.content === "string") return payload.content;
  if (typeof payload.message === "string") return payload.message;
  const json = JSON.stringify(payload);
  return json.length > 120 ? `${json.slice(0, 120)}…` : json;
}

function messageSender(msg: ChannelMessage): string {
  const p = msg.Payload as Record<string, unknown>;
  const candidates = ["sender_name", "senderName", "from_name", "fromName", "user_name", "userName", "sender", "from", "user"];
  for (const key of candidates) {
    const val = p[key];
    if (typeof val === "string" && val.trim()) return val.trim();
  }
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

export function ChannelsPage({ refreshTick }: { refreshTick: number }) {
  const [tab, setTab] = useState<Tab>("channels");
  const [channels, setChannels] = useState<Channel[]>(SEED_CHANNELS);
  const [pairings, setPairings] = useState<PairingRequest[]>(SEED_PAIRINGS);
  const [bindings, setBindings] = useState<ChannelBinding[]>(SEED_BINDINGS);
  const [messages, setMessages] = useState<ChannelMessage[]>(SEED_MESSAGES);
  const [agents, setAgents] = useState<StudioAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Channel | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [actionTarget, setActionTarget] = useState<PairingRequest | null>(null);
  const [actionKind, setActionKind] = useState<"approve" | "reject" | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  const loadAll = useCallback(async () => {
    const [chRes, pairRes, bindRes, msgRes, agentRes] = await Promise.allSettled([
      channelApi<ListResult<Channel>>("ListChannels", { Offset: 0, Limit: 100 }),
      channelApi<ListResult<PairingRequest>>("ListPairingRequests", { Offset: 0, Limit: 100 }),
      channelApi<ListResult<ChannelBinding>>("ListBindings", { Offset: 0, Limit: 100 }),
      channelApi<ListResult<ChannelMessage>>("ListMessages", { Offset: 0, Limit: 100 }),
      apiFetch("/api/v1/agents?limit=100").then(r => r.ok ? r.json() : Promise.reject(new Error("agents"))),
    ]);
    if (chRes.status === "fulfilled") setChannels(chRes.value.Items || []);
    if (pairRes.status === "fulfilled") setPairings(pairRes.value.Items || []);
    if (bindRes.status === "fulfilled") setBindings(bindRes.value.Items || []);
    if (msgRes.status === "fulfilled") setMessages(msgRes.value.Items || []);
    if (agentRes.status === "fulfilled") setAgents(agentRes.value.items || []);
    setLoading(false);
  }, []);

  useEffect(() => {
    setLoading(true);
    void loadAll();
  }, [loadAll, refreshTick]);

  const agentName = useCallback(
    (id: string) => agents.find(a => a.metadata.id === id)?.metadata.name || (id || "未绑定"),
    [agents],
  );

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
          <span className="channel-platform-tag" aria-hidden="true">{PLATFORM_BADGE[ch.Channel]}</span>
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
      cell: ch => <span className="channel-policy-tag">{DM_POLICY_LABELS[ch.DmPolicy]}</span>,
    },
    {
      id: "groupPolicy",
      header: "群聊策略",
      width: 100,
      cell: ch => <span className="channel-policy-tag">{GROUP_POLICY_LABELS[ch.GroupPolicy]}</span>,
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
          <span className="channel-platform-tag" aria-hidden="true">{PLATFORM_BADGE[pr.Channel]}</span>
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
          <span className="channel-platform-tag" aria-hidden="true">{PLATFORM_BADGE[bd.Channel]}</span>
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
      cell: bd => <span className="mono resource-origin">{bd.SessionId}</span>,
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
          <span className="channel-platform-tag" aria-hidden="true">{PLATFORM_BADGE[msg.Channel]}</span>
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

  // ── Metrics ────────────────────────────────────────────────────────────────

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
          />
        </section>
      )}

      {tab === "messages" && (
        <section className="channels-page__panel" aria-label="消息记录">
          <div className="channels-page__panel-header">
            <div><strong>消息记录</strong><span>{messages.length} 条消息</span></div>
          </div>
          <StudioDataTable
            columns={messageColumns}
            data={messages}
            getRowId={msg => msg.Id}
            caption="消息记录列表"
            minWidth={900}
            empty={{ icon: <Send size={22} />, title: "没有消息记录", description: "渠道接入并产生对话后，消息将出现在这里。" }}
          />
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
          <form id="channel-create-form" className="channels-page__create form-grid two-columns" onSubmit={submitChannel}>
            <FormField label="平台" requirement="required">
              <StudioSelect
                ariaLabel="平台"
                value={form.Channel}
                options={CHANNEL_OPTIONS}
                onValueChange={value => setForm(prev => ({ ...prev, Channel: value as ChannelType }))}
              />
            </FormField>
            <FormField label="渠道账号 ID" htmlFor="channel-account-id" requirement="required">
              <input
                id="channel-account-id"
                value={form.ChannelAccountId}
                onChange={event => setForm(prev => ({ ...prev, ChannelAccountId: event.target.value }))}
                placeholder="如：wps-default"
                required
              />
            </FormField>
            <FormField label="绑定 Agent" htmlFor="channel-agent" requirement="required">
              <StudioSelect
                id="channel-agent"
                ariaLabel="绑定 Agent"
                value={form.AgentId}
                placeholder="选择要绑定的 Agent"
                options={agents.map(a => ({ value: a.metadata.id, label: a.metadata.name }))}
                onValueChange={value => setForm(prev => ({ ...prev, AgentId: value }))}
              />
            </FormField>
            <FormField label="App ID" htmlFor="channel-app-id" requirement={isEdit ? "optional" : "required"}>
              <input
                id="channel-app-id"
                value={form.AppId}
                onChange={event => setForm(prev => ({ ...prev, AppId: event.target.value }))}
                placeholder={isEdit ? "留空则不修改" : "平台分配的应用 ID"}
              />
            </FormField>
            <FormField label="App Secret" htmlFor="channel-app-secret" requirement={isEdit ? "optional" : "required"}>
              <input
                id="channel-app-secret"
                type="password"
                value={form.AppSecret}
                onChange={event => setForm(prev => ({ ...prev, AppSecret: event.target.value }))}
                placeholder={isEdit ? "留空则不修改" : "平台分配的应用密钥"}
              />
            </FormField>
            <FormField className="channels-page__field--wide" label="启用状态">
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
            </FormField>
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
                <span>高级设置</span>
                <small>私聊策略、群聊策略、会话隔离等</small>
              </button>
            </div>
            {advancedOpen && (
              <div className="channels-page__advanced-body form-grid two-columns">
                <FormField label="私聊策略">
                  <StudioSelect
                    ariaLabel="私聊策略"
                    value={form.DmPolicy}
                    options={DM_POLICY_OPTIONS}
                    onValueChange={value => setForm(prev => ({ ...prev, DmPolicy: value as DmPolicy }))}
                  />
                </FormField>
                <FormField label="群聊策略">
                  <StudioSelect
                    ariaLabel="群聊策略"
                    value={form.GroupPolicy}
                    options={GROUP_POLICY_OPTIONS}
                    onValueChange={value => setForm(prev => ({ ...prev, GroupPolicy: value as GroupPolicy }))}
                  />
                </FormField>
                <FormField label="会话隔离">
                  <StudioSelect
                    ariaLabel="会话隔离"
                    value={form.SessionScope}
                    options={SESSION_SCOPE_OPTIONS}
                    onValueChange={value => setForm(prev => ({ ...prev, SessionScope: value as SessionScope }))}
                  />
                </FormField>
                <FormField className="channels-page__field--wide" label="群聊 @机器人">
                  <label className="channel-toggle-row">
                    <input
                      type="checkbox"
                      checked={form.RequireMention}
                      onChange={event => setForm(prev => ({ ...prev, RequireMention: event.target.checked }))}
                    />
                    <span>
                      <strong>要求 @机器人</strong>
                      <small>群聊中用户必须 @机器人才会触发响应</small>
                    </span>
                  </label>
                </FormField>
                <FormField className="channels-page__field--wide" label="扩展配置" htmlFor="channel-config-json" hint="JSON 格式的扩展配置，默认为空对象 {}">
                  <textarea
                    id="channel-config-json"
                    value={form.ConfigJson}
                    onChange={event => setForm(prev => ({ ...prev, ConfigJson: event.target.value }))}
                    placeholder="{}"
                    rows={3}
                    className="channels-page__config-textarea"
                  />
                </FormField>
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
